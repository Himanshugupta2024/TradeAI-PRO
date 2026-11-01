"""
Swing + Intraday Lightweight Server
=====================================
Sirf Swing aur Intraday tab ke liye — baaki sab band.

PC heat problem solve karne ke liye ye server banaya gaya hai.
Ye sirf zaruri APIs chalata hai:
  - /api/swing-scanner        (swing setups)
  - /api/swing/symbol/:sym    (per-symbol swing scan)
  - /api/swing/shortlist      (nightly shortlist)
  - /api/swing/history/:sym   (score sparkline)
  - /api/swing/scan-now       (manual rescan)
  - /api/intraday/latest      (intraday signals)
  - /api/intraday/scan        (intraday rescan)
  - /api/health               (health check)
  - Dashboard static files    (HTML/CSS/JS)

Ye band hain (CPU/heat bachane ke liye):
  - Live 5m NSE market data polling
  - Background signal refresh (60s loop)
  - Nightly scheduled swing scan
  - Breakout scanner
  - Index mover scanner
  - FII/DII trade flow
  - Telegram alerts
  - ML/AI ensemble
  - Paper trading engine signals

Chalane ka tarika:
    python swing_server.py
    python swing_server.py --port 8080
    python swing_server.py --host 0.0.0.0 --port 8000
    python swing_server.py --no-daily   # daily data fetch bhi skip karo
"""

import argparse
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime

# ── Project root ko Python path mein add karo ──────────────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# ── Logging setup ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("swing_server")

# ── FastAPI + CORS ──────────────────────────────────────────────────────────
from dataclasses import asdict

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agents.feature_engine.indicators import calculate_indicators

# ── Swing scanner core ───────────────────────────────────────────────────────
from agents.market_intelligence.nse_fetcher import (
    NSE_SYMBOLS,
    fetch_arbitrary_symbol,
    fetch_ohlcv,
)
from agents.market_intelligence.swing_scanner import (
    compute_sector_momentum_map,
    scan_swing_setups,
    swing_price_history_context,
)
from agents.market_intelligence.swing_scanner import (
    scan_all_symbols as swing_scan_all,
)

# ── Config ──────────────────────────────────────────────────────────────────
from config import settings

# ── DB init ─────────────────────────────────────────────────────────────────
from db.connection import init_db

# ── Intraday scanner ─────────────────────────────────────────────────────────
try:
    from agents.intraday_engine.scanner import scan_intraday_setups

    _intraday_available = True
except ImportError:
    scan_intraday_setups = None  # type: ignore
    _intraday_available = False
    logger.warning(
        "Intraday engine not found — /api/intraday/* will return empty results"
    )

# ── Swing persistence ────────────────────────────────────────────────────────
try:
    from db import intraday_store, swing_store

    _store_enabled = True
except Exception as e:
    swing_store = None  # type: ignore
    intraday_store = None  # type: ignore
    _store_enabled = False
    logger.warning("DB store unavailable: %s", e)

# ── Paper Trading Engine (lazy singleton) ────────────────────────────────────
try:
    from agents.execution.paper_trading import PaperTradingEngine as _PaperTradingEngine
    _paper_engine: "_PaperTradingEngine | None" = None  # Initialised in lifespan

    def _get_paper_engine() -> "_PaperTradingEngine | None":
        """Return the singleton paper engine (None if disabled)."""
        return _paper_engine

    _paper_trading_available = True
except ImportError as _pt_err:
    _paper_trading_available = False
    _paper_engine = None  # type: ignore
    def _get_paper_engine():
        return None
    logger.warning("PaperTradingEngine unavailable: %s", _pt_err)

# ── Dashboard static files ───────────────────────────────────────────────────
_DASHBOARD_DIR = os.path.join(_ROOT, "dashboard")

# ══════════════════════════════════════════════════════════════════════════════
# ── In-memory state (minimal) ─────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

_daily_cache: dict = {}  # symbol → pd.DataFrame (daily OHLCV)
_1h_cache: dict = {}     # symbol → pd.DataFrame (1H OHLCV for MTF confirmation)
_swing_cache: dict = {
    "setups": [],
    "total_found": 0,
    "symbols_scanned": 0,
    "real_data": False,
    "updated_at": None,
    "market_context": None,
}
_startup_done = False
_no_daily = False  # --no-daily flag se set hota hai


# ══════════════════════════════════════════════════════════════════════════════
# ── Helpers ────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════


def _normalize_setup(s: dict) -> dict:
    """
    Nightly scan ke short keys ko dashboard ke long keys mein convert karo.
    ranking_engine.rank_summary() → entry/sl/t1/rr/composite_score/entry_note
    dashboard expects → entry_zone/stop_loss/target_1/risk_reward/strength/description
    """
    out = dict(s)
    if "entry" in out and "entry_zone" not in out:
        out["entry_zone"] = out["entry"]
    if "sl" in out and "stop_loss" not in out:
        out["stop_loss"] = out["sl"]
    if "t1" in out and "target_1" not in out:
        out["target_1"] = out["t1"]
    if "t2" in out and "target_2" not in out:
        out["target_2"] = out["t2"]
    if "rr" in out and "risk_reward" not in out:
        out["risk_reward"] = out["rr"]
    if "strength" not in out or out["strength"] is None:
        out["strength"] = out.get("composite_score") or 0
    if not out.get("description"):
        out["description"] = out.get("entry_note") or ""
    if "fib_tp1" not in out:
        out["fib_tp1"] = 0
    if "fib_tp2" not in out:
        out["fib_tp2"] = 0
    if "target_2" not in out:
        out["target_2"] = 0
    if "indicators" not in out or out["indicators"] is None:
        out["indicators"] = {}
    return out


