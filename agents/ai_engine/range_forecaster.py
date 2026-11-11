"""
TradeAI-PRO — Dynamic Price Range Forecaster (Layer 5).
======================================================
Calculates dynamic price range envelopes (Lower, Upper, Target, Stop Loss)
for 2-5 day swing horizons using ATR volatility cones, multi-class probabilities,
India VIX adjustments, and swing pivot clustering.

Enforces strict physical constraints:
  lower_range < reference_price < upper_range
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def calculate_price_range(
    reference_price: float,
    atr14: float,
    prob_up: float,
    prob_down: float,
    prob_sideways: float,
    supports: Optional[List[float]] = None,
    resistances: Optional[List[float]] = None,
    vix_level: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Compute dynamic price range boundaries and scenario targets.

    Args:
        reference_price: Last traded price or closing price.
        atr14: 14-period Average True Range in currency units.
        prob_up: Calibrated UP probability (0.0 to 100.0).
        prob_down: Calibrated DOWN probability (0.0 to 100.0).
        prob_sideways: Calibrated SIDEWAYS probability (0.0 to 100.0).
        supports: List of support levels sorted descending (nearest first).
        resistances: List of resistance levels sorted ascending (nearest first).
        vix_level: India VIX index value (e.g. 14.5).

    Returns:
        Dict containing:
          - reference_price: float
          - lower_range: float (guaranteed < reference_price)
          - upper_range: float (guaranteed > reference_price)
          - expected_target: float
          - stop_loss: Optional[float]
          - risk_reward_ratio: Optional[float]
          - scenario: 'BULLISH' | 'BEARISH' | 'SIDEWAYS'
          - range_width_pct: float
    """
    if reference_price <= 0:
        return {
            "reference_price": 0.0,
            "lower_range": 0.0,
            "upper_range": 0.0,
            "expected_target": 0.0,
            "stop_loss": None,
            "risk_reward_ratio": None,
            "scenario": "SIDEWAYS",
            "range_width_pct": 0.0,
        }

    # Minimum ATR floor (at least 0.5% of price if ATR is uncomputed or compressed)
    effective_atr = max(atr14 if (atr14 and atr14 > 0) else (reference_price * 0.01), reference_price * 0.005)

    # 1. Macro Volatility (VIX) Scaling Multiplier
    vix_mult = 1.0
    if vix_level is not None and vix_level > 0:
        if vix_level < 13.0:
            vix_mult = 0.90  # Compression / low volatility
        elif vix_level <= 18.0:
            vix_mult = 1.00  # Normal healthy swing regime
        elif vix_level <= 24.0:
            vix_mult = 1.15  # Elevated volatility - widen brackets
        else:
            vix_mult = 1.30  # Extreme panic / turbulence

    # 2. Probability Skew Factors
    # When probability favors UP, expand upper boundary; when DOWN, expand lower boundary.
    up_skew = max(1.0, 1.2 + (prob_up / 100.0) * 0.8)
    down_skew = max(1.0, 1.2 + (prob_down / 100.0) * 0.8)

    raw_upper_dist = effective_atr * up_skew * vix_mult
    raw_lower_dist = effective_atr * down_skew * vix_mult

    raw_upper = reference_price + raw_upper_dist
    raw_lower = reference_price - raw_lower_dist

    # 3. Anchor Range Boundaries with Swing Pivots if Available
    clean_supports = [s for s in (supports or []) if s < reference_price]
    clean_resistances = [r for r in (resistances or []) if r > reference_price]

    upper_range = raw_upper
    if clean_resistances:
        # If nearest resistance is within 0.8x to 1.3x of raw upper, cluster around it
        nearest_res = clean_resistances[0]
        if nearest_res > reference_price and (nearest_res - reference_price) <= (raw_upper_dist * 1.4):
            upper_range = max(nearest_res, raw_upper)

    lower_range = raw_lower
    if clean_supports:
        nearest_supp = clean_supports[0]
        if nearest_supp < reference_price and (reference_price - nearest_supp) <= (raw_lower_dist * 1.4):
            lower_range = min(nearest_supp, raw_lower)

    # Floor lower range at 1% of reference price to prevent zero/negative price
    lower_range = max(lower_range, reference_price * 0.01)

    # Enforce Invariant: lower_range < reference_price < upper_range
    if lower_range >= reference_price:
        lower_range = reference_price * 0.98
    if upper_range <= reference_price:
        upper_range = reference_price * 1.02

    lower_range = round(lower_range, 2)
    upper_range = round(upper_range, 2)
    ref_price = round(reference_price, 2)

    # 4. Determine Primary Scenario & Target/SL
    if prob_up >= 50.0 and prob_up > prob_down:
        scenario = "BULLISH"
        # Target 1: nearest resistance or 1.5x ATR
        if clean_resistances and clean_resistances[0] > ref_price:
            target_1 = round(clean_resistances[0], 2)
        else:
            target_1 = round(ref_price + (1.5 * effective_atr), 2)

        # Target 2: secondary extended resistance or 2.5x ATR
        if len(clean_resistances) > 1 and clean_resistances[1] > target_1:
            target_2 = round(clean_resistances[1], 2)
        else:
            target_2 = round(ref_price + (2.5 * effective_atr), 2)

        expected_target = target_1

        # Stop loss: beyond nearest support or 1.5x ATR floor
        if clean_supports:
            stop_loss = round(min(clean_supports[0] - (0.2 * effective_atr), ref_price - (1.2 * effective_atr)), 2)
        else:
            stop_loss = round(ref_price - (1.5 * effective_atr), 2)

        risk = max(ref_price - stop_loss, 0.01)
        reward = max(expected_target - ref_price, 0.0)
        rr = round(reward / risk, 2)

    elif prob_down >= 50.0 and prob_down > prob_up:
        scenario = "BEARISH"
        # Target 1: nearest support or 1.5x ATR
        if clean_supports and clean_supports[0] < ref_price:
            target_1 = round(clean_supports[0], 2)
        else:
            target_1 = round(ref_price - (1.5 * effective_atr), 2)

        # Target 2: secondary extended support or 2.5x ATR
        if len(clean_supports) > 1 and clean_supports[1] < target_1:
            target_2 = round(clean_supports[1], 2)
        else:
            target_2 = round(ref_price - (2.5 * effective_atr), 2)

        expected_target = target_1

        if clean_resistances:
            stop_loss = round(max(clean_resistances[0] + (0.2 * effective_atr), ref_price + (1.2 * effective_atr)), 2)
        else:
            stop_loss = round(ref_price + (1.5 * effective_atr), 2)

        risk = max(stop_loss - ref_price, 0.01)
        reward = max(ref_price - expected_target, 0.0)
        rr = round(reward / risk, 2)

    else:
        scenario = "SIDEWAYS"
        target_1 = round(ref_price + (1.0 * effective_atr), 2)
        target_2 = round(ref_price - (1.0 * effective_atr), 2)
        expected_target = ref_price
        stop_loss = lower_range
        rr = None

    range_width_pct = round(((upper_range - lower_range) / ref_price) * 100.0, 2)

    return {
        "reference_price": ref_price,
        "entry_reference": ref_price,
        "lower_range": lower_range,
        "upper_range": upper_range,
        "expected_target": expected_target,
        "target_1": target_1,
        "target_2": target_2,
        "stop_loss": stop_loss,
        "risk_reward_ratio": rr,
        "scenario": scenario,
        "range_width_pct": range_width_pct,
    }
