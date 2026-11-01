"""Heuristic intraday strategies on 5m bars (educational / paper framing)."""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from agents.intraday_engine.risk_manager import (
    calculate_position_size,
    calculate_risk_reward,
    calculate_sl_tp,
)
from agents.intraday_engine.types import IntradaySignal
from config import settings

logger = logging.getLogger(__name__)


def _atr(df: pd.DataFrame, period: int = 14) -> float | None:
    if df is None or df.empty or len(df) < 3:
        return None
    try:
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        prev_close = close.shift(1)

        tr = pd.concat(
            [
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr_series = tr.rolling(window=max(3, int(period)), min_periods=3).mean()
        atr = float(atr_series.iloc[-1])
        if atr <= 0 or pd.isna(atr):
            return None
        return atr
    except Exception as exc:
        logger.debug("intraday ATR calc failed: %s", exc)
        return None


def _risk_wrapped_signal(
    *,
    symbol: str,
    strategy: str,
    direction: str,
    score: float,
    entry: float,
    rationale: str,
    df: pd.DataFrame,
) -> Optional[IntradaySignal]:
    atr = _atr(df, period=getattr(settings, "ATR_PERIOD", 14))
    if atr is None:
        return None

    stop_loss, target = calculate_sl_tp(
        entry=float(entry), atr=atr, direction=direction
    )
    if stop_loss <= 0 or target <= 0:
        return None

    rr = calculate_risk_reward(
        entry=float(entry), stop_loss=float(stop_loss), target=float(target)
    )
    min_rr = float(getattr(settings, "INTRADAY_MIN_RISK_REWARD", 1.5) or 1.5)
    if rr < min_rr:
        return None

    capital = float(getattr(settings, "INTRADAY_VIRTUAL_CAPITAL", 100000.0) or 100000.0)
    risk_pct = (
        float(getattr(settings, "INTRADAY_RISK_PER_TRADE_PCT", 1.0) or 1.0) / 100.0
    )
    qty = calculate_position_size(
        capital=capital, entry=float(entry), atr=atr, risk_pct=risk_pct
    )
    if qty <= 0:
        return None

    rvol = None
    live_vwap = None
    if hasattr(df, "attrs"):
        if df.attrs.get("rvol") is not None:
            try:
                rvol = float(df.attrs["rvol"])
            except Exception:
                pass
        if df.attrs.get("live_vwap") is not None:
            try:
                live_vwap = float(df.attrs["live_vwap"])
            except Exception:
                pass

    if live_vwap is None and "VWAP" in df.columns:
        try:
            val = df["VWAP"].iloc[-1]
            if not pd.isna(val) and float(val) > 0:
                live_vwap = float(val)
        except Exception:
            pass

    if rvol is not None and getattr(settings, "INTRADAY_RVOL_GATE_ENABLED", True):
        min_rvol = float(getattr(settings, "INTRADAY_MIN_RVOL", 1.5) or 1.5)
        if strategy.upper() in ("ORB", "GAP_GO", "MOMENTUM") and rvol < min_rvol:
            logger.debug(
                "intraday %s %s signal filtered: RVOL %.2f < %.2f",
                symbol, strategy, rvol, min_rvol,
            )
            return None

    if rvol is not None and "RVOL=" not in rationale:
        rationale = f"{rationale.rstrip()} | RVOL={rvol:.2f}x"

    return IntradaySignal(
        symbol=symbol,
        strategy=strategy,
        direction=direction,
        score=float(score),
        entry=round(float(entry), 4),
        stop_loss=round(float(stop_loss), 4),
        target=round(float(target), 4),
        qty=int(qty),
        risk_reward=float(rr),
        atr=float(atr),
        rationale=rationale,
        rvol=rvol,
        vwap=live_vwap,
    )


def _day_session(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series | None]:
    if df is None or df.empty or "timestamp" not in df.columns:
        return df, None
    last_d = df.iloc[-1]["timestamp"].date()
    day = df[df["timestamp"].dt.date == last_d].copy()
    if hasattr(df, "attrs") and df.attrs:
        day.attrs = dict(df.attrs)
    if day.empty or len(day) < 8:
        return day, None
    return day, day.iloc[-1]


def _session_vwap_sigma(day: pd.DataFrame) -> tuple[float, float] | tuple[None, None]:
    if day is None or day.empty or len(day) < 5:
        return None, None
    try:
        high = pd.to_numeric(day["high"], errors="coerce")
        low = pd.to_numeric(day["low"], errors="coerce")
        close = pd.to_numeric(day["close"], errors="coerce")
        volume = pd.to_numeric(day["volume"], errors="coerce").clip(lower=0)

        tp = (high + low + close) / 3.0
        cum_vol = volume.cumsum().replace(0, pd.NA).ffill()
        if cum_vol.empty or pd.isna(cum_vol.iloc[-1]):
            return None, None

        vwap_series = (tp * volume).cumsum() / cum_vol
        calc_vwap = float(vwap_series.iloc[-1])
        ext_vwap = None
        if ext_vwap is None and hasattr(day, "attrs") and day.attrs.get("live_vwap"):
            try:
                ext_vwap = float(day.attrs["live_vwap"])
            except Exception:
                pass
        if ext_vwap is None and "VWAP" in day.columns:
            try:
                v_col = day["VWAP"].iloc[-1]
                if not pd.isna(v_col) and float(v_col) > 0:
                    ext_vwap = float(v_col)
            except Exception:
                pass
        vwap = ext_vwap if (ext_vwap is not None and ext_vwap > 0) else calc_vwap
        if vwap <= 0 or pd.isna(vwap):
            return None, None

        variance_series = ((tp - vwap_series) ** 2 * volume).cumsum() / cum_vol
        sigma = float(
            (variance_series.iloc[-1] ** 0.5)
            if not pd.isna(variance_series.iloc[-1])
            else 0.0
        )
        return vwap, max(sigma, 0.0)
    except Exception as exc:
        logger.debug("intraday VWAP sigma calc failed: %s", exc)
        return None, None


def _adaptive_supertrend_multiplier(day: pd.DataFrame, period: int = 10) -> float:
    base = float(getattr(settings, "INTRADAY_SUPERTREND_BASE_MULTIPLIER", 2.5) or 2.5)
    low_mult = float(
        getattr(settings, "INTRADAY_SUPERTREND_LOWVOL_MULTIPLIER", 2.0) or 2.0
    )
    high_mult = float(
        getattr(settings, "INTRADAY_SUPERTREND_HIGHVOL_MULTIPLIER", 3.0) or 3.0
    )
    low_thr = float(getattr(settings, "INTRADAY_SUPERTREND_LOWVOL_ATR_PCT", 0.6) or 0.6)
    high_thr = float(
        getattr(settings, "INTRADAY_SUPERTREND_HIGHVOL_ATR_PCT", 1.5) or 1.5
    )

    atr = _atr(day, period=period)
    if atr is None:
        return base

    c = float(pd.to_numeric(day["close"], errors="coerce").iloc[-1])
    if c <= 0:
        return base

    atr_pct = (atr / c) * 100.0
    if atr_pct >= high_thr:
        return high_mult
    if atr_pct <= low_thr:
        return low_mult
    return base


def strat_orb(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    day, last = _day_session(df)
    if last is None or len(day) < 8:
        return None
    orb = day.iloc[:6]
    hi, lo = float(orb["high"].max()), float(orb["low"].min())
    mid = (hi + lo) / 2
    vol_mean = float(orb["volume"].mean() or 1)
    last_vol = float(last["volume"] or 0)
    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )
    if c > hi and last_vol >= vol_mean * 1.05:
        range_risk = max(hi - lo, mid * 0.002)
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="ORB",
            direction="LONG",
            score=min(95.0, 55 + (c - hi) / max(range_risk, 1e-6) * 30),
            entry=c,
            rationale="Opening range breakout — close above OR high with volume confirmation.",
            df=day,
        )
    if c < lo and last_vol >= vol_mean * 1.05:
        range_risk = max(hi - lo, mid * 0.002)
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="ORB",
            direction="SHORT",
            score=min(95.0, 55 + (lo - c) / max(range_risk, 1e-6) * 30),
            entry=c,
            rationale="Opening range breakdown — close below OR low with volume confirmation.",
            df=day,
        )
    return None


