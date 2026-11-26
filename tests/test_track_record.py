"""Tests for the public track-record builder."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.track_record import build_track_record, trade_pnl


def _trade(symbol="TCS", pnl=100.0, exit_time="2026-01-05T10:00:00", entry=100.0, qty=10, status="CLOSED", **extra):
    trade = {
        "id": extra.pop("id", 1),
        "symbol": symbol,
        "direction": extra.pop("direction", "LONG"),
        "status": status,
        "entry_price": entry,
        "exit_price": entry + pnl / max(qty, 1),
        "quantity": qty,
        "total_pnl": pnl,
        "entry_time": "2026-01-05T09:30:00",
        "exit_time": exit_time,
        "signal_type": extra.pop("signal_type", "SWING"),
    }
    trade.update(extra)
    return trade


class TestPnl:
    def test_total_pnl_preferred(self):
        assert trade_pnl({"total_pnl": 42.5, "pnl": 1, "quantity": 10}) == 42.5

    def test_fallback_points_times_quantity(self):
        assert trade_pnl({"pnl": 2.0, "quantity": 10}) == 20.0

    def test_invalid_is_zero(self):
        assert trade_pnl({"total_pnl": "n/a", "pnl": None}) == 0.0


class TestTrackRecord:
    def test_empty_record(self):
        record = build_track_record([])
        assert record["total_trades"] == 0
        assert record["sample_status"] == "INSUFFICIENT_SAMPLE"
        assert record["total_trades"] == 0
        assert record["per_year"] == []
        assert record["disclaimer"]

    def test_basic_stats(self):
        trades = [
            _trade("TCS", 100.0, "2026-01-05T10:00:00", id=1),
            _trade("INFY", 50.0, "2026-01-06T10:00:00", id=2),
            _trade("SBIN", -80.0, "2026-01-07T10:00:00", id=3),
        ]
        record = build_track_record(trades, initial_capital=1000.0)

        assert record["total_trades"] == 3
        assert record["wins"] == 2
        assert record["losses"] == 1
        assert record["pnl"] == 70.0
        assert record["win_rate_pct"] == 66.7
        assert record["profit_factor"] == 1.88
        assert record["avg_win"] == 75.0
        assert record["avg_loss"] == -80.0
        assert record["best_trade"] == 100.0
        assert record["worst_trade"] == -80.0

    def test_open_trades_are_skipped(self):
        trades = [
            _trade("TCS", 100.0, status="CLOSED"),
            _trade("INFY", 999.0, status="OPEN", exit_time=None, exit_price=None),
        ]
        record = build_track_record(trades)
        assert record["total_trades"] == 1
        assert record["pnl"] == 100.0

    def test_per_year_grouping(self):
        trades = [
            _trade("TCS", 100.0, "2025-12-31T10:00:00"),
            _trade("INFY", -50.0, "2026-01-02T10:00:00"),
            _trade("SBIN", 25.0, "2026-03-02T10:00:00"),
        ]
        record = build_track_record(trades)
        years = {row["year"]: row for row in record["per_year"]}
        assert years["2025"]["total_trades"] == 1
        assert years["2026"]["total_trades"] == 2
        assert years["2026"]["pnl"] == -25.0

    def test_drawdown_from_initial_capital(self):
        trades = [
            _trade("TCS", 100.0),
            _trade("INFY", 50.0),
            _trade("SBIN", -80.0),
        ]
        record = build_track_record(trades, initial_capital=1000.0)
        assert record["max_drawdown"] == 80.0
        assert record["max_drawdown_pct"] == 6.96

    def test_return_pct(self):
        record = build_track_record([_trade("TCS", 100.0, entry=100.0, qty=10)])
        assert record["trades"][0]["return_pct"] == 10.0

    def test_per_strategy_grouping(self):
        trades = [
            _trade("TCS", 10.0, signal_type="SWING"),
            _trade("INFY", 20.0, signal_type="SWING"),
            _trade("SBIN", -5.0, signal_type="INTRADAY"),
        ]
        record = build_track_record(trades)
        strategies = {row["strategy"]: row for row in record["per_strategy"]}
        assert strategies["SWING"]["total_trades"] == 2
        assert strategies["INTRADAY"]["total_trades"] == 1

    def test_sample_status_valid_at_threshold(self):
        trades = [_trade("TCS", 1.0) for _ in range(100)]
        record = build_track_record(trades, min_sample=100)
        assert record["sample_status"] == "VALID"

    def test_no_initial_capital_reports_null(self):
        record = build_track_record([_trade("TCS", 1.0)])
        assert record["initial_capital"] is None

    def test_net_pnl_zero_fallback_to_total_pnl(self):
        # F04: if net_pnl is 0.0 or uncosted, fall back to total_pnl
        trade = _trade("TCS", pnl=250.0, net_pnl=0.0, total_pnl=250.0)
        assert trade_pnl(trade) == 250.0

    def test_quarantine_legacy_unconverted_trade(self):
        # F05: legacy unconverted non-INR trade must be quarantined from INR totals
        trades = [
            _trade("TCS", 100.0, quote_currency="INR"),
            _trade("ETH/USDT", -295.88, quote_currency="USDT", fx_status="LEGACY_UNCONVERTED"),
        ]
        record = build_track_record(trades, initial_capital=10000.0)
        assert record["total_trades"] == 1  # Only 1 INR trade counted in INR ledger
        assert record["pnl"] == 100.0
        assert len(record["unconverted_legacy_trades"]) == 1
        assert record["unconverted_legacy_trades"][0]["symbol"] == "ETH/USDT"

    def test_breakeven_trade_separated(self):
        # F16: trade with 0 PnL should be counted as breakeven, not loss
        trades = [
            _trade("TCS", 100.0),
            _trade("INFY", -50.0),
            _trade("SBIN", 0.0),
        ]
        record = build_track_record(trades)
        assert record["total_trades"] == 3
        assert record["wins"] == 1
        assert record["losses"] == 1
        assert record["breakevens"] == 1



def test_track_record_quarantines_corrupt_legacy_trades():
    """Inverted-stop, unconverted non-INR FX, and >10x R:R breached rows are excluded from track record."""
    from api.track_record import build_track_record

    trades = [
        # Clean valid closed INR trade
        {
            "id": 48,
            "symbol": "TCS",
            "direction": "LONG",
            "entry_price": 2400.0,
            "stop_loss": 2360.0,
            "target": 2480.0,
            "stop_raw": 2360.0,
            "status": "CLOSED_TP",
            "exit_price": 2480.0,
            "exit_time": "2026-11-24T14:00:00+05:30",
            "net_pnl": 800.0,
            "total_pnl": 820.0,
            "estimated_costs": 20.0,
            "cost_profile": "delivery",
            "quote_currency": "INR",
            "base_currency": "INR",
            "fx_status": "ACTIVE",
        },
        # Inverted-stop legacy LONG (sl > entry, no stop_raw)
        {
            "id": 30,
            "symbol": "RELIANCE",
            "direction": "LONG",
            "entry_price": 1266.10,
            "stop_loss": 1291.33,
            "target": 1286.77,
            "stop_raw": 0.0,
            "status": "CLOSED_SL",
            "exit_price": 1260.0,
            "exit_time": "2026-11-05T15:00:00+05:30",
            "total_pnl": -200.0,
            "quote_currency": "INR",
            "fx_status": "ACTIVE",
        },
        # Unconverted USDT legacy trade
        {
            "id": 27,
            "symbol": "ETH/USDT",
            "direction": "SHORT",
            "entry_price": 2265.96,
            "stop_loss": 2263.14,
            "target": 2200.0,
            "stop_raw": 0.0,
            "status": "CLOSED_SL",
            "exit_price": 2270.0,
            "exit_time": "2026-11-05T15:00:00+05:30",
            "total_pnl": -50.0,
            "quote_currency": "USDT",
            "fx_status": "LEGACY_UNCONVERTED",
        },
        # Outlier >10x R:R midnight EOD breach (INDUSINDBK id 34 pattern)
        {
            "id": 34,
            "symbol": "INDUSINDBK",
            "direction": "SHORT",
            "entry_price": 856.75,
            "stop_loss": 872.61,
            "target": 645.0,
            "stop_raw": 0.0,
            "status": "CLOSED_SL",
            "exit_price": 907.85,
            "exit_time": "2026-11-06T00:00:00",
            "total_pnl": -1500.0,
            "quote_currency": "INR",
            "fx_status": "ACTIVE",
        },
    ]

    rec = build_track_record(trades, initial_capital=100000.0)
    assert rec["total_trades"] == 1
    assert rec["trades"][0]["id"] == 48
    assert rec["excluded_legacy_trades_count"] == 3
    assert len(rec["unconverted_legacy_trades"]) == 1
