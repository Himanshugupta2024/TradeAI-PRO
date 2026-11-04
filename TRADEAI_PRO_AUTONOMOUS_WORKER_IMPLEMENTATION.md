# TradeAI PRO — Production-Grade Zero-Click Autonomous Paper Worker Implementation Report

**Author**: Senior Cloud Infrastructure & Quantitative System Architect  
**Project**: TradeAI PRO  
**Date**: November 2026  
**Status**: COMPLETE & VERIFIED IN PRODUCTION TEST ENVIRONMENT  

---

## 1. Executive Summary & Root Cause Confirmation

In prior system audits, the **Autonomous Decision Feed** on the dashboard was found to be empty. The comprehensive investigation established the exact technical root cause:

1. **Render Free-Tier Resource Constraints**: Render runs in `LIGHTWEIGHT_API_MODE` (`BACKGROUND_REFRESH_ENABLED=false`) to protect the 512 MB RAM web process from Out-Of-Memory (OOM) process kills. Moving a continuous infinite-loop background scanner back to the web service would trigger severe stability regressions.
2. **Missing Distributed Execution Scheduler**: `scripts/scanner_daemon.py` was architected as an infinite-loop daemon suited for a persistent VM (e.g., Oracle Cloud ARM), but no dedicated scheduler existed to drive discrete, decoupled execution passes.
3. **No Distributed Idempotency or Lock**: There was no atomic PostgreSQL-backed concurrency lock to prevent overlapping runs across serverless environments, nor deterministic signal deduplication across worker retries.

### Target Solution Implemented:
We designed, implemented, and verified a **100% Zero-Click Distributed Paper Worker Architecture** that decouples heavy technical scanning and paper execution from Render, running discrete single cycles via **GitHub Actions** and on-demand API triggers into **Supabase PostgreSQL**, with live status and feed consumption delivered to the **Cloudflare Workers frontend** via Render's lightweight API.

---

## 2. Distributed Architecture & Component Flow

```
┌────────────────────────────────────────────────────────────────────────┐
│                        TRIGGERS & SCHEDULING                           │
│                                                                        │
│   GitHub Actions Cron (Mon-Fri 03:45-10:00 UTC)                        │
│   Supabase Webhook / Database Trigger                                  │
│   Manual Trigger: POST /api/autonomous/run-cycle (Rate-Limited 60s)    │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│             SINGLE-CYCLE WORKER (scripts/autonomous_cycle.py)          │
│                                                                        │
│  1. Acquire Distributed Lock (worker_distributed_locks)                │
│  2. Session Guard (Asia/Kolkata: NSE Cash vs 24/7 Crypto)             │
│  3. Asset Universe Resolution (Tier 1: Top 15 NSE + Top 3 Crypto)      │
│  4. Data Fetching & Freshness Gate (LIVE <= 15m, STALE > 60m rejected) │
│  5. Indicators & Quantitative Regime Detection                         │
│  6. Signal Generation + Deterministic Signal ID (SHA-256)              │
│  7. Idempotency Check (autonomous_decision_records)                    │
│  8. Risk Gates (Max 8 slots, R:R >= 1.5, SL/TP geometry)               │
│  9. Paper Trading Execution (NO REAL CAPITAL)                          │
│ 10. Position Management (Trailing Stop, +1R BE, +1.5R 50% TP, 15:15 EOD)│
│ 11. Dual-Write Decision Feed (DB + runtime_state 'autonomous_feed:v1') │
│ 12. Release Distributed Lock & Clean Exit 0                            │
└──────────────────┬───────────────────────────────┬─────────────────────┘
                   │                               │
                   ▼                               ▼
┌───────────────────────────────────────┐   ┌────────────────────────────┐
│      SUPABASE POSTGRESQL DATABASE     │   │   POSTGRES RUNTIME STATE   │
│                                       │   │                            │
│ • worker_distributed_locks            │   │ • autonomous_feed:v1       │
│ • autonomous_decision_records         │   │ • autonomous_worker:status │
│ • trades / trade_outcomes             │   │ • latest_market_snapshot   │
└──────────────────┬────────────────────┘   └──────────────┬─────────────┘
                   │                                       │
                   └───────────────────┬───────────────────┘
                                       │
                                       ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      RENDER LIGHTWEIGHT FASTAPI                        │
│                                                                        │
│ • GET  /api/autonomous/status    (Includes Worker Watchdog & Lock Info)│
│ • GET  /api/autonomous/feed      (Reads DB & runtime_state)            │
│ • POST /api/autonomous/run-cycle (Rate-limited GitHub / Local trigger) │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                     CLOUDFLARE WORKER FRONTEND                         │
│                                                                        │
│ • Worker Status Pill: "Healthy (Xm ago)" / "Running ⚡" / "Standby"     │
│ • Decision Feed Cards with action color-coding & real metrics          │
│ • "⚡ Run Cycle" Manual Trigger Button                                  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Database Schema: Distributed Locks & Decision Persistence

Two enterprise-grade database models were added to `db/models.py` and provisioned directly in Supabase PostgreSQL:

### A. Distributed Locks (`worker_distributed_locks`)
```python
class WorkerDistributedLock(Base):
    __tablename__ = "worker_distributed_locks"

    id = Column(Integer, primary_key=True, index=True)
    job_name = Column(String(64), unique=True, nullable=False, index=True)
    run_id = Column(String(64), nullable=False)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    locked_until = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="RUNNING")  # RUNNING, COMPLETED, FAILED, TIMED_OUT
    heartbeat_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    meta_json = Column(Text, nullable=True)
