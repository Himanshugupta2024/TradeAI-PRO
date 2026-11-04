"""
Multi-Factor Institutional Confluence & Ranking Model for Intraday Opportunities.

Builds a deterministic 0-100 Confluence Score based on transparent, verifiable factors:
  - Factor 1: Technical Setup & Core Strategy Quality (Weight: 30)
  - Factor 2: Trend & Moving Average Alignment (Weight: 20)
  - Factor 3: Volume Confirmation & RVOL Surge (Weight: 15)
  - Factor 4: Market Regime & Index Alignment (Weight: 15)
  - Factor 5: Risk/Reward & S/R Headroom (Weight: 10)
  - Factor 6: ADX Trend Strength / Anti-Chop (Weight: 10)

Exposes rich UI classifications:
  - STRONG LONG
  - LONG
  - WATCH LONG
  - NEUTRAL
  - WATCH SHORT
  - SHORT
  - STRONG SHORT
while strictly preserving core backend vocabulary: LONG | SHORT | HOLD | SKIP.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def calculate_confluence_breakdown(
    *,
    symbol: str,
    strategy: str,
    direction: str,
    raw_strategy_score: float,
    tech_snapshot: Dict[str, Any],
    rvol: Optional[float],
    nifty_context: Dict[str, Any],
    risk_reward: float,
    agreeing_strategies: List[str],
) -> Dict[str, Any]:
    """
    Compute structured factor decomposition and overall 0-100 Confluence Score.
    Never claims 'win-rate probability'; provides transparent technical alignment.
    """
    factors: List[Dict[str, Any]] = []
    total_score = 0.0

    dir_upper = direction.upper()
    ltp = tech_snapshot.get("ltp") or 0.0

    # ─────────────────────────────────────────────────────────────
    # Factor 1: Core Strategy & Confluence Agreement (Max 30)
    # ─────────────────────────────────────────────────────────────
    base_pts = min(20.0, max(5.0, (raw_strategy_score / 100.0) * 20.0))
    # Bonus for multiple strategies agreeing on direction
    agree_count = len(agreeing_strategies)
    agree_bonus = min(10.0, max(0.0, (agree_count - 1) * 5.0))
    f1_score = round(base_pts + agree_bonus, 1)
    total_score += f1_score
    factors.append({
        "name": "Strategy Confluence",
        "score": f1_score,
        "max": 30.0,
        "status": "POSITIVE" if f1_score >= 20.0 else "NEUTRAL",
        "detail": f"{strategy} active with {agree_count} strategy agreement ({', '.join(agreeing_strategies)})",
    })

    # ─────────────────────────────────────────────────────────────
    # Factor 2: Trend & Moving Average Alignment (Max 20)
    # ─────────────────────────────────────────────────────────────
    ema_20 = tech_snapshot.get("ema_20")
    ema_50 = tech_snapshot.get("ema_50")
    vwap = tech_snapshot.get("vwap")

    f2_score = 0.0
    f2_details = []
    if dir_upper == "LONG":
        if vwap and ltp > vwap:
            f2_score += 8.0
            f2_details.append("Price > VWAP (+8)")
        if ema_20 and ltp > ema_20:
            f2_score += 6.0
            f2_details.append("Price > EMA20 (+6)")
        if ema_20 and ema_50 and ema_20 > ema_50:
            f2_score += 6.0
            f2_details.append("EMA20 > EMA50 Bullish Stack (+6)")
    elif dir_upper == "SHORT":
        if vwap and ltp < vwap:
            f2_score += 8.0
            f2_details.append("Price < VWAP (+8)")
        if ema_20 and ltp < ema_20:
            f2_score += 6.0
            f2_details.append("Price < EMA20 (+6)")
        if ema_20 and ema_50 and ema_20 < ema_50:
            f2_score += 6.0
            f2_details.append("EMA20 < EMA50 Bearish Stack (+6)")

    f2_score = min(20.0, f2_score)
    total_score += f2_score
    factors.append({
        "name": "Trend Alignment",
        "score": f2_score,
        "max": 20.0,
        "status": "POSITIVE" if f2_score >= 14.0 else ("WARNING" if f2_score <= 6.0 else "NEUTRAL"),
        "detail": "; ".join(f2_details) if f2_details else "Trend structure neutral or mixed",
    })

    # ─────────────────────────────────────────────────────────────
    # Factor 3: Volume & Relative Volume (RVOL) Surge (Max 15)
    # ─────────────────────────────────────────────────────────────
    f3_score = 5.0  # baseline
    rvol_val = rvol if rvol is not None else 1.0
    if rvol_val >= 2.0:
        f3_score = 15.0
        rvol_desc = f"Extreme RVOL ({rvol_val:.1f}x normal pace)"
    elif rvol_val >= 1.5:
        f3_score = 12.0
        rvol_desc = f"Strong Volume Expansion ({rvol_val:.1f}x normal pace)"
    elif rvol_val >= 1.1:
        f3_score = 8.0
        rvol_desc = f"Above-average Volume ({rvol_val:.1f}x normal pace)"
    else:
        f3_score = 4.0
        rvol_desc = f"Normal / Moderate Volume ({rvol_val:.1f}x)"

    total_score += f3_score
    factors.append({
        "name": "Volume & RVOL",
        "score": f3_score,
        "max": 15.0,
        "status": "POSITIVE" if f3_score >= 12.0 else "NEUTRAL",
        "detail": rvol_desc,
    })

    # ─────────────────────────────────────────────────────────────
    # Factor 4: Market Regime & Index Alignment (Max 15)
    # ─────────────────────────────────────────────────────────────
    nifty_trend = str(nifty_context.get("trend") or "NEUTRAL").upper()
    f4_score = 5.0
    if (dir_upper == "LONG" and nifty_trend == "BULLISH") or (dir_upper == "SHORT" and nifty_trend == "BEARISH"):
        f4_score = 15.0
        regime_desc = f"Full Index Confluence (NIFTY is {nifty_trend})"
        regime_status = "POSITIVE"
    elif nifty_trend == "NEUTRAL":
        f4_score = 9.0
        regime_desc = "Index Neutral / Sideways (Selective stock-picking)"
        regime_status = "NEUTRAL"
    else:
        f4_score = 2.0
        regime_desc = f"Counter-Index Headwind (Stock is {dir_upper} while NIFTY is {nifty_trend})"
        regime_status = "WARNING"

    total_score += f4_score
    factors.append({
        "name": "Index Regime",
        "score": f4_score,
        "max": 15.0,
        "status": regime_status,
        "detail": regime_desc,
    })

    # ─────────────────────────────────────────────────────────────
    # Factor 5: Risk/Reward & Structural Headroom (Max 10)
    # ─────────────────────────────────────────────────────────────
    f5_score = 5.0
    if risk_reward >= 2.5:
        f5_score = 10.0
        rr_desc = f"Superior Risk:Reward ({risk_reward:.2f}x)"
    elif risk_reward >= 2.0:
        f5_score = 8.0
        rr_desc = f"Solid Risk:Reward ({risk_reward:.2f}x)"
    elif risk_reward >= 1.5:
        f5_score = 6.0
        rr_desc = f"Standard Risk:Reward ({risk_reward:.2f}x)"
    else:
        f5_score = 2.0
        rr_desc = f"Sub-optimal Risk:Reward ({risk_reward:.2f}x)"

    total_score += f5_score
    factors.append({
        "name": "Risk / Reward",
        "score": f5_score,
        "max": 10.0,
        "status": "POSITIVE" if f5_score >= 8.0 else "NEUTRAL",
        "detail": rr_desc,
    })

    # ─────────────────────────────────────────────────────────────
    # Factor 6: ADX Trend Strength / Anti-Chop (Max 10)
    # ─────────────────────────────────────────────────────────────
    adx_val = tech_snapshot.get("adx")
    adx_regime = tech_snapshot.get("adx_regime") or "RANGING"
    if adx_val and adx_val >= 25.0:
        f6_score = 10.0
        adx_desc = f"Strong directional momentum (ADX={adx_val:.1f})"
        adx_status = "POSITIVE"
    elif adx_val and adx_val >= 20.0:
        f6_score = 6.0
        adx_desc = f"Developing trend (ADX={adx_val:.1f})"
        adx_status = "NEUTRAL"
    else:
        f6_score = 3.0
        adx_desc = f"Choppy / range-bound structure (ADX={adx_val or 15.0:.1f})"
        adx_status = "WARNING"

    total_score += f6_score
    factors.append({
        "name": "ADX Trend Strength",
        "score": f6_score,
        "max": 10.0,
        "status": adx_status,
        "detail": adx_desc,
    })

    final_score = round(min(98.0, max(10.0, total_score)), 1)

    # Rich classification mapping
    if dir_upper == "LONG":
        if final_score >= 85.0:
            rich_class = "STRONG LONG"
        elif final_score >= 70.0:
            rich_class = "LONG"
        elif final_score >= 55.0:
            rich_class = "WATCH LONG"
        else:
            rich_class = "NEUTRAL"
    elif dir_upper == "SHORT":
        if final_score >= 85.0:
            rich_class = "STRONG SHORT"
        elif final_score >= 70.0:
            rich_class = "SHORT"
        elif final_score >= 55.0:
            rich_class = "WATCH SHORT"
        else:
            rich_class = "NEUTRAL"
    else:
        rich_class = "NEUTRAL"

    # Explain Why Ranked / Reasons & Key Risks
    reasons: List[str] = []
    risks: List[str] = []

    if dir_upper == "LONG":
        if vwap and ltp > vwap:
            reasons.append(f"Trading above Day VWAP (₹{vwap:.2f})")
        if ema_20 and ltp > ema_20:
            reasons.append(f"Holding above 20 EMA (₹{ema_20:.2f})")
        if rvol_val >= 1.2:
            reasons.append(f"Volume expansion confirmed (RVOL {rvol_val:.1f}x)")
        if nifty_trend == "BULLISH":
            reasons.append("Aligned with broader NIFTY 50 bullish momentum")
        res = tech_snapshot.get("resistance")
        if res and res > ltp and ((res - ltp) / ltp) < 0.008:
            risks.append(f"Approaching key resistance zone at ₹{res:.2f}")
        if nifty_trend == "BEARISH":
            risks.append("Counter-trend trade: NIFTY 50 is trending down")
    elif dir_upper == "SHORT":
        if vwap and ltp < vwap:
            reasons.append(f"Trading below Day VWAP (₹{vwap:.2f})")
        if ema_20 and ltp < ema_20:
            reasons.append(f"Rejection below 20 EMA (₹{ema_20:.2f})")
        if rvol_val >= 1.2:
            reasons.append(f"Breakdown volume confirmed (RVOL {rvol_val:.1f}x)")
        if nifty_trend == "BEARISH":
            reasons.append("Aligned with broader NIFTY 50 bearish pressure")
        sup = tech_snapshot.get("support")
        if sup and sup < ltp and ((ltp - sup) / ltp) < 0.008:
            risks.append(f"Approaching major support zone at ₹{sup:.2f}")
        if nifty_trend == "BULLISH":
            risks.append("Counter-trend trade: NIFTY 50 is trending up")

    if not risks:
        risks.append("Strict ATR stop-loss enforcement advised before 15:15 IST")

    return {
        "confluence_score": final_score,
        "classification": rich_class,
        "factors": factors,
        "reasons": reasons,
        "risks": risks,
        "why_ranked": f"Ranked with {final_score}/100 Confluence: {factors[0]['detail']}, {factors[1]['detail']}.",
    }
