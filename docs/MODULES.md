# Phase 2 — Module Inventory

This document provides a reverse-engineered architectural audit of every top-level directory and core module in the repository.

### Component Classification States
- **`DEFINED`**: Exists in the repository but has zero non-test callers (or only referenced in test suites).
- **`WIRED`**: Imported or invoked by active application code or offline operational tooling (callers cited).
- **`DEPLOYED`**: Actively reachable under the production multi-service deployment topology:
  - **Edge**: Cloudflare Worker (`src/worker.js`, `wrangler.jsonc:2`)
  - **Box A (`trading-api`)**: Core FastAPI gateway (`server.py:17`, `render.yaml:13`)
  - **Box B (`trading-ml`)**: ML Inference Sidecar (`ml_server.py:16`, `render.yaml:108`)
  - **Box C (`trading-feed`)**: Shoonya Market-Data Ingest (`feed_server.py:28`, `render.yaml:141`)
  - **CI/CD Crons**: Active GitHub Actions workflows (`.github/workflows/*.yml`)

---

## 1. Top-Level Directory Inventory

### 1.1 `agents/` — Algorithmic Intelligence & Execution Agents
The `agents` directory is the core algorithmic intelligence hub. It contains specialized domain packages for ML model training and inference (`ai_engine`), high-confluence swing scanning (`market_intelligence`), intraday session execution (`intraday_engine`), mathematical indicator/feature engineering (`feature_engine`), rule-based signal derivation (`signal_engine`), paper trading execution and market focus controls (`execution`), backtesting engines (`backtester`), macroeconomic/COT/sentiment radar (`macro`), external market data fetchers (`data_ingestion`), and telemetry/alerts (`analytics`, `alerts`, `notifications`).

