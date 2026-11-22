# TradeAI PRO — Background Jobs, Schedulers & Keep-Warm Architecture (P7)

> **Document Status**: Complete & Verified (Audit Phase 7)  
> **Repository Context**: Multi-Plane Orchestration (Render Asyncio Lifespan + Cloudflare Worker Crons + GitHub Actions CI/CD)  
> **Audit Constraint**: Read-Only reverse-engineering audit. Every entry backed by exact `file:line` citations, failure modes, and resource consumption analysis.

---

## 1. Executive Summary & Topology Map

System execution is governed by a distributed scheduling mesh split across three independent computing tiers:
1. **Tier 1: In-Process Asyncio Tasks (`api/app.py` lifespan)**: Long-running event loops and daemon supervisors hosted inside the Render containers.
2. **Tier 2: Cloudflare Edge Worker Cron (`src/worker.js`, `wrangler.jsonc`)**: External serverless triggers providing sub-minute HTTP keep-warm pings and single-flight market refresh signals.
3. **Tier 3: GitHub Actions Workflows (`.github/workflows/*.yml`)**: Serverless CI/CD runners executing compute-intensive batch jobs (weekly model training, EOD scans, and autonomous trading cycles).

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               CLOUDFLARE EDGE WORKER                                   │
│  wrangler.jsonc: "*/2 3-10 * * 1-5" (Market) + "*/10 * * * *" (24/7 Keep-Warm)         │
└────────────────────────────────────────────────────────────────────────────────────────┘
       │                                     │                                  │
       │ GET /api/health/live (every 2m/10m) │ POST /api/cron/refresh (every 2m)│ Repository Dispatch
       ▼                                     ▼                                  ▼
