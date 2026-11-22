"""
TradeAI Pro — Option Chain & Open Interest Analytics.
Aggregates ticks into strike-indexed option chains, computes CE/PE OI resistance/support walls,
Put-Call Ratios (PCR), ATM straddles, and Max Pain.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class OptionChain:
    """Manages real-time strike-indexed option chain for a single underlying symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol.strip().upper().replace(".NS", "")
        # strike -> {"strike": float, "ce": dict, "pe": dict}
        self.strikes: Dict[float, Dict[str, Any]] = {}
        # token -> (strike, opt_type)
        self.token_map: Dict[str, Tuple[float, str]] = {}

    def register_strike(self, strike: float, opt_type: str, token: str) -> None:
        """Register strike slot and token mapping."""
        k = round(float(strike), 2)
        otype = opt_type.strip().lower()
        if k not in self.strikes:
            self.strikes[k] = {
                "strike": k,
                "ce": {"token": "", "ltp": 0.0, "oi": 0, "volume": 0, "prev_oi": 0},
                "pe": {"token": "", "ltp": 0.0, "oi": 0, "volume": 0, "prev_oi": 0},
            }
        if otype in ("ce", "pe"):
            self.strikes[k][otype]["token"] = str(token)
            self.token_map[str(token)] = (k, otype)

    def update_tick(
        self,
        token: str,
        ltp: Optional[float] = None,
        oi: Optional[int] = None,
        volume: Optional[int] = None,
    ) -> bool:
        """Update live quote data by token."""
        t = str(token)
        if t not in self.token_map:
            return False
        strike, otype = self.token_map[t]
        slot = self.strikes[strike][otype]
        if ltp is not None and ltp > 0:
            slot["ltp"] = round(float(ltp), 2)
        if oi is not None and oi >= 0:
            slot["prev_oi"] = slot["oi"]
            slot["oi"] = int(oi)
        if volume is not None and volume >= 0:
            slot["volume"] = int(volume)
        return True

    def update_by_strike(
        self,
        strike: float,
        opt_type: str,
        ltp: Optional[float] = None,
        oi: Optional[int] = None,
        volume: Optional[int] = None,
    ) -> None:
        """Direct update by strike and option type."""
        k = round(float(strike), 2)
        otype = opt_type.strip().lower()
        if k not in self.strikes:
            self.register_strike(k, otype, "")
        slot = self.strikes[k][otype]
        if ltp is not None and ltp > 0:
            slot["ltp"] = round(float(ltp), 2)
        if oi is not None and oi >= 0:
            slot["oi"] = int(oi)
        if volume is not None and volume >= 0:
            slot["volume"] = int(volume)

    def get_walls(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Identify Call OI Wall (resistance) and Put OI Wall (support).
        Returns (ce_wall, pe_wall).
        """
        if not self.strikes:
            return None, None

        max_ce_oi = -1
        ce_wall = None
        max_pe_oi = -1
        pe_wall = None

        for k, data in self.strikes.items():
            ce_oi = data["ce"]["oi"]
            pe_oi = data["pe"]["oi"]
            if ce_oi > max_ce_oi:
                max_ce_oi = ce_oi
                ce_wall = k
            if pe_oi > max_pe_oi:
                max_pe_oi = pe_oi
                pe_wall = k

        return ce_wall, pe_wall

    def get_pcr(self) -> Dict[str, float]:
        """Compute Put-Call Ratio for Open Interest and Volume."""
        total_ce_oi = sum(s["ce"]["oi"] for s in self.strikes.values())
        total_pe_oi = sum(s["pe"]["oi"] for s in self.strikes.values())
        total_ce_vol = sum(s["ce"]["volume"] for s in self.strikes.values())
        total_pe_vol = sum(s["pe"]["volume"] for s in self.strikes.values())

        pcr_oi = round(total_pe_oi / max(1, total_ce_oi), 2)
        pcr_vol = round(total_pe_vol / max(1, total_ce_vol), 2)

        return {
            "pcr_oi": pcr_oi,
            "pcr_volume": pcr_vol,
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
        }

    def get_max_pain(self) -> Optional[float]:
        """
        Calculate Max Pain strike: the strike price at which total payout
        to option buyers across all calls and puts is minimized.
        """
        if len(self.strikes) < 3:
            return None

        strike_keys = sorted(self.strikes.keys())
        min_payout = float("inf")
        best_strike = None

        for expiry_price in strike_keys:
            total_loss = 0.0
            for k in strike_keys:
                ce_oi = self.strikes[k]["ce"]["oi"]
                pe_oi = self.strikes[k]["pe"]["oi"]
                # Call payout if expired at expiry_price: max(0, expiry_price - k) * ce_oi
                if expiry_price > k:
                    total_loss += (expiry_price - k) * ce_oi
                # Put payout: max(0, k - expiry_price) * pe_oi
                elif expiry_price < k:
                    total_loss += (k - expiry_price) * pe_oi

            if total_loss < min_payout:
                min_payout = total_loss
                best_strike = expiry_price

        return best_strike

    def get_atm_straddle(self, spot: float) -> Dict[str, Any]:
        """
        Find nearest ATM strike and calculate straddle premium (CE LTP + PE LTP).
        """
        if not self.strikes or spot <= 0:
            return {
                "atm_strike": None,
                "ce_ltp": 0.0,
                "pe_ltp": 0.0,
                "straddle_price": 0.0,
            }

        sorted_strikes = sorted(self.strikes.keys())
        # Find closest strike
        atm_strike = min(sorted_strikes, key=lambda k: abs(k - spot))
        slot = self.strikes[atm_strike]
        ce_ltp = slot["ce"]["ltp"]
        pe_ltp = slot["pe"]["ltp"]
        straddle_price = round(ce_ltp + pe_ltp, 2)

        return {
            "atm_strike": atm_strike,
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "straddle_price": straddle_price,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serializes current option chain snapshot."""
        return {
            "symbol": self.symbol,
            "strike_count": len(self.strikes),
            "strikes": self.strikes,
        }
