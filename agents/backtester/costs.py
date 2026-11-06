"""
Transaction-cost models for Indian equity backtests.

Design rule: **slippage and statutory costs are separate.**
  - Slippage moves the fill price (adverse by N bps on entry and exit).
  - CostModel charges brokerage, STT, exchange fees, SEBI fee, stamp duty and
    GST on the traded notional.

The previous engine applied slippage to the fill price *and* again inside the
cost calculation, so every trade paid it twice. This module removes that
coupling and replaces the single hard-coded 10 bps with explicit delivery and
intraday profiles.

Profiles are intentionally conservative and easy to audit:

Delivery (NSE equity, CNC):
    brokerage 3 bps/side · STT 10 bps buy + 10 bps sell · exchange 0.3 bps
    SEBI 0.01 bps · stamp 1.5 bps buy · GST 18% on brokerage+exchange+SEBI
    → ~30 bps round trip before slippage

Intraday (NSE equity, MIS):
    brokerage 2 bps/side · STT 0 buy / 2.5 bps sell · exchange 0.3 bps
    SEBI 0.01 bps · stamp 0.3 bps buy · GST 18%
    → ~8 bps round trip before slippage
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class CostProfile:
    """Per-side statutory and brokerage charges, expressed in basis points."""

    name: str
    brokerage_bps_per_side: float
    stt_bps_buy: float
    stt_bps_sell: float
    exchange_bps_per_side: float
    sebi_bps_per_side: float
    stamp_bps_buy: float
    gst_rate: float
    slippage_bps_per_side: float

    def _charges(self, stt_bps: float, stamp_bps: float) -> float:
        """One leg: statutory charges + GST on the taxable components only.

        GST applies to brokerage + exchange transaction charge + SEBI fee.
        It does **not** apply to STT or stamp duty.
        """
        taxable = (
            self.brokerage_bps_per_side
            + self.exchange_bps_per_side
            + self.sebi_bps_per_side
        )
        return (
            self.brokerage_bps_per_side
            + stt_bps
            + self.exchange_bps_per_side
            + self.sebi_bps_per_side
            + stamp_bps
            + taxable * self.gst_rate
        )

    def buy_bps(self) -> float:
        """Total charges on the buy leg, in bps of buy notional."""
        return self._charges(self.stt_bps_buy, self.stamp_bps_buy)

    def sell_bps(self) -> float:
        """Total charges on the sell leg, in bps of sell notional."""
        return self._charges(self.stt_bps_sell, 0.0)

    def roundtrip_bps(self) -> float:
        """Buy + sell charges, excluding slippage."""
        return self.buy_bps() + self.sell_bps()

    def roundtrip_bps_with_slippage(self) -> float:
        return self.roundtrip_bps() + 2.0 * self.slippage_bps_per_side

    def as_dict(self) -> dict:
        return {
            "profile": self.name,
            "buy_bps": round(self.buy_bps(), 2),
            "sell_bps": round(self.sell_bps(), 2),
            "roundtrip_bps": round(self.roundtrip_bps(), 2),
            "slippage_bps_per_side": self.slippage_bps_per_side,
            "roundtrip_bps_with_slippage": round(self.roundtrip_bps_with_slippage(), 2),
        }


DELIVERY = CostProfile(
    name="delivery",
    brokerage_bps_per_side=3.0,
    stt_bps_buy=10.0,
    stt_bps_sell=10.0,
    exchange_bps_per_side=0.3,
    sebi_bps_per_side=0.01,
    stamp_bps_buy=1.5,
    gst_rate=0.18,
    slippage_bps_per_side=5.0,
)

INTRADAY = CostProfile(
    name="intraday",
    brokerage_bps_per_side=2.0,
    stt_bps_buy=0.0,
    stt_bps_sell=2.5,
    exchange_bps_per_side=0.3,
    sebi_bps_per_side=0.01,
    stamp_bps_buy=0.3,
    gst_rate=0.18,
    slippage_bps_per_side=5.0,
)

PROFILES: Mapping[str, CostProfile] = {
    "delivery": DELIVERY,
    "intraday": INTRADAY,
}


def get_profile(name: str | None) -> CostProfile:
    """Resolve a profile name; unknown names fall back to delivery (conservative)."""
    if not name:
        return DELIVERY
    return PROFILES.get(str(name).strip().lower(), DELIVERY)


class CostModel:
    """Charges brokerage + statutory costs on traded notional."""

    def __init__(self, profile: CostProfile = DELIVERY):
        self.profile = profile

    @property
    def slippage_pct(self) -> float:
        """Slippage per side as a decimal fraction (0.0005 = 5 bps)."""
        return self.profile.slippage_bps_per_side / 10_000.0

    def trade_cost(
        self,
        entry_notional: float,
        exit_notional: float,
        direction: str = "LONG",
    ) -> float:
        """Rupee cost for a completed round trip.

        LONG  : buy at entry, sell at exit.
        SHORT : sell at entry (STT on sell), buy back at exit (stamp on buy).
        """
        entry_notional = max(float(entry_notional), 0.0)
        exit_notional = max(float(exit_notional), 0.0)
        if str(direction).upper() == "SHORT":
            buy_notional, sell_notional = exit_notional, entry_notional
        else:
            buy_notional, sell_notional = entry_notional, exit_notional
        return (
            buy_notional * self.profile.buy_bps() / 10_000.0
            + sell_notional * self.profile.sell_bps() / 10_000.0
        )

    def summary(self) -> dict:
        return self.profile.as_dict()


class FlatCostModel(CostModel):
    """Backward-compatible flat round-trip charge (no statutory breakdown).

    Used only when a caller explicitly passes ``cost_bps``. The value is the
    **total** round-trip charge, split evenly across the two legs.
    """

    def __init__(self, roundtrip_bps: float, slippage_bps_per_side: float = 5.0):
        self._roundtrip_bps = max(float(roundtrip_bps), 0.0)
        self._slippage_bps = max(float(slippage_bps_per_side), 0.0)
        profile = CostProfile(
            name=f"flat_{self._roundtrip_bps:g}bps",
            brokerage_bps_per_side=self._roundtrip_bps / 2.0,
            stt_bps_buy=0.0,
            stt_bps_sell=0.0,
            exchange_bps_per_side=0.0,
            sebi_bps_per_side=0.0,
            stamp_bps_buy=0.0,
            gst_rate=0.0,
            slippage_bps_per_side=self._slippage_bps,
        )
        super().__init__(profile)

    def trade_cost(self, entry_notional: float, exit_notional: float, direction: str = "LONG") -> float:
        combined = max(float(entry_notional), 0.0) + max(float(exit_notional), 0.0)
        return combined * (self._roundtrip_bps / 2.0) / 10_000.0
