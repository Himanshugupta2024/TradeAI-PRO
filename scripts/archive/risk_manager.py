"""Risk management utilities for trade entry and position lifecycle control."""

from __future__ import annotations

from datetime import date, datetime
import logging
from pathlib import Path
from typing import Any


class RiskManager:
    """Tracks open positions and enforces simple daily risk limits."""

    def __init__(self, capital: float) -> None:
        self.total_capital: float = float(capital)
        self.open_positions: list[dict[str, Any]] = []
        self.daily_pnl: float = 0.0
        self._daily_date: date = date.today()
        self._closed_trades: list[dict[str, Any]] = []
        self.logger: logging.Logger = self._build_logger()

        self.logger.info(
            "INIT | capital=%.2f | max_open_positions=%d | daily_loss_limit=%.2f",
            self.total_capital,
            3,
            self.total_capital * 0.03,
        )

    @staticmethod
    def _build_logger() -> logging.Logger:
        logger = logging.getLogger("RiskManager")
        logger.setLevel(logging.INFO)

        log_path = Path("trades.log")
        has_handler = any(
            isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path.resolve()
            for handler in logger.handlers
        )
        if not has_handler:
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
            )
            logger.addHandler(handler)

        logger.propagate = False
        return logger

    def _roll_day_if_needed(self) -> None:
        today = date.today()
        if today != self._daily_date:
            self.logger.info(
                "DAY_ROLLOVER | old_date=%s | old_daily_pnl=%.2f | new_date=%s",
                self._daily_date.isoformat(),
                self.daily_pnl,
                today.isoformat(),
            )
            self.daily_pnl = 0.0
            self._daily_date = today

    def can_enter_trade(self, symbol: str) -> bool:
        """Return whether a new trade is allowed by risk constraints."""
        self._roll_day_if_needed()
        normalized = symbol.upper().strip()

        if len(self.open_positions) >= 3:
            self.logger.warning(
                "ENTRY_BLOCKED | symbol=%s | reason=max_open_positions | open_count=%d",
                normalized,
                len(self.open_positions),
            )
            return False

        if any(pos["symbol"] == normalized for pos in self.open_positions):
            self.logger.warning(
                "ENTRY_BLOCKED | symbol=%s | reason=already_open",
                normalized,
            )
            return False

        daily_loss_limit = -0.03 * self.total_capital
        if self.daily_pnl <= daily_loss_limit:
            self.logger.warning(
                "ENTRY_BLOCKED | symbol=%s | reason=daily_loss_limit | daily_pnl=%.2f | limit=%.2f",
                normalized,
                self.daily_pnl,
                daily_loss_limit,
            )
            return False

        return True

    def add_position(self, symbol: str, entry: float, sl: float, size: float) -> None:
        """Add a newly opened position to tracking."""
        self._roll_day_if_needed()
        normalized = symbol.upper().strip()

        position = {
            "symbol": normalized,
            "entry": float(entry),
            "sl": float(sl),
            "size": float(size),
            "signal_time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        self.open_positions.append(position)

        self.logger.info(
            "POSITION_OPENED | symbol=%s | entry=%.8f | sl=%.8f | size=%.8f | signal_time=%s",
            position["symbol"],
            position["entry"],
            position["sl"],
            position["size"],
            position["signal_time"],
        )

    def close_position(self, symbol: str, exit_price: float) -> float:
        """Close an open position, update daily P&L, and return trade P&L."""
        self._roll_day_if_needed()
        normalized = symbol.upper().strip()

        idx = next(
            (i for i, pos in enumerate(self.open_positions) if pos["symbol"] == normalized),
            None,
        )
        if idx is None:
            self.logger.warning(
                "POSITION_CLOSE_FAILED | symbol=%s | reason=not_found",
                normalized,
            )
            raise ValueError(f"No open position found for symbol: {normalized}")

        position = self.open_positions.pop(idx)
        pnl = (float(exit_price) - float(position["entry"])) * float(position["size"])
        self.daily_pnl += pnl

        trade = {
            "symbol": normalized,
            "entry": float(position["entry"]),
            "exit": float(exit_price),
            "sl": float(position["sl"]),
            "size": float(position["size"]),
            "signal_time": position["signal_time"],
            "close_time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "pnl": pnl,
        }
        self._closed_trades.append(trade)

        self.logger.info(
            "POSITION_CLOSED | symbol=%s | entry=%.8f | exit=%.8f | size=%.8f | pnl=%.8f | daily_pnl=%.8f",
            trade["symbol"],
            trade["entry"],
            trade["exit"],
            trade["size"],
            trade["pnl"],
            self.daily_pnl,
        )

        return pnl

    def consecutive_losses(self, n: int = 10) -> int:
        """Return count of latest consecutive losses within last N closed trades."""
        self._roll_day_if_needed()

        streak = 0
        for trade in reversed(self._closed_trades[-max(1, int(n)) :]):
            if float(trade["pnl"]) < 0:
                streak += 1
            else:
                break

        if streak >= 3:
            msg = f"WARNING: {streak} consecutive losses detected. Consider pausing trading."
            print(msg)
            self.logger.warning(
                "LOSS_STREAK | streak=%d | lookback=%d",
                streak,
                max(1, int(n)),
            )

        return streak

    def summary(self) -> None:
        """Print current open positions and today's running P&L."""
        self._roll_day_if_needed()

        print("=" * 58)
        print("RiskManager Summary")
        print("=" * 58)
        print(f"Capital      : {self.total_capital:,.2f}")
        print(f"Daily P&L    : {self.daily_pnl:,.2f}")
        print(f"Open Count   : {len(self.open_positions)}")

        if not self.open_positions:
            print("Open Positions: None")
            return

        print("Open Positions:")
        for pos in self.open_positions:
            print(
                "- {symbol} | entry={entry:.8f} | sl={sl:.8f} | size={size:.8f} | time={signal_time}".format(
                    **pos
                )
            )