async def _load_daily_data_background():
    """
    Startup pe daily OHLCV fetch karo (swing scanner ke liye).
    Sirf TOP symbols — light load.
    """
    global _daily_cache, _swing_cache, _startup_done, _1h_cache

    if _no_daily:
        logger.info(
            "--no-daily flag set — daily fetch skip. Swing se shortlist load hogi."
        )
        # Shortlist se seed karo
        await _hydrate_from_shortlist()
        _startup_done = True
        return

    symbols = list(NSE_SYMBOLS.keys())
    limit = max(1, int(os.getenv("NSE_SYMBOL_LIMIT", "60")))
    symbols = symbols[:limit]

    logger.info("Daily data loading: %d symbols (background) ...", len(symbols))

    sem = asyncio.Semaphore(3)  # sirf 3 concurrent fetch — CPU/network light

    async def _fetch_one(sym: str):
        async with sem:
            try:
                df = await asyncio.wait_for(
                    asyncio.to_thread(fetch_ohlcv, sym, "1y", "1d"),
                    timeout=25.0,
                )
                if df is not None and not df.empty and len(df) >= 30:
                    df = await asyncio.to_thread(calculate_indicators, df)
                    _daily_cache[sym] = df
                    # Fetch 1H data for MTF confirmation (60d lookback, 20s timeout)
                    try:
                        import yfinance as _yf
                        _df1h = await asyncio.wait_for(
                            asyncio.to_thread(
                                lambda s=sym: _yf.download(
                                    s, period="60d", interval="1h",
                                    auto_adjust=True, progress=False,
                                ).rename(columns=str.lower),
                            ),
                            timeout=20.0,
                        )
                        if _df1h is not None and not _df1h.empty and len(_df1h) >= 20:
                            _1h_cache[sym] = _df1h
                    except Exception as _e1h:
                        logger.debug("1H fetch skipped for %s: %s", sym, _e1h)
            except asyncio.TimeoutError:
                logger.debug("Timeout fetching daily %s", sym)
            except Exception as e:
                logger.debug("Error fetching daily %s: %s", sym, e)

    await asyncio.gather(*[_fetch_one(s) for s in symbols])
    logger.info("Daily data loaded: %d symbols", len(_daily_cache))

    if _daily_cache:
        await _recompute_swing()

    _startup_done = True


async def _hydrate_from_shortlist():
    """
    Nightly shortlist JSON se swing cache seed karo (no daily fetch needed).
    """
    global _swing_cache
    import json

    path = os.path.join(_ROOT, "data", "swing_shortlist_latest.json")
    if not os.path.exists(path):
        logger.info("Shortlist JSON nahi mili — swing cache empty rahega")
        _swing_cache["real_data"] = True
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        setups = raw.get("shortlist") or raw.get("raw_setups") or []
        _swing_cache["setups"] = [_normalize_setup(s) for s in setups]
        _swing_cache["total_found"] = raw.get("setups_found", len(setups))
        _swing_cache["symbols_scanned"] = raw.get("symbols_scanned", 0)
        _swing_cache["updated_at"] = raw.get("scan_timestamp")
        _swing_cache["real_data"] = True
        logger.info("Shortlist se %d setups load kiye (no daily fetch)", len(setups))
    except Exception as e:
        logger.warning("Shortlist load failed: %s", e)
        _swing_cache["real_data"] = True


async def _recompute_swing():
    """Daily cache se swing setups compute karo aur cache update karo."""
    global _swing_cache
    if not _daily_cache:
        return
    try:
        ctx = _build_context()
        logger.info("Swing scan starting: %d symbols ...", len(_daily_cache))

        raw_setups = await asyncio.wait_for(
            asyncio.to_thread(
                swing_scan_all,
                dict(_daily_cache),
                market_context=ctx,
                df_1h_cache=dict(_1h_cache),
            ),
            timeout=120.0,
        )

        max_setups = max(
            1, int(getattr(settings, "SWING_MAX_SETUPS_RETURN", 100) or 100)
        )
        _swing_cache["setups"] = raw_setups[:max_setups]
        _swing_cache["total_found"] = len(raw_setups)
        _swing_cache["symbols_scanned"] = len(_daily_cache)
        _swing_cache["real_data"] = True
        _swing_cache["updated_at"] = datetime.now().isoformat()
        _swing_cache["market_context"] = ctx
        logger.info("Swing scan complete: %d setups found", len(raw_setups))

        # ── Paper Trade Auto-Capture ─────────────────────────────────────────
        # Grade A/B setups ko automatically paper journal mein log karo
        pe = _get_paper_engine()
        if pe is not None and raw_setups:
            max_open = int(getattr(settings, "PAPER_TRADING_MAX_OPEN", 5))
            current_open_syms = {t["symbol"] for t in pe.get_open_trades()}
            captured = 0
            for setup in raw_setups:
                # Only capture A/B grade — C is watchlist, too noisy
                grade = str(setup.get("grade", "C"))
                if grade not in ("A", "B"):
                    continue
                sym = str(setup.get("symbol", ""))
                if not sym or sym in current_open_syms:
                    continue  # Already open on this symbol
                if len(current_open_syms) >= max_open:
                    break
                entry  = float(setup.get("entry_zone") or setup.get("entry") or 0)
                sl     = float(setup.get("stop_loss") or setup.get("sl") or 0)
                target = float(setup.get("target_1") or setup.get("target") or 0)
                if not (entry > 0 and sl > 0 and target > 0):
                    continue
                regime_ctx = ctx.get("index_regime", "UNKNOWN") if ctx else "UNKNOWN"
                try:
                    trade = pe.execute(
                        symbol=sym,
                        direction=str(setup.get("direction", "LONG")),
                        entry=entry,
                        sl=sl,
                        tp=target,
                        signal_type=str(setup.get("pattern", "SwingSetup")),
                        regime=regime_ctx,
                        confidence=float(setup.get("strength", 0)) / 100.0,
                    )
                    if trade:
                        current_open_syms.add(sym)
                        captured += 1
                        logger.info(
                            "[PaperTrade] Captured %s %s @ %.2f (grade=%s)",
                            setup.get("direction", "?"), sym, entry, grade,
                        )
                except Exception as _pe_err:
                    logger.debug("Paper capture error for %s: %s", sym, _pe_err)
            if captured:
                logger.info("[PaperTrade] %d new paper trades captured this scan", captured)

    except asyncio.TimeoutError:
        logger.warning("Swing scan timed out — keeping previous results")
        _swing_cache["real_data"] = True
        _swing_cache["updated_at"] = datetime.now().isoformat()
    except Exception as e:
        logger.error("Swing scan error: %s", e)
        _swing_cache["real_data"] = True


# ── Regime Cache (in-memory, refreshed every 4h) ─────────────────────────────
_regime_cache: dict = {
    "index_symbol": "NIFTY 50",
    "index_regime": "UNKNOWN",
    "index_trend": "NEUTRAL",
    "adx": 0.0,
    "ema20": 0.0,
    "ema50": 0.0,
    "ema200": 0.0,
    "close": 0.0,
    "atr": 0.0,
    "atr_20avg": 0.0,
    "vol_regime": "NORMAL",     # HIGH_VOL / LOW_VOL / NORMAL
    "regime_note": "Not yet computed",
    "computed_at": None,
    "fetch_ok": False,
}
_regime_lock = asyncio.Lock()


