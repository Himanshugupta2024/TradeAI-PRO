"""Stop-loss enforcement: gap-aware fills and missed-bar reconciliation.

Regression cover for the INDUSINDBK short that blew through its stop and stayed
OPEN because the refresh loop never evaluated the bar that breached it.
"""

import os
import tempfile

import pytest

from agents.execution.paper_trading import PaperTradingEngine


def _engine():
    tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tf.close()
    return PaperTradingEngine(initial_capital=100_000, log_file=tf.name), tf.name


class TestGapAwareFills:
    def test_short_stop_gap_fills_at_open(self):
        engine, path = _engine()
        try:
            engine.execute("GAPSHORT", "SHORT", 100.0, 105.0, 89.0)
            closed = engine.update_prices(
                {"GAPSHORT": {"open": 110.0, "high": 112.0, "low": 108.0, "close": 111.0}}
            )
            assert len(closed) == 1
            assert closed[0].status == "CLOSED_SL"
            # Filled at the open, not the untouched stop.
            assert closed[0].exit_price == pytest.approx(110.0)
        finally:
            os.unlink(path)

    def test_short_stop_without_gap_fills_at_stop(self):
        engine, path = _engine()
        try:
            engine.execute("SHORT2", "SHORT", 100.0, 105.0, 89.0)
            closed = engine.update_prices(
                {"SHORT2": {"open": 101.0, "high": 106.0, "low": 99.0, "close": 104.0}}
            )
            assert len(closed) == 1
            assert closed[0].exit_price == pytest.approx(105.0)
        finally:
            os.unlink(path)

    def test_long_stop_gap_fills_at_open(self):
        engine, path = _engine()
        try:
            engine.execute("GAPLONG", "LONG", 100.0, 95.0, 111.0)
            closed = engine.update_prices(
                {"GAPLONG": {"open": 90.0, "high": 91.0, "low": 89.0, "close": 90.5}}
            )
            assert len(closed) == 1
            assert closed[0].exit_price == pytest.approx(90.0)
        finally:
            os.unlink(path)

    def test_missing_open_keeps_stop_price(self):
        engine, path = _engine()
        try:
            engine.execute("NOOPEN", "SHORT", 100.0, 105.0, 89.0)
            closed = engine.update_prices({"NOOPEN": {"high": 106.0, "low": 99.0, "close": 104.0}})
            assert len(closed) == 1
            assert closed[0].exit_price == pytest.approx(105.0)
        finally:
            os.unlink(path)


class TestReconciliation:
    def test_replays_missed_bars_and_closes_on_breach(self):
        engine, path = _engine()
        try:
            trade = engine.execute("INDUSINDBK", "SHORT", 856.75, 872.61, 645.085)
            assert trade is not None and trade.status == "OPEN"
            # Opened Thu 05 Nov, as it was live; bars after entry are eligible.
            trade.entry_time = "2026-11-05T10:25:08+05:30"

            bars = [
                # Bar the loop did evaluate: no breach.
                {"timestamp": "2026-11-05T15:30:00+05:30", "open": 858.0, "high": 865.0, "low": 850.0, "close": 860.0},
                # Bar that arrived while the process was down: gapped through the stop.
                {"timestamp": "2026-11-06T15:30:00+05:30", "open": 905.0, "high": 915.0, "low": 900.0, "close": 910.25},
            ]
            closed = engine.reconcile_open_trades({"INDUSINDBK": bars})

            assert len(closed) == 1, "position sitting past its stop must be closed"
            assert closed[0].status == "CLOSED_SL"
            assert closed[0].exit_price == pytest.approx(905.0), "must fill at the gapped open"
            assert closed[0].exit_time == "2026-11-06T15:30:00+05:30", "exit must be stamped with the breaching bar"
            assert closed[0].pnl < 0
            assert engine.open_trades == []
        finally:
            os.unlink(path)

    def test_last_checked_advances_and_second_pass_is_noop(self):
        engine, path = _engine()
        try:
            trade = engine.execute("NOWHERE", "SHORT", 100.0, 105.0, 89.0)
            trade.entry_time = "2026-11-04T10:25:00+05:30"
            bars = [
                {"timestamp": "2026-11-05T15:30:00+05:30", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
                {"timestamp": "2026-11-06T15:30:00+05:30", "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0},
            ]
            assert engine.reconcile_open_trades({"NOWHERE": bars}) == []
            open_trade = engine.open_trades[0]
            assert open_trade.last_checked_bar_ts == "2026-11-06T15:30:00+05:30"

            # Replaying the same bars closes nothing.
            assert engine.reconcile_open_trades({"NOWHERE": bars}) == []
            assert len(engine.open_trades) == 1
        finally:
            os.unlink(path)

    def test_reconciles_naive_daily_bar_after_entry(self):
        engine, path = _engine()
        try:
            trade = engine.execute("NAIVE", "SHORT", 100.0, 105.0, 89.0)
            trade.entry_time = "2026-11-05T10:25:08.238963+05:30"
            bars = [
                {"timestamp": "2026-11-06 00:00:00", "open": 106.0, "high": 108.0, "low": 105.0, "close": 107.0}
            ]
            closed = engine.reconcile_open_trades({"NAIVE": bars})
            assert len(closed) == 1, "naive daily bar must not be dropped by format mismatch"
            assert closed[0].exit_price == pytest.approx(106.0)
        finally:
            os.unlink(path)

    def test_bar_before_entry_is_ignored(self):
        engine, path = _engine()
        try:
            trade = engine.execute("PREENTRY", "SHORT", 100.0, 105.0, 89.0)
            trade.entry_time = "2026-11-06T09:00:00+05:30"
            bars = [
                # Before entry — must never close a trade that did not exist yet.
                {"timestamp": "2026-11-05T15:30:00+05:30", "open": 120.0, "high": 130.0, "low": 119.0, "close": 125.0}
            ]
            assert engine.reconcile_open_trades({"PREENTRY": bars}) == []
            assert len(engine.open_trades) == 1
        finally:
            os.unlink(path)

    def test_reconcile_skips_unknown_symbol(self):
        engine, path = _engine()
        try:
            engine.execute("MISSING", "SHORT", 100.0, 105.0, 89.0)
            assert engine.reconcile_open_trades({"OTHER": []}) == []
            assert len(engine.open_trades) == 1
        finally:
            os.unlink(path)

    def test_reconcile_ignores_malformed_bars(self):
        engine, path = _engine()
        try:
            engine.execute("JUNK", "SHORT", 100.0, 105.0, 89.0)
            engine.open_trades[0].entry_time = "2026-11-04T10:25:00+05:30"
            bars = [
                {"timestamp": "2026-11-05T15:30:00+05:30", "open": None, "high": None, "low": None, "close": None},
                {"timestamp": "2026-11-06T15:30:00+05:30", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
            ]
            assert engine.reconcile_open_trades({"JUNK": bars}) == []
            assert len(engine.open_trades) == 1
        finally:
            os.unlink(path)
