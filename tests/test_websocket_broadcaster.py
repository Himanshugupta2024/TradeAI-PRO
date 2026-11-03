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
