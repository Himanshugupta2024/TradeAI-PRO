"""Signal engine module for Binance OHLCV-based crypto trade signals."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pandas_ta as ta
import requests


CONFIG: dict[str, Any] = {
    "api": {
        "base_url": "https://api.binance.com",
        "klines_endpoint": "/api/v3/klines",
        "timeout_seconds": 12,
    },
    "defaults": {
        "timeframe": "1d",
        "limit": 250,
        "quote_asset": "USDT",
    },
    "indicators": {
        "ema_fast": 50,
        "ema_slow": 200,
        "rsi_length": 14,
        "atr_length": 14,
        "volume_sma_length": 20,
        "volume_breakout_multiplier": 1.5,
        "drop_lookback_days": 5,
        "drop_threshold_pct": -7.0,
    },
}


def _normalize_symbol(symbol: str, quote_asset: str) -> str:
    cleaned = symbol.replace("/", "").upper().strip()
    if cleaned.endswith(quote_asset):
        return cleaned
    return f"{cleaned}{quote_asset}"


def fetch_ohlcv(symbol: str, timeframe: str = "1d", limit: int = 250) -> pd.DataFrame:
    """Fetch OHLCV candles from Binance for the provided symbol and timeframe."""
    quote_asset = str(CONFIG["defaults"]["quote_asset"])
    exchange_symbol = _normalize_symbol(symbol, quote_asset)

    url = f"{CONFIG['api']['base_url']}{CONFIG['api']['klines_endpoint']}"
    params = {
        "symbol": exchange_symbol,
        "interval": timeframe,
        "limit": int(limit),
    }

    response = requests.get(url, params=params, timeout=float(CONFIG["api"]["timeout_seconds"]))
    response.raise_for_status()
    data = response.json()

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

    df = pd.DataFrame(data, columns=columns)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
    return df


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate EMA50, EMA200, RSI14, MACD and ATR14 using pandas-ta."""
    out = df.copy()

    ema_fast_len = int(CONFIG["indicators"]["ema_fast"])
    ema_slow_len = int(CONFIG["indicators"]["ema_slow"])
    rsi_len = int(CONFIG["indicators"]["rsi_length"])
    atr_len = int(CONFIG["indicators"]["atr_length"])
    volume_len = int(CONFIG["indicators"]["volume_sma_length"])

    out["EMA50"] = ta.ema(out["close"], length=ema_fast_len)
    out["EMA200"] = ta.ema(out["close"], length=ema_slow_len)
    out["RSI"] = ta.rsi(out["close"], length=rsi_len)

    macd_df = ta.macd(out["close"])
    if macd_df is not None and not macd_df.empty:
        out["MACD"] = macd_df.iloc[:, 0]
        out["MACD_signal"] = macd_df.iloc[:, 1]
        out["MACD_hist"] = macd_df.iloc[:, 2]
    else:
        out["MACD"] = pd.NA
        out["MACD_signal"] = pd.NA
        out["MACD_hist"] = pd.NA

    out["ATR"] = ta.atr(out["high"], out["low"], out["close"], length=atr_len)
    out["volume_sma_20"] = out["volume"].rolling(volume_len).mean()
    lookback = int(CONFIG["indicators"]["drop_lookback_days"])
    out["change_5d_pct"] = out["close"].pct_change(lookback) * 100.0

    return out


def determine_signal(df: pd.DataFrame) -> dict[str, Any]:
    """Determine BUY/SELL/HOLD based on configured indicator rules."""
    last = df.iloc[-1]

    price = float(last["close"])
    ema50 = float(last["EMA50"])
    ema200 = float(last["EMA200"])
    rsi = float(last["RSI"])
    atr = float(last["ATR"])
    change_5d = float(last["change_5d_pct"])

    vol_avg = float(last["volume_sma_20"])
    vol_multiplier = float(CONFIG["indicators"]["volume_breakout_multiplier"])
    vol_breakout = bool(last["volume"] > (vol_multiplier * vol_avg)) if pd.notna(last["volume_sma_20"]) else False

    buy_condition = (
        price > ema50 > ema200
        and 40.0 <= rsi <= 65.0
        and vol_breakout
    )

    drop_threshold = float(CONFIG["indicators"]["drop_threshold_pct"])
    sell_condition = (
        price < ema50
        or rsi > 75.0
        or change_5d <= drop_threshold
    )

    if buy_condition:
        signal = "BUY"
    elif sell_condition:
        signal = "SELL"
    else:
        signal = "HOLD"

    return {
        "price": price,
        "signal": signal,
        "EMA50": ema50,
        "EMA200": ema200,
        "RSI": rsi,
        "ATR": atr,
        "5d_change": change_5d,
        "vol_breakout": vol_breakout,
    }