┌───────────────────────────────┐     ┌──────────────────────────────┐   ┌───────────────────────────┐
│     RENDER BOX A (API)        │     │     RENDER BOX B (ML)        │   │    GITHUB ACTIONS CI      │
│ - _background_refresh (5m)    │     │ - Dedicated ML Sidecar       │   │ - autonomous_cycle.yml    │
│ - snapshot_broadcaster (1s)   │     │ - Warmup via ModelRegistry   │   │ - nightly_swing_scan.yml  │
│ - cycle_scheduler (1m)        │     │ - Bypasses PyTorch heap      │   │ - model_train_scheduled   │
│ - eod_review_scheduler (1m)   │     │                              │   │ - ci.yml                  │
│ - swing_outcome_eval (1h)     │     │                              │   │ - health.yml              │
└───────────────────────────────┘     └──────────────────────────────┘   └───────────────────────────┘
```

---

## 2. Complete Background Jobs & Schedulers Table

The table below catalogs every in-process asyncio task, Cloudflare cron trigger, and GitHub Actions workflow in the repository.

| Job Name | Defined at (file:line) | Trigger & Cadence | What It Reads | What It Writes | Failure Mode / Handling | Keeps Box Awake? | Est. Instance / Runner Hours/Mo | Component State |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`warmup_and_hydrate`** | `api/app.py:267` | One-shot at container boot | `DATABASE_URL`, Supabase `runtime_state` (`tradeflow_snapshot:v1`, `watchlist_eod:v1`) | Memory caches (`_tradeflow_cache`, `_watchlist_cache`) | Non-fatal: logged as warning; container boots in on-demand mode | **No** (one-shot ~3s) | Negligible (< 0.01h) | **WIRED & DEPLOYED** |
| **`snapshot_broadcaster`** | `api/app.py:268` & `api/websocket_broadcaster.py:18` | Continuous loop (1.0s interval) | In-memory `_market_data` snapshot & quote cache | Active WebSocket client connections (`/ws/prices`) | Drops disconnected clients; auto-reconnects with exponential backoff | **Yes** (while container runs) | Bound to Box A runtime | **WIRED & DEPLOYED** |
| **`startup_reconcile`** | `api/app.py:272` & `api/service.py:1059` | One-shot at container boot | `paper_positions` table, misses bars from yfinance | Executes missed stop-loss triggers; updates DB trade status | Non-fatal: logged as debug; skips if engine unavailable | **No** (one-shot ~5s) | Negligible (< 0.01h) | **WIRED & DEPLOYED** |
| **`initial_load`** | `api/app.py:280` & `api/service.py:1142` | One-shot at boot (if `INITIAL_LOAD_ENABLED=true`) | Top 15 priority NSE symbols via `fetch_ohlcv` | In-memory `_market_data`, `_loading_status` | Gated by 90s timeout; falls back to background refresh | **No** (one-shot) | Disabled by default in `render.yaml:58` | **WIRED** (Disabled in Prod) |
| **`background_refresh`** | `api/app.py:292` & `api/service.py:1311` | Periodic loop: 300s market hours, 900s off-hours/weekends | Shoonya/TV/Yahoo feeds, `DATABASE_URL` | In-memory `_market_data`, SQLite/Supabase `read_cached_ohlcv` | Single-flight locked; 240s cycle timeout; logs error and sleeps | **Yes** (internal event loop) | Bound to Box A runtime | **WIRED & DEPLOYED** |
| **`swing_outcome_background`**| `api/app.py:308` & `api/service.py:1376` | Periodic loop: 3600s (1 hour interval) | `swing_shortlist` table, current market bars | Updates `swing_outcome` records in SQLite/Supabase | Catches all exceptions, logs error, resumes next hour | **Yes** (internal event loop) | Bound to Box A runtime | **WIRED & DEPLOYED** |
| **`stock_screener_task`** | `api/app.py:326` & `agents/stock_screener.py:28` | Periodic loop: 180s (if `SCREENER_TASK_ENABLED=true`) | NSE stock universe OHLCV | Screener shortlist cache | Disabled in production (`render.yaml:72=false`) to conserve RAM | **No** (disabled in prod) | 0h (disabled) | **WIRED** (Disabled in Prod) |
| **`morning_scan_task`** | `api/app.py:336` & `agents/stock_screener.py:65` | Scheduled at 08:45 IST (if `MORNING_SCAN_TASK_ENABLED=true`) | Pre-market quotes & news | Pre-market watchlist cache | Disabled in production (`render.yaml:74=false`) | **No** (disabled in prod) | 0h (disabled) | **WIRED** (Disabled in Prod) |
| **`nightly_swing_scan` (In-Process)** | `api/app.py:345` & `api/service.py:1420` | Scheduled at 16:00 IST weekdays (if `NIGHTLY_SCAN_ENABLED=true`) | NIFTY 500 OHLCV data | `swing_shortlist` DB table | Disabled in `render.yaml` (delegated to GitHub Actions runner) | **No** (disabled in prod) | 0h (delegated) | **WIRED** (Delegated to CI) |
| **`keep_alive_task` (In-Process)** | `api/app.py:353` & `api/app.py:133` | Periodic loop: 600s (if `SELF_PING_ENABLED=true`) | None | Sends HTTP GET to `/api/health/live` & Box B `/health/live` | Disabled in production (`render.yaml:76=false`) in favor of Edge cron | **No** (disabled in prod) | 0h (disabled) | **WIRED** (Disabled in Prod) |
| **`warm_bar_probe`** | `api/app.py:393` & `api/app.py:360` | One-shot at boot | `NIFTY 50` 5m bar via `fetch_ohlcv` | Supabase `runtime_state` key `last_bar_probe:v1` | Non-fatal debug log; does not block startup | **No** (one-shot ~2s) | Negligible (< 0.01h) | **WIRED & DEPLOYED** |
| **`warmup_priority_quotes`** | `api/app.py:395` | One-shot at boot | Top priority symbols via `MarketDataRouter` | Memory quote cache | Non-fatal: skips symbols that fail | **No** (one-shot ~3s) | Negligible (< 0.01h) | **WIRED & DEPLOYED** |
| **`cycle_scheduler`** | `api/app.py:404` & `api/cycle_scheduler.py:92` | Checks every 60s; fires every 20m during 09:15-15:30 IST weekdays | GitHub repo state, `runtime_state` marker | Sends REST POST to GitHub Actions API to trigger workflow | Logs warning if `GITHUB_TOKEN` unset; backs off gracefully | **Yes** (supervisor loop) | Bound to Box A runtime | **WIRED & DEPLOYED** |
| **`eod_review_scheduler`** | `api/app.py:419` & `api/learning_scheduler.py:92` | Checks every 60s; fires once daily at 15:45-16:30 IST weekdays | `trade_outcomes_pg` table | Updates `learned_weights:v1` in `runtime_state` & JSON sidecar | Single-run-per-day lock via DB marker; logged if skipped | **Yes** (supervisor loop) | Bound to Box A runtime | **WIRED & DEPLOYED** |
| **Cloudflare Cron (Market Hours)** | `wrangler.jsonc:8` & `src/worker.js:1071` | `*/2 3-10 * * 1-5` (Every 2 mins, 08:30-16:29 IST Mon-Fri) | Box A, B, C `/health/live`, Worker secrets | POST `/api/cron/refresh`, edge cache pre-warm, dispatches GitHub cycle | Edge fetch timeout (10s); non-fatal, logs warning, retries next tick | **YES (Primary keep-warm)** | ~176h of active pings/mo | **WIRED & DEPLOYED** |
| **Cloudflare Cron (24/7 Keep-Warm)** | `wrangler.jsonc:9` & `src/worker.js:1071` | `*/10 * * * *` (Every 10 mins, 24/7) | Box A, B, C `/health/live` | Pings `/health/live` endpoints | Non-fatal: logs error, retries on next 10m tick | **YES (Keeps containers awake 24/7)** | ~720h/mo per container | **WIRED & DEPLOYED** |
| **GitHub: `autonomous_cycle.yml`** | `.github/workflows/autonomous_cycle.yml:1` | Scheduled crons + `repository_dispatch` from Worker & Box A | Market feeds, `DATABASE_URL`, Telegram secrets | `paper_positions`, `trade_outcomes_pg`, Telegram channel | 8m job timeout; concurrency group `cancel-in-progress: false` | **No** (runs on GitHub Actions) | ~22h runner time/mo (~660 min) | **WIRED & DEPLOYED** |
| **GitHub: `ci.yml`** | `.github/workflows/ci.yml:1` | Push & PR to `main`/`master` | Repository source code | Test reports, build artifacts, lint status | Fails build if pytest fails or API imports fail | **No** (runs on GitHub Actions) | ~3h runner time/mo | **WIRED & DEPLOYED** |
| **GitHub: `health.yml`** | `.github/workflows/health.yml:1` | `*/30 3-10 * * 1-5` (Every 30m Mon-Fri market hours) | `https://trading-api-n1wt.onrender.com/api/health` | Webhook alert if status code $\ge 2$ | 30s grace retry on failure; exits code 1 if critical | **YES (Secondary keep-warm)** | ~2h runner time/mo | **WIRED & DEPLOYED** |
| **GitHub: `keep_alive.yml`** | `.github/workflows/keep_alive.yml:1` | **Disabled schedule** (`workflow_dispatch` manual only) | Target URL health probe | Logs HTTP status | Retries once after 10s if non-200 | **No** (manual dispatch only) | 0h (disabled) | **DEFINED** (Schedule Commented Out) |
| **GitHub: `model_train_scheduled.yml`** | `.github/workflows/model_train_scheduled.yml:1` | `0 0 * * 0` (Every Sunday 00:00 UTC / 05:30 IST) | 730 days of OHLCV data, `DATABASE_URL` | Publishes gzip bundle to Supabase `model_bundle:v1` | 45m job timeout; accuracy gate rejects bundles $< 50\%$ | **No** (runs on GitHub Actions) | ~0.5h runner time/mo (~30 min) | **WIRED & DEPLOYED** |
| **GitHub: `nightly_swing_scan.yml`** | `.github/workflows/nightly_swing_scan.yml:1` | `30 10 * * 1-5` (16:00 IST Mon-Fri after market close) | NIFTY 500 EOD Bhavcopy, `DATABASE_URL` | `swing_shortlist` table, Telegram daily alert | 25m job timeout; holiday check skips market holidays | **No** (runs on GitHub Actions) | ~2.2h runner time/mo (~132 min) | **WIRED & DEPLOYED** |