| Submodule / Key File | Defined At | Role | State | Primary Caller / Entry Point |
|---|---|---|---|---|
| `stock_screener.py` | `agents/stock_screener.py:72` | Multi-factor equity screener (gainers, losers, breakout filters) | **DEPLOYED** | `api/app.py:163`, `api/routers/market.py:141` |
| `ai_engine/ensemble.py` | `agents/ai_engine/ensemble.py:384` | 6-layer ML ensemble orchestrator (XGBoost, LightGBM, Logistic, Calibrator, Sidecar fallback) | **DEPLOYED** | `api/service.py:24`, `api/routers/health.py:103` |
| `ai_engine/model_registry.py` | `agents/ai_engine/model_registry.py:18` | In-memory registry for loaded ML models, scalers, and metadata | **DEPLOYED** | `ml_server.py:32`, `api/app.py:254` |
| `ai_engine/xgboost_model.py` | `agents/ai_engine/xgboost_model.py:680` | XGBoost gradient boosted tree inference with feature column alignment | **DEPLOYED** | `ml_server.py:20`, `agents/ai_engine/ensemble.py:488` |
| `ai_engine/lightgbm_model.py` | `agents/ai_engine/lightgbm_model.py:417` | LightGBM classifier inference with feature column alignment | **DEPLOYED** | `ml_server.py:21`, `agents/ai_engine/ensemble.py:504` |
| `ai_engine/logistic_baseline.py` | `agents/ai_engine/logistic_baseline.py:18` | Baseline logistic regression model with StandardScaler transform | **DEPLOYED** | `ml_server.py:22`, `agents/ai_engine/ensemble.py:574` |
| `ai_engine/calibrator.py` | `agents/ai_engine/calibrator.py:35` | Isotonic probability calibration & Brier score calculation | **DEPLOYED** | `ml_server.py:25`, `agents/ai_engine/ensemble.py:706` |
| `ai_engine/fallback_predictor.py` | `agents/ai_engine/fallback_predictor.py:529` | Statistical heuristic fallback predictor for Render Free lightweight mode | **DEPLOYED** | `api/service.py:25`, `api/routers/market.py:2351` |
| `ai_engine/prediction_engine.py` | `agents/ai_engine/prediction_engine.py:47` | Multi-horizon forecast synthesizer for `/api/prediction/{symbol}` | **DEPLOYED** | `api/routers/prediction.py:19` |
| `ai_engine/meme_predictor.py` | `agents/ai_engine/meme_predictor.py:52` | Speculative high-volatility crypto momentum & risk engine | **DEPLOYED** | `api/routers/crypto.py:1186` |
| `ai_engine/crypto_sentiment.py` | `agents/ai_engine/crypto_sentiment.py:28` | Crypto Fear & Greed index and Reddit/Twitter sentiment harvester | **DEPLOYED** | `api/routers/admin.py:951` |
| `ai_engine/trainer.py` | `agents/ai_engine/trainer.py:32` | Model training pipeline for XGBoost, LightGBM, and Logistic models | **WIRED** | `scripts/train_and_publish.py:38` |
| `ai_engine/champion_challenger.py` | `agents/ai_engine/champion_challenger.py:42` | Shadow evaluation and automated model promotion engine | **WIRED** | `scripts/kronos_promotion.py:47` |
| `ai_engine/kronos_forecaster.py` | `agents/ai_engine/kronos_forecaster.py:33` | Deep-learning time-series price forecaster | **WIRED** | `scripts/kronos_batch.py:35` |
| `ai_engine/lstm_model.py` | `agents/ai_engine/lstm_model.py:25` | PyTorch bidirectional LSTM sequential price embedding network | **WIRED** | `agents/ai_engine/__init__.py:11`, `scripts/train_and_publish.py` |
| `ai_engine/anomaly_detector.py` | `agents/ai_engine/anomaly_detector.py:31` | Isolation forest & statistical price/volume anomaly detector | **WIRED** | `agents/ai_engine/ensemble.py:444` |
| `ai_engine/meta_labeler.py` | `agents/ai_engine/meta_labeler.py:35` | Triple-barrier secondary labeling filter for sizing conviction | **WIRED** | `scripts/research_primary_rules.py:29` |
| `ai_engine/meta_learner.py` | `agents/ai_engine/meta_learner.py:27` | Meta-stacking model combining underlying model predictions | **WIRED** | `agents/ai_engine/ensemble.py:673` |
| `ai_engine/range_forecaster.py` | `agents/ai_engine/range_forecaster.py:22` | Expected price range and volatility envelope calculator | **WIRED** | `agents/ai_engine/prediction_engine.py:33` |
| `ai_engine/shadow_evaluator.py` | `agents/ai_engine/shadow_evaluator.py:27` | Live shadow performance evaluator comparing model versions | **WIRED** | `agents/ai_engine/ensemble.py:819` |
| `ai_engine/time_split.py` | `agents/ai_engine/time_split.py:21` | Purged/embargoed walk-forward time-series CV splitter | **WIRED** | `agents/ai_engine/backtest_validator.py:21` |
| `ai_engine/backtest_validator.py` | `agents/ai_engine/backtest_validator.py:31` | Statistical significance validator for out-of-sample backtests | **DEPLOYED** | `api/routers/prediction.py:20` |
| `market_intelligence/swing_scanner.py` | `agents/market_intelligence/swing_scanner.py:51` | SwingSpectrum confluence scanner (patterns, regime, R:R, delivery) | **DEPLOYED** | `api/service.py:39`, `api/routers/market.py:151` |
| `market_intelligence/nse_fetcher.py` | `agents/market_intelligence/nse_fetcher.py:399` | Daily OHLCV fetcher, corporate action adjustment, multi-symbol sync | **DEPLOYED** | `api/app.py:361`, `api/service.py:80` |
| `market_intelligence/nse_holidays.py` | `agents/market_intelligence/nse_holidays.py:81` | NSE market session holiday calendar and trading hour validator | **DEPLOYED** | `api/data_honesty.py:329`, `api/routers/market.py:159` |
| `market_intelligence/next_day_picks.py` | `agents/market_intelligence/next_day_picks.py:103` | Positional EOD daily pick generator with composite institutional scoring | **DEPLOYED** | `api/routers/research.py:48` |
| `market_intelligence/forex_scanner.py` | `agents/market_intelligence/forex_scanner.py:258` | Twelve Data forex scanner (paper trading only, no live broker) | **DEPLOYED** | `api/routers/forex.py:34` |
| `market_intelligence/breakout_scanner.py` | `agents/market_intelligence/breakout_scanner.py:45` | Volatility contraction (VCP) and 20-day high breakout scanner | **DEPLOYED** | `api/routers/market.py:125` |
| `market_intelligence/market_data.py` | `agents/market_intelligence/market_data.py:35` | FII/DII institutional cash flow, tradeflow data synthesizer | **DEPLOYED** | `api/app.py:195`, `api/service.py:38` |
| `market_intelligence/index_mover.py` | `agents/market_intelligence/index_mover.py:32` | Nifty 50 point contribution calculator for heavyweights | **DEPLOYED** | `api/routers/market.py:131` |
| `market_intelligence/watchlist_analyzer.py`| `agents/market_intelligence/watchlist_analyzer.py:42` | Multi-asset watchlist quantitative overview and risk ranker | **DEPLOYED** | `api/app.py:224` |
| `market_intelligence/global_macro_fetcher.py`| `agents/market_intelligence/global_macro_fetcher.py:35` | US Treasury yields, DXY, Brent crude, and global index monitor | **DEPLOYED** | `api/routers/research.py:514` |
| `intraday_engine/scanner.py` | `agents/intraday_engine/scanner.py:42` | 5m/15m intraday momentum and breakout scanner | **DEPLOYED** | `api/routers/swing.py:434` |
| `intraday_engine/session.py` | `agents/intraday_engine/session.py:35` | Intraday trading session state machine (Opening Range, Mid-Day, Close) | **DEPLOYED** | `api/routers/swing.py:438` |
| `intraday_engine/risk_manager.py` | `agents/intraday_engine/risk_manager.py:28` | Intraday loss limits, max open positions, and circuit breakers | **DEPLOYED** | `api/routers/swing.py:439` |
| `intraday_engine/confluence_model.py`| `agents/intraday_engine/confluence_model.py:31` | Multi-factor intraday signal scoring engine | **WIRED** | `agents/intraday_engine/scanner.py:26` |
| `intraday_engine/strategies.py` | `agents/intraday_engine/strategies.py:38` | VWAP pullback, Opening Range Breakout (ORB), and EMA momentum strategies | **WIRED** | `agents/intraday_engine/scanner.py:34` |
| `intraday_engine/fetcher.py` | `agents/intraday_engine/fetcher.py:25` | Intraday 1m/5m OHLCV bar fetcher with local caching | **DEPLOYED** | `api/routers/swing.py:713` |
| `intraday_engine/indicators.py` | `agents/intraday_engine/indicators.py:31` | Intraday VWAP, Supertrend, ATR, and Bollinger bands | **DEPLOYED** | `api/routers/swing.py:714` |
| `intraday_engine/types.py` | `agents/intraday_engine/types.py:18` | Typed dataclasses for intraday bars, signals, and session state | **WIRED** | `agents/execution/paper_trading.py:395` |
| `feature_engine/indicators.py` | `agents/feature_engine/indicators.py:195` | Base 22 technical indicators (RSI, MACD, Bollinger, ATR, ADX, SMA/EMA) | **DEPLOYED** | `api/service.py:28` |
| `feature_engine/advanced_features.py` | `agents/feature_engine/advanced_features.py:30` | 23 advanced stationary features (Garman-Klass, Hurst, Parkinson, Entropy) | **DEPLOYED** | `api/service.py:30` |
| `feature_engine/trend.py` | `agents/feature_engine/trend.py:16` | Multi-timeframe trend classifier (Bullish, Bearish, Sideways) | **DEPLOYED** | `api/service.py:31`, `api/routers/market.py:123` |
| `feature_engine/mtf_scorer.py` | `agents/feature_engine/mtf_scorer.py:32` | Multi-timeframe alignment score (Daily + 4H + 1H confluence) | **DEPLOYED** | `api/service.py:33` |
| `feature_engine/regime_detector.py` | `agents/feature_engine/regime_detector.py:29` | Market volatility & trend regime detector (Trending vs Ranging) | **DEPLOYED** | `api/routers/market.py:121` |
| `feature_engine/regime_classifier.py`| `agents/feature_engine/regime_classifier.py:25` | Unsupervised Gaussian Mixture Model / HMM regime classifier | **WIRED** | `agents/feature_engine/__init__.py:6` |
| `feature_engine/label_generator.py` | `agents/feature_engine/label_generator.py:34` | Triple-barrier labeling (upper profit, lower stop, vertical timeout) | **WIRED** | `scripts/train_and_publish.py:42` |
| `feature_engine/session_features.py` | `agents/feature_engine/session_features.py:22` | Intraday session time and day-of-week cyclical features | **WIRED** | `agents/signal_engine/quality_scorer.py:37` |
| `feature_engine/support_resistance.py`| `agents/feature_engine/support_resistance.py:24` | Pivot point and dynamic horizontal S/R level cluster identification | **WIRED** | `agents/feature_engine/__init__.py:2` |
| `signal_engine/signal_generator.py` | `agents/signal_engine/signal_generator.py:378` | Primary rule engine: combines trend, MTF, features, regime, SL/TP levels | **DEPLOYED** | `api/service.py:42`, `api/routers/market.py:167` |
| `signal_engine/quality_scorer.py` | `agents/signal_engine/quality_scorer.py:68` | Quality Score (QS 0-100) scoring algorithm incorporating ML conviction | **WIRED** | `agents/signal_engine/signal_generator.py:1017` |
| `signal_engine/composite_decision.py`| `agents/signal_engine/composite_decision.py:30` | 9-pillar composite decision synthesizer for "Data Brain V3" | **DEFINED** | `tests/test_data_brain_v3.py:35` (UNWIRED in runtime) |
| `execution/autonomous_daemon.py` | `agents/execution/autonomous_daemon.py:596` | Background autonomous trading execution loop & cycle dispatcher | **DEPLOYED** | `api/app.py:382`, `api/routers/admin.py:78` |
| `execution/paper_trading.py` | `agents/execution/paper_trading.py:32` | Paper trading portfolio manager, virtual order book, and fill simulation | **DEPLOYED** | `api/service.py:44`, `api/routers/portfolio.py:28` |
| `execution/market_control.py` | `agents/execution/market_control.py:248` | Operator gate toggles: Crypto/Forex enable/disable, admin password auth | **DEPLOYED** | `api/routers/admin.py:73`, `api/routers/crypto.py:37` |
| `execution/session_guard.py` | `agents/execution/session_guard.py:34` | Indian market IST time conversions and trading session boundaries | **DEPLOYED** | `api/routers/admin.py:301`, `api/cycle_scheduler.py:185` |
| `execution/runtime_controls.py` | `agents/execution/runtime_controls.py:28` | Hot-reconfigurable risk and execution limits | **DEPLOYED** | `api/routers/admin.py:1137` |
| `execution/position_sizing.py` | `agents/execution/position_sizing.py:34` | Volatility-adjusted and Kelly-criterion position sizing | **DEPLOYED** | `api/routers/market.py:177` |
| `execution/currency.py` | `agents/execution/currency.py:25` | INR multi-currency conversion, Decimal quantization, forex conversions | **DEPLOYED** | `api/service.py:1765` |
| `execution/trailing_sl.py` | `agents/execution/trailing_sl.py:30` | Dynamic ATR-based trailing stop-loss management | **DEPLOYED** | `api/routers/market.py:179` |
| `execution/distributed_lock.py` | `agents/execution/distributed_lock.py:28` | Postgres/Supabase distributed lock manager preventing race conditions | **DEPLOYED** | `api/routers/admin.py:300` |
| `execution/idempotency.py` | `agents/execution/idempotency.py:26` | Trade execution idempotency token validator | **DEPLOYED** | `api/routers/portfolio.py:35` |
| `execution/trade_costs.py` | `agents/execution/trade_costs.py:24` | Real-world Indian equity transaction costs (STT, GST, SEBI, brokerage) | **DEPLOYED** | `api/routers/portfolio.py:36` |
| `execution/reconcile_bars.py` | `agents/execution/reconcile_bars.py:28` | Bar timestamp reconciliation and missing bar interpolation | **WIRED** | `agents/execution/paper_trading.py:410` |
| `backtester/engine.py` | `agents/backtester/engine.py:35` | Event-driven historical strategy backtester | **DEPLOYED** | `api/service.py:26`, `api/routers/market.py:115` |
| `backtester/ensemble_validator.py` | `agents/backtester/ensemble_validator.py:32` | Cross-sectional validation of ML models across historical market cycles | **DEPLOYED** | `api/routers/admin.py:829` |
| `backtester/portfolio.py` | `agents/backtester/portfolio.py:28` | Multi-asset backtest portfolio tracking and drawdown analyzer | **WIRED** | `agents/backtester/__init__.py:12` |
| `backtester/walk_forward.py` | `agents/backtester/walk_forward.py:25` | Anchored/rolling walk-forward parameter optimization engine | **WIRED** | `agents/backtester/__init__.py:2` |
| `backtester/costs.py` | `agents/backtester/costs.py:22` | Backtest slippage, impact cost, and regulatory fee models | **WIRED** | `agents/backtester/__init__.py:3` |
| `macro/edge_score_engine.py` | `agents/macro/edge_score_engine.py:42` | Quantitative Edge Score engine (FII, VIX, Breadth, Macro confluence) | **DEPLOYED** | `api/routers/research.py:128` |
| `macro/risk_gauge.py` | `agents/macro/risk_gauge.py:35` | Institutional systemic risk gauge (Risk-On vs Risk-Off) | **DEPLOYED** | `api/routers/research.py:567` |
| `macro/carry_scanner.py` | `agents/macro/carry_scanner.py:38` | FX interest rate carry trade yield scanner | **WIRED** | `agents/macro/__init__.py:25` |
| `macro/cot_fetcher.py` | `agents/macro/cot_fetcher.py:31` | CFTC Commitment of Traders (COT) institutional positioning fetcher | **WIRED** | `agents/macro/__init__.py:20` |
| `macro/cot_scorer.py` | `agents/macro/cot_scorer.py:25` | COT commercial vs non-commercial positioning indexer | **WIRED** | `agents/macro/__init__.py:21` |
| `macro/macro_fetcher.py` | `agents/macro/macro_fetcher.py:29` | Macroeconomic indicator scraper (GDP, CPI, Repo rate, RBI decisions) | **WIRED** | `agents/macro/__init__.py:18` |
| `macro/macro_scorer.py` | `agents/macro/macro_scorer.py:26` | Composite macroeconomic health scoring model | **WIRED** | `agents/macro/__init__.py:19` |
| `macro/retail_sentiment.py` | `agents/macro/retail_sentiment.py:32` | Retail contrarian sentiment indexer | **WIRED** | `agents/macro/__init__.py:22` |
| `macro/seasonality.py` | `agents/macro/seasonality.py:28` | Monthly and quarterly calendar return seasonality scanner | **WIRED** | `agents/macro/__init__.py:24` |
| `data_ingestion/context_fetcher.py` | `agents/data_ingestion/context_fetcher.py:74` | News sentiment aggregator (Google News RSS, CryptoPanic, CoinGecko) | **DEPLOYED** | `api/service.py:78`, `api/routers/market.py:117` |
| `data_ingestion/nse_bhavcopy.py` | `agents/data_ingestion/nse_bhavcopy.py:43` | Official NSE Bhavcopy downloader, parsing institutional delivery percentage | **DEPLOYED** | `api/service.py:112`, `agents/market_intelligence/swing_scanner.py:45` |
| `data_ingestion/binance_fetcher.py` | `agents/data_ingestion/binance_fetcher.py:159` | Binance spot/USDT public HTTP ticker and OHLCV kline fetcher | **DEPLOYED** | `api/service.py:1081`, `api/routers/market.py:881` |
| `data_ingestion/forex_fetcher.py` | `agents/data_ingestion/forex_fetcher.py:290` | Twelve Data forex rates fetcher with rate limit throttling | **DEPLOYED** | `api/routers/forex.py:33`, `api/routers/market.py:867` |
| `data_ingestion/fii_dii_fetcher.py` | `agents/data_ingestion/fii_dii_fetcher.py:32` | Institutional FII/DII daily cash investment scraper | **DEPLOYED** | `api/service.py:125`, `agents/market_intelligence/market_data.py:45` |
| `data_ingestion/derivatives_oi.py` | `agents/data_ingestion/derivatives_oi.py:30` | F&O open interest and put-call ratio intelligence parser | **DEPLOYED** | `api/app.py:215` |
| `data_ingestion/delivery_breadth.py` | `agents/data_ingestion/delivery_breadth.py:28` | Market-wide institutional delivery accumulation breadth indexer | **DEPLOYED** | `api/service.py:126` |
| `data_ingestion/vix_fetcher.py` | `agents/data_ingestion/vix_fetcher.py:25` | India VIX volatility index scraper and regime gate | **DEPLOYED** | `api/service.py:127` |
| `data_ingestion/corporate_events.py` | `agents/data_ingestion/corporate_events.py:24` | Earnings release and dividend date scraper (hard gate against earnings gap risk) | **WIRED** | `agents/data_ingestion/context_fetcher.py:194` |
| `data_ingestion/validator.py` | `agents/data_ingestion/validator.py:130` | OHLCV bar anomaly, null, spike, and timestamp consistency validator | **DEPLOYED** | `api/service.py:82` |
| `data_ingestion/data_manager.py` | `agents/data_ingestion/data_manager.py:28` | Unified local cache manager for historical market bars | **WIRED** | `agents/data_ingestion/__init__.py:12` |
| `data_ingestion/dexscreener_client.py`| `agents/data_ingestion/dexscreener_client.py:26` | On-chain DEX liquidity and swap volume tracker | **WIRED** | `agents/ai_engine/meme_predictor.py:45` |
| `data_ingestion/social_sentiment.py` | `agents/data_ingestion/social_sentiment.py:28` | Twitter/Reddit social keyword sentiment scoring engine | **WIRED** | `agents/data_ingestion/__init__.py:15` |
| `data_ingestion/binance_ws.py` | `agents/data_ingestion/binance_ws.py:35` | Binance live trade streaming WebSocket client | **WIRED** | `autonomous_runner.py:35` |
| `data_ingestion/nse_fetcher.py` | `agents/data_ingestion/nse_fetcher.py:40` | Batch ingestion variant for NSE data (distinct from market_intelligence) | **WIRED** | `docs/nse_fetcher_modules.md`, `agents/data_ingestion/__init__.py:5` |
| `data_ingestion/nse_live_quote.py` | `agents/data_ingestion/nse_live_quote.py:30` | Multi-tier quote fallbacks (TradingView, Google, Yahoo) | **WIRED** | `agents/data_ingestion/__init__.py:8` |
| `data_ingestion/nse_native_quote.py`| `agents/data_ingestion/nse_native_quote.py:25` | Direct NSE quote-equity scraper (Akamai WAF blocked in cloud) | **WIRED** | `data/providers/nse_bhavcopy.py:42` |
| `analytics/trade_journal.py` | `agents/analytics/trade_journal.py:35` | Automated trade journal, execution log, and post-trade performance analytics | **DEPLOYED** | `api/service.py:76`, `api/routers/market.py:113` |
| `analytics/feedback_loop.py` | `agents/analytics/feedback_loop.py:42` | Self-learning reinforcement loop updating feature weights based on win/loss | **DEPLOYED** | `api/app.py:232`, `api/service.py:3994` |
| `analytics/scorecard_bootstrapper.py`| `agents/analytics/scorecard_bootstrapper.py:28` | Historical win rate and performance metrics reconciler | **DEPLOYED** | `api/routers/market.py:1735` |
| `analytics/signal_tracker.py` | `agents/analytics/signal_tracker.py:32` | Historical signal accuracy tracker against forward market prices | **DEPLOYED** | `api/routers/market.py:181` |
| `analytics/gate_ablation.py` | `agents/analytics/gate_ablation.py:30` | Filter gate ablation study tracking false positives vs true edge | **WIRED** | `agents/execution/autonomous_daemon.py:227` |
| `alerts/telegram_bot.py` | `agents/alerts/telegram_bot.py:38` | Interactive Telegram bot interface with inline operator commands | **DEPLOYED** | `api/service.py:46`, `api/routers/admin.py:75` |
| `notifications/telegram_alerts.py` | `agents/notifications/telegram_alerts.py:28` | Outbound Telegram signal broadcast dispatcher | **DEPLOYED** | `api/routers/market.py:225` |