def generate_signal(symbol: str, timeframe: str = "1d", limit: int = 250) -> dict[str, Any]:
    """Generate final signal dict; daily HOLD can become WATCH on 4h BUY confirmation."""
    df = fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)
    df = calculate_indicators(df)
    result = determine_signal(df)

    output = {
        "symbol": _normalize_symbol(symbol, str(CONFIG["defaults"]["quote_asset"])),
        **result,
    }

    if timeframe == "1d" and output["signal"] == "HOLD":
        df_4h = fetch_ohlcv(symbol=symbol, timeframe="4h", limit=limit)
        df_4h = calculate_indicators(df_4h)
        result_4h = determine_signal(df_4h)
        if result_4h["signal"] == "BUY":
            output["signal"] = "WATCH"

    return output


def _format_money(value: float) -> str:
    return f"${value:,.2f}"


def _format_signed_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def _line(label: str, value: str, width: int = 30) -> str:
    content = f"  {label:<9}: {value}"
    return f"|{content:<{width}}|"


def generate_trade_plan(symbol: str, capital: float) -> dict[str, Any]:
    """Generate and print a formatted trade plan from latest signal and ATR values."""
    data = generate_signal(symbol)
    normalized_symbol = str(data["symbol"])
    quote_asset = str(CONFIG["defaults"]["quote_asset"])
    base_asset = normalized_symbol.replace(quote_asset, "")

    entry = float(data["price"])
    atr = float(data["ATR"])
    signal = str(data["signal"])

    if atr <= 0:
        print(f"Skipping {normalized_symbol}: invalid ATR value ({atr:.6f}).")
        return {
            "symbol": normalized_symbol,
            "signal": signal,
            "skipped": True,
            "reason": "invalid_atr",
        }

    stop_loss = entry - (atr * 1.5)
    target_1 = entry + (atr * 2.0)
    target_2 = entry + (atr * 3.5)

    risk_per_unit = entry - stop_loss
    rr_ratio = (target_1 - entry) / risk_per_unit if risk_per_unit > 0 else 0.0
    position_size = ((float(capital) * 0.01) / risk_per_unit) if risk_per_unit > 0 else 0.0

    sl_pct = ((stop_loss - entry) / entry) * 100.0
    t1_pct = ((target_1 - entry) / entry) * 100.0
    t2_pct = ((target_2 - entry) / entry) * 100.0

    plan = {
        "symbol": normalized_symbol,
        "signal": signal,
        "entry": entry,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "rr_ratio": rr_ratio,
        "position_size": position_size,
        "ATR": atr,
        "capital": float(capital),
        "skipped": rr_ratio < 1.5,
    }

    if plan["skipped"]:
        print(f"Skipping {normalized_symbol}: R:R {rr_ratio:.2f} < 1.50")
        return plan

    title = f"  {base_asset} TRADE PLAN"
    width = 30

    print("+" + "-" * width + "+")
    print(f"|{title:<{width}}|")
    print("+" + "-" * width + "+")
    print(_line("Signal", signal, width))
    print(_line("Entry", _format_money(entry), width))
    print(_line("Stop Loss", f"{_format_money(stop_loss)} ({_format_signed_pct(sl_pct)})", width))
    print(_line("Target 1", f"{_format_money(target_1)} ({_format_signed_pct(t1_pct)})", width))
    print(_line("Target 2", f"{_format_money(target_2)} ({_format_signed_pct(t2_pct)})", width))
    print(_line("R:R Ratio", f"1 : {rr_ratio:.2f}", width))
    print(_line("Position", f"{position_size:.6f} {base_asset}", width))
    print("+" + "-" * width + "+")

    return plan


if __name__ == "__main__":
    # Example local run
    print(generate_signal("BTCUSDT"))
