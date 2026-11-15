"""
Unit tests for SnapshotBroadcaster and WebSocket origin validation.
"""

import pytest
from fastapi.testclient import TestClient
from api.app import create_app
import api.service as svc


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_broadcaster_builds_valid_payload():
    import asyncio
    payload = asyncio.run(svc.broadcaster.get_initial_payload())
    assert isinstance(payload, dict)
    assert payload.get("type") == "price_update"
    assert "timestamp" in payload
    assert "prices" in payload
    assert "stats" in payload
    assert "source" in payload


def test_websocket_accepts_approved_origin(client):
    headers = {"Origin": "https://tradeai-pro.pages.dev"}
    with client.websocket_connect("/ws/prices", headers=headers) as ws:
        data = ws.receive_json()
        assert data["type"] == "price_update"
        assert "prices" in data
        assert "stats" in data


def test_websocket_rejects_unauthorized_origin(client):
    headers = {"Origin": "https://attacker.pages.dev"}
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/prices", headers=headers) as ws:
            ws.receive_json()


def test_build_payload_survives_market_data_mutation_during_iteration(monkeypatch):
    """
    Regression: the in-process fallback walked ``_market_data`` through a live dict
    view while downloader threads insert symbols into the same mapping. An insert
    during that walk raised "dictionary changed size during iteration", which
    aborted the whole broadcast cycle and silently dropped the tick for every
    connected WebSocket client. ``_build_payload`` now snapshots the mapping first.
    """
    import asyncio

    import pandas as pd

    import db.runtime_state as runtime_state

    bar = pd.DataFrame(
        [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "signal": "HOLD"}]
    )

    def make_store() -> dict:
        """Mapping whose first ``.empty`` probe inserts a symbol mid-iteration."""
        state = {"probes": 0}
        store: dict = {}

        class MutatingFrame(pd.DataFrame):
            @property
            def empty(self):
                state["probes"] += 1
                if state["probes"] == 1:
                    # Simulate a downloader thread publishing a new symbol while
                    # the broadcaster is walking the mapping.
                    store["INTRUDER"] = pd.DataFrame([dict(bar.iloc[0])])
                return False

        store["RELIANCE"] = MutatingFrame(bar.copy())
        return store

    # Sanity check: the fixture really does break a naive live-view walk, so the
    # assertion below is not vacuous.
    naive_store = make_store()
    with pytest.raises(RuntimeError, match="dictionary changed size during iteration"):
        for _sym, _df in naive_store.items():
            assert not _df.empty

    # Force the in-process fallback branch: no durable snapshot available.
    monkeypatch.setattr(runtime_state, "get_runtime_state", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_get_feed_status", lambda: {"status": "test"})
    monkeypatch.setattr(svc, "_compute_market_mood", lambda: {"mood": "NEUTRAL"})
    monkeypatch.setattr(
        svc,
        "_get_engine",
        lambda: type(
            "StubEngine",
            (),
            {
                "get_stats": lambda self, current_prices=None: {"total_equity": 100000.0},
                "get_open_trades": lambda self: [],
            },
        )(),
    )

    store = make_store()
    monkeypatch.setattr(svc, "_market_data", store)

    payload = asyncio.run(svc.broadcaster._build_payload())

    assert payload["source"] == "IN_PROCESS"
    assert "RELIANCE" in payload["prices"]
    assert payload["prices"]["RELIANCE"]["close"] == 100.5
    assert "INTRUDER" in store, "fixture must mutate mid-walk for this test to mean anything"