---

### 1.2 `api/` — Web Application, API Routers & Business Logic
The `api` directory contains the FastAPI server definition (`api/app.py`), route aggregator (`api/routes.py`), centralized service business logic (`api/service.py`), background task schedulers (`cycle_scheduler.py`, `learning_scheduler.py`), data honesty and valuation contracts (`data_honesty.py`, `valuation.py`), model bundle synchronization (`model_sync.py`), and microservice RPC HTTP clients (`ml_client.py`, `feed_client.py`). All modular REST endpoints are partitioned under `api/routers/`.

| File | Defined At | Role | State | Primary Caller / Entry Point |
|---|---|---|---|---|
| `app.py` | `api/app.py:465` | FastAPI application factory, lifespan startup/shutdown lifecycle, and background task spawner | **DEPLOYED** | `server.py:40` |
| `routes.py` | `api/routes.py:40` | Master route aggregator mounting all domain sub-routers with backwards compatibility re-exports | **DEPLOYED** | `api/app.py:69` |
| `service.py` | `api/service.py:2918` | Central business logic: data routing, indicator pipeline, signal filtering, mood scoring | **DEPLOYED** | `api/app.py:67`, `api/routers/*` |
| `security.py` | `api/security.py:48` | Admin API key (`ADMIN_KEY`), Operator credentials, and JWT auth verification | **DEPLOYED** | `api/routers/admin.py:48` |
| `schemas.py` | `api/schemas.py:25` | Pydantic response models and request payload validation schemas | **DEPLOYED** | `api/routers/market.py:89` |
| `ml_client.py` | `api/ml_client.py:38` | HTTP client communicating with Box B (`trading-ml`) over `ML_INTERNAL_TOKEN` | **DEPLOYED** | `agents/ai_engine/ensemble.py:423` |
| `feed_client.py` | `api/feed_client.py:29` | HTTP client querying Box C (`trading-feed`) over `FEED_INTERNAL_TOKEN` | **DEPLOYED** | `data/providers/shoonya_feed.py:14` |
| `model_sync.py` | `api/model_sync.py:220` | Model bundle synchronization from Supabase `runtime_state` table | **DEPLOYED** | `api/routers/admin.py:705` |
| `cycle_scheduler.py` | `api/cycle_scheduler.py:185` | In-process periodic task triggering autonomous cycles during market hours | **DEPLOYED** | `api/app.py:402` |
| `learning_scheduler.py` | `api/learning_scheduler.py:47` | End-of-Day EOD trade review scheduler running post-market self-learning | **DEPLOYED** | `api/app.py:417` |
| `data_honesty.py` | `api/data_honesty.py:26` | Telemetry validator distinguishing live broker quotes vs synthetic/stale feeds | **DEPLOYED** | `api/app.py:363` |
| `valuation.py` | `api/valuation.py:35` | Intrinsic valuation metrics (P/E, PEG, DCF) for equity candidates | **DEPLOYED** | `api/data_honesty.py:321` |
| `health_v2.py` | `api/health_v2.py:28` | Multi-subsystem deep health check probe for Box A | **DEPLOYED** | `api/routers/health.py:144` |
| `track_record.py` | `api/track_record.py:32` | Public track record, equity curve, and win rate calculation helper | **DEPLOYED** | `api/routers/portfolio.py:95` |
| `trade_hygiene.py` | `api/trade_hygiene.py:28` | Position reconciliation, ghost trade pruning, and orderbook cleanup | **DEPLOYED** | `api/routers/portfolio.py:490` |
| `paths.py` | `api/paths.py:1` | Static filesystem directory path constants (`STATIC_DIR = Path("dashboard")`) | **DEPLOYED** | `api/app.py:65` |
| `routers/health.py` | `api/routers/health.py:144` | `/api/health`, `/api/readiness`, `/api/ready`, `/favicon.ico` endpoints | **DEPLOYED** | `api/routes.py:27` |
| `routers/admin.py` | `api/routers/admin.py:73` | `/api/admin/*`, `/api/autonomous/*`, `/api/models/sync` administrative control | **DEPLOYED** | `api/routes.py:28` |
| `routers/market.py` | `api/routers/market.py:151` | `/api/market/*`, `/api/quotes`, `/api/signals/*`, `/ws/prices` endpoints | **DEPLOYED** | `api/routes.py:34` |
| `routers/swing.py` | `api/routers/swing.py:434` | `/api/swing/*`, `/api/intraday/*` SwingSpectrum candidate endpoints | **DEPLOYED** | `api/routes.py:32` |
| `routers/portfolio.py` | `api/routers/portfolio.py:28` | `/api/portfolio`, `/api/paper-trading/*`, `/api/stats`, `/api/trades` | **DEPLOYED** | `api/routes.py:33` |
| `routers/research.py` | `api/routers/research.py:48` | `/api/research/*`, `/api/nse/dashboard`, `/api/next-day-picks` endpoints | **DEPLOYED** | `api/routes.py:29` |
| `routers/crypto.py` | `api/routers/crypto.py:37` | `/api/crypto/*` market radar, Binance proxies, meme coin intelligence | **DEPLOYED** | `api/routes.py:30` |
| `routers/forex.py` | `api/routers/forex.py:34` | `/api/forex/*` Twelve Data rates, sessions, and forex radar | **DEPLOYED** | `api/routes.py:31` |
| `routers/prediction.py`| `api/routers/prediction.py:19` | `/api/prediction/{symbol}` multi-horizon forecasting endpoint | **DEPLOYED** | `api/routes.py:35` |