def compute_vwap_bands(df: pd.DataFrame, live_vwap: float | None = None) -> dict | None:
    """Compute session VWAP and ±1/±2 standard-deviation bands."""
    if df is None or df.empty or len(df) < 5:
        return None
    try:
        close_col = "close" if "close" in df.columns else "Close"
        high_col = "high" if "high" in df.columns else "High"
        low_col = "low" if "low" in df.columns else "Low"
        vol_col = "volume" if "volume" in df.columns else "Volume"

        tp = (
            pd.to_numeric(df[high_col], errors="coerce")
            + pd.to_numeric(df[low_col], errors="coerce")
            + pd.to_numeric(df[close_col], errors="coerce")
        ) / 3.0
        vol = pd.to_numeric(df[vol_col], errors="coerce").fillna(0)
        cum_vol = vol.cumsum()
        total_vol = float(cum_vol.iloc[-1])
        if total_vol <= 0:
            return None
        cum_pv = (tp * vol).cumsum()
        vwap_series = cum_pv / cum_vol.replace(0, float("nan"))
        deviation = tp - vwap_series
        std_series = deviation.expanding(min_periods=2).std(ddof=0)
        calc_vwap = float(vwap_series.iloc[-1])
        ext_vwap = None
        if ext_vwap is None and hasattr(day, "attrs") and day.attrs.get("live_vwap"):
            try:
                ext_vwap = float(day.attrs["live_vwap"])
            except Exception:
                pass
        if ext_vwap is None and "VWAP" in day.columns:
            try:
                v_col = day["VWAP"].iloc[-1]
                if not pd.isna(v_col) and float(v_col) > 0:
                    ext_vwap = float(v_col)
            except Exception:
                pass
        vwap = ext_vwap if (ext_vwap is not None and ext_vwap > 0) else calc_vwap
        std = float(std_series.iloc[-1])
        if pd.isna(vwap) or pd.isna(std) or std < 0:
            return None
        return {
            "vwap": vwap,
            "upper_1": vwap + 1.0 * std,
            "upper_2": vwap + 2.0 * std,
            "lower_1": vwap - 1.0 * std,
            "lower_2": vwap - 2.0 * std,
        }
    except Exception as exc:
        logger.debug("compute_vwap_bands failed: %s", exc)
        return None


