"""Tests for zombie detection and bulk-close selection."""

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.trade_hygiene import IST, is_zombie, select_bulk_targets  # noqa: E402

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=IST)  # Saturday


def _trade(symbol="TCS", signal_type="INTRADAY", days_ago=0, hours_ago=0, trade_id=1):
    entered = NOW - timedelta(days=days_ago, hours=hours_ago)
    return {
        "id": trade_id,
        "symbol": symbol,
        "signal_type": signal_type,
        "entry_time": entered.isoformat(),
        "entry_price": 100.0,
        "quantity": 10.0,
    }


class TestZombie:
    def test_intraday_from_previous_session_is_zombie(self):
        zombie, reason = is_zombie(_trade(days_ago=1), now=NOW)
        assert zombie is True
        assert reason == "intraday_missed_eod"

    def test_intraday_same_session_is_not_zombie(self):
        zombie, _ = is_zombie(_trade(hours_ago=2), now=NOW)
        assert zombie is False

    def test_swing_beyond_horizon(self):
        zombie, reason = is_zombie(_trade(signal_type="SWING", days_ago=20), now=NOW)
        assert zombie is True
        assert reason == "swing_older_than_15d"

    def test_swing_within_horizon(self):
        zombie, _ = is_zombie(_trade(signal_type="SWING", days_ago=5), now=NOW)
        assert zombie is False

    def test_crypto_beyond_horizon(self):
        zombie, reason = is_zombie(_trade(symbol="BTC/USDT", days_ago=8), now=NOW)
        assert zombie is True
        assert reason == "crypto_older_than_7d"

    def test_crypto_within_horizon(self):
        zombie, _ = is_zombie(_trade(symbol="BTC/USDT", days_ago=2), now=NOW)
        assert zombie is False

    def test_missing_entry_time_is_unknown_not_zombie(self):
        zombie, reason = is_zombie({"id": 1, "symbol": "TCS"}, now=NOW)
        assert zombie is False
        assert reason == ""

    def test_naive_entry_time_is_assumed_ist(self):
        trade = _trade(hours_ago=3)
        trade["entry_time"] = trade["entry_time"].replace("+05:30", "")
        zombie, _ = is_zombie(trade, now=NOW)
        assert zombie is False

    def test_object_trades_supported(self):
        trade = SimpleNamespace(
            id=1,
            symbol="TCS",
            signal_type="INTRADAY",
            entry_time=(NOW - timedelta(days=2)).isoformat(),
        )
        zombie, _ = is_zombie(trade, now=NOW)
        assert zombie is True


class TestSelection:
    def test_intraday_mode_excludes_swing_and_crypto(self):
        trades = [
            _trade("TCS", "INTRADAY", days_ago=0, hours_ago=1, trade_id=1),
            _trade("INFY", "SWING", days_ago=1, trade_id=2),
            _trade("BTC/USDT", "INTRADAY", hours_ago=1, trade_id=3),
        ]
        picked = select_bulk_targets(trades, mode="intraday", now=NOW)
        assert [t["id"] for t in picked] == [1]

    def test_zombie_mode_picks_only_zombies(self):
        trades = [
            _trade("TCS", "INTRADAY", days_ago=1, trade_id=1),
            _trade("INFY", "INTRADAY", hours_ago=1, trade_id=2),
            _trade("BTC/USDT", "INTRADAY", days_ago=9, trade_id=3),
        ]
        picked = select_bulk_targets(trades, mode="zombie", now=NOW)
        assert {t["id"] for t in picked} == {1, 3}

    def test_all_mode_returns_everything(self):
        trades = [_trade(trade_id=1), _trade("BTC/USDT", trade_id=2)]
        assert len(select_bulk_targets(trades, mode="all", now=NOW)) == 2

    def test_ids_mode_matches(self):
        trades = [_trade(trade_id=1), _trade(trade_id=2), _trade(trade_id=3)]
        picked = select_bulk_targets(trades, mode="ids", ids=[1, 3], now=NOW)
        assert [t["id"] for t in picked] == [1, 3]

    def test_empty_input(self):
        assert select_bulk_targets([], mode="intraday", now=NOW) == []