---

### 1.3 `cache/` — In-Memory Caching Primitives
The `cache` directory provides low-latency memory caching abstractions with TTL expirations and thread-safe lock primitives.

| File | Defined At | Role | State | Primary Caller / Entry Point |
|---|---|---|---|---|
| `memory.py` | `cache/memory.py:42` | In-memory key-value cache with thread-safe locks, item count caps, and TTL eviction | **DEPLOYED** | `data/providers/base.py:13`, `cache/__init__.py:2` |
| `__init__.py` | `cache/__init__.py:2` | Package init re-exporting `InMemoryCache` | **DEPLOYED** | `data/providers/base.py:13` |

---

### 1.4 `config/` — Configuration, Constants & Exchange Schedules
The `config` directory stores global application settings (`config/settings.py`), standardized logging configuration (`config/logger.py`), symbol mapping dictionaries (`config/symbol_aliases.py`), dynamic learned weights (`learned_weights.json`), and official NSE holiday calendar definitions.

| File | Defined At | Role | State | Primary Caller / Entry Point |
|---|---|---|---|---|
| `settings.py` | `config/settings.py:1221` | Pydantic/dataclass environment variable schema, defaults, and runtime validator | **DEPLOYED** | `api/app.py:59`, `server.py:19` |
| `logger.py` | `config/logger.py:28` | Structured colorized logging formatter, log rotation, and log level setup | **DEPLOYED** | `api/cycle_scheduler.py:27`, `server.py:18` |
| `symbol_aliases.py` | `config/symbol_aliases.py:5` | Canonical symbol mapping (e.g. `NIFTY 50` $\leftrightarrow$ `^NSEI` $\leftrightarrow$ `NIFTY`) | **DEPLOYED** | `api/routers/market.py:927` |
| `nse_holidays_2025.json` | `config/nse_holidays_2025.json:1` | Calendar definitions for 2025 NSE market holidays | **DEPLOYED** | `agents/market_intelligence/nse_holidays.py:35` |
| `nse_holidays_2026.json` | `config/nse_holidays_2026.json:1` | Calendar definitions for 2026 NSE market holidays | **DEPLOYED** | `agents/market_intelligence/nse_holidays.py:36` |
| `nse_holidays_2027.json` | `config/nse_holidays_2027.json:1` | Calendar definitions for 2027 NSE market holidays | **DEPLOYED** | `agents/market_intelligence/nse_holidays.py:37` |
| `learned_weights.json` | `config/learned_weights.json:1` | Persisted feature importance and gate weighting parameters | **DEPLOYED** | `agents/analytics/feedback_loop.py:65` |