def strat_vwap(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    day, last = _day_session(df)
    if last is None or len(day) < 10:
        return None

    # Prefer richer band computation; fall back to legacy sigma helper
    bands = compute_vwap_bands(day)
    if bands is not None:
        vwap = bands["vwap"]
        upper_1 = bands["upper_1"]
        upper_2 = bands["upper_2"]
        lower_1 = bands["lower_1"]
        lower_2 = bands["lower_2"]
        std = upper_1 - vwap  # derived std for stretch calculations
    else:
        # Fallback: use legacy _session_vwap_sigma
        vwap, sigma = _session_vwap_sigma(day)
        if vwap is None:
            return None
        sigma_k = float(getattr(settings, "INTRADAY_VWAP_BAND_SIGMA", 1.0) or 1.0)
        std = max(float(sigma or 0.0), max(vwap, 1e-9) * 0.0005)
        upper_1 = vwap + sigma_k * std
        upper_2 = vwap + 2.0 * std
        lower_1 = vwap - sigma_k * std
        lower_2 = vwap - 2.0 * std

    std_eff = max(std, max(vwap, 1e-9) * 0.0005)

    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )
    prev_c = float(day["close"].iloc[-3]) if len(day) >= 3 else c
    recent = pd.to_numeric(day["close"].iloc[-8:-1], errors="coerce")
    recent_min = float(recent.min()) if not recent.empty else c
    recent_max = float(recent.max()) if not recent.empty else c

    # ── LONG signals (ascending conviction) ──────────────────────────────────
    # 1) Bounce off lower_2 band (highest conviction: +15 score)
    if recent_min <= lower_2 and c > lower_2 and c > prev_c:
        stretch = max(0.0, (vwap - recent_min) / max(std_eff, 1e-9))
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="LONG",
            score=min(90.0, 69 + stretch * 6),
            entry=c,
            rationale=(
                f"VWAP bounce off -2σ band (lower_2={lower_2:.2f}) — "
                f"price {c:.2f} reclaiming | vwap_band_hit=lower_2 | "
                f"stretch={stretch:.2f}σ."
            ),
            df=day,
        )
    # 2) Bounce from lower_1 back above VWAP
    if recent_min <= lower_1 and c > vwap * 1.001 and prev_c < vwap:
        stretch = max(0.0, (vwap - recent_min) / max(std_eff, 1e-9))
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="LONG",
            score=min(90.0, 59 + stretch * 7),
            entry=c,
            rationale=(
                f"VWAP reclaim after -1σ stretch (lower_1={lower_1:.2f}) — "
                f"crossed above VWAP={vwap:.2f} | vwap_band_hit=lower_1 | "
                f"stretch={stretch:.2f}σ."
            ),
            df=day,
        )
    # 3) Plain VWAP reclaim (lowest conviction)
    if c > vwap * 1.001 and prev_c < vwap and recent_min <= lower_1:
        stretch = max(0.0, (vwap - recent_min) / max(std_eff, 1e-9))
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="LONG",
            score=min(90.0, 54 + stretch * 8),
            entry=c,
            rationale=(
                f"VWAP reclaim after lower-band stretch — crossed above VWAP={vwap:.2f} | "
                f"vwap_band_hit=lower_1 | stretch={stretch:.2f}σ."
            ),
            df=day,
        )

    # ── SHORT signals (ascending conviction) ─────────────────────────────────
    # 1) Rejection at upper_2 band (highest conviction: +15 score)
    if recent_max >= upper_2 and c < upper_2 and c < prev_c:
        stretch = max(0.0, (recent_max - vwap) / max(std_eff, 1e-9))
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="SHORT",
            score=min(90.0, 69 + stretch * 6),
            entry=c,
            rationale=(
                f"VWAP rejection at +2σ band (upper_2={upper_2:.2f}) — "
                f"price {c:.2f} turning lower | vwap_band_hit=upper_2 | "
                f"stretch={stretch:.2f}σ."
            ),
            df=day,
        )
    # 2) Drop from upper_1 back below VWAP
    if recent_max >= upper_1 and c < vwap * 0.999 and prev_c > vwap:
        stretch = max(0.0, (recent_max - vwap) / max(std_eff, 1e-9))
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="SHORT",
            score=min(90.0, 59 + stretch * 7),
            entry=c,
            rationale=(
                f"VWAP breakdown after +1σ stretch (upper_1={upper_1:.2f}) — "
                f"crossed below VWAP={vwap:.2f} | vwap_band_hit=upper_1 | "
                f"stretch={stretch:.2f}σ."
            ),
            df=day,
        )
    # 3) Plain VWAP breakdown (lowest conviction)
    if c < vwap * 0.999 and prev_c > vwap and recent_max >= upper_1:
        stretch = max(0.0, (recent_max - vwap) / max(std_eff, 1e-9))
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="SHORT",
            score=min(90.0, 54 + stretch * 8),
            entry=c,
            rationale=(
                f"VWAP rejection after upper-band stretch — crossed below VWAP={vwap:.2f} | "
                f"vwap_band_hit=upper_1 | stretch={stretch:.2f}σ."
            ),
            df=day,
        )

    return None


