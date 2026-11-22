# Phase 4 — API Surface Reference

This document provides a complete reverse-engineered API catalog for all HTTP and WebSocket endpoints exposed by the backend services and Cloudflare Edge Worker.

### Schema & Contract Overview
- **Gateway Entry**: `server.py:17` -> `api/app.py::create_app()` mounting master router `api/routes.py:40`.
- **Total Endpoints Documented**: 142 distinct application routes across 9 domain routers.
- **Edge Routing**: Cloudflare Worker (`src/worker.js:552`) proxies client traffic, evaluates cache eligibility, and provides degraded fallbacks during origin stalls.

---

## 1. Market Data, Quotes, Sectors & WebSocket (`api/routers/market.py`)
*Total Routes in Sub-Router*: 41

| Method | Path | Location | Auth | Request Params | Response Schema (Keys) | Engine | Upstream Dependencies | Fallback Mode | Edge Cache Policy |
|---|---|---|---|---|---|---|---|---|---|
| `GET` | `/` | [api/routers/market.py:2562](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/ai/ensemble/status` | [api/routers/market.py:361](file:///E:/trading/api/routers/market.py) | Public | None | `status`, `version`, `timestamp` | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/analytics/scorecard` | [api/routers/market.py:1723](file:///E:/trading/api/routers/market.py) | Public | Query: `bootstrap_if_empty` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `POST` | `/api/analytics/scorecard/bootstrap` | [api/routers/market.py:1746](file:///E:/trading/api/routers/market.py) | Public | Query: `force`, `min_trades` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/analytics/signals` | [api/routers/market.py:1765](file:///E:/trading/api/routers/market.py) | Public | Query: `limit` | `signals`, `count`, `as_of` | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/backtest/{symbol}` | [api/routers/market.py:1231](file:///E:/trading/api/routers/market.py) | Public | Path: `symbol`<br>Query: `days` | `dict` JSON Object | MarketDataRouter / Service | Yahoo Finance OHLCV, ML Sidecar (Box B) | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/context/events` | [api/routers/market.py:3447](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | ContextFetcher / SentimentEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/context/market-mood` | [api/routers/market.py:3594](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | ContextFetcher / SentimentEngine | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/context/v3` | [api/routers/market.py:339](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | ContextFetcher / SentimentEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/context/{symbol}` | [api/routers/market.py:3842](file:///E:/trading/api/routers/market.py) | Public | Path: `symbol` | `dict` JSON Object | ContextFetcher / SentimentEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/dashboard/overview` | [api/routers/market.py:409](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/fno/overview` | [api/routers/market.py:3909](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/fno/symbol/{symbol}` | [api/routers/market.py:3890](file:///E:/trading/api/routers/market.py) | Public | Path: `symbol`<br>Query: `spot`, `atr` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/market/mood` | [api/routers/market.py:1857](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | ContextFetcher / SentimentEngine | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/market/{symbol:path}` | [api/routers/market.py:825](file:///E:/trading/api/routers/market.py) | Public | Path: `symbol`<br>Query: `limit`, `interval` | `dict` JSON Object | MarketDataRouter / Service | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/news/{symbol}` | [api/routers/market.py:1393](file:///E:/trading/api/routers/market.py) | Public | Path: `symbol`<br>Query: `limit` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/nlp/summary` | [api/routers/market.py:1865](file:///E:/trading/api/routers/market.py) | Public | Query: `symbol` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/nse/news` | [api/routers/market.py:2605](file:///E:/trading/api/routers/market.py) | Public | Query: `limit` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/nse/prediction` | [api/routers/market.py:2851](file:///E:/trading/api/routers/market.py) | Public | None | `status`, `direction`, `probabilities`, `price_levels` | PredictionEngine (6-Layer Ensemble) | Yahoo Finance OHLCV, ML Sidecar (Box B) | Fallback to statistical heuristic (50.0% uncalibrated) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/overview/intelligence` | [api/routers/market.py:1915](file:///E:/trading/api/routers/market.py) | Public | Query: `force`, `refresh` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/regime/all` | [api/routers/market.py:597](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/regime/{symbol}` | [api/routers/market.py:1150](file:///E:/trading/api/routers/market.py) | Public | Path: `symbol` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/screener/gainers` | [api/routers/market.py:1522](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | StockScreener | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/screener/losers` | [api/routers/market.py:1597](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | StockScreener | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/screener/opportunities` | [api/routers/market.py:2178](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | StockScreener | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/search/latency` | [api/routers/market.py:2236](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/search/quote/{symbol}` | [api/routers/market.py:2252](file:///E:/trading/api/routers/market.py) | Public | Path: `symbol` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/search/symbols` | [api/routers/market.py:2027](file:///E:/trading/api/routers/market.py) | Public | Query: `q` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/sectors` | [api/routers/market.py:1672](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/signal/{symbol:path}` | [api/routers/market.py:1127](file:///E:/trading/api/routers/market.py) | Public | Path: `symbol` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/signals/all` | [api/routers/market.py:615](file:///E:/trading/api/routers/market.py) | Public | None | `signals`, `count`, `as_of` | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/signals/leaderboard` | [api/routers/market.py:760](file:///E:/trading/api/routers/market.py) | Public | None | `signals`, `count`, `as_of` | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/signals/winrate` | [api/routers/market.py:738](file:///E:/trading/api/routers/market.py) | Public | Query: `symbol` | `signals`, `count`, `as_of` | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/stats` | [api/routers/market.py:1176](file:///E:/trading/api/routers/market.py) | Public | None | `capital`, `total_equity`, `win_rate` | MarketDataRouter / Service | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Private (No-Store / Per-User State) |
| `GET` | `/api/symbols` | [api/routers/market.py:776](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/tradeflow` | [api/routers/market.py:1775](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/watchlist/quotes` | [api/routers/market.py:1014](file:///E:/trading/api/routers/market.py) | Public | Query: `symbols`, `force_refresh` | `symbol`, `ltp`, `change`, `change_pct` | MarketDataRouter / Service | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/landing` | [api/routers/market.py:2576](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/legal` | [api/routers/market.py:2594](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/legal.html` | [api/routers/market.py:2594](file:///E:/trading/api/routers/market.py) | Public | None | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `WEBSOCKET` | `/ws/prices` | [api/routers/market.py:1963](file:///E:/trading/api/routers/market.py) | Public | Query: `token` | `dict` JSON Object | MarketDataRouter / Service | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |

---

## 2. SwingSpectrum & Intraday Scanners (`api/routers/swing.py`)
*Total Routes in Sub-Router*: 17

| Method | Path | Location | Auth | Request Params | Response Schema (Keys) | Engine | Upstream Dependencies | Fallback Mode | Edge Cache Policy |
|---|---|---|---|---|---|---|---|---|---|
| `GET` | `/api/breakouts` | [api/routers/swing.py:1568](file:///E:/trading/api/routers/swing.py) | Public | Query: `min_strength` | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/index-mover/{index_name}` | [api/routers/swing.py:1500](file:///E:/trading/api/routers/swing.py) | Public | Path: `index_name` | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | In-Memory State / Local DB | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/intraday/latest` | [api/routers/swing.py:569](file:///E:/trading/api/routers/swing.py) | Public | None | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/intraday/live-quote/{symbol}` | [api/routers/swing.py:754](file:///E:/trading/api/routers/swing.py) | Public | Path: `symbol` | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `POST` | `/api/intraday/scan` | [api/routers/swing.py:431](file:///E:/trading/api/routers/swing.py) | `Admin API Key` (`X-Admin-Key`) | None | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/intraday/stats` | [api/routers/swing.py:778](file:///E:/trading/api/routers/swing.py) | Public | Query: `days` | `capital`, `total_equity`, `win_rate` | SwingScanner (SwingSpectrum Confluence) | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/intraday/symbol/{symbol}` | [api/routers/swing.py:708](file:///E:/trading/api/routers/swing.py) | Public | Path: `symbol` | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/morning-scan` | [api/routers/swing.py:1683](file:///E:/trading/api/routers/swing.py) | Public | None | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/swing-debug` | [api/routers/swing.py:1324](file:///E:/trading/api/routers/swing.py) | Public | Query: `symbol` | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fallback to pre-computed morning shortlist | Never-Cache (Edge Passthrough) |
| `GET` | `/api/swing-scanner` | [api/routers/swing.py:195](file:///E:/trading/api/routers/swing.py) | Public | None | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fallback to pre-computed morning shortlist | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/swing/history` | [api/routers/swing.py:373](file:///E:/trading/api/routers/swing.py) | Public | Query: `limit` | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fallback to pre-computed morning shortlist | Never-Cache (Edge Passthrough) |
| `GET` | `/api/swing/history/{scan_id}` | [api/routers/swing.py:387](file:///E:/trading/api/routers/swing.py) | Public | Path: `scan_id` | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fallback to pre-computed morning shortlist | Never-Cache (Edge Passthrough) |
| `GET` | `/api/swing/history/{symbol}` | [api/routers/swing.py:149](file:///E:/trading/api/routers/swing.py) | Public | Path: `symbol` | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fallback to pre-computed morning shortlist | Never-Cache (Edge Passthrough) |
| `POST` | `/api/swing/scan-now` | [api/routers/swing.py:411](file:///E:/trading/api/routers/swing.py) | Public | None | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fallback to pre-computed morning shortlist | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/swing/shortlist` | [api/routers/swing.py:63](file:///E:/trading/api/routers/swing.py) | Public | None | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fallback to pre-computed morning shortlist | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/swing/stats` | [api/routers/swing.py:401](file:///E:/trading/api/routers/swing.py) | Public | Query: `days` | `capital`, `total_equity`, `win_rate` | SwingScanner (SwingSpectrum Confluence) | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance, Supabase PostgreSQL, runtime_state DB | Fallback to pre-computed morning shortlist | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/swing/symbol/{symbol}` | [api/routers/swing.py:898](file:///E:/trading/api/routers/swing.py) | Public | Path: `symbol` | `dict` JSON Object | SwingScanner (SwingSpectrum Confluence) | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |

---

## 3. 6-Layer AI Price Prediction (`api/routers/prediction.py`)
*Total Routes in Sub-Router*: 4

| Method | Path | Location | Auth | Request Params | Response Schema (Keys) | Engine | Upstream Dependencies | Fallback Mode | Edge Cache Policy |
|---|---|---|---|---|---|---|---|---|---|
| `POST` | `/api/prediction/batch` | [api/routers/prediction.py:75](file:///E:/trading/api/routers/prediction.py) | Public | Body: `req` | `Dict` | PredictionEngine (6-Layer Ensemble) | Yahoo Finance OHLCV, ML Sidecar (Box B) | Stale Edge Cache Fallback (30s window) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/prediction/status` | [api/routers/prediction.py:32](file:///E:/trading/api/routers/prediction.py) | Public | None | `Dict` | PredictionEngine (6-Layer Ensemble) | Yahoo Finance OHLCV, ML Sidecar (Box B) | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/prediction/validation-metrics` | [api/routers/prediction.py:90](file:///E:/trading/api/routers/prediction.py) | Public | Query: `symbol` | `Dict` | PredictionEngine (6-Layer Ensemble) | Yahoo Finance OHLCV, ML Sidecar (Box B) | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/prediction/{symbol}` | [api/routers/prediction.py:53](file:///E:/trading/api/routers/prediction.py) | Public | Path: `symbol`<br>Query: `force` | `Dict` | PredictionEngine (6-Layer Ensemble) | Yahoo Finance OHLCV, ML Sidecar (Box B) | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |

---

## 4. Next-Day Picks & Quantitative Edge Radar (`api/routers/research.py`)
*Total Routes in Sub-Router*: 14

| Method | Path | Location | Auth | Request Params | Response Schema (Keys) | Engine | Upstream Dependencies | Fallback Mode | Edge Cache Policy |
|---|---|---|---|---|---|---|---|---|---|
| `GET` | `/api/edge/macro-heatmap` | [api/routers/research.py:406](file:///E:/trading/api/routers/research.py) | Public | Query: `force_refresh` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/edge/research/{symbol}` | [api/routers/research.py:363](file:///E:/trading/api/routers/research.py) | Public | Path: `symbol`<br>Query: `asset_class` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/edge/risk-gauge` | [api/routers/research.py:541](file:///E:/trading/api/routers/research.py) | Public | Query: `force_refresh` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/edge/score/{symbol:path}` | [api/routers/research.py:104](file:///E:/trading/api/routers/research.py) | Public | Path: `symbol`<br>Query: `force_refresh` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/edge/top-setups` | [api/routers/research.py:234](file:///E:/trading/api/routers/research.py) | Public | Query: `asset_class`, `min_score`, `limit`, `force_refresh` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/macro/intelligence` | [api/routers/research.py:511](file:///E:/trading/api/routers/research.py) | Public | Query: `force_refresh` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/macro/sector-impact` | [api/routers/research.py:524](file:///E:/trading/api/routers/research.py) | Public | Query: `symbol`, `direction` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/nse/dashboard` | [api/routers/research.py:647](file:///E:/trading/api/routers/research.py) | Public | None | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/research/next-day-picks` | [api/routers/research.py:45](file:///E:/trading/api/routers/research.py) | Public | None | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `POST` | `/api/research/next-day-picks/generate` | [api/routers/research.py:51](file:///E:/trading/api/routers/research.py) | Public | Body: `req` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/research/next-day-picks/history` | [api/routers/research.py:67](file:///E:/trading/api/routers/research.py) | Public | None | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/research/next-day-picks/latest` | [api/routers/research.py:74](file:///E:/trading/api/routers/research.py) | Public | None | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `POST` | `/api/research/next-day-picks/resolve` | [api/routers/research.py:89](file:///E:/trading/api/routers/research.py) | Public | Body: `req` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/research/next-day-picks/{report_id}` | [api/routers/research.py:80](file:///E:/trading/api/routers/research.py) | Public | Path: `report_id` | `dict` JSON Object | NextDayPicksEngine | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |

---

## 5. Paper Trading, Portfolio & Performance (`api/routers/portfolio.py`)
*Total Routes in Sub-Router*: 15

| Method | Path | Location | Auth | Request Params | Response Schema (Keys) | Engine | Upstream Dependencies | Fallback Mode | Edge Cache Policy |
|---|---|---|---|---|---|---|---|---|---|
| `GET` | `/api/ai/weights` | [api/routers/portfolio.py:153](file:///E:/trading/api/routers/portfolio.py) | Public | None | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `POST` | `/api/paper/capital` | [api/routers/portfolio.py:637](file:///E:/trading/api/routers/portfolio.py) | `Admin API Key` (`X-Admin-Key`) | Body: `payload` | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `POST` | `/api/paper/close-bulk` | [api/routers/portfolio.py:575](file:///E:/trading/api/routers/portfolio.py) | `Admin API Key` (`X-Admin-Key`) | Body: `payload` | `confirm` | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `POST` | `/api/paper/close/{trade_id}` | [api/routers/portfolio.py:525](file:///E:/trading/api/routers/portfolio.py) | `Admin API Key` (`X-Admin-Key`) | Path: `trade_id`<br>Body: `payload` | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/paper/open` | [api/routers/portfolio.py:501](file:///E:/trading/api/routers/portfolio.py) | `Account Auth` (`X-Account-ID` / Bearer) | None | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `POST` | `/api/paper/reset` | [api/routers/portfolio.py:651](file:///E:/trading/api/routers/portfolio.py) | `Admin API Key` (`X-Admin-Key`) | Query: `capital` | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `POST` | `/api/paper/resolve` | [api/routers/portfolio.py:281](file:///E:/trading/api/routers/portfolio.py) | `Admin API Key` (`X-Admin-Key`) | None | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/paper/scorecard` | [api/routers/portfolio.py:187](file:///E:/trading/api/routers/portfolio.py) | Public | None | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `POST` | `/api/paper/trade` | [api/routers/portfolio.py:317](file:///E:/trading/api/routers/portfolio.py) | `Admin API Key` (`X-Admin-Key`) | Body: `payload` | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/performance` | [api/routers/portfolio.py:1366](file:///E:/trading/api/routers/portfolio.py) | Public | None | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/portfolio` | [api/routers/portfolio.py:761](file:///E:/trading/api/routers/portfolio.py) | `Account Auth` (`X-Account-ID` / Bearer) | None | `dict` JSON Object | PaperTradingManager | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Private (No-Store / Per-User State) |
| `POST` | `/api/risk/reset-circuit` | [api/routers/portfolio.py:171](file:///E:/trading/api/routers/portfolio.py) | `Admin API Key` (`X-Admin-Key`) | Query: `rebase_capital` | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/risk/state` | [api/routers/portfolio.py:143](file:///E:/trading/api/routers/portfolio.py) | Public | None | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/track-record` | [api/routers/portfolio.py:108](file:///E:/trading/api/routers/portfolio.py) | Public | Query: `limit` | `dict` JSON Object | PaperTradingManager | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/trades` | [api/routers/portfolio.py:68](file:///E:/trading/api/routers/portfolio.py) | `Account Auth` (`X-Account-ID` / Bearer) | Query: `limit` | `dict` JSON Object | PaperTradingManager | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Private (No-Store / Per-User State) |

---

## 6. Admin Controls, Autonomous Worker & Model Sync (`api/routers/admin.py`)
*Total Routes in Sub-Router*: 28

| Method | Path | Location | Auth | Request Params | Response Schema (Keys) | Engine | Upstream Dependencies | Fallback Mode | Edge Cache Policy |
|---|---|---|---|---|---|---|---|---|---|
| `GET` | `/admin` | [api/routers/admin.py:1206](file:///E:/trading/api/routers/admin.py) | Public | None | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/admin.html` | [api/routers/admin.py:1206](file:///E:/trading/api/routers/admin.py) | Public | None | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/admin/` | [api/routers/admin.py:1206](file:///E:/trading/api/routers/admin.py) | Public | None | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `POST` | `/api/admin/kill-switch` | [api/routers/admin.py:1182](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | Body: `payload` | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/admin/runtime-controls` | [api/routers/admin.py:1120](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | None | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Private (No-Store / Per-User State) |
| `POST` | `/api/admin/runtime-controls` | [api/routers/admin.py:1129](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | Body: `payload` | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `POST` | `/api/admin/runtime-controls/reset` | [api/routers/admin.py:1165](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | Body: `payload` | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/autonomous/feed` | [api/routers/admin.py:494](file:///E:/trading/api/routers/admin.py) | Public | Query: `limit` | `dict` JSON Object | AutonomousTradingDaemon | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `POST` | `/api/autonomous/review-now` | [api/routers/admin.py:679](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | None | `dict` JSON Object | AutonomousTradingDaemon | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `POST` | `/api/autonomous/run-cycle` | [api/routers/admin.py:547](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | Body: `payload` | `dict` JSON Object | AutonomousTradingDaemon | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/autonomous/status` | [api/routers/admin.py:384](file:///E:/trading/api/routers/admin.py) | Public | None | `status`, `version`, `timestamp` | AutonomousTradingDaemon | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `POST` | `/api/autonomous/toggle` | [api/routers/admin.py:659](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | Body: `payload` | `dict` JSON Object | AutonomousTradingDaemon | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `POST` | `/api/cron/refresh` | [api/routers/admin.py:1066](file:///E:/trading/api/routers/admin.py) | Public | None | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/ensemble/status` | [api/routers/admin.py:897](file:///E:/trading/api/routers/admin.py) | Public | None | `status`, `version`, `timestamp` | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/ensemble/validate` | [api/routers/admin.py:853](file:///E:/trading/api/routers/admin.py) | Public | Query: `symbol`, `timeframe`, `days`, `n_folds`, `run` | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/learning/outcomes` | [api/routers/admin.py:793](file:///E:/trading/api/routers/admin.py) | Public | Query: `limit` | `dict` JSON Object | Core FastAPIService | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/learning/stats` | [api/routers/admin.py:738](file:///E:/trading/api/routers/admin.py) | Public | None | `capital`, `total_equity`, `win_rate` | Core FastAPIService | Supabase PostgreSQL, runtime_state DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/market-control/focus` | [api/routers/admin.py:164](file:///E:/trading/api/routers/admin.py) | Public | None | `dict` JSON Object | Core FastAPIService | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `POST` | `/api/market-control/focus` | [api/routers/admin.py:171](file:///E:/trading/api/routers/admin.py) | `Header Auth` (`x_admin_key`) | Body: `req` | `dict` JSON Object | Core FastAPIService | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/market-control/status` | [api/routers/admin.py:83](file:///E:/trading/api/routers/admin.py) | Public | None | `status`, `version`, `timestamp` | Core FastAPIService | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `POST` | `/api/market-control/toggle` | [api/routers/admin.py:90](file:///E:/trading/api/routers/admin.py) | `Header Auth` (`x_admin_key`) | Body: `req` | `dict` JSON Object | Core FastAPIService | NSE Bhavcopy (Archives), Shoonya Feed (Box C), Yahoo Finance | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/models/status` | [api/routers/admin.py:702](file:///E:/trading/api/routers/admin.py) | Public | None | `status`, `version`, `timestamp` | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `POST` | `/api/models/sync` | [api/routers/admin.py:726](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | Body: `payload` | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `POST` | `/api/refresh` | [api/routers/admin.py:1054](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | None | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `POST` | `/api/retrain` | [api/routers/admin.py:976](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | Query: `symbols`, `timeframe`, `days` | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |
| `GET` | `/api/retrain/status` | [api/routers/admin.py:1048](file:///E:/trading/api/routers/admin.py) | Public | None | `status`, `version`, `timestamp` | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/telegram/status` | [api/routers/admin.py:237](file:///E:/trading/api/routers/admin.py) | Public | None | `status`, `version`, `timestamp` | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `POST` | `/api/telegram/test` | [api/routers/admin.py:250](file:///E:/trading/api/routers/admin.py) | `Admin API Key` (`X-Admin-Key`) | None | `dict` JSON Object | Core FastAPIService | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Mutation / Non-GET) |

---

## 7. Crypto Radar, Intelligence & Binance Proxy (`api/routers/crypto.py`)
*Total Routes in Sub-Router*: 8

| Method | Path | Location | Auth | Request Params | Response Schema (Keys) | Engine | Upstream Dependencies | Fallback Mode | Edge Cache Policy |
|---|---|---|---|---|---|---|---|---|---|
| `GET` | `/api/crypto/intelligence` | [api/routers/crypto.py:1619](file:///E:/trading/api/routers/crypto.py) | Public | None | `dict` JSON Object | CryptoEngine (Binance / Fear&Greed) | Binance Public REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/crypto/meme` | [api/routers/crypto.py:128](file:///E:/trading/api/routers/crypto.py) | Public | None | `dict` JSON Object | CryptoEngine (Binance / Fear&Greed) | Binance Public REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/crypto/news` | [api/routers/crypto.py:1381](file:///E:/trading/api/routers/crypto.py) | Public | Query: `limit` | `dict` JSON Object | CryptoEngine (Binance / Fear&Greed) | Binance Public REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/crypto/prediction` | [api/routers/crypto.py:1836](file:///E:/trading/api/routers/crypto.py) | Public | Query: `timeframe` | `status`, `direction`, `probabilities`, `price_levels` | PredictionEngine (6-Layer Ensemble) | Binance Public REST, Yahoo Finance OHLCV, ML Sidecar (Box B) | Fallback to statistical heuristic (50.0% uncalibrated) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/crypto/proxy/binance/24hr` | [api/routers/crypto.py:1692](file:///E:/trading/api/routers/crypto.py) | Public | Query: `symbol` | `dict` JSON Object | CryptoEngine (Binance / Fear&Greed) | Binance Public REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/crypto/proxy/binance/klines` | [api/routers/crypto.py:1792](file:///E:/trading/api/routers/crypto.py) | Public | Query: `symbol`, `interval`, `limit` | `dict` JSON Object | CryptoEngine (Binance / Fear&Greed) | Binance Public REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/crypto` | [api/routers/crypto.py:54](file:///E:/trading/api/routers/crypto.py) | Public | None | `dict` JSON Object | CryptoEngine (Binance / Fear&Greed) | Binance Public REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/crypto/` | [api/routers/crypto.py:54](file:///E:/trading/api/routers/crypto.py) | Public | None | `dict` JSON Object | CryptoEngine (Binance / Fear&Greed) | Binance Public REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |

---

## 8. Twelve Data Forex Radar & Sessions (`api/routers/forex.py`)
*Total Routes in Sub-Router*: 6

| Method | Path | Location | Auth | Request Params | Response Schema (Keys) | Engine | Upstream Dependencies | Fallback Mode | Edge Cache Policy |
|---|---|---|---|---|---|---|---|---|---|
| `GET` | `/api/forex/intelligence` | [api/routers/forex.py:150](file:///E:/trading/api/routers/forex.py) | Public | None | `dict` JSON Object | ForexScanner (Twelve Data) | Twelve Data REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/forex/ohlcv/{symbol}` | [api/routers/forex.py:332](file:///E:/trading/api/routers/forex.py) | Public | Path: `symbol`<br>Query: `interval`, `bars` | `dict` JSON Object | ForexScanner (Twelve Data) | Twelve Data REST | Stale Edge Cache Fallback (30s window) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/forex/rates` | [api/routers/forex.py:54](file:///E:/trading/api/routers/forex.py) | Public | None | `dict` JSON Object | ForexScanner (Twelve Data) | Twelve Data REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/forex/sessions` | [api/routers/forex.py:238](file:///E:/trading/api/routers/forex.py) | Public | None | `dict` JSON Object | ForexScanner (Twelve Data) | Twelve Data REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |
| `GET` | `/api/forex/signal/{symbol}` | [api/routers/forex.py:192](file:///E:/trading/api/routers/forex.py) | Public | Path: `symbol` | `dict` JSON Object | ForexScanner (Twelve Data) | Twelve Data REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/forex/signals` | [api/routers/forex.py:86](file:///E:/trading/api/routers/forex.py) | Public | None | `signals`, `count`, `as_of` | ForexScanner (Twelve Data) | Twelve Data REST | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Cacheable (SWR Edge Cache 15s/30s) |

---

## 9. Health Probes, Liveness & Telemetry (`api/routers/health.py`)
*Total Routes in Sub-Router*: 9

| Method | Path | Location | Auth | Request Params | Response Schema (Keys) | Engine | Upstream Dependencies | Fallback Mode | Edge Cache Policy |
|---|---|---|---|---|---|---|---|---|---|
| `GET` | `/api/health` | [api/routers/health.py:87](file:///E:/trading/api/routers/health.py) | Public | None | `status`, `database`, `uptime_seconds` | HealthV2 / SystemTelemetry | Local OS / psutil | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Health/Liveness Probe Bypass) |
| `GET` | `/api/health/live` | [api/routers/health.py:514](file:///E:/trading/api/routers/health.py) | Public | None | `status`, `database`, `uptime_seconds` | HealthV2 / SystemTelemetry | Local OS / psutil | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Health/Liveness Probe Bypass) |
| `GET` | `/api/health/providers` | [api/routers/health.py:532](file:///E:/trading/api/routers/health.py) | Public | None | `status`, `database`, `uptime_seconds` | HealthV2 / SystemTelemetry | Local OS / psutil | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Health/Liveness Probe Bypass) |
| `GET` | `/api/health/ready` | [api/routers/health.py:527](file:///E:/trading/api/routers/health.py) | Public | None | `status`, `database`, `uptime_seconds` | HealthV2 / SystemTelemetry | Local OS / psutil | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Health/Liveness Probe Bypass) |
| `GET` | `/api/ready` | [api/routers/health.py:255](file:///E:/trading/api/routers/health.py) | Public | None | `dict` JSON Object | HealthV2 / SystemTelemetry | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Health/Liveness Probe Bypass) |
| `GET` | `/api/system/data-health` | [api/routers/health.py:345](file:///E:/trading/api/routers/health.py) | Public | None | `dict` JSON Object | HealthV2 / SystemTelemetry | Local OS / psutil | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Health/Liveness Probe Bypass) |
| `GET` | `/api/system/memory` | [api/routers/health.py:308](file:///E:/trading/api/routers/health.py) | Public | None | `dict` JSON Object | HealthV2 / SystemTelemetry | Local OS / psutil | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/api/warmup` | [api/routers/health.py:550](file:///E:/trading/api/routers/health.py) | Public | None | `dict` JSON Object | HealthV2 / SystemTelemetry | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |
| `GET` | `/favicon.ico` | [api/routers/health.py:69](file:///E:/trading/api/routers/health.py) | Public | None | `dict` JSON Object | HealthV2 / SystemTelemetry | In-Memory State / Local DB | Fail-Closed (HTTP 400 / 404 / 500 / 503) | Never-Cache (Edge Passthrough) |

---

## 10. Worker Edge Cache Audit & Route Discrepancies

### 10.1 Orphaned Cache Keys in `src/worker.js`
The following paths are explicitly declared in `PUBLIC_CACHEABLE_PATHS` (`src/worker.js:64-110`) but **do not exist** in the FastAPI route table:

- ⚠️ **`/api/section-6b/picks`**: Registered in `src/worker.js:109` as cacheable, but does not exist in FastAPI backend. Refactored into `/api/research/next-day-picks` (`api/routers/research.py:48`). Stale entry in Worker set.

### 10.2 Uncached Public Read-Only GET Endpoints
The following 51 public GET endpoints are **not** present in `PUBLIC_CACHEABLE_PATHS` nor matched by prefix rules in `isPathCacheable()` (`src/worker.js:129-145`). Every request to these endpoints bypasses the Cloudflare cache and directly burdens Render origin CPU:

- `/`
- `/admin`
- `/admin.html`
- `/admin/`
- `/api/ai/ensemble/status`
- `/api/ai/weights`
- `/api/analytics/signals`
- `/api/context/{symbol}`
- `/api/crypto/meme`
- `/api/crypto/news`
- `/api/crypto/prediction`
- `/api/crypto/proxy/binance/24hr`
- `/api/crypto/proxy/binance/klines`
- `/api/edge/research/{symbol}`
- `/api/edge/score/{symbol:path}`
- `/api/ensemble/status`
- `/api/ensemble/validate`
- `/api/fno/overview`
- `/api/fno/symbol/{symbol}`
- `/api/forex/signal/{symbol}`
- `/api/intraday/live-quote/{symbol}`
- `/api/intraday/stats`
- `/api/intraday/symbol/{symbol}`
- `/api/macro/sector-impact`
- `/api/market/{symbol:path}`
- `/api/models/status`
- `/api/nlp/summary`
- `/api/nse/news`
- `/api/paper/scorecard`
- `/api/regime/all`
- `/api/regime/{symbol}`
- `/api/research/next-day-picks/latest`
- `/api/research/next-day-picks/{report_id}`
- `/api/retrain/status`
- `/api/risk/state`
- `/api/signal/{symbol:path}`
- `/api/swing-debug`
- `/api/swing/history`
- `/api/swing/history/{scan_id}`
- `/api/swing/history/{symbol}`
- `/api/symbols`
- `/api/system/memory`
- `/api/telegram/status`
- `/api/track-record`
- `/api/warmup`
- `/crypto`
- `/crypto/`
- `/favicon.ico`
- `/landing`
- `/legal`
- `/legal.html`

### 10.3 Health & Liveness Probe Bypass Contract
In accordance with `src/worker.js:119-127`, health probes (`/api/health`, `/api/ready`, `/api/system/data-health`) strictly bypass the edge cache with headers:
```http
Cache-Control: no-store, no-cache, must-revalidate, max-age=0
```
This guarantees that Cloudflare edge never masks origin cold starts or crashes with stale HTTP 200 responses.

### 10.4 Private State Isolation Contract
User portfolio state, paper trading records, and admin endpoints (`src/worker.js:150-180`) strictly enforce `isPrivateStatePath()`, returning:
```http
Cache-Control: private, no-cache, no-store, must-revalidate
```
This guarantees that multi-user paper trading portfolios and open positions never leak across browser sessions via shared edge caching.