---

### 1.5 `data/` — Market Data Providers & Ingestion Router
The `data` directory contains the multi-tier market data router (`data/router.py`), data provider implementations (`data/providers/`), official NSE Bhavcopy archives (`data/bhavcopy/`), intraday scorecard records, and historical swing candidate snapshots (`data/swing_shortlist_*.json`).

| File | Defined At | Role | State | Primary Caller / Entry Point |
|---|---|---|---|---|
| `router.py` | `data/router.py:19` | Multi-tier provider router (Shoonya $\rightarrow$ Bhavcopy $\rightarrow$ TradingView $\rightarrow$ Yahoo) | **DEPLOYED** | `api/health_v2.py:282`, `api/service.py:84` |
| `bhavcopy_panel.py` | `data/bhavcopy_panel.py:36` | Multi-day Bhavcopy delivery percentage panel generator | **WIRED** | `scripts/kronos_batch.py:63` |
| `providers/base.py` | `data/providers/base.py:18` | Abstract base class `DataProvider` defining standard OHLCV interface | **DEPLOYED** | `data/providers/gdelt.py:16`, `data/providers/yahoo.py:27` |
| `providers/gdelt.py` | `data/providers/gdelt.py:28` | GDELT global news sentiment data provider | **DEPLOYED** | `api/service.py:142` |
| `providers/yahoo.py` | `data/providers/yahoo.py:35` | Yahoo Finance fallback market data provider | **DEPLOYED** | `api/routers/prediction.py:99` |
| `providers/shoonya_feed.py` | `data/providers/shoonya_feed.py:22` | HTTP client adapter querying Box C (`trading-feed`) | **DEPLOYED** | `data/router.py:235` |
| `providers/nse_bhavcopy.py` | `data/providers/nse_bhavcopy.py:29` | Public archive Bhavcopy fetcher adapter | **WIRED** | `data/bhavcopy_panel.py:36` |
| `providers/tradingview.py` | `data/providers/tradingview.py:25` | Unofficial TradingView quote scraper | **WIRED** | `agents/data_ingestion/nse_live_quote.py:311` |
| `providers/twelve_data.py` | `data/providers/twelve_data.py:22` | Twelve Data forex data provider adapter | **WIRED** | `data/providers/__init__.py:5` |
| `bhavcopy/sec_bhavdata_*.csv` | `data/bhavcopy/*.csv:1` | Cached official NSE Bhavcopy EOD security data files | **DEPLOYED** | `agents/data_ingestion/nse_bhavcopy.py:75` |
| `cache/shoonya_tokens.json` | `data/cache/*.json:1` | Local token cache for Shoonya script masters | **DEPLOYED** | `feed/token_master.py:45` |
| `swing_shortlist_*.json` | `data/*.json:1` | Persisted EOD candidate shortlists for historical audit | **DEPLOYED** | `db/swing_store.py:65` |
| `tradeai.db` / `trades.db` | `data/*.db:1` | Ephemeral SQLite database files used during local development | **DEFINED** | Local standalone use only |