def strat_gap_go(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    if df is None or len(df) < 30:
        return None
    last_d = df.iloc[-1]["timestamp"].date()
    prev = df[df["timestamp"].dt.date < last_d]
    if prev.empty:
        return None
    prev_close = float(prev.iloc[-1]["close"])
    day, last = _day_session(df)
    if last is None:
        return None
    open_ = float(day.iloc[0]["open"])
    gap_pct = (open_ - prev_close) / max(abs(prev_close), 1e-9) * 100.0
    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )
    if gap_pct > 0.6 and c > open_ * 1.002:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="GAP_GO",
            direction="LONG",
            score=min(90.0, 52 + min(gap_pct * 3, 25)),
            entry=c,
            rationale=f"Gap-and-go — gap up ~{gap_pct:.2f}%, price holding above open.",
            df=day,
        )
    if gap_pct < -0.6 and c < open_ * 0.998:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="GAP_GO",
            direction="SHORT",
            score=min(90.0, 52 + min(abs(gap_pct) * 3, 25)),
            entry=c,
            rationale=f"Gap-down continuation — gap ~{gap_pct:.2f}%, price weak vs open.",
            df=day,
        )
    return None


def strat_momentum(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    day, last = _day_session(df)
    if last is None or len(day) < 6:
        return None
    o = float(day.iloc[0]["open"])
    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )
    chg = (c - o) / max(abs(o), 1e-9) * 100.0
    vm = float(day["volume"].mean() or 1)
    lv = float(last["volume"] or 0)
    rel = lv / vm
    score = abs(float(chg)) * 100.0 * float(min(rel, 3.0))
    if chg > 0.35 and rel > 1.05:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="MOMENTUM",
            direction="LONG",
            score=float(min(92.0, 48 + abs(score))),
            entry=c,
            rationale=f"Session momentum — day ~{chg:.2f}% vs open, relative vol {rel:.2f}x.",
            df=day,
        )
    if chg < -0.35 and rel > 1.05:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="MOMENTUM",
            direction="SHORT",
            score=float(min(92.0, 48 + abs(score))),
            entry=c,
            rationale=f"Session weakness — day ~{chg:.2f}% vs open, relative vol {rel:.2f}x.",
            df=day,
        )
    return None


