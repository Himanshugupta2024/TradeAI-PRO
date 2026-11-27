"""Unit tests for shared Binance REST routing and US HTTP 451 geo-block cooldown."""
from __future__ import annotations

import email.message
import logging
import urllib.error
from unittest.mock import patch

import requests

from agents.data_ingestion.binance_http import (
    fetch_binance_spot_json,
    is_host_in_cooldown,
    reset_binance_geo_cooldown,
    spot_candidate_urls,
)
from agents.data_ingestion.context_fetcher import ContextFetcher


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, f"HTTP {code}", email.message.Message(), None)


def test_spot_candidate_urls_prefers_vision_mirror():
    reset_binance_geo_cooldown()
    urls = spot_candidate_urls("/api/v3/ticker/24hr")
    assert urls == [
        "https://data-api.binance.vision/api/v3/ticker/24hr",
        "https://api.binance.com/api/v3/ticker/24hr",
    ]


def test_fetch_binance_spot_json_cooldown_skips_451_host(caplog):
    reset_binance_geo_cooldown()
    seen_urls: list[str] = []

    def fake_fetcher(url: str, timeout: float):
        seen_urls.append(url)
        if "api.binance.com" in url:
            raise _http_error(url, 451)
        if "data-api.binance.vision" in url:
            raise _http_error(url, 451)
        return []

    log = logging.getLogger("test_binance_http")
    with caplog.at_level(logging.WARNING):
        res1 = fetch_binance_spot_json("/api/v3/ticker/24hr", fetcher=fake_fetcher, log=log)
        assert res1 is None
        assert len(seen_urls) == 2

        # Second call during cooldown skips both blocked hosts completely
        res2 = fetch_binance_spot_json("/api/v3/ticker/24hr", fetcher=fake_fetcher, log=log)
        assert res2 is None
        assert len(seen_urls) == 2


def test_fapi_context_latches_451_cooldown_without_repeat_calls():
    import api.service as svc

    reset_binance_geo_cooldown()
    call_count = {"n": 0}

    def blocked_urlopen(url, timeout=6):
        call_count["n"] += 1
        raise _http_error(str(url), 451)

    with patch("urllib.request.urlopen", side_effect=blocked_urlopen):
        first = svc._fetch_binance_futures_context(["BTC/USDT", "ETH/USDT"])
        second = svc._fetch_binance_futures_context(["BTC/USDT", "ETH/USDT"])

    assert first == {}
    assert second == {}
    assert call_count["n"] == 1
    assert is_host_in_cooldown("https://fapi.binance.com") is True


def test_context_fetcher_falls_back_on_451_to_next_host():
    reset_binance_geo_cooldown()
    fetcher = ContextFetcher()
    calls: list[str] = []

    def fake_get(url: str, timeout: float = 10.0):
        calls.append(url)
        resp = requests.Response()
        resp.url = url
        if "data-api.binance.vision" in url:
            resp.status_code = 451
            raise requests.HTTPError("451 Client Error", response=resp)
        resp.status_code = 200
        resp._content = b'{"priceChangePercent":"3.4","volume":"10","highPrice":"100","lowPrice":"90","count":5}'
        return resp

    fetcher._http_get = fake_get
    parsed = fetcher._fetch_binance_ticker("BTCUSDT")
    assert parsed["price_change_pct"] == 3.4
    assert any("data-api.binance.vision" in u for u in calls)
    assert any("api.binance.com" in u for u in calls)
    assert is_host_in_cooldown("https://data-api.binance.vision") is True
    assert is_host_in_cooldown("https://api.binance.com") is False