---

### 1.6 `db/` — Persistence Tier & Database Abstractions
The `db` directory houses the PostgreSQL/Supabase database connection pool (`db/connection.py`), SQLAlchemy ORM models (`db/models.py`), runtime key-value state store (`db/runtime_state.py`), swing candidate store (`db/swing_store.py`), trade resolution tracker (`db/swing_outcome_tracker.py`), and 1m/5m intraday bar store (`db/intraday_store.py`).

| File | Defined At | Role | State | Primary Caller / Entry Point |
|---|---|---|---|---|
| `connection.py` | `db/connection.py:150` | Database engine creation, connection pooling, and `init_db()` runner | **DEPLOYED** | `api/app.py:61`, `server.py:22` |
| `models.py` | `db/models.py:125` | SQLAlchemy ORM tables (`signals`, `trades`, `candles`, `daily_scorecard`, `prediction_history`) | **DEPLOYED** | `api/app.py:235`, `db/connection.py:28` |
| `runtime_state.py` | `db/runtime_state.py:42` | Key-value store on Supabase (`runtime_state` table) for model bundles & flags | **DEPLOYED** | `api/app.py:194`, `api/routers/admin.py:84` |
| `swing_store.py` | `db/swing_store.py:48` | Persistence layer for swing setups, scans, shortlists, and EOD history | **DEPLOYED** | `api/routers/market.py:173`, `api/service.py:1300` |
| `swing_outcome_tracker.py`| `db/swing_outcome_tracker.py:35` | Tracks realized MFE/MAE and win/loss resolution of swing signals | **DEPLOYED** | `api/service.py:1301` |
| `intraday_store.py` | `db/intraday_store.py:42` | Stores and retrieves 1m/5m intraday OHLCV bars from Postgres | **DEPLOYED** | `api/routers/health.py:182` |
| `ohlcv_cache.py` | `db/ohlcv_cache.py:32` | Disk/database cache for historical daily equity bars | **WIRED** | `agents/macro/seasonality.py:26` |
| `candles_store.py` | `db/candles_store.py:25` | Dedicated candle storage helper | **DEFINED** | `tests/test_watchlist_quant_engine.py:250` (UNWIRED in runtime) |
| `prediction_tracker.py` | `db/prediction_tracker.py:38` | Unused JSON audit store for predictions | **DEFINED** | **ZERO callers across repository** (Dead Code) |

---

### 1.7 `feed/` — Shoonya Live Market Ingestion & F&O Analytics Engine
The `feed` directory contains the dedicated codebase for **Box C (`trading-feed`)** (`feed_server.py:28`, `render.yaml:141`). It manages real-time Shoonya WebSocket market data feeds (`shoonya_client.py`), real-time bar aggregation (`bar_builder.py`), token resolution (`token_master.py`), NFO contracts master (`nfo_master.py`), and real-time options Greeks analytics (`fno_analytics.py`, `option_chain.py`).

| File | Defined At | Role | State | Primary Caller / Entry Point |
|---|---|---|---|---|
| `shoonya_client.py` | `feed/shoonya_client.py:161` | WebSocket client connecting to Shoonya Finvasia tick feed | **DEPLOYED** | `feed_server.py:22` |
| `bar_builder.py` | `feed/bar_builder.py:28` | Real-time tick aggregator building 1m/5m OHLCV bars from raw trade ticks | **DEPLOYED** | `feed/shoonya_client.py:17` |
| `token_master.py` | `feed/token_master.py:57` | Resolves symbol names (e.g. `RELIANCE`) to Shoonya exchange tokens | **DEPLOYED** | `feed/shoonya_client.py:18` |
| `nfo_master.py` | `feed/nfo_master.py:32` | Downloads and parses NFO options/futures contract specifications | **DEPLOYED** | `feed_server.py:21` |
| `fno_analytics.py` | `feed/fno_analytics.py:23` | Computes Black-Scholes Greeks (Delta, Gamma, Theta, Vega) & Put-Call Ratio | **DEPLOYED** | `feed_server.py:20` |
| `option_chain.py` | `feed/option_chain.py:30` | In-memory options chain structure with real-time IV calculation | **DEPLOYED** | `feed/fno_analytics.py:15` |