def strat_cpr(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """Central Pivot Range (CPR) — Indian traders' go-to intraday setup.

    Logic:
      - Compute yesterday's Pivot (P), BC (Bottom Central), TC (Top Central)
      - If today's 5m price closes above TC with volume → LONG (breakout above CPR)
      - If today's 5m price closes below BC with volume → SHORT (breakdown below CPR)
      - CPR Width matters: narrow CPR = trending day, wide CPR = sideways
    """
    if df is None or df.empty or "timestamp" not in df.columns:
        return None
    last_d = df.iloc[-1]["timestamp"].date()
    prev = df[df["timestamp"].dt.date < last_d]
    day, last = _day_session(df)
    if prev.empty or last is None or len(day) < 6:
        return None

    # Yesterday's OHLC for pivot calculation
    prev_high = float(prev["high"].max())
    prev_low = float(prev["low"].min())
    prev_close = float(prev.iloc[-1]["close"])

    pivot = (prev_high + prev_low + prev_close) / 3.0
    bc = (prev_high + prev_low) / 2.0
    tc = (pivot - bc) + pivot  # TC = (P - BC) + P

    # Ensure TC > BC
    if tc < bc:
        tc, bc = bc, tc

    cpr_width_pct = (tc - bc) / max(pivot, 1e-9) * 100.0
    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )
    vol_mean = float(day["volume"].mean() or 1)
    last_vol = float(last["volume"] or 0)

    # Narrow CPR = trending day (< 0.5% of price) → higher confidence
    trend_day_bonus = 10.0 if cpr_width_pct < 0.5 else 0.0

    if c > tc and last_vol >= vol_mean * 1.05:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="CPR",
            direction="LONG",
            score=min(92.0, 58 + trend_day_bonus + (c - tc) / max(tc, 1e-9) * 500),
            entry=c,
            rationale=(
                f"CPR breakout — price {c:.2f} above TC={tc:.2f} | "
                f"CPR width={cpr_width_pct:.2f}% | Pivot={pivot:.2f}"
            ),
            df=day,
        )
    if c < bc and last_vol >= vol_mean * 1.05:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="CPR",
            direction="SHORT",
            score=min(92.0, 58 + trend_day_bonus + (bc - c) / max(bc, 1e-9) * 500),
            entry=c,
            rationale=(
                f"CPR breakdown — price {c:.2f} below BC={bc:.2f} | "
                f"CPR width={cpr_width_pct:.2f}% | Pivot={pivot:.2f}"
            ),
            df=day,
        )
    return None