def _compute_regime_from_df(df) -> dict:
    """
    Nifty 50 ke daily OHLCV DataFrame se regime classify karo.

    Logic:
      ADX > 25 + close > EMA50 > EMA200  → TRENDING_BULLISH
      ADX > 25 + close < EMA50 < EMA200  → TRENDING_BEARISH
      ADX > 25 (but EMAs mixed)           → MIXED_TRENDING
      ADX 20-25                           → MIXED
      ADX < 20                            → SIDEWAYS

    Vol regime:
      current ATR > 1.5x 20-bar ATR avg  → HIGH_VOL
      current ATR < 0.7x 20-bar ATR avg  → LOW_VOL
      else                                → NORMAL
    """
    import numpy as _np
    import pandas as _pd

    result = {
        "adx": 0.0, "ema20": 0.0, "ema50": 0.0, "ema200": 0.0,
        "close": 0.0, "atr": 0.0, "atr_20avg": 0.0,
        "index_regime": "UNKNOWN", "index_trend": "NEUTRAL",
        "vol_regime": "NORMAL", "regime_note": "",
    }
    if df is None or df.empty or len(df) < 30:
        result["regime_note"] = "Insufficient data (< 30 bars)"
        return result

    def _ema_s(s, p):
        return s.ewm(span=p, adjust=False).mean()

    def _atr_s(h, l, c, p=14):
        prev_c = c.shift(1)
        tr = _pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        return tr.ewm(span=p, adjust=False).mean()

    def _adx_s(h, l, c, p=14):
        pdm = h.diff().clip(lower=0)
        ndm = (-l.diff()).clip(lower=0)
        pdm = _pd.Series(_np.where((pdm > ndm) & (pdm > 0), pdm, 0.0), index=h.index)
        ndm = _pd.Series(_np.where((ndm > pdm) & (ndm > 0), ndm, 0.0), index=l.index)
        atr_v = _atr_s(h, l, c, p)
        pdi = 100 * _ema_s(pdm, p) / atr_v.replace(0, _np.nan)
        ndi = 100 * _ema_s(ndm, p) / atr_v.replace(0, _np.nan)
        dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, _np.nan)
        return _ema_s(dx, p)

    try:
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)

        ema20  = float(_ema_s(c, 20).iloc[-1])
        ema50  = float(_ema_s(c, 50).iloc[-1])
        ema200 = float(_ema_s(c, 200).iloc[-1]) if len(df) >= 200 else float(_ema_s(c, len(df)).iloc[-1])
        adx    = float(_adx_s(h, l, c, 14).iloc[-1])
        atr_s  = _atr_s(h, l, c, 14)
        atr    = float(atr_s.iloc[-1])
        atr_20 = float(atr_s.iloc[-20:].mean()) if len(atr_s) >= 20 else atr
        close  = float(c.iloc[-1])

        result.update({
            "adx": round(adx, 1), "ema20": round(ema20, 1),
            "ema50": round(ema50, 1), "ema200": round(ema200, 1),
            "close": round(close, 1), "atr": round(atr, 1),
            "atr_20avg": round(atr_20, 1),
        })

        # ── Vol Regime ────────────────────────────────────────────────────────
        vol_thresh_high = float(getattr(settings, "REGIME_HIGH_VOL_ATR_RATIO", 1.5))
        vol_thresh_low  = float(getattr(settings, "REGIME_LOW_VOL_ATR_RATIO",  0.7))
        if atr_20 > 0:
            atr_ratio = atr / atr_20
            if atr_ratio >= vol_thresh_high:
                vol_regime = "HIGH_VOL"
            elif atr_ratio <= vol_thresh_low:
                vol_regime = "LOW_VOL"
            else:
                vol_regime = "NORMAL"
        else:
            vol_regime = "NORMAL"
        result["vol_regime"] = vol_regime

        # ── Trend Regime ──────────────────────────────────────────────────────
        adx_trend_floor    = float(getattr(settings, "REGIME_ADX_TRENDING_FLOOR",   25.0))
        adx_mixed_floor    = float(getattr(settings, "REGIME_ADX_MIXED_FLOOR",      20.0))

        bullish_ema = close > ema50 and ema50 > ema200
        bearish_ema = close < ema50 and ema50 < ema200

        if adx >= adx_trend_floor:
            if bullish_ema:
                regime     = "TRENDING_BULLISH"
                trend      = "BULLISH"
                note       = f"ADX={adx:.1f} (strong) + Price > EMA50 > EMA200 → Bullish trend"
            elif bearish_ema:
                regime     = "TRENDING_BEARISH"
                trend      = "BEARISH"
                note       = f"ADX={adx:.1f} (strong) + Price < EMA50 < EMA200 → Bearish trend"
            else:
                regime     = "MIXED_TRENDING"
                trend      = "NEUTRAL"
                note       = f"ADX={adx:.1f} (strong) but EMAs mixed — transitional"
        elif adx >= adx_mixed_floor:
            regime     = "MIXED"
            trend      = "NEUTRAL"
            note       = f"ADX={adx:.1f} (weak trend) — mixed signals"
        else:
            regime     = "SIDEWAYS"
            trend      = "NEUTRAL"
            note       = f"ADX={adx:.1f} (below 20) — sideways / choppy market"

        # Extra: HIGH_VOL override note
        if vol_regime == "HIGH_VOL":
            note += f" | ⚠️ HIGH VOLATILITY (ATR {atr:.1f} vs avg {atr_20:.1f})"

        result["index_regime"] = regime
        result["index_trend"]  = trend
        result["regime_note"]  = note

    except Exception as _e:
        result["regime_note"] = f"Compute error: {_e}"

    return result


async def _refresh_regime_cache() -> None:
    """
    Nifty 50 (^NSEI) ka data fetch karo aur regime cache update karo.
    Agar fetch fail ho → UNKNOWN rahega lekin pichla valid result preserve hoga.
    """
    global _regime_cache
    async with _regime_lock:
        ix      = getattr(settings, "SWING_INDEX_SYMBOL", "NIFTY 50") or "NIFTY 50"
        ticker  = getattr(settings, "NSE_TICKER_MAP", {}).get(ix, "^NSEI")
        logger.info("Regime fetch: %s (%s)", ix, ticker)
        try:
            import yfinance as _yf

            def _fetch_nsei():
                _df = _yf.download(
                    ticker, period="1y", interval="1d",
                    auto_adjust=True, progress=False,
                )
                # yFinance v0.2+ returns MultiIndex columns: (Close, ^NSEI), (High, ^NSEI)...
                # Flatten to single-level and lowercase
                if isinstance(_df.columns, __import__("pandas").MultiIndex):
                    _df.columns = [col[0].lower() for col in _df.columns]
                else:
                    _df = _df.rename(columns=str.lower)
                return _df

            df_raw = await asyncio.wait_for(
                asyncio.to_thread(_fetch_nsei),
                timeout=30.0,
            )
            if df_raw is None or df_raw.empty or len(df_raw) < 30:
                logger.warning("Regime fetch: empty data for %s", ticker)
                _regime_cache["regime_note"] = f"Empty data from yFinance for {ticker}"
                _regime_cache["computed_at"] = datetime.now().isoformat()
                return

            computed = _compute_regime_from_df(df_raw)
            _regime_cache.update(computed)
            _regime_cache["index_symbol"] = ix
            _regime_cache["fetch_ok"]     = True
            _regime_cache["computed_at"]  = datetime.now().isoformat()
            logger.info(
                "Regime updated: %s | trend=%s | ADX=%.1f | Vol=%s",
                computed["index_regime"],
                computed["index_trend"],
                computed["adx"],
                computed["vol_regime"],
            )

        except asyncio.TimeoutError:
            logger.warning("Regime fetch timed out (30s)")
            _regime_cache["regime_note"] = "Fetch timed out — keeping last result"
            _regime_cache["computed_at"] = datetime.now().isoformat()
        except Exception as _e:
            logger.error("Regime fetch error: %s", _e)
            _regime_cache["regime_note"] = f"Fetch error: {_e}"
            _regime_cache["computed_at"] = datetime.now().isoformat()


