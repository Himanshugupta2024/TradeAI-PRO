"""
Gate Ablation & Counterfactual Expectancy Tracker.
==================================================
Empirically quantifies the value of every risk & filtering gate in TradeAI PRO:
  - Correlation Gate (Max 1 position per cluster)
  - Cooldown Gate (N candles after trade exit)
  - Consecutive Loss Gate (Halt after 3 daily losses)
  - Macro Sector Gate (Crude / Yield / DXY headwinds)
  - Quality Score / Confidence Gate

For each blocked trade, tracks a shadow forward simulation to determine if the
gate SAVED capital (prevented an SL hit) or BLEW alpha (blocked a TP hit).
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from utils.atomic_io import atomic_write_json

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_SHADOW_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "shadow_blocked_trades.json")


@dataclass
class ShadowBlockedTrade:
    id: str
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    target: float
    gate_reason: str
    regime: str
    timestamp: str
    status: str = "SHADOW_PENDING"  # SHADOW_PENDING, SHADOW_TP, SHADOW_SL, SHADOW_TIMEOUT
    resolved_at: str = ""
    exit_price: float = 0.0
    shadow_pnl_r: float = 0.0


class GateAblationTracker:
    """
    Tracks and resolves counterfactual outcomes of trades rejected by risk gates.
    """

    def __init__(self, storage_file: str = _SHADOW_FILE):
        self.storage_file = os.path.abspath(storage_file)
        self.shadow_trades: List[ShadowBlockedTrade] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load()
        self._loaded = True

    def _load(self) -> None:
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self.shadow_trades = [ShadowBlockedTrade(**item) for item in raw]
                log.info("[GateAblation] Loaded %d shadow blocked trades", len(self.shadow_trades))
            except Exception as exc:
                log.warning("[GateAblation] Failed to load shadow file %s: %s", self.storage_file, exc)
                self.shadow_trades = []
        else:
            self.shadow_trades = []

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.storage_file), exist_ok=True)
        data = [asdict(t) for t in self.shadow_trades]
        try:
            atomic_write_json(self.storage_file, data)
        except Exception as exc:
            log.warning("[GateAblation] Atomic save failed: %s", exc)

    def record_blocked_candidate(
        self,
        symbol: str,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        gate_reason: str,
        regime: str = "UNKNOWN",
        timestamp: Optional[str] = None,
    ) -> ShadowBlockedTrade | None:
        """
        Record a trade setup that was blocked by an internal gate.
        """
        self._ensure_loaded()
        if not symbol or entry <= 0 or sl <= 0 or tp <= 0:
            return None

        norm_dir = "LONG" if direction in ("LONG", "BUY") else "SHORT"
        ts = timestamp or datetime.now(_IST).isoformat()
        trade_id = f"shadow-{len(self.shadow_trades) + 1}-{int(datetime.now().timestamp())}"

        shadow = ShadowBlockedTrade(
            id=trade_id,
            symbol=symbol,
            direction=norm_dir,
            entry_price=float(entry),
            stop_loss=float(sl),
            target=float(tp),
            gate_reason=str(gate_reason).upper(),
            regime=str(regime or "UNKNOWN"),
            timestamp=ts,
            status="SHADOW_PENDING",
        )
        self.shadow_trades.append(shadow)
        self._save()
        log.debug("[GateAblation] Recorded shadow candidate %s %s blocked by %s", norm_dir, symbol, gate_reason)
        return shadow

    def resolve_shadow_trades(self, current_prices: Dict[str, Dict[str, Any]]) -> int:
        """
        Evaluate pending shadow trades against incoming prices.
        """
        self._ensure_loaded()
        resolved_count = 0
        now_str = datetime.now(_IST).isoformat()

        for t in self.shadow_trades:
            if t.status != "SHADOW_PENDING":
                continue

            price_info = current_prices.get(t.symbol) or {}
            high = float(price_info.get("high") or price_info.get("close", 0.0))
            low = float(price_info.get("low") or price_info.get("close", 0.0))
            close = float(price_info.get("close", 0.0))

            if high <= 0 or low <= 0:
                continue

            risk_per_unit = abs(t.entry_price - t.stop_loss) or 1.0

            if t.direction == "LONG":
                # Check Stop Loss first (conservative evaluation)
                if low <= t.stop_loss:
                    t.status = "SHADOW_SL"
                    t.exit_price = t.stop_loss
                    t.resolved_at = now_str
                    t.shadow_pnl_r = -1.0
                    resolved_count += 1
                elif high >= t.target:
                    t.status = "SHADOW_TP"
                    t.exit_price = t.target
                    t.resolved_at = now_str
                    t.shadow_pnl_r = round((t.target - t.entry_price) / risk_per_unit, 2)
                    resolved_count += 1
            else:
                if high >= t.stop_loss:
                    t.status = "SHADOW_SL"
                    t.exit_price = t.stop_loss
                    t.resolved_at = now_str
                    t.shadow_pnl_r = -1.0
                    resolved_count += 1
                elif low <= t.target:
                    t.status = "SHADOW_TP"
                    t.exit_price = t.target
                    t.resolved_at = now_str
                    t.shadow_pnl_r = round((t.entry_price - t.target) / risk_per_unit, 2)
                    resolved_count += 1

        if resolved_count > 0:
            self._save()
        return resolved_count

    def get_ablation_report(self) -> Dict[str, Any]:
        """
        Generate empirical gate value metrics across all recorded blocked setups.
        """
        self._ensure_loaded()
        by_gate: Dict[str, Dict[str, Any]] = {}

        for t in self.shadow_trades:
            reason = t.gate_reason
            if reason not in by_gate:
                by_gate[reason] = {
                    "total_blocked": 0,
                    "resolved": 0,
                    "saved_losses": 0,
                    "missed_wins": 0,
                    "net_expectancy_r": 0.0,
                }

            entry = by_gate[reason]
            entry["total_blocked"] += 1
            if t.status in ("SHADOW_TP", "SHADOW_SL"):
                entry["resolved"] += 1
                if t.status == "SHADOW_SL":
                    entry["saved_losses"] += 1
                    # A blocked loss saves +1.0R of capital
                    entry["net_expectancy_r"] += 1.0
                elif t.status == "SHADOW_TP":
                    entry["missed_wins"] += 1
                    # A blocked win costs the lost R multiple
                    entry["net_expectancy_r"] -= max(1.0, float(t.shadow_pnl_r))

        report: Dict[str, Any] = {}
        for reason, data in by_gate.items():
            net_r = round(data["net_expectancy_r"], 2)
            resolved = data["resolved"]
            if resolved >= 5:
                if net_r > 1.5:
                    advice = "GATE_HIGHLY_EFFECTIVE (Keep strict)"
                elif net_r < -1.5:
                    advice = "GATE_BLEEDING_ALPHA (Soft-scale to 0.5x size instead of binary reject)"
                else:
                    advice = "NEUTRAL_EFFECT (Monitor sample)"
            else:
                advice = "COLLECTING_SAMPLE"

            report[reason] = {
                "total_blocked": data["total_blocked"],
                "resolved": resolved,
                "saved_losses": data["saved_losses"],
                "missed_wins": data["missed_wins"],
                "net_saved_r": net_r,
                "advice": advice,
            }

        return {
            "total_blocked_trades": len(self.shadow_trades),
            "pending_resolution": sum(1 for t in self.shadow_trades if t.status == "SHADOW_PENDING"),
            "gates": report,
        }


_GLOBAL_ABLATION_TRACKER: GateAblationTracker | None = None


def get_ablation_tracker() -> GateAblationTracker:
    global _GLOBAL_ABLATION_TRACKER
    if _GLOBAL_ABLATION_TRACKER is None:
        _GLOBAL_ABLATION_TRACKER = GateAblationTracker()
    return _GLOBAL_ABLATION_TRACKER