def strat_supertrend(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """Supertrend — ATR-band trend-following strategy (NSE intraday standard).

    Logic:
      - Compute Supertrend bands with ATR(10) * multiplier(2.5)
      - Detect flip: previous bar was in opposite band, current bar crossed over
      - LONG on flip from bearish→bullish band
      - SHORT on flip from bullish→bearish band
      - Requires minimum 20 bars in today's session for band stability
    """
    day, last = _day_session(df)
    if last is None or len(day) < 20:
        return None

    period = 10

    high = pd.to_numeric(day["high"], errors="coerce")
    low = pd.to_numeric(day["low"], errors="coerce")
    close = pd.to_numeric(day["close"], errors="coerce")
    prev_close = close.shift(1)

    # True Range
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr_series = tr.rolling(window=period, min_periods=period).mean()

    # Adaptive multiplier based on symbol volatility
    last_atr_val = atr_series.iloc[-1] if not atr_series.empty else None
    last_close_val = float(close.iloc[-1]) if not close.empty else 0
    if last_atr_val is not None and not pd.isna(last_atr_val) and last_close_val > 0:
        atr_pct = float(last_atr_val) / last_close_val * 100.0
        if atr_pct > 2.5:
            multiplier = 3.0  # high volatility
        elif atr_pct > 1.5:
            multiplier = 2.75  # medium volatility
        else:
            multiplier = 2.25  # low volatility
    else:
        multiplier = 2.5  # fallback

    atr = atr_series

    hl2 = (high + low) / 2.0
    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    # Compute Supertrend direction (1 = bullish, -1 = bearish)
    n = len(day)
    direction = pd.Series(index=day.index, dtype=float)
    final_upper = upper_band.copy()
    final_lower = lower_band.copy()

    for i in range(period, n):
        c = close.iloc[i]
        prev_c = close.iloc[i - 1]

        # Upper band logic
        fu_prev = final_upper.iloc[i - 1]
        fu_curr = upper_band.iloc[i]
        final_upper.iloc[i] = min(fu_curr, fu_prev) if prev_c <= fu_prev else fu_curr

        # Lower band logic
        fl_prev = final_lower.iloc[i - 1]
        fl_curr = lower_band.iloc[i]
        final_lower.iloc[i] = max(fl_curr, fl_prev) if prev_c >= fl_prev else fl_curr

        # Direction
        prev_dir = direction.iloc[i - 1] if i > period else 1.0
        if prev_dir == -1.0 and c > final_upper.iloc[i]:
            direction.iloc[i] = 1.0  # Flipped bullish
        elif prev_dir == 1.0 and c < final_lower.iloc[i]:
            direction.iloc[i] = -1.0  # Flipped bearish
        else:
            direction.iloc[i] = prev_dir

    if direction.dropna().empty or len(direction.dropna()) < 2:
        return None

    valid = direction.dropna()
    curr_dir = float(valid.iloc[-1])
    prev_dir = float(valid.iloc[-2])

    # Only fire on a fresh flip (not mid-trend)
    if curr_dir == prev_dir:
        return None

    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )

    if curr_dir == 1.0:  # Just flipped bullish
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="SUPERTREND",
            direction="LONG",
            score=min(90.0, 62.0),
            entry=c,
            rationale=(
                f"Supertrend flip BULLISH — price {c:.2f} crossed above upper band | "
                f"ATR({period})*{multiplier}"
            ),
            df=day,
        )
    if curr_dir == -1.0:  # Just flipped bearish
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="SUPERTREND",
            direction="SHORT",
            score=min(90.0, 62.0),
            entry=c,
            rationale=(
                f"Supertrend flip BEARISH — price {c:.2f} crossed below lower band | "
                f"ATR({period})*{multiplier}"
            ),
            df=day,
        )
    return None


STRATS = (
    strat_orb,
    strat_vwap,
    strat_gap_go,
    strat_momentum,
    strat_cpr,
    strat_supertrend,
)


def score_confluence(signals: list) -> list:
    """
    Boost scores when multiple strategies agree on direction for same symbol.
    1 strategy = grade C (no boost)
    2 strategies same direction = grade B, score += 10
    3+ strategies same direction = grade A, score += 20
    """
    if not signals:
        return []
    from collections import defaultdict

    direction_groups = defaultdict(list)
    for sig in signals:
        direction_groups[sig.direction.upper()].append(sig)

    dominant_dir = max(direction_groups, key=lambda d: len(direction_groups[d]))
    dominant_group = direction_groups[dominant_dir]
    count = len(dominant_group)

    if count >= 3:
        score_boost, grade = 20.0, "A"
    elif count == 2:
        score_boost, grade = 10.0, "B"
    else:
        score_boost, grade = 0.0, "C"

    agreeing_names = [s.strategy for s in dominant_group]

    for sig in dominant_group:
        sig.score = round(min(100.0, sig.score + score_boost), 2)
        sig.grade = grade
        sig.confluence_count = count
        sig.rationale = (
            sig.rationale.rstrip()
            + f" | confluence={grade} ({count} strats agree: {', '.join(agreeing_names)})"
        )

    for dir_, group in direction_groups.items():
        if dir_ == dominant_dir:
            continue
        for sig in group:
            sig.grade = "C"
            sig.confluence_count = len(group)
            sig.rationale = (
                sig.rationale.rstrip()
                + f" | confluence=C (minority: {len(group)} vs {count})"
            )

    return sorted(signals, key=lambda s: s.score, reverse=True)
