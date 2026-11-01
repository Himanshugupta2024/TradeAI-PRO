"""
SwingSpectrum Walk-Forward Backtester
=======================================
Har historical din ka swing signal generate karo, phir aage ke N din dekho --
target hit hua (WIN) ya SL hit (LOSS)?

Output:
  backtest_results/trades_YYYYMMDD.csv  — Har trade ka detail log
  backtest_results/summary_YYYYMMDD.csv — Per-pattern, per-regime win rate
  Console                               — Quick summary table

Usage:
  python scripts/backtester.py                          # All symbols, 6 months
  python scripts/backtester.py --lookback 90            # Last 3 months only
  python scripts/backtester.py --symbols TCS INFY BEL  # Specific symbols
  python scripts/backtester.py --forward 3              # Check 3 days ahead
  python scripts/backtester.py --slippage 0.002         # 0.2% slippage

Design:
  Walk-forward: day D ka data upto D tak use karo (no lookahead bias).
  Regime: Nifty 50 ka state each day ke liye compute karo (Nifty full history fetch).
  Slippage: Entry price = Close * (1 + slippage) for LONG, (1 - slippage) for SHORT.
  Result: First bar among next N candles jo SL ya T1 cross kare.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

# ── Project root ─────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from agents.feature_engine.indicators import calculate_indicators
from agents.market_intelligence.nse_fetcher import NSE_SYMBOLS
from agents.market_intelligence.swing_scanner import scan_swing_setups
from config import settings

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backtester")

OUTPUT_DIR = os.path.join(_ROOT, "backtest_results")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# ── Helper: yFinance fetch with MultiIndex fix ────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_daily(ticker: str, period: str = "2y") -> pd.DataFrame:
    """
    Fetch daily OHLCV from yFinance. Handles MultiIndex columns (v0.2+ fix).
    Returns DataFrame with lowercase columns: open, high, low, close, volume.
    Returns empty DataFrame on failure.
    """
    try:
        import yfinance as yf
        df = yf.download(ticker, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            return pd.DataFrame()
        # Flatten MultiIndex if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0].lower() for col in df.columns]
        else:
            df = df.rename(columns=str.lower)
        # Add timestamp column for indicator engine
        df["timestamp"] = df.index
        df = df.reset_index(drop=True)
        return df
    except Exception as e:
        log.debug("Fetch failed for %s: %s", ticker, e)
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# ── Regime detection (from Nifty 50 data) ────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _compute_adx(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    pdm = h.diff().clip(lower=0)
    ndm = (-l.diff()).clip(lower=0)
    pdm = np.where((pdm > ndm) & (pdm > 0), pdm, 0.0)
    ndm = np.where((ndm > pd.Series(pdm, index=h.index)) & (ndm > 0), ndm, 0.0)
    pdm_s = pd.Series(pdm, index=h.index)
    ndm_s = pd.Series(ndm, index=h.index)

    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=p, adjust=False).mean()

    pdi = 100 * pdm_s.ewm(span=p, adjust=False).mean() / atr.replace(0, np.nan)
    ndi = 100 * ndm_s.ewm(span=p, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(span=p, adjust=False).mean()


def classify_regime(nifty_df: pd.DataFrame, as_of_idx: int) -> str:
    """
    Nifty 50 data ke ek specific row (as_of_idx) tak ke data se regime classify karo.
    Returns: TRENDING_BULLISH | TRENDING_BEARISH | SIDEWAYS | MIXED
    """
    if nifty_df is None or nifty_df.empty or as_of_idx < 30:
        return "UNKNOWN"

    try:
        sub = nifty_df.iloc[: as_of_idx + 1].copy()
        c = sub["close"].astype(float)
        h = sub["high"].astype(float)
        l = sub["low"].astype(float)

        ema50  = float(c.ewm(span=50,  adjust=False).mean().iloc[-1])
        ema200 = float(c.ewm(span=200, adjust=False).mean().iloc[-1]) if len(sub) >= 200 \
                 else float(c.ewm(span=len(sub), adjust=False).mean().iloc[-1])
        adx    = float(_compute_adx(h, l, c, 14).iloc[-1])
        close  = float(c.iloc[-1])

        adx_trend = float(getattr(settings, "REGIME_ADX_TRENDING_FLOOR", 25.0))
        adx_mixed = float(getattr(settings, "REGIME_ADX_MIXED_FLOOR", 20.0))

        if adx >= adx_trend:
            if close > ema50 and ema50 > ema200:
                return "TRENDING_BULLISH"
            elif close < ema50 and ema50 < ema200:
                return "TRENDING_BEARISH"
            else:
                return "MIXED_TRENDING"
        elif adx >= adx_mixed:
            return "MIXED"
        else:
            return "SIDEWAYS"
    except Exception as e:
        log.debug("Regime compute error at idx %d: %s", as_of_idx, e)
        return "UNKNOWN"


# ══════════════════════════════════════════════════════════════════════════════
# ── Trade outcome checker ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def check_outcome(
    full_df: pd.DataFrame,
    signal_row_idx: int,
    entry: float,
    sl: float,
    target: float,
    direction: str,
    forward_days: int = 5,
) -> str:
    """
    Signal ke baad ke N candles mein check karo:
    - HIGH >= target (LONG) ya LOW <= target (SHORT) → WIN
    - LOW  <= sl     (LONG) ya HIGH >= sl    (SHORT) → LOSS
    - Pehle jo level touch kare wo result hota hai (bar-by-bar)
    - Koi nahi → EXPIRED

    Returns: "WIN" | "LOSS" | "EXPIRED"
    """
    future = full_df.iloc[signal_row_idx + 1 : signal_row_idx + 1 + forward_days]
    if future.empty:
        return "EXPIRED"

    for _, row in future.iterrows():
        h = float(row["high"])
        lo = float(row["low"])

        if direction == "LONG":
            # Check SL first (conservative — if both hit same bar, SL wins)
            if lo <= sl:
                return "LOSS"
            if h >= target:
                return "WIN"
        else:  # SHORT
            if h >= sl:
                return "LOSS"
            if lo <= target:
                return "WIN"

    return "EXPIRED"


# ══════════════════════════════════════════════════════════════════════════════
# ── Per-symbol walk-forward ───────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def backtest_symbol(
    symbol: str,
    full_df: pd.DataFrame,
    nifty_df: pd.DataFrame,
    test_start_idx: int,
    forward_days: int,
    slippage_pct: float,
    ignore_regime: bool = False,
) -> list[dict]:
    """
    Ek symbol ke liye walk-forward test:
    har test_start_idx se aage ke har din pe scan karo.
    Returns list of trade dicts.
    """
    trades = []
    total_rows = len(full_df)
    sector = getattr(settings, "NSE_SECTOR_MAP", {}).get(symbol, "Other")

    for day_idx in range(test_start_idx, total_rows - forward_days - 1):
        # Data up to this day only (no lookahead)
        hist_df = full_df.iloc[: day_idx + 1].copy()

        if len(hist_df) < 50:
            continue  # Not enough history for indicators

        # Indicators already calculated in full_df — slice is sufficient
        # But we need to re-pass full hist for accurate indicator warmup
        try:
            hist_with_ind = calculate_indicators(hist_df)
        except Exception:
            continue

        # Regime from Nifty data up to same day
        # Find matching Nifty row by date
        bar_date = hist_df.iloc[-1].get("timestamp", None)
        if bar_date is not None:
            try:
                bar_date = pd.Timestamp(bar_date).date()
                nifty_dates = pd.to_datetime(nifty_df["timestamp"]).dt.date
                nifty_idx = nifty_dates[nifty_dates <= bar_date].index
                nifty_as_of = int(nifty_idx[-1]) if len(nifty_idx) > 0 else day_idx
            except Exception:
                nifty_as_of = min(day_idx, len(nifty_df) - 1)
        else:
            nifty_as_of = min(day_idx, len(nifty_df) - 1)

        regime = classify_regime(nifty_df, nifty_as_of)

        # Build market context for scanner
        if ignore_regime:
            # Bypass regime gate — capture all signals for baseline
            market_ctx = {
                "index_symbol": "NIFTY 50",
                "index_regime": "UNKNOWN",
                "index_trend":  "NEUTRAL",
            }
        else:
            market_ctx = {
                "index_symbol": "NIFTY 50",
                "index_regime": regime,
                "index_trend": (
                    "BULLISH" if regime == "TRENDING_BULLISH" else
                    "BEARISH" if regime == "TRENDING_BEARISH" else
                    "NEUTRAL"
                ),
            }

        # Run swing scanner
        # Backtest mode: temporarily disable stale-bar gate so historical slices work.
        # In live use, this gate prevents scanning stale data; in backtesting it is
        # expected that past bars are "old" relative to today.
        _orig_bar_age = getattr(settings, "SWING_MAX_BAR_AGE_DAYS", 5)
        settings.SWING_MAX_BAR_AGE_DAYS = 9999  # Disable for this call
        try:
            setups = scan_swing_setups(
                hist_with_ind,
                symbol=symbol,
                sector=sector,
                market_context=market_ctx,
                allow_fallback=True,   # Capture all signals including watchlist
            )
        except Exception as e:
            log.debug("Scan error %s day %d: %s", symbol, day_idx, e)
            continue
        finally:
            settings.SWING_MAX_BAR_AGE_DAYS = _orig_bar_age  # Always restore

        if not setups:
            continue

        # Use strongest setup
        setup = setups[0]
        entry_raw = float(setup.entry_zone)
        sl = float(setup.stop_loss)
        target = float(setup.target_1)
        direction = str(setup.direction)

        # Skip if SL or target invalid
        if entry_raw <= 0 or sl <= 0 or target <= 0:
            continue
        if direction == "LONG" and (sl >= entry_raw or target <= entry_raw):
            continue
        if direction == "SHORT" and (sl <= entry_raw or target >= entry_raw):
            continue

        # Apply slippage
        if direction == "LONG":
            entry = round(entry_raw * (1 + slippage_pct), 2)
        else:
            entry = round(entry_raw * (1 - slippage_pct), 2)

        # Recalculate RR with slippage-adjusted entry
        risk   = abs(entry - sl)
        reward = abs(target - entry)
        rr     = round(reward / risk, 2) if risk > 0 else 0.0

        # Check outcome using actual next-day prices
        outcome = check_outcome(
            full_df, day_idx, entry, sl, target, direction, forward_days
        )

        trade = {
            "symbol":    symbol,
            "date":      str(bar_date) if bar_date else f"idx_{day_idx}",
            "pattern":   str(setup.pattern),
            "direction": direction,
            "grade":     str(setup.grade),
            "strength":  int(setup.strength),
            "entry":     entry,
            "sl":        sl,
            "target":    target,
            "rr":        rr,
            "regime":    regime,
            "outcome":   outcome,
            "sector":    sector,
        }
        trades.append(trade)

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# ── Report generation ─────────────────────────────────════════════════════════
# ══════════════════════════════════════════════════════════════════════════════

def build_report(trades: list[dict]) -> dict:
    """
    Trades list se summary report banao.
    Per-pattern, per-regime, per-grade win rates.
    """
    if not trades:
        return {"error": "No trades found in test period"}

    df = pd.DataFrame(trades)
    # Exclude EXPIRED for win rate calc (they didn't resolve)
    resolved = df[df["outcome"] != "EXPIRED"].copy()

    if resolved.empty:
        return {"error": "All trades EXPIRED — forward_days too short or SL/Target too wide"}

    total       = len(resolved)
    wins        = (resolved["outcome"] == "WIN").sum()
    losses      = (resolved["outcome"] == "LOSS").sum()
    overall_wr  = round(wins / total * 100, 1)
    avg_rr      = round(resolved["rr"].mean(), 2)

    # ── Per Pattern ──────────────────────────────────────────────────────────
    pattern_stats = []
    for pat, grp in resolved.groupby("pattern"):
        w = (grp["outcome"] == "WIN").sum()
        n = len(grp)
        pattern_stats.append({
            "pattern":    pat,
            "trades":     n,
            "wins":       int(w),
            "losses":     int(n - w),
            "win_rate_%": round(w / n * 100, 1),
            "avg_rr":     round(grp["rr"].mean(), 2),
            "verdict":    "[KEEP]" if w / n >= 0.50 else ("[REVIEW]" if w / n >= 0.40 else "[DISABLE]"),
        })
    pattern_stats.sort(key=lambda x: -x["win_rate_%"])

    # ── Per Regime ───────────────────────────────────────────────────────────
    regime_stats = []
    for reg, grp in resolved.groupby("regime"):
        w = (grp["outcome"] == "WIN").sum()
        n = len(grp)
        regime_stats.append({
            "regime":     reg,
            "trades":     n,
            "win_rate_%": round(w / n * 100, 1),
            "avg_rr":     round(grp["rr"].mean(), 2),
        })
    regime_stats.sort(key=lambda x: -x["win_rate_%"])

    # ── Per Grade ────────────────────────────────────────────────────────────
    grade_stats = []
    for grade, grp in resolved.groupby("grade"):
        w = (grp["outcome"] == "WIN").sum()
        n = len(grp)
        grade_stats.append({
            "grade":      grade,
            "trades":     n,
            "win_rate_%": round(w / n * 100, 1),
            "avg_rr":     round(grp["rr"].mean(), 2),
        })
    grade_stats.sort(key=lambda x: x["grade"])

    # ── Pattern × Regime cross-tab ────────────────────────────────────────────
    cross = []
    for (pat, reg), grp in resolved.groupby(["pattern", "regime"]):
        w = (grp["outcome"] == "WIN").sum()
        n = len(grp)
        if n >= 3:  # Only show if enough samples
            cross.append({
                "pattern": pat, "regime": reg,
                "trades": n, "win_rate_%": round(w / n * 100, 1),
                "verdict": "WIN" if w / n >= 0.50 else "LOSS",
            })
    cross.sort(key=lambda x: (-x["win_rate_%"], x["pattern"]))

    return {
        "generated_at":     datetime.now().isoformat(),
        "total_signals":    len(df),
        "resolved_trades":  total,
        "expired_trades":   len(df) - total,
        "overall_wins":     int(wins),
        "overall_losses":   int(losses),
        "overall_win_rate": overall_wr,
        "avg_rr":           avg_rr,
        "pattern_stats":    pattern_stats,
        "regime_stats":     regime_stats,
        "grade_stats":      grade_stats,
        "pattern_x_regime": cross,
    }


def print_report(report: dict) -> None:
    """Console pe readable summary print karo."""
    if "error" in report:
        print(f"\nERROR: {report['error']}")
        return

    print("\n" + "=" * 65)
    print("  SwingSpectrum Backtest Report")
    print("=" * 65)
    print(f"  Total signals:    {report['total_signals']}")
    print(f"  Resolved trades:  {report['resolved_trades']}")
    print(f"  Expired (no hit): {report['expired_trades']}")
    print(f"  Overall Win Rate: {report['overall_win_rate']}%  "
          f"({report['overall_wins']}W / {report['overall_losses']}L)")
    print(f"  Average R:R:      {report['avg_rr']}x")
    print()

    # Pattern table
    print("  --- Pattern Win Rates ------------------------------------------")
    print(f"  {'Pattern':<40} {'Trades':>6} {'Win%':>6} {'RR':>5}  Verdict")
    print("  " + "-" * 62)
    for p in report["pattern_stats"]:
        name = p["pattern"][:38]
        print(f"  {name.ljust(40)}{str(p['trades']).rjust(6)}{str(p['win_rate_%']).rjust(6)}%{str(p['avg_rr']).rjust(6)}  {p['verdict']}")

    print()

    # Regime table
    print("  --- Regime Win Rates -------------------------------------------")
    print(f"  {'Regime':<25} {'Trades':>6} {'Win%':>6} {'RR':>5}")
    print("  " + "-" * 45)
    for r in report["regime_stats"]:
        print(f"  {r['regime']:<25} {r['trades']:>6} {r['win_rate_%']:>5}% {r['avg_rr']:>5}")

    print()

    # Grade table
    print("  --- Grade Win Rates --------------------------------------------")
    print(f"  {'Grade':<10} {'Trades':>6} {'Win%':>6} {'RR':>5}")
    print("  " + "-" * 30)
    for g in report["grade_stats"]:
        print(f"  {g['grade']:<10} {g['trades']:>6} {g['win_rate_%']:>5}% {g['avg_rr']:>5}")

    print()

    # Pattern × Regime
    if report["pattern_x_regime"]:
        print("  --- Pattern x Regime (min 3 trades) ----------------------------")
        print(f"  {'Pattern':<35} {'Regime':<20} {'N':>4} {'Win%':>6}")
        print("  " + "-" * 70)
        for c in report["pattern_x_regime"][:20]:  # Top 20
            print(f"  {c['pattern'][:33].ljust(35)}{c['regime'].ljust(22)}{str(c['trades']).rjust(4)}{str(c['win_rate_%']).rjust(6)}%  {c['verdict']}")

    print()
    print("  --- Actionable Insights -----------------------------------------")
    for p in report["pattern_stats"]:
        if p["verdict"] == "[DISABLE]":
            print(f"  [DISABLE] '{p['pattern']}' -- only {p['win_rate_%']}% win rate")
        elif p["verdict"] == "[REVIEW]":
            print(f"  [REVIEW]  '{p['pattern']}' -- {p['win_rate_%']}% win rate (below 50%)")

    print("=" * 65)
    print()


# ══════════════════════════════════════════════════════════════════════════════
# ── Main ─────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="SwingSpectrum Walk-Forward Backtester"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="NSE symbols to test (default: top 20 from NSE_SYMBOLS)"
    )
    parser.add_argument(
        "--ignore-regime", action="store_true", default=False,
        help="Bypass regime gate — measure raw pattern accuracy without filtering (baseline)"
    )
    parser.add_argument(
        "--lookback", type=int, default=180,
        help="Days to test (default: 180 = 6 months)"
    )
    parser.add_argument(
        "--forward", type=int, default=5,
        help="Days ahead to check SL/Target hit (default: 5)"
    )
    parser.add_argument(
        "--slippage", type=float, default=0.0015,
        help="Slippage fraction (default: 0.0015 = 0.15%%)"
    )
    parser.add_argument(
        "--data-period", default="2y",
        help="yFinance period for history (default: 2y)"
    )
    args = parser.parse_args()

    # ── Symbols ──────────────────────────────────────────────────────────────
    all_syms = list(NSE_SYMBOLS.keys())
    # Remove index symbols — they don't have swing setups
    index_syms = {"NIFTY 50", "BANKNIFTY", "SENSEX", "NIFTY IT", "NIFTY BANK"}
    all_syms   = [s for s in all_syms if s not in index_syms]

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = all_syms[:20]  # Default: top 20

    log.info("Backtester starting: %d symbols | lookback=%dd | forward=%dd | slippage=%.2f%%",
             len(symbols), args.lookback, args.forward, args.slippage * 100)

    # ── Fetch Nifty 50 for regime ─────────────────────────────────────────────
    log.info("Fetching Nifty 50 data for regime calculation...")
    nifty_df = _fetch_daily("^NSEI", period=args.data_period)
    if nifty_df.empty:
        log.warning("Nifty 50 fetch failed — regime will be UNKNOWN for all days")

    # ── Fetch + test each symbol ──────────────────────────────────────────────
    all_trades: list[dict] = []
    ticker_map = getattr(settings, "NSE_TICKER_MAP", {})

    ignore_regime = getattr(args, "ignore_regime", False)
    if ignore_regime:
        log.info("Mode: BASELINE (regime gate disabled — measuring raw pattern accuracy)")
    else:
        log.info("Mode: REGIME-FILTERED (only signals that pass current regime gate)")

    for i, sym in enumerate(symbols):
        # NSE_SYMBOLS values already contain proper tickers (.NS suffix)
        ticker = NSE_SYMBOLS.get(sym) or ticker_map.get(sym) or f"{sym}.NS"
        if not isinstance(ticker, str) or not ticker:
            ticker = f"{sym}.NS"
        log.info("[%d/%d] Fetching %s (%s)...", i + 1, len(symbols), sym, ticker)

        df = _fetch_daily(ticker, period=args.data_period)
        if df.empty or len(df) < 60:
            log.warning("  Skipping %s — not enough data (%d rows)", sym, len(df))
            continue

        # Test start index = total_rows - lookback_days
        test_start = max(50, len(df) - args.lookback)  # min 50 for indicator warmup
        log.info("  %s: %d rows total, testing from row %d (%d test days)",
                 sym, len(df), test_start, len(df) - test_start)

        trades = backtest_symbol(
            sym, df, nifty_df,
            test_start_idx=test_start,
            forward_days=args.forward,
            slippage_pct=args.slippage,
            ignore_regime=ignore_regime,
        )
        log.info("  %s: %d signals found, %d resolved",
                 sym, len(trades), sum(1 for t in trades if t["outcome"] != "EXPIRED"))
        all_trades.extend(trades)

    log.info("Total trades collected: %d", len(all_trades))

    # ── Build and print report ────────────────────────────────────────────────
    report = build_report(all_trades)
    print_report(report)

    # ── Save outputs ──────────────────────────────────────────────────────────
    date_str = datetime.now().strftime("%Y%m%d_%H%M")

    # Full trade log → CSV
    if all_trades:
        trades_df = pd.DataFrame(all_trades)
        csv_path = os.path.join(OUTPUT_DIR, f"trades_{date_str}.csv")
        trades_df.to_csv(csv_path, index=False)
        log.info("Trade log saved: %s", csv_path)

    # Summary report → JSON
    json_path = os.path.join(OUTPUT_DIR, f"report_{date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    log.info("Report saved: %s", json_path)

    # Also save as latest.json for easy access
    latest_path = os.path.join(OUTPUT_DIR, "latest_report.json")
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    log.info("Latest report: %s", latest_path)

    return report


if __name__ == "__main__":
    main()
