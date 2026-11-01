"""
Local smoke test for Phase 1 Swing Scanner changes.

Runs a full scan against live NSE daily data (yFinance) and prints:
  - Per-symbol stage counters (what gate blocked what)
  - Final setups with grade / strength / volume ratio / ADX / R:R
  - Aggregate summary (total scanned, raw patterns, gated, dropped by R:R, final)

Usage (from e:\\trading):
    python scripts\\swing_smoke_test.py
    python scripts\\swing_smoke_test.py --symbols RELIANCE TCS INFY
    python scripts\\swing_smoke_test.py --limit 20       # scan first 20 from NSE_SYMBOLS
    python scripts\\swing_smoke_test.py --verbose        # per-symbol stage dump

No server required. Directly exercises scanner + fetcher.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from typing import Iterable

# Ensure workspace root is on sys.path so `agents`, `config` resolve even when
# the script is launched from scripts/ directly.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agents.feature_engine.indicators import calculate_indicators
from agents.market_intelligence.nse_fetcher import NSE_SYMBOLS as NSE_FETCHER_SYMBOLS, fetch_ohlcv
from agents.market_intelligence.swing_scanner import (
    compute_sector_momentum_map,
    scan_swing_setups,
)
from config import settings

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("swing_smoke")
logger.setLevel(logging.INFO)


def _resolve_symbols(cli_symbols: list[str] | None, limit: int | None) -> list[str]:
    """Resolve scan universe.

    Priority: CLI args > nse_fetcher.NSE_SYMBOLS (full tracked map) > settings.NSE_SYMBOLS.
    Indices ("_INDEX" sector) are excluded since they aren't tradeable swing setups.
    """
    if cli_symbols:
        return cli_symbols
    # Prefer the full fetcher map (~80+ tracked NSE names); settings list is a narrow fallback.
    if isinstance(NSE_FETCHER_SYMBOLS, dict) and NSE_FETCHER_SYMBOLS:
        universe = list(NSE_FETCHER_SYMBOLS.keys())
    else:
        universe = list(settings.NSE_SYMBOLS)
    symbols = [
        s for s in universe
        if not str(settings.NSE_SECTOR_MAP.get(s, "")).startswith("_")
    ]
    if limit and limit > 0:
        return symbols[:limit]
    return symbols


def _fetch_one(symbol: str) -> tuple[str, "object | None"]:
    """Fetch 1y daily bars + indicators (matches live service flow exactly)."""
    try:
        df = fetch_ohlcv(symbol, "1y", "1d")
        if df is None or getattr(df, "empty", True) or len(df) < 30:
            return symbol, None
        df = calculate_indicators(df)
        return symbol, df
    except Exception as exc:
        logger.warning("Fetch failed for %s: %s", symbol, exc)
        return symbol, None


def _scan_all(symbols: Iterable[str], verbose: bool) -> None:
    t0 = time.time()
    symbol_data: dict[str, object] = {}
    for i, sym in enumerate(symbols, 1):
        name, df = _fetch_one(sym)
        if df is not None and not getattr(df, "empty", True):
            symbol_data[name] = df
        if i % 10 == 0:
            logger.info("Fetched %d symbols (%.1fs elapsed)", i, time.time() - t0)

    logger.info(
        "Fetched %d daily DFs in %.1fs. Starting scan …",
        len(symbol_data),
        time.time() - t0,
    )

    sector_mom = compute_sector_momentum_map(symbol_data)
    if sector_mom:
        preview = ", ".join(f"{k}:{v:+.1f}%" for k, v in list(sector_mom.items())[:6])
        logger.info("Sector momentum (median) — %s%s", preview,
                    " …" if len(sector_mom) > 6 else "")

    agg_counter: Counter[str] = Counter()
    blocked_reasons: Counter[str] = Counter()
    all_setups = []

    for sym, df in symbol_data.items():
        sector = settings.NSE_SECTOR_MAP.get(sym, "Other")
        momentum = sector_mom.get(sector, 0.0)
        stages: dict = {}
        try:
            setups = scan_swing_setups(
                df,
                sym,
                sector_momentum=momentum,
                sector=sector,
                stages_out=stages,
            )
        except Exception as exc:
            logger.error("Scan error for %s: %s", sym, exc)
            continue

        blocked = stages.get("blocked")
        if blocked:
            blocked_reasons[blocked] += 1

        agg_counter["raw_patterns"] += int(stages.get("raw_pattern_count", 0) or 0)
        agg_counter["after_rr"] += int(stages.get("after_rr_count", 0) or 0)
        agg_counter["after_regime"] += int(stages.get("after_regime_count", 0) or 0)
        agg_counter["final"] += int(stages.get("after_dedupe_count", 0) or 0)

        if verbose and (setups or blocked):
            print(
                f"  {sym:<18} bars={stages.get('bars'):<4} age={stages.get('bar_age_days')!s:<5} "
                f"raw={stages.get('raw_pattern_count',0)} rr={stages.get('after_rr_count',0)} "
                f"final={stages.get('after_dedupe_count',0)} blocked={blocked or '-'}"
            )

        for s in setups:
            all_setups.append(s)

    all_setups.sort(key=lambda s: (s.grade, -s.strength))
    print("\n" + "=" * 78)
    print(" AGGREGATE SUMMARY")
    print("=" * 78)
    print(f"  Symbols fetched        : {len(symbol_data)}")
    print(f"  Raw pattern hits       : {agg_counter['raw_patterns']}")
    print(f"  After R:R gate         : {agg_counter['after_rr']}")
    print(f"  After regime gate      : {agg_counter['after_regime']}")
    print(f"  FINAL setups (deduped) : {agg_counter['final']}")
    print(f"  Wall time              : {time.time() - t0:.1f}s")
    if blocked_reasons:
        print("  Blocked symbols:")
        for reason, count in blocked_reasons.most_common():
            print(f"    {reason:<20} {count}")

    if all_setups:
        print("\n" + "=" * 78)
        print(" SETUPS (sorted by grade, strength)")
        print("=" * 78)
        print(
            f"  {'SYM':<16} {'GRADE':<6} {'DIR':<6} {'STR':<4} {'R:R':<4} "
            f"{'ENTRY':<8} {'SL':<8} {'T1':<8} {'ADX':<5} {'VOL×':<5} PATTERN"
        )
        print("-" * 78)
        for s in all_setups[:40]:
            vol_ratio = (s.indicators or {}).get("vol_ratio", "")
            print(
                f"  {s.symbol:<16} {s.grade:<6} {s.direction:<6} "
                f"{s.strength:<4} {s.risk_reward:<4} "
                f"{s.entry_zone:<8} {s.stop_loss:<8} {s.target_1:<8} "
                f"{s.adx_value:<5.1f} {vol_ratio!s:<5} {s.pattern}"
            )
        if len(all_setups) > 40:
            print(f"  … and {len(all_setups) - 40} more")
    else:
        print("\n  No setups passed all gates. Try --verbose to see per-symbol stage counters.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Swing scanner smoke test (Phase 1)")
    parser.add_argument(
        "--symbols", nargs="*", help="Specific symbols to scan (uses NSE display names)"
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Scan only the first N symbols from NSE_SYMBOLS (default: all)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print per-symbol stage counters",
    )
    args = parser.parse_args()

    syms = _resolve_symbols(args.symbols, args.limit or None)
    if not syms:
        logger.error("No symbols to scan. Check NSE_SYMBOLS or pass --symbols.")
        return 1

    print(f"\nScanning {len(syms)} symbol(s) with Phase 1 gates:")
    print(f"  SWING_MAX_BAR_AGE_DAYS             = {settings.SWING_MAX_BAR_AGE_DAYS}")
    print(f"  SWING_VOL_ROLLING_DAYS             = {settings.SWING_VOL_ROLLING_DAYS}")
    print(f"  SWING_ADX_MIN_FOR_BREAKOUT         = {settings.SWING_ADX_MIN_FOR_BREAKOUT}")
    print(f"  SWING_REQUIRE_2BAR_BREAKOUT_CONFIRM= {settings.SWING_REQUIRE_2BAR_BREAKOUT_CONFIRM}")
    print(f"  SWING_MIN_BARS_FOR_EMA200          = {settings.SWING_MIN_BARS_FOR_EMA200}")
    print(f"  SWING_NEWS_AFFECTS_STRENGTH        = {settings.SWING_NEWS_AFFECTS_STRENGTH}")
    print(f"  SWING_SECTOR_MOMENTUM_AGG          = {settings.SWING_SECTOR_MOMENTUM_AGG}")
    print(f"  SWING_MIN_RISK_REWARD              = {settings.SWING_MIN_RISK_REWARD}")
    print()

    try:
        _scan_all(syms, verbose=args.verbose)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