---

### 1.8 `models/` — Serialized Machine Learning Weights & Scalers
The `models` directory contains the serialized binary machine learning artifacts loaded by `agents/ai_engine/model_registry.py:18` on **Box B (`trading-ml`)** and synchronized via `api/model_sync.py:220`.

| File | Format | Role | State | Primary Consumer |
|---|---|---|---|---|
| `xgb_model.pkl` | Pickle | Serialized XGBoost gradient boosting classifier | **DEPLOYED** | `agents/ai_engine/model_registry.py:48` |
| `lgbm_model.pkl` | Pickle | Serialized LightGBM classifier | **DEPLOYED** | `agents/ai_engine/model_registry.py:55` |
| `logistic_model.pkl` | Pickle | Serialized Logistic Regression baseline | **DEPLOYED** | `agents/ai_engine/model_registry.py:62` |
| `logistic_scaler.pkl`| Pickle | StandardScaler fitted on 45 base+advanced training features | **DEPLOYED** | `agents/ai_engine/model_registry.py:69` |
| `meta_learner.pkl` | Pickle | Stacking meta-classifier combining sub-model predictions | **DEPLOYED** | `agents/ai_engine/model_registry.py:76` |
| `calibrator.pkl` | Pickle | Isotonic probability calibrator mapping raw scores to true win rates | **DEPLOYED** | `agents/ai_engine/model_registry.py:83` |
| `best_model.pt` | PyTorch | Deep bidirectional LSTM neural network checkpoint | **DEPLOYED** | `agents/ai_engine/model_registry.py:90` |
| `scaler_params.json` | JSON | Feature minimum/maximum clipping and normalization boundaries | **DEPLOYED** | `agents/ai_engine/model_registry.py:97` |
| `model_meta.json` | JSON | Model bundle provenance (git commit, trained timestamp, accuracy) | **DEPLOYED** | `agents/ai_engine/model_registry.py:104` |

---

### 1.9 `risk/` — Swing Risk & Capital Protection Guardrails
The `risk` directory houses risk management and capital allocation logic for the swing trading pipeline.

| File | Defined At | Role | State | Primary Caller / Entry Point |
|---|---|---|---|---|
| `swing_risk.py` | `risk/swing_risk.py:11` | Enforces max portfolio heat, ATR-based position sizing, and stop-loss limits | **DEPLOYED** | `agents/market_intelligence/swing_scanner.py:40` |
| `__init__.py` | `risk/__init__.py:1` | Package initialization re-exporting `SwingRiskManager` | **DEPLOYED** | `agents/market_intelligence/swing_scanner.py:40` |

---

### 1.10 `scripts/` — Operational Maintenance, CI & Retraining Tooling
The `scripts` directory contains CLI automation tools, health verifiers, and offline model training pipelines. Four scripts are deployed in production via GitHub Actions crons.

| File | Defined At | Role | State | Primary Invoker |
|---|---|---|---|---|
| `autonomous_cycle.py` | `scripts/autonomous_cycle.py:35` | Triggers on-demand autonomous trading cycles | **DEPLOYED** | `.github/workflows/autonomous_cycle.yml:124` |
| `check_production_health.py`| `scripts/check_production_health.py:28`| Probes multi-box endpoints and sends alerts on failure | **DEPLOYED** | `.github/workflows/health.yml:37,48` |
| `train_and_publish.py` | `scripts/train_and_publish.py:45` | Trains ML models on external GPU and publishes bundle to Supabase | **DEPLOYED** | `.github/workflows/model_train_scheduled.yml:64` |
| `nightly_swing_scan.py` | `scripts/nightly_swing_scan.py:42` | Runs after-market Bhavcopy/Swing scans and sends Telegram alerts | **DEPLOYED** | `.github/workflows/nightly_swing_scan.yml:57` |
| `kronos_promotion.py` | `scripts/kronos_promotion.py:35` | Compares challenger model against champion and updates registry | **WIRED** | Standalone automated promotion |
| `ranking_engine.py` | `scripts/ranking_engine.py:28` | Multi-factor equity ranking script | **WIRED** | Standalone research CLI |
| *Other 35 scripts* | `scripts/*.py` | Ad-hoc audit scripts, smoke tests, and local migration tools | **DEFINED** | Manual developer CLI only |

---

### 1.11 `src/` — Cloudflare Edge Worker Layer
The `src` directory contains the Cloudflare Worker entry point that sits between browsers and Render origin servers.

| File | Defined At | Role | State | Primary Invoker |
|---|---|---|---|---|
| `worker.js` | `src/worker.js:551` | Cloudflare Worker script: proxies API calls to Box A/B, edge-caches responses, schedules keep-warm crons, enforces SWR | **DEPLOYED** | `wrangler.jsonc:2`, Cloudflare Edge Runtime |

---

### 1.12 `utils/` — Cross-Cutting Utility Primitives
The `utils` directory provides system-level helper utilities for atomic file operations and memory profiling.

| File | Defined At | Role | State | Primary Caller / Entry Point |
|---|---|---|---|---|
| `memory_profiler.py` | `utils/memory_profiler.py:32` | Process RSS memory monitor tracking 512MB RAM cap on Render Free tier | **DEPLOYED** | `api/routers/health.py:192` |
| `profiler.py` | `utils/profiler.py:25` | Execution timing decorator for latency benchmarking | **DEPLOYED** | `api/routers/market.py:423` |
| `atomic_io.py` | `utils/atomic_io.py:15` | Crash-resilient atomic file writing (write temp file $\rightarrow$ rename) | **WIRED** | `agents/ai_engine/trainer.py:21` |
| `retry.py` | `utils/retry.py:24` | Unused generic async retry decorator | **DEFINED** | **ZERO callers across repository** (Dead Code) |

---

### 1.13 `tests/` — Automated Test Suite
The `tests` directory contains 155 test modules providing regression verification, data honesty assertion, and unit coverage.

