"""
Multi-Dimensional Composite Decision Engine — TradeAI-PRO Data Brain V3
Synthesizes:
  1. Technical Setup Score
  2. AI Ensemble Probability
  3. Market Breadth Modifier
  4. Institutional FII/DII Flow
  5. Security Delivery Conviction
  6. Sector Relative Strength
  7. India VIX Volatility Penalty & Sizing Multiplier
  8. Corporate Event Risk Hard-Gate
  9. Data Quality Telemetry
into a unified, transparent trade decision and position-sizing verdict.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def build_composite_decision(
    symbol: str,
    raw_signal: str,
    technical_score: float = 75.0,
    ai_confidence: float = 70.0,
    sector: str = "Broad Market",
    sector_strength: int = 5,
    vix_data: dict[str, Any] | None = None,
    fii_dii_data: dict[str, Any] | None = None,
    breadth_data: dict[str, Any] | None = None,
    delivery_data: dict[str, Any] | None = None,
    event_risk_data: dict[str, Any] | None = None,
    oi_data: dict[str, Any] | None = None,
    data_quality: float = 98.0,
) -> dict[str, Any]:
    """
    Synthesize all orthogonal dimensions into a multi-attribute decision.
    """
    clean_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()

    # 1. Defaults for context dimensions
    vix = vix_data or {}
    fii = fii_dii_data or {}
    breadth = breadth_data or {}
    delivery = delivery_data or {}
    event_risk = event_risk_data or {}
    oi = oi_data or {}

    # Extract score modifiers
    m_breadth = int(breadth.get("market_breadth_modifier", 0))
    m_fii = int(fii.get("institutional_flow_score", 0))
    m_delivery = int(delivery.get("delivery_strength_bonus", 0))
    m_sector = int(sector_strength)
    vix_penalty = int(vix.get("confidence_penalty", 0))
    vix_sizing = float(vix.get("vix_size_multiplier", 1.0))
    event_penalty = int(event_risk.get("event_risk_penalty", 0))
    hard_skip_event = bool(event_risk.get("hard_skip", False))

    # Base score: 50% Technical, 50% AI Ensemble
    base_score = (technical_score * 0.5) + (ai_confidence * 0.5)

    # Context modifier sum
    context_sum = m_breadth + m_fii + m_delivery + m_sector + vix_penalty - event_penalty
    final_score = int(round(max(0.0, min(100.0, base_score + (context_sum * 0.75)))))

    # Decision determination & Event Risk Gating
    decision = raw_signal.upper()
    if decision not in ("LONG", "SHORT", "BUY", "SELL"):
        decision = "SKIP"
    elif decision == "BUY":
        decision = "LONG"
    elif decision == "SELL":
        decision = "SHORT"

    gated_reason = None
    if hard_skip_event:
        decision = "SKIP"
        gated_reason = "Gated by Corporate Event Risk (earnings/results/announcement in window)"
    elif final_score < 60 and decision in ("LONG", "SHORT"):
        decision = "SKIP"
        gated_reason = f"Final composite score ({final_score}/100) below quality threshold (60)"

    # Build human-readable rationale
    reasons = []
    if m_delivery > 0:
        reasons.append("institutional delivery accumulation")
    if m_fii > 0:
        reasons.append("favorable institutional flow")
    if m_breadth > 0:
        reasons.append("broad market breadth support")
    if m_sector > 0:
        reasons.append(f"{sector} sector strength")
    if vix_penalty < 0:
        reasons.append(f"VIX caution ({vix.get('vix_regime', 'NORMAL')})")

    if gated_reason:
        reasoning_str = gated_reason
    elif reasons:
        reasoning_str = f"Technical setup supported by {', '.join(reasons)}."
    else:
        reasoning_str = "Balanced technical structure under neutral market context."

    breakdown = {
        "technical_score": round(technical_score, 1),
        "ai_probability": round(ai_confidence, 1),
        "market_breadth": m_breadth,
        "institutional_flow": m_fii,
        "delivery_strength": m_delivery,
        "sector_strength": m_sector,
        "event_risk": event_penalty,
        "india_vix_penalty": vix_penalty,
        "data_quality_pct": round(data_quality, 1),
    }

    return {
        "symbol": clean_sym,
        "signal": decision,
        "raw_signal": raw_signal,
        "final_trade_score": final_score,
        "position_sizing_multiplier": vix_sizing,
        "breakdown": breakdown,
        "reasoning": reasoning_str,
        "vix_regime": vix.get("vix_regime", "NORMAL"),
        "institutional_sentiment": fii.get("sentiment", "NEUTRAL"),
        "event_risk_level": event_risk.get("risk_level", "CLEAR"),
        "delivery_interpretation": delivery.get("interpretation", "NEUTRAL_DELIVERY"),
        "derivatives_buildup": oi.get("oi_buildup_classification", "CONSOLIDATION"),
    }
