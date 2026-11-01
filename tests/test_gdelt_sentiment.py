"""
Unit tests for GDELTProvider (Targeted Sentiment & News Engine).
"""
import pytest
from unittest.mock import patch, MagicMock
from data.providers.gdelt import GDELTProvider


@pytest.mark.asyncio
async def test_gdelt_bullish_sentiment_analysis():
    provider = GDELTProvider(cache_ttl_sec=60.0)

    mock_gdelt_data = {
        "articles": [
            {"title": "Infosys posts massive Q3 profit growth and surge in deal wins"},
            {"title": "Tech stocks rally as INFY gains 4% on strong guidance"},
            {"title": "Infosys declares special dividend following record outperformance"},
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_gdelt_data

    with patch("requests.get", return_value=mock_resp):
        res = await provider.get_sentiment("INFY")

    assert res["symbol"] == "INFY"
    assert res["sentiment"] == "BULLISH"
    assert res["tone_score"] > 2.0
    assert res["confidence"] > 0.6
    assert res["article_count"] == 3
    assert len(res["top_headlines"]) == 3
    assert res["source"] == "GDELT_2.0"


@pytest.mark.asyncio
async def test_gdelt_bearish_sentiment_analysis():
    provider = GDELTProvider(cache_ttl_sec=60.0)

    mock_gdelt_data = {
        "articles": [
            {"title": "TCS shares plunge and slump following severe profit decline"},
            {"title": "Brokerage issues downgrade on TCS amid recession fears and loss"},
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_gdelt_data

    with patch("requests.get", return_value=mock_resp):
        res = await provider.get_sentiment("TCS")

    assert res["symbol"] == "TCS"
    assert res["sentiment"] == "BEARISH"
    assert res["tone_score"] < -2.0
    assert res["confidence"] > 0.6


@pytest.mark.asyncio
async def test_gdelt_rss_fallback_when_api_empty():
    provider = GDELTProvider(cache_ttl_sec=60.0)

    # GDELT returns 200 with 0 articles
    mock_gdelt_empty = MagicMock()
    mock_gdelt_empty.status_code = 200
    mock_gdelt_empty.json.return_value = {"articles": []}

    # Google News RSS returns valid XML
    rss_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
        <channel>
            <item><title>Reliance Industries gains 3% on new green energy deal</title></item>
            <item><title>RIL quarterly profit beats market expectations</title></item>
        </channel>
    </rss>"""
    mock_rss_resp = MagicMock()
    mock_rss_resp.status_code = 200
    mock_rss_resp.content = rss_xml

    def _mock_requests(url, **kwargs):
        if "gdeltproject" in url:
            return mock_gdelt_empty
        return mock_rss_resp

    with patch("requests.get", side_effect=_mock_requests):
        res = await provider.get_sentiment("RELIANCE")

    assert res["symbol"] == "RELIANCE"
    assert res["sentiment"] == "BULLISH"
    assert res["source"] == "GOOGLE_NEWS_RSS"
    assert res["article_count"] == 2


@pytest.mark.asyncio
async def test_gdelt_cache_avoids_repeated_requests():
    provider = GDELTProvider(cache_ttl_sec=100.0)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "articles": [{"title": "Tata Motors quarterly sales up"}]
    }

    with patch("requests.get", return_value=mock_resp) as mock_req:
        res1 = await provider.get_sentiment("TATAMOTORS")
        assert mock_req.call_count == 1

        # Second call hits memory cache
        res2 = await provider.get_sentiment("TATAMOTORS")
        assert mock_req.call_count == 1
        assert res1 == res2


@pytest.mark.asyncio
async def test_gdelt_batch_sentiment():
    provider = GDELTProvider(cache_ttl_sec=60.0)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "articles": [{"title": "Strong earnings surge for stock"}]
    }

    with patch("requests.get", return_value=mock_resp):
        results = await provider.get_batch_sentiment(["INFY", "TCS", "SBIN"])

    assert len(results) == 3
    assert "INFY" in results
    assert "TCS" in results
    assert "SBIN" in results
    assert results["INFY"]["sentiment"] == "BULLISH"