```

### B. Autonomous Decision Records (`autonomous_decision_records`)
```python
class AutonomousDecisionRecord(Base):
    __tablename__ = "autonomous_decision_records"

    id = Column(Integer, primary_key=True, index=True)
    signal_id = Column(String(96), unique=True, nullable=False, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    action = Column(String(32), nullable=False)  # AUTO_ENTER, TRAILING_STOP, TARGET_HIT, STOP_LOSS, etc.
    direction = Column(String(16), nullable=True)
    entry_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    target_price = Column(Float, nullable=True)
    quality_score = Column(Float, nullable=True)
    confluence_score = Column(Float, nullable=True)
    risk_reward = Column(Float, nullable=True)
    market_regime = Column(String(32), nullable=True)
    source_freshness = Column(String(16), nullable=True)  # LIVE, DELAYED, STALE, EOD
    paper_trade_id = Column(Integer, nullable=True)
    reason = Column(Text, nullable=True)
    meta_json = Column(Text, nullable=True)
```

---

## 4. Key Modules Implemented

### 1. `agents/execution/distributed_lock.py`
- **Class `DistributedLockManager`**:
  - `acquire(job_name, run_id, duration_sec, meta)`: Atomically checks existing lock. If active (`RUNNING` and `locked_until > now` and fresh heartbeat), returns `False`. If expired/stale, reclaims ownership.
  - `heartbeat(job_name, run_id, extend_sec)`: Extends lock duration during long scans.
  - `release(job_name, run_id, status, meta)`: Marks job `COMPLETED` and clears lock.
  - `get_lock_info(job_name)`: Returns inspection dictionary for API watchdog.
- **Context Manager `DistributedLock`**: Enables `with DistributedLock("job", run_id) as acquired:` blocks.

### 2. `agents/execution/idempotency.py`
- `generate_signal_id(symbol, strategy, direction, candle_time, session_date, version)`: Computes a deterministic SHA-256 hash formatted as `sig-{symbol}-{session_date}-{hash16}`.
- `is_signal_already_executed(signal_id)`: Verifies if this signal has already produced an `AUTO_ENTER` order.
- `record_autonomous_decision_to_db(...)`: Dual-writes the decision record into the relational `autonomous_decision_records` table and updates `runtime_state("autonomous_feed:v1")` with a capped rolling window of 100 entries.

### 3. `agents/execution/session_guard.py`
- `is_nse_market_open(dt)`: Enforces Indian equity cash trading hours:
  - Weekday check: Mon–Fri only.
  - Time window: **09:15 to 15:30 IST**.
  - Indian holiday calendar check (`agents.market_intelligence.nse_holidays`).
- `check_session_gate(asset_class, dt)`: Handles asset-specific market sessions (NSE for Equities, 24/7 for Crypto, 24/5 for Forex).
- `evaluate_data_freshness(df, symbol)`: Evaluates bar timestamp against UTC now:
  - `LIVE`: $\le$ 15 minutes.
  - `DELAYED`: 15–60 minutes.
  - `STALE`: > 60 minutes (**rejects entry orders**).
  - `EOD`: Prior trading day bar (**rejects intraday entry orders**).

### 4. `scripts/autonomous_cycle.py` & `scripts/scanner_daemon.py`
- Single-cycle CLI:
  ```bash
  python scripts/autonomous_cycle.py --universe tier1
  python scripts/scanner_daemon.py --once
  ```
- Flags: `--universe`, `--once`, `--run-id`, `--force-scan`, `--dry-run`, `--max-symbols`.
- Executes all 15 discrete lifecycle steps and releases the lock before exiting with code 0.

---

## 5. GitHub Actions Workflow: `.github/workflows/autonomous_cycle.yml`

A dedicated, lightweight GitHub Actions workflow was created with:
- **Intraday Market Crons**:
  - `45 3 * * 1-5`: 03:45 UTC (09:15 IST Market Open)
  - `*/20 4-9 * * 1-5`: Every 20 minutes across 09:30–15:10 IST trading hours
  - `0,15 10 * * 1-5`: 15:30 IST Market Close & EOD auto-square-off
  - `0 */3 * * 0,6`: Weekend crypto checks
- **Manual Dispatches**: `workflow_dispatch` with parameter dropdowns and `repository_dispatch` for external webhooks.
- **Concurrency Protection**: `group: autonomous-paper-worker`, `cancel-in-progress: false`.
- **Execution Budget**: 8-minute timeout constraint.

---

## 6. Render API Endpoints & Health Watchdog

Updated in `api/routes.py`:
- `GET /api/autonomous/status`:
  - Returns `status` (mode, enabled, limits, open positions).
  - Returns `worker` (latest run_id, timestamp, duration, error count).
  - Returns `lock` (lock holder, started_at, status).
  - Returns `watchdog`:
    - `RUNNING`: Worker currently active.
    - `HEALTHY`: Last cycle < 35 minutes ago during market hours.
    - `WORKER_STALE`: Last cycle > 35 minutes ago during market hours.
    - `MARKET_CLOSED`: Off-hours standby.
- `GET /api/autonomous/feed?limit=25`:
  - Three-tiered fallback: `runtime_state("autonomous_feed:v1")` $\to$ `autonomous_decision_records` table $\to$ memory. Never returns a blank feed if records exist.
- `POST /api/autonomous/run-cycle`:
  - 60-second rate-limiting cooldown per client.
  - Dispatches via GitHub Actions REST API if `GITHUB_TOKEN` is present, or runs asynchronously in a local background thread.

---

## 7. Dashboard Frontend Enhancements

In `dashboard/index.html` & `dashboard/app.js`:
- **Worker Status Pill (`#autonomousWorkerBadge`)**: Dynamically indicates `Worker: Healthy (Xm ago)`, `Worker: Running ⚡`, `Worker: Standby (Closed)`, or `Worker: Stale`.
- **Manual Trigger Button (`#runAutonomousCycleBtn`)**: `⚡ Run Cycle` triggers immediate cycle with visual loading feedback and toast notifications.
- **Polished Decision Feed Cards**: Distinct color badges for `AUTO_ENTER` (emerald), `TRAILING_STOP`/`TARGET_HIT` (green), `STOP_LOSS` (rose), `EOD_SQUAREOFF` (amber), `SKIP_DUPLICATE_SIGNAL`/`SKIP_STALE_DATA` (purple), and `NO_TRADE`/`MARKET_CLOSED` (slate), with live timestamps, prices, and QS/AI score badges.
- **Cache-Buster Bumped**: Updated to `app.js?v=ee3_4` in `dashboard/index.html`.

---

## 8. Verification & Test Results

### A. Dedicated Test Suite
```bash
pytest tests/test_autonomous_cycle.py tests/test_autonomous_api.py -v
```
**Results**:
- `tests/test_autonomous_cycle.py::test_distributed_lock_acquisition_and_release`: **PASSED**
- `tests/test_autonomous_cycle.py::test_distributed_lock_stale_recovery`: **PASSED**
- `tests/test_autonomous_cycle.py::test_distributed_lock_context_manager`: **PASSED**
- `tests/test_autonomous_cycle.py::test_deterministic_signal_id_generation`: **PASSED**
- `tests/test_autonomous_cycle.py::test_idempotency_duplicate_signal_check`: **PASSED**
- `tests/test_autonomous_cycle.py::test_nse_market_open_time_windows`: **PASSED**
- `tests/test_autonomous_cycle.py::test_crypto_session_gate_always_active`: **PASSED**
- `tests/test_autonomous_cycle.py::test_data_freshness_evaluation`: **PASSED**
- `tests/test_autonomous_api.py::test_get_autonomous_status`: **PASSED**
- `tests/test_autonomous_api.py::test_get_autonomous_feed`: **PASSED**
- `tests/test_autonomous_api.py::test_toggle_autonomous_mode`: **PASSED**
- `tests/test_autonomous_api.py::test_trigger_autonomous_cycle_endpoint`: **PASSED**
- **12 passed, 0 failed in 9.61s**.

### B. Live Worker Dry-Run
```bash
python scripts/autonomous_cycle.py --universe BTC/USDT --dry-run
```
- **Distributed Lock Acquired**: `cycle-20261104-224657-9cc0a6`
- **Session Gate**: Evaluated NSE as closed (`NSE_POST_MARKET`), correctly isolated `BTC/USDT`.
- **Candle Freshness**: Fetched 864 live 5m candles, validated as `LIVE`.
- **Indicators & ML Models**: Computed ATR, RSI, Regimes, and Ensemble inference.
- **Runtime State & DB**: Persisted `AutonomousDecisionRecord` (`NO_TRADE`), updated `latest_market_snapshot` and `autonomous_worker:latest_status`.
- **Lock Released**: Status `COMPLETED`, duration **18.32 seconds**.

---

## 9. Deployment & Operator Instructions

1. **Commit and Push to GitHub**:
   All changes are staged and tested. Pushing to `main` deploys the new lightweight API to Render and activates the GitHub Actions cron schedules in `.github/workflows/autonomous_cycle.yml`.
2. **GitHub Secrets Verification**:
   Ensure the following repository secrets exist under **Settings $\to$ Secrets and variables $\to$ Actions**:
   - `DATABASE_URL`: Supabase PostgreSQL connection string (`postgresql://postgres.[ref]:[pwd]@db.wtvdvaeumdoeozaxwqhv.supabase.co:5432/postgres`).
   - `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Optional alert notifications.
3. **Optional Supabase pg_cron Setup**:
   To trigger cycles directly from Supabase, run in the Supabase SQL editor:
   ```sql
   select cron.schedule(
     'autonomous-paper-worker-cycle',
     '*/20 4-9 * * 1-5',
     $$
     select net.http_post(
       url:='https://trading-api-n1wt.onrender.com/api/autonomous/run-cycle',
       headers:='{"Content-Type": "application/json"}'::jsonb,
       body:='{"universe": "tier1"}'::jsonb
     ) as request_id;
     $$
   );
   ```
4. **Cloudflare Worker Static Assets**:
   Run `npx wrangler deploy` to sync the updated `dashboard/index.html` and `dashboard/app.js?v=ee3_4` to `https://tradeai-pro.tradingpro.workers.dev/`.