---

## 3. Keep-Warm Sources & Redundancy Analysis

### 3.1 All Identified Keep-Warm Sources

There are **four distinct mechanisms** designed to ping or keep services awake:

1. **Cloudflare Worker Edge Cron (`src/worker.js:1071`)**:
   - Primary active keep-warm mechanism.
   - Pings Box A `/api/health/live`, Box B `/health/live`, and Box C `/health/live`.
   - Frequency: Every 2 minutes during market hours (`*/2 3-10 * * 1-5`) and every 10 minutes 24/7 (`*/10 * * * *`).
2. **GitHub Actions Health Probe (`.github/workflows/health.yml:6`)**:
   - Secondary external keep-warm mechanism.
   - Frequency: Every 30 minutes during market hours (`*/30 3-10 * * 1-5`).
   - Hits Box A `/api/health` directly.
3. **In-Process Self-Ping Task (`api/app.py:133` `_keep_alive_task`)**:
   - Internal keep-warm mechanism.
   - Frequency: Every 600 seconds (10 minutes).
   - **Current Production State**: Explicitly set to `SELF_PING_ENABLED="false"` in `render.yaml:76`. Inactive in production.
4. **GitHub Actions Keep-Alive Workflow (`.github/workflows/keep_alive.yml`)**:
   - Legacy keep-warm mechanism.
   - **Current Production State**: Scheduled trigger is commented out (`# - cron: '*/14 3-10 * * 1-5'`). Inactive unless manually dispatched.

