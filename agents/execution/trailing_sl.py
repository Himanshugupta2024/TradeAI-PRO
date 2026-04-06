"""
Trailing Stop-Loss Manager.

Dynamically adjusts stop-loss levels for open trades as price
moves favorably. Never loosens — only tightens.

Modes:
  - ATR trailing: SL follows price at N * ATR distance
  - Break-even: Once profit >= 1 ATR, SL moves to entry price

State is persisted to JSON file so trailing stops survive restarts.
"""

import json
import os
from dataclasses import dataclass, asdict
from config import settings
from config.logger import get_logger

log = get_logger(__name__)

TRAILING_STATE_FILE = "trailing_sl_state.json"


@dataclass
class TrailingState:
    """Tracks the trailing SL state for a single trade."""
    original_sl: float
    current_sl: float
    highest_favorable: float  # Highest price for BUY, lowest for SELL
    breakeven_triggered: bool = False


class TrailingStopManager:
    """
    Manages trailing stop-loss for all open trades.

    Usage:
        tsm = TrailingStopManager()
        tsm.register(trade_id=1, direction="BUY", entry=65000, sl=64000, atr=500)
        new_sl = tsm.update(trade_id=1, direction="BUY", current_high=66500, current_low=65800, atr=500)
    """

    def __init__(
        self,
        trailing_atr_mult: float = settings.TRAILING_ATR_MULTIPLIER,
        breakeven_atr_threshold: float = settings.BREAKEVEN_ATR_THRESHOLD,
        state_file: str = TRAILING_STATE_FILE,
    ):
        self.trailing_atr_mult = trailing_atr_mult
        self.breakeven_threshold = breakeven_atr_threshold
        self._states: dict[int, TrailingState] = {}
        self._state_file = state_file
        self._load_state()

    def register(self, trade_id: int, direction: str, entry: float, sl: float, atr: float):
        """Register a new trade for trailing SL tracking."""
        self._states[trade_id] = TrailingState(
            original_sl=sl,
            current_sl=sl,
            highest_favorable=entry,
        )
        self._save_state()

    def update(
        self,
        trade_id: int,
        direction: str,
        current_high: float,
        current_low: float,
        atr: float,
    ) -> float | None:
        """
        Update trailing SL for a trade based on latest price.

        Returns:
            New SL level, or None if trade is not registered.
        """
        state = self._states.get(trade_id)
        if not state:
            return None

        if direction in ("LONG", "BUY"):
            # Track highest high since entry
            state.highest_favorable = max(state.highest_favorable, current_high)
            favorable_price = state.highest_favorable

            # Break-even check: if price moved >= breakeven_threshold * ATR above entry
            entry_distance = favorable_price - state.original_sl  # approx entry from original SL
            if not state.breakeven_triggered and atr > 0:
                profit_atrs = (favorable_price - (state.original_sl + atr * settings.ATR_SL_MULTIPLIER)) / atr
                if profit_atrs >= self.breakeven_threshold:
                    # Move SL to approximate entry (original_sl + original risk distance)
                    entry_approx = state.original_sl + atr * settings.ATR_SL_MULTIPLIER
                    state.current_sl = max(state.current_sl, entry_approx)
                    state.breakeven_triggered = True
                    log.info("🔒 Trade #%d break-even triggered: SL → %.2f", trade_id, state.current_sl)

            # ATR trailing: SL = highest_favorable - (trailing_mult * ATR)
            trailing_sl = favorable_price - (self.trailing_atr_mult * atr)

            # Only tighten, never loosen
            if trailing_sl > state.current_sl:
                state.current_sl = trailing_sl
                log.debug("📈 Trade #%d trailing SL tightened → %.2f", trade_id, state.current_sl)

        elif direction in ("SHORT", "SELL"):
            # Track lowest low since entry
            state.highest_favorable = min(state.highest_favorable, current_low)
            favorable_price = state.highest_favorable

            # Break-even for shorts
            if not state.breakeven_triggered and atr > 0:
                profit_atrs = ((state.original_sl - atr * settings.ATR_SL_MULTIPLIER) - favorable_price) / atr
                if profit_atrs >= self.breakeven_threshold:
                    entry_approx = state.original_sl - atr * settings.ATR_SL_MULTIPLIER
                    state.current_sl = min(state.current_sl, entry_approx)
                    state.breakeven_triggered = True
                    log.info("🔒 Trade #%d break-even triggered: SL → %.2f", trade_id, state.current_sl)

            # ATR trailing for shorts: SL = lowest_low + (trailing_mult * ATR)
            trailing_sl = favorable_price + (self.trailing_atr_mult * atr)

            if trailing_sl < state.current_sl:
                state.current_sl = trailing_sl
                log.debug("📉 Trade #%d trailing SL tightened → %.2f", trade_id, state.current_sl)

        self._save_state()
        return state.current_sl

    def get_sl(self, trade_id: int) -> float | None:
        """Get current trailing SL for a trade."""
        state = self._states.get(trade_id)
        return state.current_sl if state else None

    def remove(self, trade_id: int):
        """Remove a closed trade from tracking."""
        self._states.pop(trade_id, None)
        self._save_state()

    def _save_state(self):
        """Persist trailing SL state to JSON file."""
        try:
            data = {}
            for tid, state in self._states.items():
                data[str(tid)] = asdict(state)
            with open(self._state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error("Failed to save trailing SL state: %s", e)

    def _load_state(self):
        """Restore trailing SL state from JSON file."""
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                data = json.load(f)
            for tid_str, state_dict in data.items():
                self._states[int(tid_str)] = TrailingState(**state_dict)
            log.info("Restored %d trailing SL states from %s", len(self._states), self._state_file)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.error("Failed to load trailing SL state: %s — starting fresh", e)