| Category / Key File | Defined At | Role | State |
|---|---|---|---|
| `conftest.py` | `tests/conftest.py:1` | Pytest fixtures, mock data providers, and test environment configuration | **DEPLOYED** (`pytest`) |
| `test_accuracy_guards.py` | `tests/test_accuracy_guards.py:25` | Validates that synthetic or unverified metrics are rejected | **DEPLOYED** (`pytest`) |
| `test_data_honesty.py` | `tests/test_data_honesty.py:30` | Validates quote staleness and market status truth gates | **DEPLOYED** (`pytest`) |
| `test_ml_sidecar_contract.py` | `tests/test_ml_sidecar_contract.py:35` | Validates HTTP contract between Box A and Box B | **DEPLOYED** (`pytest`) |
| `test_nse_bhavcopy_delivery.py`| `tests/test_nse_bhavcopy_delivery.py:28`| Validates Bhavcopy downloading and delivery percentage scoring | **DEPLOYED** (`pytest`) |
| `test_shoonya_feed.py` | `tests/test_shoonya_feed.py:32` | Validates Shoonya WebSocket parser and reconnection mechanics | **DEPLOYED** (`pytest`) |
| `test_swing_risk.py` | `tests/test_swing_risk.py:26` | Validates ATR stop loss, R:R calculation, and position sizing | **DEPLOYED** (`pytest`) |
| `test_worker_scheduled.js` | `tests/test_worker_scheduled.js:1` | Node.js unit tests for Cloudflare Worker cron handlers | **DEPLOYED** (`npm test`) |

---

### 1.14 `dashboard/` — Frontend Trading Terminal & Static Assets
The `dashboard` directory houses the production trading UI served statically by Cloudflare Worker edge caching and `api/routers/market.py:2563`.

| File | Defined At | Role | State | Primary Servicing Route |
|---|---|---|---|---|
| `index.html` | `dashboard/index.html:1` | Main trading terminal UI layout | **DEPLOYED** | `/`, `/index.html` |
| `app.js` | `dashboard/app.js:1` | Main client SPA script (WebSocket listener, UI DOM binding) | **DEPLOYED** | `/app.js` |
| `admin.html` | `dashboard/admin.html:1` | Operator administrative control panel | **DEPLOYED** | `/admin.html` |
| `crypto.html` | `dashboard/crypto.html:1` | Dedicated high-volatility cryptocurrency scanner terminal | **DEPLOYED** | `/crypto.html` |
| `landing.html` | `dashboard/landing.html:1` | Marketing landing page | **DEPLOYED** | `/landing` |
| `track-record.html` | `dashboard/track-record.html:1` | Public audited performance track record page | **DEPLOYED** | `/track-record.html` |
| `legal.html` | `dashboard/legal.html:1` | Regulatory disclaimers and terms of service | **DEPLOYED** | `/legal` |
| `config.js` | `dashboard/config.js:1` | Frontend environment configuration and API endpoint base URL | **DEPLOYED** | `/config.js` |
| `honesty.js` | `dashboard/honesty.js:1` | Real-time quote honesty indicator badge and provenance checker | **DEPLOYED** | `/honesty.js` |
| `glass-charts.js` | `dashboard/glass-charts.js:1` | Canvas/Chart.js financial charting and multi-timeframe overlays | **DEPLOYED** | `/glass-charts.js` |
| `tradeflow_helper.js` | `dashboard/tradeflow_helper.js:1` | Orderbook depth and tick volume visualizer | **DEPLOYED** | `/tradeflow_helper.js` |
| `stats_helper.js` | `dashboard/stats_helper.js:1` | Win rate, profit factor, and Sharpe ratio calculation helper | **DEPLOYED** | `/stats_helper.js` |
| `skin.js` | `dashboard/skin.js:1` | UI dark/light theme switching and local storage persistence | **DEPLOYED** | `/skin.js` |
| `styles.css` / `glass.css` | `dashboard/*.css:1` | CSS styling rules, responsive layouts, and modal themes | **DEPLOYED** | Static CSS delivery |
| `vendor/chart.umd.min.js` | `dashboard/vendor/*:1` | Bundled Chart.js v4.4.1 library | **DEPLOYED** | `/vendor/chart.umd.min.js` |

---

## 2. Auxiliary, Inactive & Unused Directories

The following directories exist in the repository root but are **NOT** part of the active production runtime path:

| Directory | Purpose | State | Rationale & Evidence |
|---|---|---|---|
| `tradvix-app/` | Vite + React frontend experiment | **DEFINED** (CI Only) | Tested in CI (`.github/workflows/ci.yml:100`), but **not** deployed or served on Render or Cloudflare Worker (`wrangler.jsonc:2`). The live UI is entirely served from `dashboard/`. |
| `deploy/` | Linux systemd service unit | **DEFINED** | Contains `tradeai-daemon.service`. Irrelevant to containerized Render deployment (`render.yaml`). |
| `migrations/` | Alembic database migrations | **DEFINED** | Database tables are created dynamically at application boot via `init_db()` (`db/connection.py:150`, `db/models.py:590`). Alembic versions are not executed in production. |
| `reports/` | Test execution XML artifacts | **DEFINED** | Contains static JUnit XML reports (`junit_baseline.xml`, `junit_final.xml`). |
| `backtest_results/`| Historical backtest dumps | **DEFINED** | Contains historical JSON backtest dumps and CSV trade exports (`report_20260428_*.json`). |
| `awesome-codex-skills/`| External coding skill manifests | **DEFINED** | External agent metadata, unreferenced by application code. |
| `scratch/` | Developer scratch scripts | **DEFINED** | Temporary development scripts for testing and profiling. |
| `logs/` | Runtime log and paper trade files | **DEFINED** | Local JSON trade dumps (`paper_trades_*.json`) and log files (`trading.log`). |

---

## 3. Disconnected / Dead Code Modules Flagged in Active Directories

During the AST and caller audit, three modules in core directories were identified as having **zero non-test callers**:

1. **`db/prediction_tracker.py`** (`db/prediction_tracker.py:38`):
   - **Defined**: In-memory circular buffer and JSON audit store for prediction outcomes.
   - **Evidence**: `0 callers` across the entire codebase. Completely disconnected.
2. **`utils/retry.py`** (`utils/retry.py:24`):
   - **Defined**: Async retry and timeout decorators (`async_retry`, `retry_with_timeout`).
   - **Evidence**: `0 callers` across the entire codebase. Callers across `api/` and `agents/` implement custom retry loops or rely on `httpx` directly.
3. **`agents/signal_engine/composite_decision.py`** (`agents/signal_engine/composite_decision.py:30`):
   - **Defined**: 9-pillar composite decision synthesizer for "Data Brain V3".
   - **Evidence**: Only called in `tests/test_data_brain_v3.py:35`. Zero callers in production signal generation (`signal_generator.py`) or API routes (`api/service.py`).
