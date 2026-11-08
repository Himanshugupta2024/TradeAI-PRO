from datetime import datetime, timedelta, timezone

from agents.market_intelligence import market_data


def _snapshot(fetched_at):
    return {
        "source": "NSE",
        "real_data": True,
        "stale": False,
        "date": "06-Nov-2026",
        "fetched_at": fetched_at.isoformat(),
        "flows": [
            {"category": "FII/FPI", "net_cr": -0.25},
            {"category": "DII", "net_cr": -700.31},
        ],
    }


def test_restore_tradeflow_cache_preserves_fetch_age(monkeypatch):
    monkeypatch.setattr(market_data, "_tradeflow_cache", {})
    monkeypatch.setattr(market_data, "_tradeflow_fetched_at", 0.0)
    fetched_at = datetime.now(timezone.utc) - timedelta(minutes=12)

    assert market_data.restore_tradeflow_cache(_snapshot(fetched_at)) is True

    restored = market_data._tradeflow_cache
    assert restored["source"] == "NSE"
    assert restored["flows"][0]["net_cr"] == -0.25
    assert restored["stale"] is True
    assert 11.9 <= restored["age_minutes"] <= 12.1
    assert abs(market_data._tradeflow_fetched_at - fetched_at.timestamp()) < 0.01


def test_restore_recent_tradeflow_cache_remains_fresh(monkeypatch):
    monkeypatch.setattr(market_data, "_tradeflow_cache", {})
    monkeypatch.setattr(market_data, "_tradeflow_fetched_at", 0.0)
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=60)

    assert market_data.restore_tradeflow_cache(_snapshot(fetched_at)) is True
    assert market_data._tradeflow_cache["stale"] is False
    assert 0.9 <= market_data._tradeflow_cache["age_minutes"] <= 1.1


def test_restore_tradeflow_cache_rejects_non_nse_or_incomplete_data(monkeypatch):
    monkeypatch.setattr(market_data, "_tradeflow_cache", {})
    monkeypatch.setattr(market_data, "_tradeflow_fetched_at", 0.0)

    assert market_data.restore_tradeflow_cache(None) is False
    assert market_data.restore_tradeflow_cache({"source": "OFFLINE", "real_data": False, "flows": []}) is False
    assert market_data._tradeflow_cache == {}


def test_failed_refresh_marks_last_known_tradeflow_stale(monkeypatch):
    fetched_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    snapshot = _snapshot(fetched_at)
    monkeypatch.setattr(market_data, "_tradeflow_cache", snapshot)
    monkeypatch.setattr(market_data, "_tradeflow_fetched_at", fetched_at.timestamp())

    def fail_nse_request(*args, **kwargs):
        raise OSError("NSE unavailable")

    monkeypatch.setattr(market_data.urllib.request, "urlopen", fail_nse_request)

    result = market_data.fetch_fii_dii_flow()

    assert result["stale"] is True
    assert result["source"] == "NSE"
    assert market_data._tradeflow_cache["stale"] is True
