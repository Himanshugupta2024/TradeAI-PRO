"""CLI crypto signal tool using Binance public OHLCV data.

Strategy:
- BUY: Price above EMA fast/slow and volume breakout
- SELL: Price below EMA fast or support break
- HOLD: Sideways/unclear setup
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import requests

try:
    import pandas_ta as ta
except ImportError:  # pragma: no cover
    ta = None


CONFIG: dict[str, Any] = {
    "api": {
        "base_url": "https://api.binance.com",
        "klines_path": "/api/v3/klines",
        "timeout_seconds": 12,
        "default_interval": "1h",
        "default_limit": 300,
    },
    "market": {
        "symbols": ["BTC", "ETH", "SOL", "BNB", "XRP"],
        "quote_asset": "USDT",
    },
    "strategy": {
        "ema_fast": 50,
        "ema_slow": 200,
        "volume_window": 20,
        "volume_breakout_multiplier": 1.5,
        "support_lookback": 20,
        "atr_period": 14,
        "atr_stoploss_multiplier": 1.5,
        "min_required_rows": 210,
    },
    "risk": {
        "capital": 10000.0,
        "risk_per_trade_fraction": 0.01,
    },
    "runtime": {
        "watch": False,
        "watch_sleep_seconds": 300,
    },
    "telegram": {
        "enabled": False,
        "send_hold_signals": False,
        "bot_token": "",
        "chat_id": "",
        "api_url_template": "https://api.telegram.org/bot{token}/sendMessage",
    },
    "output": {
        "float_precision": 3,
    },
}


@dataclass
class SignalResult:
    symbol: str
    exchange_symbol: str
    signal: str
    reason: str
    close: float
    ema_fast: float
    ema_slow: float
    support: float
    atr: float
    volume: float
    volume_sma: float
    volume_ratio: float
    stop_loss: float | None
    position_size: float
    risk_amount: float


def to_exchange_symbol(asset: str, quote_asset: str) -> str:
    return f"{asset.upper()}{quote_asset.upper()}"


def fetch_ohlcv(exchange_symbol: str, cfg: dict[str, Any]) -> pd.DataFrame:
    api_cfg = cfg["api"]
    url = f"{api_cfg['base_url']}{api_cfg['klines_path']}"
    params = {
        "symbol": exchange_symbol,
        "interval": api_cfg["default_interval"],
        "limit": api_cfg["default_limit"],
    }

    response = requests.get(url, params=params, timeout=api_cfg["timeout_seconds"])
    response.raise_for_status()
    klines = response.json()

    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base_asset_volume",
        "taker_buy_quote_asset_volume",
        "ignore",
    ]
    df = pd.DataFrame(klines, columns=columns)

    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.dropna(subset=numeric_cols).reset_index(drop=True)
    return df


def compute_indicators(df: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    if ta is None:
        raise RuntimeError("pandas-ta is required. Install with: pip install pandas-ta")

    strategy_cfg = cfg["strategy"]
    out = df.copy()

    out["ema_fast"] = ta.ema(out["close"], length=strategy_cfg["ema_fast"])
    out["ema_slow"] = ta.ema(out["close"], length=strategy_cfg["ema_slow"])
    out["atr"] = ta.atr(
        high=out["high"],
        low=out["low"],
        close=out["close"],
        length=strategy_cfg["atr_period"],
    )

    out["volume_sma"] = out["volume"].rolling(strategy_cfg["volume_window"]).mean()
    out["support"] = out["low"].rolling(strategy_cfg["support_lookback"]).min().shift(1)

    out["volume_ratio"] = np.where(
        out["volume_sma"] > 0,
        out["volume"] / out["volume_sma"],
        np.nan,
    )
    return out


def evaluate_signal(indicator_df: pd.DataFrame, symbol: str, exchange_symbol: str, cfg: dict[str, Any]) -> SignalResult:
    strategy_cfg = cfg["strategy"]
    risk_cfg = cfg["risk"]

    last = indicator_df.iloc[-1]

    close = float(last["close"])
    ema_fast = float(last["ema_fast"])
    ema_slow = float(last["ema_slow"])
    support = float(last["support"]) if pd.notna(last["support"]) else np.nan
    atr = float(last["atr"])
    volume = float(last["volume"])
    volume_sma = float(last["volume_sma"]) if pd.notna(last["volume_sma"]) else np.nan
    volume_ratio = float(last["volume_ratio"]) if pd.notna(last["volume_ratio"]) else np.nan

    required_ready = (
        len(indicator_df) >= strategy_cfg["min_required_rows"]
        and pd.notna(last["ema_fast"])
        and pd.notna(last["ema_slow"])
        and pd.notna(last["atr"])
        and pd.notna(last["volume_sma"])
    )

    risk_amount = float(risk_cfg["capital"]) * float(risk_cfg["risk_per_trade_fraction"])
    stop_multiplier = float(strategy_cfg["atr_stoploss_multiplier"])

    if not required_ready:
        return SignalResult(
            symbol=symbol,
            exchange_symbol=exchange_symbol,
            signal="HOLD",
            reason="Not enough indicator history",
            close=close,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            support=support,
            atr=atr,
            volume=volume,
            volume_sma=volume_sma,
            volume_ratio=volume_ratio,
            stop_loss=None,
            position_size=0.0,
            risk_amount=risk_amount,
        )

    volume_breakout = volume_ratio >= float(strategy_cfg["volume_breakout_multiplier"])
    buy_condition = close > ema_fast and close > ema_slow and volume_breakout
    support_break = pd.notna(last["support"]) and close < support
    sell_condition = close < ema_fast or bool(support_break)

    if buy_condition:
        stop_loss = close - (atr * stop_multiplier)
        stop_distance = abs(close - stop_loss)
        position_size = risk_amount / stop_distance if stop_distance > 0 else 0.0
        reason = "Above EMA fast/slow with volume breakout"
        signal = "BUY"
    elif sell_condition:
        stop_loss = close + (atr * stop_multiplier)
        stop_distance = abs(stop_loss - close)
        position_size = risk_amount / stop_distance if stop_distance > 0 else 0.0
        reason = "Below EMA fast or support break"
        signal = "SELL"
    else:
        stop_loss = None
        position_size = 0.0
        reason = "Sideways or mixed conditions"
        signal = "HOLD"

    return SignalResult(
        symbol=symbol,
        exchange_symbol=exchange_symbol,
        signal=signal,
        reason=reason,
        close=close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        support=support,
        atr=atr,
        volume=volume,
        volume_sma=volume_sma,
        volume_ratio=volume_ratio,
        stop_loss=stop_loss,
        position_size=position_size,
        risk_amount=risk_amount,
    )


def analyze_symbol(symbol: str, cfg: dict[str, Any]) -> SignalResult:
    exchange_symbol = to_exchange_symbol(symbol, cfg["market"]["quote_asset"])
    df = fetch_ohlcv(exchange_symbol, cfg)
    indicator_df = compute_indicators(df, cfg)
    return evaluate_signal(indicator_df, symbol, exchange_symbol, cfg)


def fmt_float(value: float | None, precision: int) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:.{precision}f}"


def print_results(results: list[SignalResult], cfg: dict[str, Any]) -> None:
    precision = int(cfg["output"]["float_precision"])

    headers = [
        "SYMBOL",
        "SIGNAL",
        "CLOSE",
        "EMA_FAST",
        "EMA_SLOW",
        "SUPPORT",
        "VOL_RATIO",
        "ATR",
        "STOP_LOSS",
        "SIZE",
    ]
    rows = []
    for r in results:
        rows.append(
            [
                r.exchange_symbol,
                r.signal,
                fmt_float(r.close, precision),
                fmt_float(r.ema_fast, precision),
                fmt_float(r.ema_slow, precision),
                fmt_float(r.support, precision),
                fmt_float(r.volume_ratio, precision),
                fmt_float(r.atr, precision),
                fmt_float(r.stop_loss, precision),
                fmt_float(r.position_size, precision),
            ]
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))

    sep = " | "
    header_line = sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))
    rule = "-+-".join("-" * w for w in widths)

    print(header_line)
    print(rule)
    for row, result in zip(rows, results):
        print(sep.join(value.ljust(widths[i]) for i, value in enumerate(row)))
        print(f"Reason: {result.reason}")


def build_telegram_message(results: list[SignalResult], cfg: dict[str, Any]) -> str:
    telegram_cfg = cfg["telegram"]
    include_hold = bool(telegram_cfg["send_hold_signals"])

    lines = ["Crypto Signal Update"]
    for r in results:
        if r.signal == "HOLD" and not include_hold:
            continue
        lines.append(
            (
                f"{r.exchange_symbol} | {r.signal} | Close={r.close:.4f} | "
                f"SL={fmt_float(r.stop_loss, 4)} | Size={r.position_size:.4f} | {r.reason}"
            )
        )

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def send_telegram_alert(message: str, cfg: dict[str, Any]) -> None:
    telegram_cfg = cfg["telegram"]
    token = telegram_cfg["bot_token"] or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = telegram_cfg["chat_id"] or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id or not message:
        return

    url = telegram_cfg["api_url_template"].format(token=token)
    payload = {"chat_id": chat_id, "text": message}
    response = requests.post(url, json=payload, timeout=cfg["api"]["timeout_seconds"])
    response.raise_for_status()


def run_once(cfg: dict[str, Any]) -> list[SignalResult]:
    results: list[SignalResult] = []
    for symbol in cfg["market"]["symbols"]:
        try:
            results.append(analyze_symbol(symbol, cfg))
        except Exception as exc:
            print(f"{symbol}: ERROR - {exc}")
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crypto signal CLI with EMA + volume + ATR risk")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated assets, e.g. BTC,ETH,SOL")
    parser.add_argument("--interval", type=str, default=None, help="Binance kline interval, e.g. 15m,1h,4h,1d")
    parser.add_argument("--limit", type=int, default=None, help="Number of klines to fetch")
    parser.add_argument("--capital", type=float, default=None, help="Account capital for position sizing")
    parser.add_argument("--watch", action="store_true", help="Run continuously")
    parser.add_argument("--sleep", type=int, default=None, help="Sleep seconds between watch cycles")
    parser.add_argument("--telegram", action="store_true", help="Enable Telegram alerts for this run")
    return parser.parse_args()


def apply_cli_overrides(cfg: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    merged = {
        key: (value.copy() if isinstance(value, dict) else value)
        for key, value in cfg.items()
    }

    if args.symbols:
        merged["market"]["symbols"] = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.interval:
        merged["api"]["default_interval"] = args.interval
    if args.limit:
        merged["api"]["default_limit"] = int(args.limit)
    if args.capital:
        merged["risk"]["capital"] = float(args.capital)
    if args.watch:
        merged["runtime"]["watch"] = True
    if args.sleep:
        merged["runtime"]["watch_sleep_seconds"] = int(args.sleep)
    if args.telegram:
        merged["telegram"]["enabled"] = True

    return merged


def main() -> None:
    args = parse_args()
    cfg = apply_cli_overrides(CONFIG, args)

    while True:
        results = run_once(cfg)
        print_results(results, cfg)

        if cfg["telegram"]["enabled"]:
            message = build_telegram_message(results, cfg)
            try:
                send_telegram_alert(message, cfg)
                if message:
                    print("Telegram alert sent.")
            except Exception as exc:
                print(f"Telegram error: {exc}")

        if not cfg["runtime"]["watch"]:
            break

        time.sleep(cfg["runtime"]["watch_sleep_seconds"])


if __name__ == "__main__":
    main()
