"""
Nightly SwingSpectrum Batch Scanner
====================================
Roz raat chalao (cron / Task Scheduler) taaki subah aapke paas
ek ready shortlist ho — "Kal kaunsa stock buy karu?"

Run kaise karein:
    python scripts/nightly_swing_scan.py

Optional flags:
    --grade A          → sirf Grade-A setups dikhao
    --top 10           → top N setups print karo
    --output custom.json

Output:
    data/swing_shortlist_YYYY-MM-DD.json
    data/swing_shortlist_latest.json   ← hamesha latest run ka link
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

# ── Project root sys.path fix ────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.data_ingestion.context_fetcher import ContextFetcher
from agents.feature_engine.indicators import calculate_indicators
from agents.market_intelligence.nse_fetcher import NSE_SYMBOLS, fetch_arbitrary_symbol
from agents.market_intelligence.swing_scanner import scan_swing_setups
from db import swing_store
from db.swing_outcome_tracker import run_outcome_evaluation_once
from scripts.ranking_engine import rank_setups, rank_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nightly_scan")


# ── Config ───────────────────────────────────────────────────────────────────

OUTPUT_DIR = ROOT / "data"
OHLCV_PERIOD = "1y"         # 1 saal ka data — EMA_200 ke liye zaroori
OHLCV_INTERVAL = "1d"       # Daily candles
RATE_LIMIT_SLEEP = 1.2      # yFinance throttle se bachne ke liye (seconds)

# Index symbols skip karein — yeh scanner ke liye relevant nahi
SKIP_SYMBOLS = {"NIFTY 50", "BANKNIFTY", "SENSEX", "NIFTY IT"}

# NSE Gazetted Trading Holidays 2026
NSE_HOLIDAYS_2026 = {
    "2026-01-26", "2026-02-17", "2026-03-03", "2026-03-20", "2026-04-03",
    "2026-04-14", "2026-04-26", "2026-05-01", "2026-05-27", "2026-06-25",
    "2026-08-15", "2026-08-25", "2026-10-02", "2026-10-20", "2026-11-08",
    "2026-11-24", "2026-12-25"
}

def is_nse_trading_day(target_date: date | None = None) -> bool:
    """Check if given date is an active NSE trading day (not weekend or gazetted holiday)."""
    if target_date is None:
        target_date = date.today()
    if target_date.weekday() >= 5:  # Saturday or Sunday
        return False
    if target_date.isoformat() in NSE_HOLIDAYS_2026:
        return False
    return True

# NSE public CSV URLs for index constituents
_NSE_INDEX_URLS = {
    "nifty500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "nifty200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fetch_nse_index_symbols(index_name: str = "nifty500") -> list[str]:
    """
    NSE ke public CSV se NIFTY 500/200/100 symbols fetch karo.
    Returns list of stock symbols (e.g. ['RELIANCE', 'TCS', ...]).
    """
    import requests

    url = _NSE_INDEX_URLS.get(index_name)
    if not url:
        logger.warning("Unknown index: %s — using nifty500", index_name)
        url = _NSE_INDEX_URLS["nifty500"]

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()

        lines = resp.text.strip().split("\n")
        # CSV header: Company Name, Industry, Symbol, Series, ISIN Code
        symbols = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 3:
                sym = parts[2].strip().strip('"')
                if sym and sym not in SKIP_SYMBOLS:
                    symbols.append(sym)

        logger.info("Fetched %d symbols from NSE %s index", len(symbols), index_name.upper())
        return symbols

    except Exception as exc:
        logger.warning("NSE index fetch failed (%s): %s — falling back to local list", index_name, exc)
        return []


def _symbol_list(universe: str = "nifty500") -> list[str]:
    """
    Stock symbols return karo for scanning.

    Universe options:
        - 'nifty500': All NIFTY 500 stocks (~500 liquid stocks) — DEFAULT
        - 'nifty200': NIFTY 200 stocks (~200)
        - 'nifty100': NIFTY 100 stocks (~100)
        - 'local':    Hardcoded NSE_SYMBOLS list (~115)
    """
    if universe == "local":
        return [s for s in NSE_SYMBOLS if s not in SKIP_SYMBOLS]

    # Try dynamic fetch from NSE
    symbols = _fetch_nse_index_symbols(universe)

    if symbols:
        # Merge with local list to keep any custom small-caps we added manually
        local = {s for s in NSE_SYMBOLS if s not in SKIP_SYMBOLS}
        merged = list(dict.fromkeys(symbols + list(local)))  # preserve order, dedupe
        return merged

    # Fallback to local hardcoded list
    logger.warning("Using local symbol list (NSE API unavailable)")
    return [s for s in NSE_SYMBOLS if s not in SKIP_SYMBOLS]


def _fetch_and_prepare(symbol: str) -> "pd.DataFrame | None":
    """Ek symbol ke liye OHLCV fetch karo + indicators compute karo."""
    try:
        df = fetch_arbitrary_symbol(symbol, period=OHLCV_PERIOD, interval=OHLCV_INTERVAL)
        if df is None or df.empty or len(df) < 30:
            logger.debug("  [SKIP] %s — insufficient data (%d bars)", symbol, len(df) if df is not None else 0)
            return None
        df = calculate_indicators(df)
        return df
    except Exception as exc:
        logger.warning("  [ERR] %s — fetch/indicator error: %s", symbol, exc)
        return None


def _grade_label(grade: str) -> str:
    icons = {"A": "[A]", "B": "[B]", "C": "[C]"}
    return icons.get(grade, grade)


def _setup_console() -> None:
    """Windows console ko UTF-8 mein force karo."""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _print_shortlist(results: list[dict], top_n: int = 15) -> None:
    """Console par shortlist print karo (human-readable)."""
    if not results:
        print("\n[WARNING] Aaj ke liye koi qualifying setup nahi mila.\n")
        return

    shown = results[:top_n]
    print(f"\n{'='*65}")
    print(f"  [SWING] SwingSpectrum Morning Shortlist -- {date.today().isoformat()}")
    print(f"  Top {len(shown)} setups out of {len(results)} qualifying symbols")
    print(f"{'='*65}")

    for i, s in enumerate(shown, 1):
        # ranked dict fields (from rank_summary top_picks)
        grade     = s.get("grade") or s.get("new_grade", "C")
        direction = s.get("direction", "")
        symbol    = s.get("symbol", "")
        pattern   = s.get("pattern", "")
        entry     = s.get("entry") or s.get("entry_zone", 0)
        sl        = s.get("sl") or s.get("stop_loss", 0)
        t1        = s.get("t1") or s.get("target_1", 0)
        rr        = s.get("rr") or s.get("risk_reward", 0)
        score     = s.get("composite_score", 0)
        breakdown = s.get("score_breakdown", {})
        direction_icon = "LONG ^" if direction == "LONG" else "SHORT v"

        print(
            f"\n{i:>2}. Grade {_grade_label(grade)}  {direction_icon}  "
            f"{symbol:<14}  {pattern}"
        )
        print(
            f"      Entry: Rs{entry:.2f}  |  SL: Rs{sl:.2f}  "
            f"|  T1: Rs{t1:.2f}  |  R:R {rr:.1f}x"
        )
        print(f"      Composite Score: {score:.1f}/100  |  Sector: {s.get('sector_tag') or 'N/A'}")
        # News sentiment indicator
        ns = float(s.get("news_score", 0) or 0)
        if ns != 0:
            news_icon = "[+] Positive" if ns > 0.1 else "[-] Negative" if ns < -0.1 else "[~] Neutral"
            print(f"      News Sentiment: {ns:+.2f} {news_icon}")
        if breakdown:
            print(
                f"      Scores — RR:{breakdown.get('rr_score',0):.0f} "
                f"Str:{breakdown.get('strength_score',0):.0f} "
                f"Vol:{breakdown.get('volume_score',0):.0f} "
                f"Conf:{breakdown.get('confluence_score',0):.0f} "
                f"Sec:{breakdown.get('sector_score',0):.0f}"
            )
        if s.get("entry_note"):
            print(f"      Note: {s['entry_note']}")

    print(f"\n{'='*65}")
    print(f"  [!] Education only -- not financial advice. Always use SL.")
    print(f"{'='*65}\n")


# ── Main scan logic ───────────────────────────────────────────────────────────

def run_scan(
    grade_filter: str | None = None,
    top_n: int = 15,
    output_path: str | None = None,
    universe: str = "nifty500",
) -> list[dict]:
    """
    Full nightly scan run karo.

    Args:
        grade_filter : "A", "B", "C", ya None (sab grades)
        top_n        : kitne results print karein
        output_path  : custom JSON output file path
        universe     : 'nifty500', 'nifty200', 'nifty100', or 'local'

    Returns:
        List of setup dicts sorted by (grade, strength).
    """
    symbols = _symbol_list(universe=universe)
    total = len(symbols)
    logger.info("Scan shuru -- %d symbols scan honge", total)
    logger.info("   Period: %s | Interval: %s", OHLCV_PERIOD, OHLCV_INTERVAL)

    # ── News Context Engine ──────────────────────────────────────────────────
    # Initialize once; ContextFetcher caches internally so repeated calls for
    # the same symbol within refresh window are near-free.
    ctx_fetcher: ContextFetcher | None = None
    try:
        ctx_fetcher = ContextFetcher()
        logger.info("   News context engine initialized (RSS + cache)")
    except Exception as exc:
        logger.warning("   News context engine failed to init: %s — scanning without news", exc)

    all_setups: list[dict] = []
    errors = 0
    skipped = 0
    news_fetched = 0

    for idx, symbol in enumerate(symbols, 1):
        logger.info("[%d/%d] Scanning %s ...", idx, total, symbol)

        df = _fetch_and_prepare(symbol)
        if df is None:
            skipped += 1
            time.sleep(RATE_LIMIT_SLEEP * 0.5)
            continue

        # ── Fetch news context for this symbol ────────────────────────────────
        news_context: dict | None = None
        news_score_display = "N/A"
        if ctx_fetcher is not None:
            try:
                snapshot = ctx_fetcher.get_snapshot_sync(symbol)
                news_context = {"context_score": snapshot.context_score}
                news_score_display = f"{snapshot.context_score:+.2f}"
                news_fetched += 1
            except Exception as exc:
                logger.debug("  [NEWS] %s — context fetch failed: %s", symbol, exc)

        stages: dict = {}
        try:
            setups = scan_swing_setups(
                df=df,
                symbol=symbol,
                stages_out=stages,
                allow_fallback=False,   # Batch scan mein strict mode — no fallback
                news_context=news_context,
            )
        except Exception as exc:
            logger.warning("  [SCAN ERR] %s: %s", symbol, exc)
            errors += 1
            time.sleep(RATE_LIMIT_SLEEP)
            continue

        if setups:
            for setup in setups:
                d = asdict(setup)
                d["scan_stages"] = stages
                d["news_score"] = float(news_context["context_score"]) if news_context else 0.0
                all_setups.append(d)
            logger.info(
                "  >> %s -- %d setup(s) | Best: %s (%s) R:R %.1f | News: %s",
                symbol,
                len(setups),
                setups[0].pattern,
                setups[0].grade,
                setups[0].risk_reward,
                news_score_display,
            )
        else:
            blocked = stages.get("blocked")
            raw_count = stages.get("raw_pattern_count", 0)
            logger.debug(
                "  -- %s -- No setup | blocked=%s raw=%s rr=%s regime=%s",
                symbol,
                blocked or "-",
                raw_count,
                stages.get("after_rr_count", 0),
                stages.get("after_regime_count", 0),
            )

        # yFinance rate limit se bachne ke liye thoda wait
        time.sleep(RATE_LIMIT_SLEEP)

    # ── Multi-Factor Ranking (Step 2) ────────────────────────────────────────
    logger.info("Ranking %d setups with multi-factor engine...", len(all_setups))
    ranked = rank_setups(all_setups)
    summary = rank_summary(ranked)

    logger.info(
        "   Grades → A:%d  B:%d  C:%d",
        summary["grade_a"], summary["grade_b"], summary["grade_c"]
    )

    # Grade filter (on new_grade from ranking engine)
    top_picks = summary["top_picks"]
    if grade_filter:
        top_picks = [p for p in top_picks if p["grade"] == grade_filter.upper()]

    # ── Save to JSON ─────────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().isoformat()
    run_ts = datetime.now().isoformat()

    payload = {
        "scan_date": today_str,
        "scan_timestamp": run_ts,
        "symbols_scanned": total,
        "symbols_skipped": skipped,
        "scan_errors": errors,
        "setups_found": len(all_setups),
        "news_enriched": news_fetched > 0,
        "news_fetched_count": news_fetched,
        "grade_summary": {
            "A": summary["grade_a"],
            "B": summary["grade_b"],
            "C": summary["grade_c"],
        },
        "grade_filter": grade_filter,
        "shortlist": top_picks,          # ranked + re-graded
        "raw_setups": all_setups,        # original scanner output
    }

    # Date-stamped file
    dated_path = output_path or str(OUTPUT_DIR / f"swing_shortlist_{today_str}.json")
    with open(dated_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    # Always overwrite "latest"
    latest_path = OUTPUT_DIR / "swing_shortlist_latest.json"
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    # ── Save to DB (creates OPEN outcome rows for future evaluation) ──────────
    scan_id = None
    try:
        scan_id = swing_store.save_scan(
            setups=all_setups,
            symbols_scanned=total,
            trigger="nightly_batch",
        )
        if scan_id:
            logger.info("   DB: Scan #%d saved (%d outcome rows created)", scan_id, len(all_setups))
        else:
            logger.warning("   DB: save_scan returned None")
    except Exception as exc:
        logger.warning("   DB: save_scan failed (non-fatal): %s", exc)

    # ── Evaluate past OPEN outcomes (T1_HIT / SL_HIT / EXPIRED) ──────────────
    outcome_stats: dict = {}
    try:
        outcome_stats = run_outcome_evaluation_once(max_outcomes=200)
        updated = outcome_stats.get("updated", 0)
        if updated > 0:
            logger.info("   Outcomes: %d resolved (from %d open)", updated, outcome_stats.get("open_queried", 0))
        else:
            logger.info("   Outcomes: no open rows to evaluate")
    except Exception as exc:
        logger.warning("   Outcome evaluation failed (non-fatal): %s", exc)

    # ── Compute historical win-rate stats ─────────────────────────────────────
    backtest_stats: dict = {}
    try:
        backtest_stats = swing_store.compute_stats(days=30)
        closed = backtest_stats.get("closed", 0)
        if closed > 0:
            logger.info(
                "   Backtest (30d): %d closed | Win: %.1f%% | W:%d L:%d E:%d",
                closed,
                backtest_stats.get("win_rate_pct", 0),
                backtest_stats.get("wins", 0),
                backtest_stats.get("losses", 0),
                backtest_stats.get("expired", 0),
            )
            # Per-grade breakdown
            for g in ("A", "B", "C"):
                gd = backtest_stats.get("by_grade", {}).get(g, {})
                gc = gd.get("closed", 0)
                if gc > 0:
                    logger.info(
                        "     Grade %s: %d closed | Win: %.1f%%",
                        g, gc, gd.get("win_rate_pct", 0)
                    )
    except Exception as exc:
        logger.warning("   Backtest stats failed (non-fatal): %s", exc)

    # Add backtest stats to payload for Telegram / dashboard
    payload["backtest_30d"] = backtest_stats
    payload["outcome_eval"] = outcome_stats
    payload["db_scan_id"] = scan_id

    # ── History Archive for UI Score over Time ────────────────────────────────
    history_dir = OUTPUT_DIR / "swing_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    for setup in top_picks:
        sym = setup["symbol"]
        hist_path = history_dir / f"{sym}.json"
        
        history_data = []
        if hist_path.exists():
            try:
                with open(hist_path, "r", encoding="utf-8") as f:
                    history_data = json.load(f)
            except Exception:
                pass
        
        # Avoid duplicate entries for the same date
        if not any(entry.get("date") == today_str for entry in history_data):
            history_data.append({
                "date": today_str,
                "score": round(float(setup.get("composite_score") or 0), 1),
                "grade": setup.get("grade", "C"),
                "direction": setup.get("direction", "LONG"),
                "entry_price": setup.get("entry_price"),
                "stop_loss": setup.get("stop_loss"),
                "target_1": setup.get("target_1"),
                "risk_reward": setup.get("risk_reward")
            })
            
            # Keep last 30 days of data to limit file size
            history_data = history_data[-30:]
            with open(hist_path, "w", encoding="utf-8") as f:
                json.dump(history_data, f, indent=2, default=str)

    logger.info("-" * 55)
    logger.info("Scan complete!")
    logger.info("   Symbols scanned : %d", total)
    logger.info("   Skipped          : %d", skipped)
    logger.info("   Errors           : %d", errors)
    logger.info("   News enriched    : %d / %d symbols", news_fetched, total - skipped)
    logger.info("   Setups found     : %d  (A:%d B:%d C:%d)",
                len(all_setups), summary["grade_a"], summary["grade_b"], summary["grade_c"])
    logger.info("   Saved to         : %s", dated_path)
    logger.info("-" * 55)

    return top_picks, payload


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    _setup_console()
    parser = argparse.ArgumentParser(
        description="SwingSpectrum Nightly Batch Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--grade", choices=["A", "B", "C"], default=None,
        help="Sirf yeh grade ke setups filter karo (default: sab)"
    )
    parser.add_argument(
        "--top", type=int, default=15,
        help="Console par top N setups print karo (default: 15)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Custom output JSON file path"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Sirf errors dikhao (verbose logging band)"
    )
    parser.add_argument(
        "--telegram", action="store_true",
        help="Scan ke baad Telegram pe morning shortlist bhejo"
    )
    parser.add_argument(
        "--universe", type=str, default="nifty500",
        choices=["nifty500", "nifty200", "nifty100", "local"],
        help="Stock universe: nifty500 (default, ~500 stocks), nifty200, nifty100, or local (115 hardcoded)"
    )
    parser.add_argument(
        "--ignore-holiday", action="store_true",
        help="Bypass NSE weekend/holiday check and execute scan immediately"
    )
    args = parser.parse_args()

    if not args.ignore_holiday and not is_nse_trading_day():
        logger.info("Today (%s) is an NSE weekend or gazetted holiday. Skipping scan to conserve runner compute.", date.today().isoformat())
        sys.exit(0)

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    results, scan_payload = run_scan(
        grade_filter=args.grade,
        top_n=args.top,
        output_path=args.output,
        universe=args.universe,
    )
    _print_shortlist(results, top_n=args.top)

    # ── Telegram Alert ────────────────────────────────────────────────────────
    if args.telegram:
        try:
            from agents.alerts.telegram_bot import validate_bot, send_swing_shortlist_alert
            if validate_bot():
                sent = send_swing_shortlist_alert(
                    shortlist=results,
                    scan_meta=scan_payload,
                    max_setups=7,
                )
                if sent:
                    logger.info("Telegram morning shortlist sent!")
                else:
                    logger.warning("Telegram send returned False — check bot token / chat ID")
            else:
                logger.warning(
                    "Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
                )
        except Exception as exc:
            logger.error("Telegram alert failed: %s", exc)


if __name__ == "__main__":
    main()

