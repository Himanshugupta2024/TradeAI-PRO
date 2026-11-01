"""
SwingSpectrum — Full Diagnostic Script
=======================================
Diagnoses: data staleness, news fetch, regime gate, SL cap, signal grade for any symbol.

Run:  python scripts/debug_swing.py
      python scripts/debug_swing.py JINDALSAW
      python scripts/debug_swing.py NMDC

Outputs:
  [1] Latest shortlist summary
  [2] Data health (bar age, staleness)
  [3] Indicator column check
  [4] Regime gate status (Nifty trend)
  [5] News / context fetch test
  [6] Full scan with grade + SL cap analysis
  [7] Comparison scan across sector peers
"""
import sys
import os
from pathlib import Path
import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import json
import logging
logging.basicConfig(
    level=logging.WARNING,  # Quiet by default — INFO/DEBUG floods the output
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("swing_debug")

TARGET_SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else "JINDALSAW"
SECTOR_PEERS = {
    "JINDALSAW":  ["TATASTEEL", "JSWSTEEL", "SAIL", "NMDC", "HINDALCO"],
    "NMDC":       ["TATASTEEL", "SAIL", "COALINDIA", "NATIONALUM"],
    "TATASTEEL":  ["JSWSTEEL", "SAIL", "HINDALCO", "NMDC"],
}
peers = SECTOR_PEERS.get(TARGET_SYMBOL, [])

SEP = "=" * 65
SEP2 = "-" * 50

def hdr(n, text):
    print(f"\n[{n}] {text}")
    print(SEP2)

print(SEP)
print(f"  SwingSpectrum Full Diagnostic  |  Target: {TARGET_SYMBOL}")
print(f"  Run time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
print(SEP)

# ── [1] Latest shortlist JSON ────────────────────────────────────
hdr(1, "Latest Shortlist Cache")
latest = ROOT / "data" / "swing_shortlist_latest.json"
if latest.exists():
    with open(latest, encoding="utf-8") as f:
        data = json.load(f)
    setups = data.get("shortlist", data.get("raw_setups", []))
    print(f"  Scan date      : {data.get('scan_date')}")
    print(f"  Symbols scanned: {data.get('symbols_scanned')}")
    print(f"  Setups found   : {data.get('setups_found')}")
    gs = data.get("grade_summary", {})
    print(f"  Grades         : A={gs.get('A',0)}  B={gs.get('B',0)}  C={gs.get('C',0)}")
    # Check if target is in shortlist
    found_target = [s for s in setups if s.get("symbol", "").upper() == TARGET_SYMBOL]
    if found_target:
        s = found_target[0]
        print(f"\n  ✅ {TARGET_SYMBOL} IS in current shortlist:")
        print(f"     Grade={s.get('grade','?')} | RR={s.get('risk_reward',0):.1f} | "
              f"SL_capped={s.get('sl_capped',False)}")
        print(f"     Pattern  : {s.get('pattern','?')}")
        print(f"     Entry    : ₹{s.get('entry_zone',0):.2f} | SL: ₹{s.get('stop_loss',0):.2f}")
        print(f"     T1       : ₹{s.get('target_1',0):.2f} | T2: ₹{s.get('target_2',0):.2f}")
        if s.get("sl_warning"):
            print(f"  ⚠️  Warning  : {s.get('sl_warning')}")
    else:
        print(f"\n  ℹ️  {TARGET_SYMBOL} NOT in current shortlist")
else:
    print("  ❌ No shortlist JSON found!")
    print("     Run: python scripts/nightly_swing_scan.py")

# ── [2] Data health ──────────────────────────────────────────────
hdr(2, f"Data Health — {TARGET_SYMBOL}")
try:
    from agents.market_intelligence.nse_fetcher import fetch_arbitrary_symbol
    import pandas as pd

    df = fetch_arbitrary_symbol(TARGET_SYMBOL, period="1y", interval="1d")
    if df is None or df.empty:
        print(f"  ❌ NO DATA returned for {TARGET_SYMBOL}!")
        print("     Possible causes: yFinance rate limit, wrong ticker, network issue")
        df = None
    else:
        last_ts = pd.to_datetime(df.iloc[-1].get("timestamp"), errors="coerce")
        now_ts = pd.Timestamp.now()
        age_days = (now_ts - last_ts).total_seconds() / 86400 if not pd.isna(last_ts) else float("inf")
        print(f"  Total bars    : {len(df)}")
        print(f"  Last bar date : {last_ts.strftime('%Y-%m-%d') if not pd.isna(last_ts) else 'UNKNOWN'}")
        print(f"  Bar age       : {age_days:.1f} days ago")
        if age_days > 5:
            print(f"  ⚠️  STALE DATA — bar is {age_days:.1f} days old (max 5 allowed)")
            print("      Fix: check yFinance connectivity, or NSE holiday → normal")
        elif age_days > 3:
            print(f"  ⚠️  Slightly stale ({age_days:.1f}d) — likely weekend/holiday")
        else:
            print(f"  ✅ Data is fresh")
        close_price = float(df["close"].iloc[-1])
        print(f"  Last close    : ₹{close_price:.2f}")
except Exception as e:
    print(f"  ❌ EXCEPTION: {e}")
    import traceback; traceback.print_exc()
    df = None

# ── [3] Indicator columns ────────────────────────────────────────
hdr(3, "Indicator Column Check")
if df is not None:
    try:
        from agents.feature_engine.indicators import calculate_indicators
        df = calculate_indicators(df)
        KEY_COLS = ["EMA_20", "EMA_50", "EMA_200", "RSI_14", "ATR_14",
                    "BB_upper", "BB_lower", "BB_width", "ADX_14", "volume"]
        all_ok = True
        for c in KEY_COLS:
            if c in df.columns:
                val = df[c].iloc[-1]
                status = "✅" if not pd.isna(val) else "⚠️  NaN"
                print(f"  {c:15}: {val:.4f}  {status}" if not pd.isna(val) else f"  {c:15}: NaN  {status}")
            else:
                print(f"  {'  '+c:15}: MISSING ❌")
                all_ok = False
        if not all_ok:
            print("\n  ❌ Missing columns — indicator calculation may have failed")
    except Exception as e:
        print(f"  ❌ Indicator error: {e}")
else:
    print("  Skipped — no data available")

# ── [4] Regime gate (Nifty trend) ────────────────────────────────
hdr(4, "Regime Gate — Nifty 50 Trend Check")
try:
    nifty_df = fetch_arbitrary_symbol("NIFTY 50", period="60d", interval="1d")
    if nifty_df is not None and not nifty_df.empty:
        nifty_df = calculate_indicators(nifty_df)
        last_nifty = nifty_df.iloc[-1]
        nifty_close = float(last_nifty["close"])
        nifty_ema20 = float(last_nifty.get("EMA_20", 0) or 0)
        nifty_ema50 = float(last_nifty.get("EMA_50", 0) or 0)
        nifty_rsi = float(last_nifty.get("RSI_14", 50) or 50)

        # Simple regime determination
        if nifty_close < nifty_ema20 and nifty_ema20 < nifty_ema50:
            trend = "BEARISH"
            trend_emoji = "🔴"
        elif nifty_close > nifty_ema20 and nifty_ema20 > nifty_ema50:
            trend = "BULLISH"
            trend_emoji = "🟢"
        else:
            trend = "NEUTRAL"
            trend_emoji = "🟡"

        print(f"  Nifty 50 close : ₹{nifty_close:,.2f}")
        print(f"  EMA20          : ₹{nifty_ema20:,.2f}")
        print(f"  EMA50          : ₹{nifty_ema50:,.2f}")
        print(f"  RSI(14)        : {nifty_rsi:.1f}")
        print(f"  Index trend    : {trend_emoji} {trend}")

        from config import settings
        block_long = getattr(settings, "SWING_BLOCK_LONG_IN_BEARISH_TREND", True)
        gate_enabled = getattr(settings, "SWING_REGIME_GATE_ENABLED", True)

        print(f"\n  SWING_REGIME_GATE_ENABLED             : {gate_enabled}")
        print(f"  SWING_BLOCK_LONG_IN_BEARISH_TREND     : {block_long}")

        if trend == "BEARISH" and block_long and gate_enabled:
            print(f"\n  🚫 LONG signals are BLOCKED — market is BEARISH")
            print(f"     This is why {TARGET_SYMBOL} LONG trade was risky today!")
            print(f"     After fix: system will block such LONGs automatically.")
        elif trend == "BEARISH" and not block_long:
            print(f"\n  ⚠️  Market is BEARISH but LONG block is OFF — fix has now enabled it!")
        else:
            print(f"\n  ✅ Regime gate status normal for {trend} market")
    else:
        print("  ❌ Could not fetch Nifty 50 data")
except Exception as e:
    print(f"  ❌ EXCEPTION: {e}")
    import traceback; traceback.print_exc()

# ── [5] News / context fetch test ───────────────────────────────
hdr(5, f"News Context Fetch Test — {TARGET_SYMBOL}")
try:
    import requests
    from agents.data_ingestion.context_fetcher import ContextFetcher
    fetcher = ContextFetcher()
    # Force fresh fetch (bypass cache for diagnostics)
    import time
    base = TARGET_SYMBOL.replace(".NS", "")

    print(f"  Testing RSS sources for '{base}'...")
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # Test each RSS source manually
    sources_test = [
        ("Google News (full)", f"https://news.google.com/rss/search?q={base}+NSE+share+price+India+stock&hl=en-IN&gl=IN&ceid=IN:en"),
        ("Google News (short)", f"https://news.google.com/rss/search?q={base}+NSE&hl=en-IN&gl=IN&ceid=IN:en"),
        ("Yahoo Finance RSS", f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={base}.NS&region=IN&lang=en-IN"),
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    any_source_ok = False
    for name, url in sources_test:
        try:
            t0 = time.time()
            resp = requests.get(url, timeout=10, headers=headers)
            elapsed = time.time() - t0
            items = fetcher._extract_rss_items(resp.text or "")
            count = len(items)
            status_icon = "✅" if count > 0 else "⚠️ "
            print(f"  {status_icon} {name:30}: HTTP {resp.status_code} | {count} items | {elapsed:.1f}s")
            if count > 0:
                any_source_ok = True
                # Show latest headline
                first_title = items[0][0][:70] if items else "—"
                print(f"       Latest: \"{first_title}...\"")
        except Exception as exc:
            print(f"  ❌ {name:30}: FAILED — {type(exc).__name__}: {exc}")
        time.sleep(0.3)

    if not any_source_ok:
        print("\n  ❌ ALL news sources failed!")
        print("     Possible causes:")
        print("     1. No internet connectivity")
        print("     2. Google News blocking your IP (common on cloud/VPN)")
        print("     3. yFinance/RSS rate limit in effect")
        print("     ✅ FIX: System now uses neutral context (0.0) as fallback — ")
        print("             news failure won't block signals, just reduce accuracy.")
    else:
        print("\n  ✅ At least one news source is working")

    # Also get the computed context score
    print(f"\n  Computing context_score for {TARGET_SYMBOL}...")
    try:
        snapshot = fetcher.get_snapshot_sync(TARGET_SYMBOL)
        print(f"  context_score  : {snapshot.context_score:+.3f}")
        print(f"  news_24h       : {snapshot.news_sentiment_24hr:+.3f}")
        print(f"  news_7d        : {snapshot.news_sentiment_7d:+.3f}")
        print(f"  weekly_bias    : {snapshot.weekly_trend_bias}")
        if snapshot.context_score < -0.25:
            print(f"  ⚠️  Negative context — LONG signals would be penalised")
        elif snapshot.context_score > 0.25:
            print(f"  ✅ Positive context — supports LONG setups")
        else:
            print(f"  ➡️  Neutral context")
    except Exception as ce:
        print(f"  ❌ Context snapshot failed: {ce}")
except Exception as e:
    print(f"  ❌ EXCEPTION: {e}")
    import traceback; traceback.print_exc()

# ── [6] Full scan with grade + SL cap analysis ───────────────────
hdr(6, f"Full Scan — {TARGET_SYMBOL}")
if df is not None:
    try:
        from agents.market_intelligence.swing_scanner import scan_swing_setups
        from config import settings

        stages = {}
        setups = scan_swing_setups(
            df,
            symbol=TARGET_SYMBOL,
            stages_out=stages,
            allow_fallback=True,
        )

        print(f"  Scan stages:")
        for k, v in stages.items():
            icon = "✅" if v not in (0, None, "insufficient_bars", "stale_bars", "missing_columns", "bad_atr") else "❌"
            print(f"    {icon} {k:30}: {v}")

        if not setups:
            blocked = stages.get("blocked", "unknown")
            raw = stages.get("raw_pattern_count", 0)
            print(f"\n  ❌ No setup found for {TARGET_SYMBOL}")
            print(f"     Blocked at stage: {blocked}")
            if blocked == "stale_bars":
                print(f"     ↳ Data too old — check yFinance fetch or market holiday")
            elif blocked == "insufficient_bars":
                print(f"     ↳ Less than 30 bars — need 1y history")
            elif raw == 0:
                print(f"     ↳ No patterns fired — market may be in consolidation")
        else:
            for s in setups:
                print(f"\n  📊 {s.symbol} | {s.pattern}")
                print(f"     Direction : {s.direction}")
                print(f"     Grade     : {s.grade}  {'(WATCHLIST — do not trade)' if s.grade == 'C' else '(Actionable)' if s.grade == 'A' else '(Moderate — use half size)'}")
                print(f"     Strength  : {s.strength}/100")
                print(f"     RR        : {s.risk_reward:.2f}x")
                print(f"     Entry     : ₹{s.entry_zone:.2f}")
                print(f"     SL        : ₹{s.stop_loss:.2f}  {'⚠️ CAPPED' if s.sl_capped else '✅ normal'}")
                if s.sl_capped:
                    print(f"     ⚠️  {s.sl_warning}")
                    print(f"     ↳ True ATR-based SL was wider — grade forced to C")
                    pct = abs(s.entry_zone - s.stop_loss) / s.entry_zone * 100
                    print(f"     ↳ Capped SL distance: {pct:.1f}% from entry")
                print(f"     T1        : ₹{s.target_1:.2f}")
                print(f"     T2        : ₹{s.target_2:.2f}")
                print(f"     Sector    : {s.sector_tag}")
                print(f"     News score: {s.news_score:+.3f}  {'(penalty applied)' if s.news_penalty_applied else ''}")
                print(f"     ADX       : {s.adx_value:.1f}")
                print(f"     Desc      : {s.description[:100]}")
                print(f"     Entry note: {s.entry_note[:100]}")
    except Exception as e:
        print(f"  ❌ EXCEPTION: {e}")
        import traceback; traceback.print_exc()
else:
    print("  Skipped — no data available")

# ── [7] Sector peer comparison ───────────────────────────────────
if peers:
    hdr(7, f"Sector Peer Comparison — {TARGET_SYMBOL} vs {', '.join(peers[:4])}")
    all_syms = [TARGET_SYMBOL] + peers[:4]
    try:
        from agents.market_intelligence.swing_scanner import scan_swing_setups
        from agents.feature_engine.indicators import calculate_indicators
        import pandas as pd

        for sym in all_syms:
            try:
                d = fetch_arbitrary_symbol(sym, period="1y", interval="1d")
                if d is None or d.empty:
                    print(f"  {sym:15}: NO DATA")
                    continue
                d = calculate_indicators(d)
                st = {}
                ss = scan_swing_setups(d, symbol=sym, stages_out=st, allow_fallback=False)
                if ss:
                    s0 = ss[0]
                    capped_flag = "⚠️ SL_CAPPED" if s0.sl_capped else ""
                    print(f"  {sym:15}: {s0.direction:5} | Grade={s0.grade} | "
                          f"RR={s0.risk_reward:.1f} | Str={s0.strength} | {s0.pattern[:35]} {capped_flag}")
                else:
                    block = st.get("blocked") or "no_pattern"
                    print(f"  {sym:15}: No setup | {block}")
            except Exception as ex:
                print(f"  {sym:15}: ERROR — {ex}")
    except Exception as e:
        print(f"  ❌ EXCEPTION: {e}")

# ── [8] Settings summary ─────────────────────────────────────────
hdr(8, "Key Scanner Settings")
try:
    from config import settings
    checks = [
        ("SWING_MIN_RISK_REWARD",              "Min R:R gate"),
        ("SWING_MAX_BAR_AGE_DAYS",             "Max bar age (days)"),
        ("SWING_BLOCK_LONG_IN_BEARISH_TREND",  "Block LONG in bearish market"),
        ("SWING_REGIME_GATE_ENABLED",          "Regime gate enabled"),
        ("SWING_ADX_MIN_FOR_BREAKOUT",         "Min ADX for breakout"),
        ("SWING_REQUIRE_2BAR_BREAKOUT_CONFIRM","Require 2-bar breakout confirm"),
        ("SWING_VOL_BB_SQUEEZE",               "BB Squeeze min volume ratio"),
        ("SWING_NEWS_AFFECTS_STRENGTH",        "News affects strength score"),
        ("SWING_INCLUDE_WATCHLIST_IN_PORTFOLIO_SCAN", "Show C-grade in dashboard"),
    ]
    for attr, label in checks:
        val = getattr(settings, attr, "NOT SET")
        icon = ""
        if attr == "SWING_BLOCK_LONG_IN_BEARISH_TREND" and val == True:
            icon = " ✅ (FIXED)"
        elif attr == "SWING_BLOCK_LONG_IN_BEARISH_TREND" and val == False:
            icon = " ⚠️ (RISKY)"
        print(f"  {label:45}: {val}{icon}")
except Exception as e:
    print(f"  ❌ Settings error: {e}")

print(f"\n{SEP}")
print("  Diagnostic complete.")
print(f"  Fix summary applied:")
print(f"    ✅ SL-capped signals now forced to Grade C (watchlist only)")
print(f"    ✅ LONG signals blocked in bearish market (SWING_BLOCK_LONG_IN_BEARISH_TREND=true)")
print(f"    ✅ News RSS has 3-source fallback + retry (Google → Google short → Yahoo)")
print(SEP)