def _build_context() -> dict:
    """
    Dynamic market context from regime cache.
    Nifty 50 ka ADX + EMA analysis use karta hai — UNKNOWN nahi dega.
    """
    ix = getattr(settings, "SWING_INDEX_SYMBOL", "NIFTY 50") or "NIFTY 50"
    return {
        "index_symbol": _regime_cache.get("index_symbol", ix),
        "index_regime": _regime_cache.get("index_regime", "UNKNOWN"),
        "index_trend":  _regime_cache.get("index_trend",  "NEUTRAL"),
        "vol_regime":   _regime_cache.get("vol_regime",   "NORMAL"),
        "regime_note":  _regime_cache.get("regime_note",  ""),
        "adx":          _regime_cache.get("adx",          0.0),
        "computed_at":  _regime_cache.get("computed_at",  None),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── FastAPI app + lifespan ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════


async def _regime_refresh_loop() -> None:
    """Background loop: har 4 ghante mein regime cache refresh karo."""
    regime_interval_hours = float(
        getattr(settings, "REGIME_REFRESH_INTERVAL_HOURS", 4.0) or 4.0
    )
    interval_sec = int(regime_interval_hours * 3600)
    while True:
        try:
            await _refresh_regime_cache()
        except Exception as _e:
            logger.error("Regime refresh loop error: %s", _e)
        await asyncio.sleep(interval_sec)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup: DB init + daily data + regime fetch (lightweight)."""
    logger.info("=" * 55)
    logger.info("  Swing + Intraday Lightweight Server starting...")
    logger.info("  (Signal scan, nightly loop, ML/AI — sab BAND hain)")
    logger.info("=" * 55)

    # DB init (non-blocking)
    try:
        await asyncio.to_thread(init_db)
        logger.info("DB initialized")
    except Exception as e:
        logger.warning("DB init failed (non-fatal): %s", e)

    # Shortlist se immediate seed (fast)
    await _hydrate_from_shortlist()

    # ── Regime fetch at startup (background — don't block server ready) ─────
    asyncio.create_task(_refresh_regime_cache())
    logger.info("Market regime fetch queued (background)...")

    # ── Daily data background mein load karo ────────────────────────────────
    asyncio.create_task(_load_daily_data_background())

    # ── Periodic regime refresh (every 4h) ──────────────────────────────────
    asyncio.create_task(_regime_refresh_loop())

    # ── Paper Trading Engine init ────────────────────────────────────────────
    global _paper_engine
    if _paper_trading_available and getattr(settings, "PAPER_TRADING_ENABLED", True):
        try:
            capital = float(getattr(settings, "PAPER_TRADING_CAPITAL", 500_000.0))
            _paper_engine = _PaperTradingEngine(initial_capital=capital)
            logger.info(
                "Paper Trading Engine ready (capital=%.0f, PAPER_TRADING_ENABLED=True)",
                capital,
            )
        except Exception as _pe_err:
            logger.warning("Paper engine init failed: %s", _pe_err)
            _paper_engine = None
    else:
        logger.info("Paper Trading disabled (PAPER_TRADING_ENABLED=False)")

    logger.info(
        "Server ready at http://localhost:8000  (data loading background mein...)"
    )
    yield
    logger.info("Server shut down.")


app = FastAPI(
    title="Swing + Intraday Server (Lightweight)",
    version="1.0.0",
    lifespan=_lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════════════════════
# ── Health ─────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "mode": "swing+intraday-only",
        "startup_done": _startup_done,
        "daily_symbols_loaded": len(_daily_cache),
        "swing_setups": _swing_cache.get("total_found", 0),
        "swing_real_data": _swing_cache.get("real_data", False),
        "updated_at": _swing_cache.get("updated_at"),
        "no_daily_mode": _no_daily,
        # Regime summary in health
        "market_regime": _regime_cache.get("index_regime", "UNKNOWN"),
        "market_trend":  _regime_cache.get("index_trend",  "NEUTRAL"),
        "vol_regime":    _regime_cache.get("vol_regime",   "NORMAL"),
        "regime_adx":    _regime_cache.get("adx", 0.0),
        "regime_ok":     _regime_cache.get("fetch_ok", False),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/regime")
async def get_market_regime():
    """
    Live market regime (Nifty 50 analysis).

    Returns:
      index_regime: TRENDING_BULLISH | TRENDING_BEARISH | SIDEWAYS | MIXED | MIXED_TRENDING | UNKNOWN
      index_trend:  BULLISH | BEARISH | NEUTRAL
      vol_regime:   HIGH_VOL | LOW_VOL | NORMAL
      adx:          Current ADX value
      regime_note:  Human-readable explanation
      signal_impact: What this means for swing signals
    """
    regime  = _regime_cache.get("index_regime", "UNKNOWN")
    trend   = _regime_cache.get("index_trend",  "NEUTRAL")
    vol     = _regime_cache.get("vol_regime",   "NORMAL")
    adx     = _regime_cache.get("adx", 0.0)

    # Signal impact explanation for dashboard
    impact_map = {
        "TRENDING_BULLISH":  "✅ LONG setups allowed. SHORT setups blocked. Breakout patterns preferred.",
        "TRENDING_BEARISH":  "🔴 SHORT setups allowed. LONG setups BLOCKED. Protect capital.",
        "SIDEWAYS":          "⚠️ Both LONG and SHORT BLOCKED. Range-bound — wait for breakout.",
        "MIXED":             "🟡 Signals shown but confidence reduced. Smaller position sizes recommended.",
        "MIXED_TRENDING":    "🟡 Trending but direction unclear. Only high-conviction (Grade A) setups.",
        "UNKNOWN":           "⏳ Regime data not yet available. Signals shown without gate.",
    }

    vol_impact_map = {
        "HIGH_VOL": "⚠️ High volatility — SL should be wider. Reduce position size.",
        "LOW_VOL":  "📉 Low volatility — tight SL possible. Watch for breakout.",
        "NORMAL":   "✅ Normal volatility — standard sizing applies.",
    }

    return {
        "index_symbol":  _regime_cache.get("index_symbol", "NIFTY 50"),
        "index_regime":  regime,
        "index_trend":   trend,
        "vol_regime":    vol,
        "adx":           adx,
        "ema20":         _regime_cache.get("ema20",     0.0),
        "ema50":         _regime_cache.get("ema50",     0.0),
        "ema200":        _regime_cache.get("ema200",    0.0),
        "close":         _regime_cache.get("close",     0.0),
        "atr":           _regime_cache.get("atr",       0.0),
        "atr_20avg":     _regime_cache.get("atr_20avg", 0.0),
        "regime_note":   _regime_cache.get("regime_note", ""),
        "signal_impact": impact_map.get(regime, ""),
        "vol_impact":    vol_impact_map.get(vol, ""),
        "fetch_ok":      _regime_cache.get("fetch_ok", False),
        "computed_at":   _regime_cache.get("computed_at", None),
        "updated_at":    datetime.now().isoformat(),
    }


@app.post("/api/regime/refresh")
async def regime_refresh_now():
    """Manual regime cache refresh trigger karo (dashboard se)."""
    asyncio.create_task(_refresh_regime_cache())
    return {
        "ok": True,
        "message": "Regime refresh queued (5-10 sec mein complete hoga).",
        "current_regime": _regime_cache.get("index_regime", "UNKNOWN"),
    }



# ══════════════════════════════════════════════════════════════════════════════
# ── Swing Scanner APIs ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/swing-scanner")
async def get_swing_setups():
    """Main swing scanner — dashboard ke liye."""
    if not _swing_cache.get("real_data"):
        return {
            "setups": [],
            "total_found": 0,
            "symbols_scanned": 0,
            "real_data": False,
            "updated_at": datetime.now().isoformat(),
            "market_context": None,
            "message": "Data load ho raha hai... thoda wait karo (30-60 sec).",
        }

    normalized = [_normalize_setup(s) for s in _swing_cache.get("setups", [])]
    return {
        "setups": normalized,
        "total_found": _swing_cache.get("total_found", 0),
        "symbols_scanned": _swing_cache.get("symbols_scanned", 0),
        "real_data": True,
        "updated_at": _swing_cache.get("updated_at"),
        "market_context": _swing_cache.get("market_context"),
    }


@app.post("/api/swing/scan-now")
async def swing_scan_now():
    """Manual swing rescan trigger."""
    if not _daily_cache:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "message": "Daily data abhi load nahi hua. Thoda wait karo.",
            },
        )
    asyncio.create_task(_recompute_swing())
    return {
        "ok": True,
        "message": "Swing rescan shuru ho gaya (background mein).",
        "symbols": len(_daily_cache),
    }


@app.get("/api/swing/shortlist")
async def get_swing_shortlist():
    """Nightly shortlist JSON se setups return karo."""
    import json

    path = os.path.join(_ROOT, "data", "swing_shortlist_latest.json")
    if not os.path.exists(path):
        return {
            "setups": [],
            "message": "Shortlist nahi bani abhi tak. Run: python scripts/nightly_swing_scan.py",
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        setups = raw.get("shortlist") or raw.get("raw_setups") or []
        return {
            "setups": setups,
            "scan_date": raw.get("scan_date"),
            "generated_at": raw.get("scan_timestamp"),
            "symbols_scanned": raw.get("symbols_scanned", 0),
            "setups_found": raw.get("setups_found", len(setups)),
            "grade_summary": raw.get("grade_summary", {}),
            "message": f"{len(setups)} setups from {raw.get('scan_date', 'unknown')}",
        }
    except Exception as e:
        logger.error("Shortlist load error: %s", e)
        return {"setups": [], "error": str(e)}


@app.get("/api/swing/history/{symbol}")
async def get_swing_history(symbol: str):
    """Per-symbol swing score history (sparkline ke liye)."""
    import json

    sym = symbol.upper()
    path = os.path.join(_ROOT, "data", "swing_history", f"{sym}.json")
    if not os.path.exists(path):
        return {"history": [], "symbol": sym}
    try:
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
        return {"history": history, "symbol": sym}
    except Exception as e:
        return {"history": [], "symbol": sym, "error": str(e)}


@app.get("/api/swing/history")
async def swing_scan_history(limit: int = Query(30, ge=1, le=100)):
    """Recent scan rows from DB."""
    if not _store_enabled or swing_store is None:
        return {"scans": [], "updated_at": datetime.now().isoformat()}
    try:
        rows = await asyncio.to_thread(swing_store.list_recent_scans, limit)
        return {"scans": rows, "updated_at": datetime.now().isoformat()}
    except Exception as e:
        return {"scans": [], "error": str(e)}


@app.get("/api/swing/stats")
async def swing_outcome_stats(days: int = Query(30, ge=7, le=365)):
    """Hit-rate stats from DB."""
    if not _store_enabled or swing_store is None:
        return {"stats": {}, "updated_at": datetime.now().isoformat()}
    try:
        stats = await asyncio.to_thread(swing_store.compute_stats, int(days))
        return {"stats": stats, "updated_at": datetime.now().isoformat()}
    except Exception as e:
        return {"stats": {}, "error": str(e)}


@app.get("/api/swing/symbol/{symbol}")
async def get_swing_for_symbol(symbol: str):
    """
    Ek specific symbol ke liye swing scan karo.
    Daily cache se ya fresh fetch karke.
    """
    raw = symbol.upper().replace(".NS", "").strip()
    if not raw or len(raw) > 25:
        return JSONResponse(status_code=400, content={"error": "Invalid symbol"})

    # Cache mein hai?
    df = _daily_cache.get(raw)

    # Cache mein nahi — fresh fetch karo
    if df is None or df.empty:
        logger.info("Fresh daily fetch for symbol: %s", raw)
        try:
            df_fetched = await asyncio.wait_for(
                asyncio.to_thread(fetch_arbitrary_symbol, f"{raw}.NS", "1y", "1d"),
                timeout=35.0,
            )
            if df_fetched is None or df_fetched.empty or len(df_fetched) < 30:
                df_fetched = await asyncio.wait_for(
                    asyncio.to_thread(fetch_arbitrary_symbol, raw, "1y", "1d"),
                    timeout=35.0,
                )
            if (
                df_fetched is not None
                and not df_fetched.empty
                and len(df_fetched) >= 30
            ):
                df = await asyncio.to_thread(calculate_indicators, df_fetched)
                _daily_cache[raw] = df
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"error": "Fetch timed out", "symbol": raw, "setups": []},
            )
        except Exception as e:
            return JSONResponse(
                status_code=500, content={"error": str(e), "symbol": raw, "setups": []}
            )

    if df is None or df.empty:
        return JSONResponse(
            status_code=404,
            content={
                "error": f"{raw} ke liye data nahi mila",
                "symbol": raw,
                "setups": [],
            },
        )

    # Scan karo
    try:
        ctx = _build_context()
        sector = getattr(settings, "NSE_SECTOR_MAP", {}).get(raw, "Other")
        momentum = 0.0
        try:
            if _daily_cache:
                sector_map = await asyncio.to_thread(
                    compute_sector_momentum_map, dict(_daily_cache)
                )
                momentum = sector_map.get(sector, 0.0)
        except Exception:
            pass

        stages: dict = {}

        def _run_scan():
            news_ctx = None
            try:
                from agents.data_ingestion.context_fetcher import build_context_fetcher

                _cf = build_context_fetcher()
                snap = _cf.get_snapshot_sync(raw)
                news_ctx = {
                    "context_score": float(snap.context_score),
                    "news_sentiment_24hr": float(snap.news_sentiment_24hr),
                    "news_sentiment_7d": float(snap.news_sentiment_7d),
                    "weekly_trend_bias": str(snap.weekly_trend_bias),
                    "news_volume_spike": bool(snap.news_volume_spike),
                    "event_risk_active": bool(snap.event_risk_active),
                }
            except Exception:
                pass
            return scan_swing_setups(
                df,
                raw,
                sector_momentum=momentum,
                sector=sector,
                market_context=ctx,
                stages_out=stages,
                allow_fallback=True,
                news_context=news_ctx,
            )

        setups_raw = await asyncio.wait_for(
            asyncio.to_thread(_run_scan),
            timeout=90.0,
        )
        setups = [asdict(s) for s in setups_raw]

        # Tech summary (even if no setup)
        tech_summary = None
        try:
            last = df.iloc[-1]
            import pandas as pd_inner

            def _safe(v):
                return (
                    None
                    if v is None or (hasattr(v, "__float__") and pd_inner.isna(v))
                    else float(v)
                )

            _close = float(last["close"])
            tech_summary = {
                "close": round(_close, 2),
                "rsi": round(float(last["RSI_14"]), 1)
                if _safe(last.get("RSI_14"))
                else None,
                "ema20": round(float(last["EMA_20"]), 2)
                if _safe(last.get("EMA_20"))
                else None,
                "ema50": round(float(last["EMA_50"]), 2)
                if _safe(last.get("EMA_50"))
                else None,
                "atr": round(float(last["ATR_14"]), 2)
                if _safe(last.get("ATR_14"))
                else None,
                "trend": "UPTREND"
                if (
                    _safe(last.get("EMA_20"))
                    and _safe(last.get("EMA_50"))
                    and float(last["EMA_20"]) > float(last["EMA_50"])
                )
                else "DOWNTREND",
                "bars": len(df),
            }
            tech_summary.update(swing_price_history_context(df, _close))
        except Exception:
            pass

        msg = None
        if not setups:
            raw_n = stages.get("raw_pattern_count", 0)
            after_rr = stages.get("after_rr_count", 0)
            after_reg = stages.get("after_regime_count", 0)
            blocked = stages.get("blocked")
            if blocked:
                msg = f"Data issue: {blocked}"
            elif raw_n == 0:
                msg = f"{raw} ke liye koi pattern match nahi hua. Chart check karo."
            elif after_rr == 0:
                msg = f"Pattern mila lekin R:R gate fail hua (minimum 1.5x chahiye)."
            elif after_reg == 0:
                msg = f"Pattern aur R:R OK, lekin regime gate ne block kiya (trend conflict)."
            else:
                msg = f"{raw} ke liye abhi koi actionable setup nahi hai."

        return {
            "symbol": raw,
            "setups": setups,
            "total_found": len(setups),
            "real_data": True,
            "updated_at": datetime.now().isoformat(),
            "market_context": ctx,
            "tech_summary": tech_summary,
            "swing_scan": stages,
            "message": msg,
            "active_symbols": [],
        }

    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=504,
            content={"error": "Scan timed out", "symbol": raw, "setups": []},
        )
    except Exception as e:
        logger.error("Symbol scan error %s: %s", raw, e)
        return JSONResponse(
            status_code=500, content={"error": str(e), "symbol": raw, "setups": []}
        )


# ══════════════════════════════════════════════════════════════════════════════
# ── Intraday APIs ──────────────────────────────────────────────────════════════
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/intraday/latest")
async def intraday_latest():
    """Last saved intraday scan return karo."""
    if not _store_enabled or intraday_store is None:
        return {"signals": [], "scan_time": None, "message": "DB unavailable"}
    try:
        row = await asyncio.to_thread(intraday_store.load_latest_scan)
        if not row:
            return {
                "signals": [],
                "scan_time": None,
                "message": "Abhi tak koi scan nahi hua",
            }
        return {
            "signals": row.get("setups", []),
            "scan_time": row.get("scan_time"),
            "scan_id": row.get("id"),
        }
    except Exception as e:
        logger.error("Intraday latest error: %s", e)
        return {"signals": [], "error": str(e)}


@app.post("/api/intraday/scan")
async def intraday_scan():
    """Fresh intraday scan chalao (5m delayed yFinance)."""
    if not _intraday_available or scan_intraday_setups is None:
        return JSONResponse(
            status_code=501,
            content={"ok": False, "message": "Intraday engine available nahi hai"},
        )

    # Tracked symbols
    all_syms = list(NSE_SYMBOLS.keys())
    limit = max(1, int(os.getenv("NSE_SYMBOL_LIMIT", "60")))
    symbols = all_syms[:limit]

    try:
        logger.info("Intraday scan: %d symbols ...", len(symbols))
        sigs = await asyncio.wait_for(
            asyncio.to_thread(scan_intraday_setups, symbols),
            timeout=300.0,
        )
        sigs_list = []
        for s in sigs or []:
            if hasattr(s, "__dataclass_fields__"):
                sigs_list.append(asdict(s))
            elif isinstance(s, dict):
                sigs_list.append(s)
            else:
                sigs_list.append(vars(s) if hasattr(s, "__dict__") else {})

        # DB mein save karo
        scan_id = None
        if _store_enabled and intraday_store is not None:
            try:
                scan_id = await asyncio.to_thread(
                    intraday_store.save_scan,
                    sigs_list,
                    symbols_scanned=len(symbols),
                    trigger="manual",
                )
            except Exception as e:
                logger.warning("Intraday save error: %s", e)

        return {
            "ok": True,
            "signals": sigs_list,
            "count": len(sigs_list),
            "scan_id": scan_id,
            "updated_at": datetime.now().isoformat(),
        }

    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=504,
            content={"ok": False, "message": "Intraday scan timed out (> 5 min)"},
        )
    except Exception as e:
        logger.error("Intraday scan error: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ══════════════════════════════════════════════════════════════════════════════
# ── Symbol search (swing ke liye zaruri) ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/search/symbols")
async def search_symbols_api(q: str = Query("", min_length=1)):
    """Symbol search (NSE symbols mein se)."""
    q_upper = q.upper().strip()
    results = []
    for sym, info in NSE_SYMBOLS.items():
        name = info if isinstance(info, str) else str(info)
        if q_upper in sym or q_upper in name.upper():
            results.append(
                {
                    "symbol": sym,
                    "ticker": f"{sym}.NS",
                    "type": "NSE",
                    "tracked": sym in _daily_cache,
                }
            )
        if len(results) >= 15:
            break
    return {"results": results, "query": q}


# ══════════════════════════════════════════════════════════════════════════════
# ── Paper Trading API ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/paper/scorecard")
async def paper_scorecard():
    """
    Live paper trading scorecard.
    Shows win rate, trade count, and unlock status.
    Dashboard pe validation badge ke liye use karo.
    """
    pe = _get_paper_engine()
    if pe is None:
        return {
            "enabled": False,
            "message": "Paper trading disabled (PAPER_TRADING_ENABLED=false)",
        }

    stats = pe.get_stats()
    total  = stats.get("total_trades", 0)
    wins   = stats.get("winning_trades", 0)
    losses = stats.get("losing_trades", 0)
    wr     = stats.get("win_rate", 0.0)

    min_trades = int(getattr(settings, "PAPER_TRADING_UNLOCK_MIN_TRADES", 30))
    min_wr     = float(getattr(settings, "PAPER_TRADING_UNLOCK_MIN_WR_PCT", 50.0))

    validated = total >= min_trades and wr >= min_wr
    trades_needed = max(0, min_trades - total)

    if validated:
        unlock_msg = "VALIDATED — System ready for real trading"
        unlock_level = "green"
    elif total == 0:
        unlock_msg = f"Need {min_trades} paper trades (0 done)"
        unlock_level = "grey"
    elif wr < min_wr and total >= min_trades:
        unlock_msg = f"Win rate {wr:.1f}% < {min_wr:.0f}% threshold — keep paper trading"
        unlock_level = "red"
    else:
        unlock_msg = f"Need {trades_needed} more trades (current WR: {wr:.1f}%)"
        unlock_level = "orange"

    return {
        "enabled": True,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "open_trades": len(pe.get_open_trades()),
        "win_rate_pct": wr,
        "avg_rr": round(stats.get("profit_factor", 0), 2),
        "capital_virtual": round(stats.get("capital", 0), 2),
        "total_pnl_virtual": round(stats.get("total_pnl", 0), 2),
        "validated": validated,
        "unlock_message": unlock_msg,
        "unlock_level": unlock_level,
        "unlock_min_trades": min_trades,
        "unlock_min_wr_pct": min_wr,
        "updated_at": datetime.now().isoformat(),
    }


@app.get("/api/paper/trades")
async def paper_trades(limit: int = Query(50, ge=1, le=200)):
    """Open + recent closed paper trades."""
    pe = _get_paper_engine()
    if pe is None:
        return {"enabled": False, "open": [], "history": []}
    return {
        "enabled": True,
        "open": pe.get_open_trades(),
        "history": pe.get_history(limit=limit),
        "stats": pe.get_stats(),
        "updated_at": datetime.now().isoformat(),
    }


@app.post("/api/paper/resolve")
async def paper_resolve_eod():
    """
    EOD outcome check — yFinance se actual prices fetch karo
    aur open paper trades ke SL/Target check karo.
    Raat ko ya next day subah call karo.
    """
    pe = _get_paper_engine()
    if pe is None:
        return JSONResponse(status_code=400, content={"ok": False, "message": "Paper trading disabled"})

    open_trades = pe.get_open_trades()
    if not open_trades:
        return {"ok": True, "message": "No open paper trades to resolve", "resolved": 0}

    import yfinance as yf

    resolved = 0
    price_map: dict[str, dict] = {}

    # Fetch today's OHLC for all open symbols
    symbols_to_fetch = list({t["symbol"] for t in open_trades})
    for sym in symbols_to_fetch:
        try:
            ticker = f"{sym}.NS" if not sym.endswith(".NS") else sym
            df = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda t=ticker: yf.download(t, period="5d", interval="1d",
                                                  auto_adjust=True, progress=False)
                ),
                timeout=15.0,
            )
            if df is not None and not df.empty:
                if isinstance(df.columns, __import__("pandas").MultiIndex):
                    df.columns = [c[0].lower() for c in df.columns]
                else:
                    df = df.rename(columns=str.lower)
                last = df.iloc[-1]
                price_map[sym] = {
                    "high":  float(last.get("high", 0)),
                    "low":   float(last.get("low", 0)),
                    "close": float(last.get("close", 0)),
                }
        except Exception as _e:
            logger.debug("EOD price fetch failed for %s: %s", sym, _e)

    if price_map:
        closed = pe.update_prices(price_map)
        resolved = len(closed)
        logger.info("[PaperTrade] EOD resolve: %d trades closed", resolved)

    return {
        "ok": True,
        "resolved": resolved,
        "remaining_open": len(pe.get_open_trades()),
        "scorecard": (await paper_scorecard()),
        "updated_at": datetime.now().isoformat(),
    }



_DISABLED_MSG = {
    "error": "Yeh feature is lightweight server mein band hai",
    "disabled": True,
}


@app.get("/api/signals/all")
async def signals_disabled():
    return JSONResponse(
        status_code=200,
        content={
            "signals": [],
            "updated_at": datetime.now().isoformat(),
            "message": "Signal scan band hai (lightweight mode)",
        },
    )


@app.get("/api/stats")
async def stats_disabled():
    return {"capital": 100000, "total_pnl": 0, "total_trades": 0, "win_rate": 0}


@app.get("/api/nse/dashboard")
async def nse_dashboard_disabled():
    return {
        "mood": {"mood": "NEUTRAL", "score": 50},
        "movers": {"gainers": [], "losers": []},
        "ai_summary": {
            "lines": ["Lightweight mode — sirf Swing + Intraday active hai"]
        },
    }


@app.get("/api/overview/intelligence")
async def cockpit_disabled():
    return JSONResponse(
        status_code=200,
        content={
            "error": "Band hai",
            "top_setups": [],
            "market_guardrails": {},
            "system_state": {},
        },
    )


@app.get("/api/screener/opportunities")
async def screener_disabled():
    return {"opportunities": []}


@app.get("/api/morning-scan")
async def morning_scan_disabled():
    return {"results": []}


@app.get("/api/crypto/intelligence")
async def crypto_disabled():
    return {"error": "Band hai", "top_setups": []}


@app.get("/api/nse/prediction")
async def nse_prediction_disabled():
    return {
        "prediction": {"direction": "NEUTRAL", "confidence": 0},
        "signals": {},
        "news": {},
    }


@app.get("/api/context/market-mood")
async def market_mood_disabled():
    return {"crypto_mood": "NEUTRAL", "btc_24hr_change": 0, "nifty_24hr_change": 0}


@app.get("/api/portfolio")
async def portfolio_disabled():
    return {
        "open_positions": [],
        "total_open": 0,
        "total_unrealized_pnl": 0,
        "concentration_warnings": [],
        "risk_metrics": {},
        "drawdown": {},
        "pnl_attribution": {},
    }


@app.get("/api/trades")
async def trades_disabled():
    return {"trades": []}


@app.get("/api/performance")
async def performance_disabled():
    return {
        "metrics": {},
        "equity_curve": [],
        "trade_distribution": {},
        "exit_reasons": [],
        "monthly_pnl": [],
        "symbol_breakdown": [],
        "recent_trades": [],
    }


@app.get("/api/tradeflow")
async def tradeflow_disabled():
    return {"flows": [], "sentiment": "UNAVAILABLE", "real_data": False, "date": None}


@app.get("/api/sectors")
async def sectors_disabled():
    return {"sectors": []}


@app.get("/api/screener/gainers")
async def gainers_disabled():
    return {"data": []}


@app.get("/api/screener/losers")
async def losers_disabled():
    return {"data": []}


@app.get("/api/context/events")
async def events_disabled():
    return {"events": []}


@app.get("/api/analytics/scorecard")
async def scorecard_disabled():
    return {
        "total_signals": 0,
        "overall_win_rate": 0,
        "by_regime": {},
        "by_quality_grade": {},
        "by_hour": {},
        "by_confidence": {},
        "false_signal_reasons": {},
        "by_symbol": {},
    }


@app.get("/api/learning/stats")
async def learning_disabled():
    return {"insights": {}, "health": {}, "weights": {}}


@app.get("/api/learning/outcomes")
async def outcomes_disabled():
    return {"outcomes": []}


@app.get("/api/forex/signals")
async def forex_signals_disabled():
    return {"setups": []}


@app.get("/api/forex/rates")
async def forex_rates_disabled():
    return {"rates": {}}


@app.get("/api/forex/sessions")
async def forex_sessions_disabled():
    return {"sessions": {}}


@app.get("/api/index-mover/{index}")
async def index_mover_disabled(index: str):
    return {"error": "Band hai", "top_gainers": [], "top_draggers": [], "movers": []}


@app.get("/api/breakouts")
async def breakouts_disabled():
    return {
        "all": [],
        "total_breakouts": 0,
        "grade_a_count": 0,
        "grade_b_count": 0,
        "week_52_high": [],
        "week_52_low": [],
    }


@app.post("/api/refresh")
async def refresh_disabled():
    return {"ok": True, "message": "Lightweight mode mein manual refresh nahi hota"}


# WebSocket stub (dashboard connect karta hai — error nahi dena chahiye)
from fastapi import WebSocket, WebSocketDisconnect


@app.websocket("/ws/prices")
async def ws_prices_stub(websocket: WebSocket):
    await websocket.accept()
    # Ek dummy message bhejo, phir close karo
    try:
        import json as _json

        await websocket.send_text(
            _json.dumps(
                {
                    "prices": {},
                    "timestamp": datetime.now().isoformat(),
                    "stats": {
                        "capital": 100000,
                        "total_pnl": 0,
                        "total_trades": 0,
                        "win_rate": 0,
                    },
                }
            )
        )
        # Client disconnect hone tak wait karo
        while True:
            await asyncio.sleep(30)
            await websocket.send_text(_json.dumps({"ping": True}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# ── Favicon + Static dashboard serve karo ─────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    ico = os.path.join(_DASHBOARD_DIR, "favicon.ico")
    if os.path.exists(ico):
        return FileResponse(ico)
    return JSONResponse(status_code=204, content={})


# Dashboard HTML/CSS/JS static files
if os.path.exists(_DASHBOARD_DIR):
    app.mount("/static", StaticFiles(directory=_DASHBOARD_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_dashboard():
        idx = os.path.join(_DASHBOARD_DIR, "index.html")
        if os.path.exists(idx):
            with open(idx, "r", encoding="utf-8") as f:
                return HTMLResponse(f.read())
        return HTMLResponse(
            "<h2>Dashboard files nahi mile. trading/dashboard/ folder check karo.</h2>"
        )
else:

    @app.get("/", include_in_schema=False)
    async def no_dashboard():
        return HTMLResponse("<h2>Dashboard folder nahi mili: trading/dashboard/</h2>")


# FileResponse import (favicon ke liye)
from fastapi.responses import FileResponse

# ══════════════════════════════════════════════════════════════════════════════
# ── Entry point ────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════


def _parse_args():
    p = argparse.ArgumentParser(description="Swing + Intraday Lightweight Server")
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    p.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    p.add_argument(
        "--no-daily",
        action="store_true",
        help="Daily yFinance fetch skip karo — sirf nightly shortlist load hogi (fastest startup)",
    )
    p.add_argument(
        "--symbols",
        type=int,
        default=60,
        help="Kitne symbols scan karein (default: 60)",
    )
    return p.parse_args()


if __name__ == "__main__":
    import uvicorn

    args = _parse_args()
    _no_daily = args.no_daily

    # Symbol limit env var se set karo
    os.environ["NSE_SYMBOL_LIMIT"] = str(args.symbols)

    logger.info("━" * 55)
    logger.info("  MODE    : Swing + Intraday only")
    logger.info("  HOST    : http://%s:%d", args.host, args.port)
    logger.info("  SYMBOLS : %d", args.symbols)
    logger.info("  NO-DAILY: %s", _no_daily)
    logger.info("━" * 55)
    logger.info("")
    logger.info("  Tabs jo kaam karenge:")
    logger.info("    ✅  Swing Scanner")
    logger.info("    ✅  Intraday")
    logger.info("")
    logger.info("  Tabs jo BAND hain (heat save):")
    logger.info("    ❌  Overview / Signal cards")
    logger.info("    ❌  Portfolio / Performance")
    logger.info("    ❌  Market / TradeFlow / NSE Prediction")
    logger.info("    ❌  All background polling loops")
    logger.info("")
    logger.info("  Dashboard: http://%s:%d", args.host, args.port)
    logger.info("  Agar Swing tab khali hai — 30-60 sec wait karo")
    logger.info("  Ya shortlist ke liye --no-daily use karo (instant)")
    logger.info("")

    uvicorn.run(
        "swing_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        workers=1,  # single worker — minimum CPU
        loop="asyncio",
    )
