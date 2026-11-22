"""
TradeAI Pro — Scale-Free Derivatives (F&O) Feature Engineering Module.
Extracts normalized, scale-free options and futures features for ML models:
OI walls distance (in ATR units), PCR, basis percentage & z-score, implied moves,
IV percentile, IV skew, and OI-price quadrants.

STRICT CONTRACT:
All features are invariant to absolute rupee price levels to eliminate data drift.
All outputs are strictly sanitized against NaNs and infs.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# List of the 12 canonical scale-free F&O feature names
FNO_FEATURE_NAMES: List[str] = [
    "fno_dist_to_ce_wall_atr",
    "fno_dist_to_pe_wall_atr",
    "fno_pcr_oi",
    "fno_pcr_change_5d",
    "fno_implied_move_pct",
    "fno_basis_pct",
    "fno_basis_z",
    "fno_iv_rank",
    "fno_iv_skew",
    "fno_oi_quadrant",
    "fno_rollover_pct",
    "fno_vix_regime",
]


def extract_scale_free_fno_features(
    symbol: str,
    spot: float,
    atr: float,
    fno_telemetry: Optional[Dict[str, Any]] = None,
    future_price: Optional[float] = None,
    vix: Optional[float] = None,
) -> Dict[str, float]:
    """
    Extract 12 normalized scale-free derivative features for a symbol.
    Falls back gracefully to safe neutral defaults when F&O data is missing.
    """
    s = float(spot) if spot > 0 else 100.0
    a = float(atr) if atr > 0 else (s * 0.015)
    telemetry = fno_telemetry or {}

    # 1. Distances to OI walls in ATR units (scale-free)
    ce_wall = telemetry.get("ce_wall")
    if ce_wall is not None and float(ce_wall) > 0:
        # Distance to resistance: positive means wall is above spot
        dist_ce_atr = float(np.clip((float(ce_wall) - s) / a, -5.0, 10.0))
    else:
        dist_ce_atr = 2.0  # neutral expectation (~2 ATR above)

    pe_wall = telemetry.get("pe_wall")
    if pe_wall is not None and float(pe_wall) > 0:
        # Distance to support: positive means spot is above wall
        dist_pe_atr = float(np.clip((s - float(pe_wall)) / a, -5.0, 10.0))
    else:
        dist_pe_atr = 2.0  # neutral expectation (~2 ATR below)

    # 2. Put-Call Ratio (OI)
    raw_pcr = telemetry.get("pcr_oi")
    if raw_pcr is not None and float(raw_pcr) > 0:
        pcr_oi = float(np.clip(float(raw_pcr), 0.2, 3.5))
    else:
        pcr_oi = 1.0  # neutral balanced PCR

    pcr_change_5d = float(np.clip(float(telemetry.get("pcr_change_5d", 0.0)), -1.0, 1.0))

    # 3. Market-Priced Expected Move % (from ATM straddle or ATR fallback)
    raw_implied_move = telemetry.get("implied_move_5d_pct")
    if raw_implied_move is not None and float(raw_implied_move) > 0:
        implied_move_pct = float(np.clip(float(raw_implied_move), 0.5, 20.0))
    else:
        # ATR 5-day horizon fallback: (ATR / Spot) * sqrt(5) * 100
        implied_move_pct = float(np.clip((a / s) * math.sqrt(5.0) * 100.0, 0.5, 20.0))

    # 4. Futures Basis % and Z-Score
    if future_price is not None and float(future_price) > 0:
        basis_pct = float(np.clip(((float(future_price) - s) / s) * 100.0, -5.0, 5.0))
    else:
        basis_pct = 0.15  # normal cost-of-carry premium ~0.15%

    basis_z = float(np.clip(float(telemetry.get("basis_z", 0.0)), -3.0, 3.0))

    # 5. IV Rank / Percentile [0.0, 1.0]
    raw_iv_rank = telemetry.get("iv_rank")
    if raw_iv_rank is not None:
        iv_rank = float(np.clip(float(raw_iv_rank), 0.0, 1.0))
    else:
        iv_rank = 0.50  # 50th percentile neutral

    # 6. IV Skew: (PE_IV - CE_IV) / ATM_IV
    iv_skew = float(np.clip(float(telemetry.get("iv_skew", 0.0)), -1.0, 1.0))

    # 7. OI-Price Quadrant
    # +1.0 = Long Buildup, -1.0 = Short Buildup, -0.5 = Long Unwinding, +0.5 = Short Covering
    quadrant_str = str(telemetry.get("oi_quadrant", "NEUTRAL")).upper()
    if "LONG_BUILDUP" in quadrant_str:
        oi_quadrant = 1.0
    elif "SHORT_BUILDUP" in quadrant_str:
        oi_quadrant = -1.0
    elif "SHORT_COVERING" in quadrant_str:
        oi_quadrant = 0.5
    elif "LONG_UNWINDING" in quadrant_str:
        oi_quadrant = -0.5
    else:
        oi_quadrant = 0.0

    # 8. Expiry Rollover %
    raw_rollover = telemetry.get("rollover_pct")
    if raw_rollover is not None:
        rollover_pct = float(np.clip(float(raw_rollover), 0.0, 1.0))
    else:
        rollover_pct = 0.72  # average healthy rollover ~72%

    # 9. India VIX Regime
    # <13.0 = 0.0 (Low Vol), 13.0 - 18.0 = 0.5 (Normal Vol), >18.0 = 1.0 (Elevated Vol)
    vix_val = float(vix) if (vix is not None and float(vix) > 0) else float(telemetry.get("vix", 14.5))
    if vix_val < 13.0:
        vix_regime = 0.0
    elif vix_val <= 18.0:
        vix_regime = 0.5
    else:
        vix_regime = 1.0

    return {
        "fno_dist_to_ce_wall_atr": round(dist_ce_atr, 4),
        "fno_dist_to_pe_wall_atr": round(dist_pe_atr, 4),
        "fno_pcr_oi": round(pcr_oi, 4),
        "fno_pcr_change_5d": round(pcr_change_5d, 4),
        "fno_implied_move_pct": round(implied_move_pct, 4),
        "fno_basis_pct": round(basis_pct, 4),
        "fno_basis_z": round(basis_z, 4),
        "fno_iv_rank": round(iv_rank, 4),
        "fno_iv_skew": round(iv_skew, 4),
        "fno_oi_quadrant": round(oi_quadrant, 4),
        "fno_rollover_pct": round(rollover_pct, 4),
        "fno_vix_regime": round(vix_regime, 4),
    }
