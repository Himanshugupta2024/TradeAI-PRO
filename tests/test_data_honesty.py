"""Tests for the data-honesty helpers (no FastAPI/DB required)."""

import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.data_honesty import (
    IST,
    apply_signal_freshness,
    disabled_scan_payload,
    empty_scan_payload,
    filter_valid_setups,
    mark_trade_pricing,
    signal_age_minutes,
    stale_threshold_minutes,
    validate_setup_levels,
)


class TestAge:
    def test_naive_timestamp_is_treated_as_ist(self):
        stamp = datetime(2026, 11, 6, 10, 0, 0)          # 10:00 IST
        now = datetime(2026, 11, 6, 4, 31, tzinfo=timezone.utc)  # 10:01 IST
        assert abs(signal_age_minutes(stamp, now) - 1.0) < 1e-6

    def test_aware_timestamp(self):
        stamp = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)
        now = datetime(2026, 11, 6, 4, 30, tzinfo=timezone.utc)
        assert signal_age_minutes(stamp, now) == 30.0

    def test_iso_string_with_z(self):
        age = signal_age_minutes(
            "2026-11-06T04:00:00Z", datetime(2026, 11, 6, 4, 10, tzinfo=timezone.utc)
        )
        assert age == 10.0

    def test_future_timestamp_clamps_to_zero(self):
        stamp = datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc)
        now = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)
        assert signal_age_minutes(stamp, now) == 0.0

    def test_missing_or_invalid(self):
        assert signal_age_minutes(None) is None
        assert signal_age_minutes("not-a-date") is None


class TestFreshness:
    def test_old_nse_signal_is_stale(self):
        sig = {"symbol": "SENSEX", "timestamp": "2026-11-03T13:45:00"}
        out = apply_signal_freshness(sig, now="2026-11-06T05:00:00Z")
        assert out["stale"] is True
        assert out["freshness"] == "stale"
        assert out["stale_minutes"] > 30

    def test_recent_nse_signal_is_fresh(self):
        sig = {"symbol": "NIFTY 50", "timestamp": "2026-11-06T10:20:00"}
        out = apply_signal_freshness(sig, now="2026-11-06T05:00:00Z")  # 10:30 IST
        assert out["stale"] is False
        assert out["freshness"] == "fresh"

    def test_crypto_gets_longer_budget(self):
        assert stale_threshold_minutes("BTC/USDT") == 60.0
        assert stale_threshold_minutes("RELIANCE") == 30.0
        sig = {"symbol": "BTC/USDT", "timestamp": "2026-11-06T09:30:00"}  # 45 min old
        out = apply_signal_freshness(sig, now="2026-11-06T04:45:00Z")
        assert out["stale"] is False

    def test_missing_timestamp_is_unknown_and_stale(self):
        out = apply_signal_freshness({"symbol": "TCS"}, now="2026-11-06T05:00:00Z")
        assert out["stale"] is True
        assert out["freshness"] == "unknown"
        assert out["stale_minutes"] is None

    def test_does_not_mutate_input(self):
        sig = {"symbol": "INFY", "timestamp": "2026-11-06T10:30:00"}
        apply_signal_freshness(sig, now="2026-11-06T05:00:00Z")
        assert "stale" not in sig


class TestSetupValidation:
    def test_valid_long(self):
        assert validate_setup_levels(100, 95, 110, "LONG") == (True, "")

    def test_valid_short(self):
        assert validate_setup_levels(100, 105, 90, "SHORT") == (True, "")

    def test_degenerate_levels_rejected(self):
        ok, reason = validate_setup_levels(0.07, 0.07, 0.07, "SHORT")
        assert ok is False
        assert reason == "degenerate_levels"

    def test_inconsistent_direction_rejected(self):
        ok, reason = validate_setup_levels(100, 105, 90, "LONG")
        assert ok is False
        assert reason == "inconsistent_levels"

    def test_missing_levels_rejected(self):
        assert validate_setup_levels(100, None, 110)[1] == "missing_or_invalid_levels"
        assert validate_setup_levels(0, 95, 110)[1] == "missing_or_invalid_levels"

    def test_unknown_direction_rejected(self):
        assert validate_setup_levels(100, 95, 110, "HOLD")[1] == "unknown_direction"

    def test_filter_splits_valid_and_rejected(self):
        setups = [
            {"symbol": "A", "signal": "LONG", "entry": 100, "stop_loss": 95, "target": 110},
            {"symbol": "B", "signal": "SHORT", "entry": 0.07, "stop_loss": 0.07, "target": 0.07},
            {"symbol": "C", "signal": "SHORT", "entry": 100, "stop_loss": 105, "target": 90},
        ]
        valid, rejected = filter_valid_setups(setups)
        assert [s["symbol"] for s in valid] == ["A", "C"]
        assert len(rejected) == 1
        assert rejected[0][0]["symbol"] == "B"
        assert rejected[0][1] == "degenerate_levels"


class TestTradePricing:
    def test_entry_fallback_is_unpriced(self):
        out = mark_trade_pricing({"is_entry_fallback": True, "unrealized_pnl": 0})
        assert out["priced"] is False
        assert out["pnl_status"] == "UNPRICED"

    def test_quote_source_fallback_is_unpriced(self):
        out = mark_trade_pricing({"quote_source": "ENTRY_FALLBACK"})
        assert out["priced"] is False

    def test_normal_trade_is_marked(self):
        out = mark_trade_pricing({"quote_source": "LIVE", "unrealized_pnl": 12.3})
        assert out["priced"] is True
        assert out["pnl_status"] == "MARKED"


class TestScanPayloads:
    def test_disabled_morning_scan_shape(self):
        payload = disabled_scan_payload("morning_scan", "no_scan_recorded")
        assert payload["status"] == "disabled"
        assert payload["reason"] == "no_scan_recorded"
        assert payload["results"] == []
        assert payload["count"] == 0

    def test_empty_screener_shape(self):
        payload = empty_scan_payload("screener_opportunities", "no_candidates")
        assert payload["status"] == "empty"
        assert payload["opportunities"] == []
