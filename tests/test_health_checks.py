"""Tests for the production health probe (pure evaluators, no network)."""

import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.check_production_health import (
    evaluate_asset_probe,
    evaluate_docs,
    evaluate_headers,
    evaluate_health,
    evaluate_setups,
    evaluate_signals,
    summarize,
)


def _levels(results):
    return [item["level"] for item in results]


class TestHealth:
    def test_healthy_payload(self):
        payload = {
            "healthy": True,
            "database": {"connected": True},
            "symbols_loaded": 15,
            "worker_health": "OK",
        }
        results = evaluate_health(200, payload)
        assert set(_levels(results)) == {"ok"}
        assert summarize(results)[0] == 0

    def test_database_down_is_critical(self):
        payload = {
            "healthy": False,
            "database": {"connected": False},
            "symbols_loaded": 15,
            "worker_health": "OK",
        }
        results = evaluate_health(200, payload)
        assert "critical" in _levels(results)
        assert summarize(results) == (2, "CRITICAL")

    def test_no_recent_worker_run_is_warning(self):
        payload = {
            "healthy": True,
            "database": {"connected": True},
            "symbols_loaded": 15,
            "worker_health": "NO_RECENT_RUN",
        }
        results = evaluate_health(200, payload)
        assert "warn" in _levels(results)
        assert summarize(results) == (1, "DEGRADED")

    def test_zero_symbols_is_critical(self):
        payload = {
            "healthy": True,
            "database": {"connected": True},
            "symbols_loaded": 0,
            "worker_health": "OK",
        }
        assert "critical" in _levels(evaluate_health(200, payload))

    def test_http_error_is_critical(self):
        assert evaluate_health(500, {})[0]["level"] == "critical"


class TestSignals:
    NOW = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)

    def test_fresh_signal(self):
        payload = {"signals": [{"symbol": "TCS", "timestamp": "2026-11-06T05:50:00Z", "stale": False}]}
        results = evaluate_signals(200, payload, now=self.NOW)
        assert "critical" not in _levels(results)

    def test_old_signal_flagged_stale_is_only_a_warning(self):
        payload = {"signals": [{"symbol": "SENSEX", "timestamp": "2026-11-06T04:00:00Z", "stale": True}]}
        results = evaluate_signals(200, payload, now=self.NOW, max_age_min=45)
        assert "warn" in _levels(results)
        assert "critical" not in _levels(results)

    def test_old_signal_not_flagged_is_critical(self):
        payload = {"signals": [{"symbol": "SENSEX", "timestamp": "2026-11-03T13:45:00Z", "stale": False}]}
        results = evaluate_signals(200, payload, now=self.NOW)
        assert "critical" in _levels(results)

    def test_no_signals_is_warning(self):
        assert evaluate_signals(200, {"signals": []})[0]["level"] == "warn"


class TestSetups:
    def test_valid_setup(self):
        payload = {"top_setups": [
            {"symbol": "XRP", "signal": "SHORT", "entry": 1.24, "stop_loss": 1.26, "target": 1.20}
        ]}
        assert evaluate_setups(200, payload)[0]["level"] == "ok"

    def test_degenerate_setup_is_critical(self):
        payload = {"top_setups": [
            {"symbol": "DOGE", "signal": "SHORT", "entry": 0.07, "stop_loss": 0.07, "target": 0.07}
        ]}
        results = evaluate_setups(200, payload)
        assert results[0]["level"] == "critical"
        assert "invalid risk geometry" in results[0]["detail"]


class TestAssetsDocsHeaders:
    def test_missing_asset_404_ok(self):
        assert evaluate_asset_probe(404, "text/plain")[0]["level"] == "ok"

    def test_missing_asset_html_is_critical(self):
        assert evaluate_asset_probe(200, "text/html")[0]["level"] == "critical"

    def test_docs_closed(self):
        assert evaluate_docs(404)[0]["level"] == "ok"

    def test_docs_public_is_warning(self):
        assert evaluate_docs(200)[0]["level"] == "warn"

    def test_headers_present(self):
        headers = {"Content-Security-Policy": "default-src 'self'", "Strict-Transport-Security": "max-age=1"}
        assert evaluate_headers(headers)[0]["level"] == "ok"

    def test_headers_missing_csp(self):
        assert evaluate_headers({"Strict-Transport-Security": "max-age=1"})[0]["level"] == "warn"