### 3.2 Overlap & Risk Analysis

- **Market-Hours Ping Collision**: Between 08:30 and 16:30 IST on weekdays, Cloudflare Worker pings every 2 minutes while GitHub Actions `health.yml` simultaneously pings every 30 minutes. This overlap is intentionally redundant so that if GitHub drops scheduled runs, the Cloudflare Worker maintains container readiness.
- **Probe Route Discrepancy**:
  - Cloudflare Worker correctly probes `/api/health/live` (< 1ms, zero DB access).
  - GitHub Actions `health.yml` probes `/api/health`, which queries database pools, model registry, and memory stats. On a 0.1-CPU free container, this can occasionally take > 2 seconds.
- **24/7 Idle Sleep Prevention**:
  - Render free tier spins down containers after **15 minutes** of HTTP inactivity.
  - The Cloudflare Worker cron `"*/10 * * * *"` executes every 10 minutes 24/7.
  - **Direct Consequence**: Because 10 minutes < 15 minutes, **the containers are kept awake 24/7 indefinitely**.

---

## 4. Render Instance-Hours Budget & Multi-Account Allocation

### 4.1 The Render Free-Tier Math

- Render Free Tier Policy: **750 free instance-hours per calendar month per account**.
- Total hours in a 30-day month: $30 	imes 24 = 720	ext{ hours}$.
- Total hours in a 31-day month: $31 	imes 24 = 744	ext{ hours}$.

If a single Render account hosted Box A, Box B, and Box C continuously awake:
$$	ext{Total Required Hours} = 3 	imes 720 = 2,160	ext{ hours/month}$$
A single account would exhaust its 750-hour allowance in **approximately 10.4 days**, causing all three services to be suspended for the remainder of the month.

### 4.2 Multi-Account Architecture Contract

To resolve this constraint, the production infrastructure is segregated across dedicated Render accounts:

1. **Account 1 (Primary - `srv-d7bsr8juibrs739ujdj0`)**:
   - Hosts Box A (`trading-api`).
   - Kept awake 24/7 via Cloudflare 10-minute cron $pprox 720	ext{ hours/month}$.
   - **Status**: Consumes $pprox 96\%$ of Account 1's 750-hour allowance. Stays strictly within quota.
2. **Account 2 (Secondary - `srv-trading-ml-8umr`)**:
   - Hosts Box B (`trading-ml`).
   - Kept awake 24/7 via Cloudflare 10-minute cron $pprox 720	ext{ hours/month}$.
   - **Status**: Consumes $pprox 96\%$ of Account 2's 750-hour allowance. Stays strictly within quota.
3. **Box C Market Data Feed (`trading-feed`) Strategy**:
   - If hosted on a third independent Render account, it can safely run 24/7.
   - If hosted on Account 1 or 2, its keep-warm ping in Cloudflare Worker must be restricted strictly to market hours (08:30 - 16:30 IST Mon-Fri = 176 hours/month) to prevent exhausting pooled hours.

---

## 5. GitHub Actions Runner Consumption

GitHub Free Tier allowance: **2,000 Linux runner minutes/month**.

| Workflow | Frequency | Duration per Run | Monthly Runs | Monthly Runner Minutes | % of 2,000 Min Quota |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `autonomous_cycle.yml` | ~20 runs/day (weekdays) | ~1.5 min | ~440 | ~660 min | 33.0% |
| `nightly_swing_scan.yml` | Once daily (weekdays) | ~6.0 min | ~22 | ~132 min | 6.6% |
| `model_train_scheduled.yml` | Once weekly (Sundays) | ~8.0 min (quick) | ~4 | ~32 min | 1.6% |
| `health.yml` | Every 30m market hours | ~0.5 min | ~352 | ~176 min | 8.8% |
| `ci.yml` | PR & commit triggers | ~2.5 min | ~40 | ~100 min | 5.0% |
| **TOTAL** | — | — | — | **~1,100 min/mo** | **55.0% (Well within 2,000 min cap)** |
