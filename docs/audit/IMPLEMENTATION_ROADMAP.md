# Master Implementation Roadmap & Technical Evolution Plan

**Document ID:** TRADEAI-ROADMAP-001  
**Platform:** TradeAI PRO  
**Current Milestone:** Master Technical Audit & Core Engine Hardening (Complete)  
**Date:** November 1, 2026  

---

## 1. Roadmap Overview & Timeline

```mermaid
timeline
    title TradeAI PRO Strategic Engineering Roadmap
    section Milestone 1 (Completed)
        A-Z Master Technical Audit : Complete 15 workstreams
        Quant Hardening & Leverage Bounds : 1.0x cash purchasing power
        Conservative Same-Bar Resolution : Stop-Loss priority
        Data Rate Limiter & Pacing : Non-blocking Forex cache
        Model Transparency Badging : Heuristic mode notification
        100% Test Suite Verification : 389 passing unit tests
    section Milestone 2 (Next Sprint)
        AI Model Weights Training : XGBoost & LightGBM pretraining pipeline
        Automated Nightly Retraining : Cron-based data ingestion & training
        DEX On-Chain Pool Scraping : DexScreener enhanced liquidity filters
    section Milestone 3 (Scale & Infrastructure)
        Managed PostgreSQL Cloud Wireup : Supabase / Render Postgres
        Redis Distributed Rate Limiter : Multi-worker cache tier
        Real-time NSE WebSocket Broker Feed : Low-latency tick streaming
```

---

## 2. Completed Milestones (Milestone 1)

### Workstreams Executed:
1. **Core Architecture & Repositories Mapped**:
   * Documented 83 API endpoints, 14 ORM models, and system topology in `REPOSITORY_MAP.md`.
2. **Database Engine Hardening**:
   * Implemented hybrid database connection (`db/connection.py`) supporting both zero-config SQLite WAL and high-concurrency PostgreSQL connection pooling (`pool_pre_ping=True`).
   * Eliminated all deprecated `datetime.utcnow()` warnings across `db/swing_store.py` and `db/runtime_state.py`.
3. **Quantitative Risk & Leverage Containment**:
   * Bound `PaperTradingEngine` and `calculate_position_size` to strict $1.0\text{x}$ cash purchasing power limit (`MAX_POSITION_LEVERAGE = 1.0`).
   * Validated conservative same-bar collision rule (SL hit first).
   * Aligned Sharpe and Sortino ratios to true calendar daily equity returns.
4. **Market Data & Zero-Cost Architecture**:
   * Hardened Twelve Data Forex scanner (`agents/market_intelligence/forex_scanner.py`) with non-blocking concurrency control (`is_forex_scanning()`), eliminating 135-second client timeouts.
   * Free keyless RSS sentiment and public Binance WebSockets operational.
5. **AI Engine Transparency & Full Spectrum Contract**:
   * Added `get_model_status()` exposing ensemble readiness.
   * Displayed explicit UI badges (`AI Engine: Statistical Heuristic (0/3 models fitted)`) across Brahmand Cockpit and Navbar.
   * Restored missing Momentum component column in Edge Top Setups.
6. **Automated Verification**:
   * Achieved **100% test pass rate** (`389 passed, 0 failed` in 84s).

---

## 3. Milestone 2: Machine Learning Model Pre-Training Pipeline (Weeks 1–2)

### Objectives:
1. **Offline Training Script Orchestration**:
   * Build `scripts/train_ensemble.py` to ingest 2 years of daily/hourly OHLCV data for top 50 NSE equities and top 10 crypto pairs.
   * Extract indicators via `agents/feature_engine/indicators.py` and compute outcome labels via `label_generator.py`.
2. **Model Artifact Generation**:
   * Train and serialize `models/xgb_model.json`.
   * Train and serialize `models/lgbm_model.txt`.
   * Train and export PyTorch LSTM weights to `models/best_model.pt`.
3. **Verification**:
   * Confirm `/api/health` transitions from `Statistical Heuristic (0/3 models fitted)` to `Trained ML Ensemble (3/3 fitted)`.

---

## 4. Milestone 3: Distributed State & Database Scale (Weeks 3–4)

### Objectives:
1. **Cloud Database Provisioning**:
   * Provision Render PostgreSQL or Supabase instance.
   * Configure environment variable `DATABASE_URL=postgresql://user:pass@host:5432/dbname`.
   * Execute schema migrations via `db.connection.init_db()`.
2. **Distributed Cache**:
   * Replace in-memory JSON file caches (`edge_scores_cache.json`, `paper_trades.json`) with PostgreSQL tables or Redis for multi-worker container horizontal scaling.

---

## 5. Milestone 4: Live Data Ingestion & Tick Feed Expansion (Month 2)

### Objectives:
1. **NSE Web-Socket Tick Pipe**:
   * Integrate keyless / paper-safe NSE tick receiver or simulated tick generator for live tick updates.
2. **DEX Liquidity Engine**:
   * Expand crypto meme-coin scoring with token liquidity depth, honeypot detection, and contract audit hooks.
