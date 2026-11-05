# TradeAI PRO — Comprehensive Verification Audit Report

**Date of Audit**: November 5, 2026  
**Audit Times**: 16:45:00 IST / 11:15:00 UTC  
**Environment**: Production (`https://tradeai-pro.tradingpro.workers.dev/`, `https://tradeai-pro.tradingpro.workers.dev/crypto`, `https://trading-api-n1wt.onrender.com/`) & Local Workspace (`http://localhost:8000/`, branch `main`)  
**Auditor Role**: Senior QA Automation Engineer, Full-Stack Reviewer, Market-Data Engineer, Quantitative Validation Specialist  
**Evaluated Architecture**: FastAPI backend (110 routes), Cloudflare Workers edge static delivery, SQLite / Supabase PostgreSQL hybrid persistence, Twelve Data & Yahoo Finance market fetchers, Binance crypto connector, Ensemble AI Engine (4 ML models + Calibrator).

---

## 1. Executive Summary & Overall Verdict

### Final System Verdict: **FAIL** (Material Financial & UI Defects)

TradeAI PRO features an advanced, multi-tiered institutional architecture spanning 110 API routes, 15 cockpit dashboard tabs, automated background worker cycles, multi-timeframe swing and intraday scanners, and ensemble machine learning infrastructure. However, a rigorous, evidence-based audit of both production and local staging environments revealed critical defects in financial accounting, pricing precision, data freshness indication, and indicator mathematics that preclude a PASS verdict:

1. **CRITICAL FINANCIAL INTEGRITY DEFECT**: Currency unit mixing without FX conversion. USD crypto P&L (e.g., -$295.88 on ETH/USDT, +$576.92 on BTC/USDT) is directly summed with INR equity P&L as raw numbers and displayed as Rupee totals (`+₹521`, `₹1,00,119`). This distorts actual portfolio risk and monetary return by ~84.5×.
2. **HIGH SEVERITY UI REGRESSION**: Loss-making open positions render negative currency amounts without a minus sign (`₹135.91 (-0.60%)` instead of `-₹135.91`), caused by an omission in `dashboard/app.js` (`const pnlSign = pnl >= 0 ? '+' : '';`).
3. **HIGH SEVERITY ACCURACY REGRESSION**: Low-priced crypto assets (e.g., DOGE, SHIB) round entry, stop loss, and target prices to 2 decimal places (`$0.07`, `$0.07`, `$0.07`), collapsing R:R to `—` and rendering trade geometry unreadable.
4. **HIGH SEVERITY CONFLICT REGRESSION**: The Overview cockpit displays `0W / 9L • 9 trades` while displaying `Win Rate: 55.6%` directly above it, because `/api/dashboard/overview` omits `winning_trades` and `losing_trades` from its returned `portfolio_summary` payload.
5. **MEDIUM SEVERITY PRICING DEFECT**: Current prices equal entry prices and unrealized P&L defaults to 0 across open positions on the Portfolio page whenever symbols are not pre-cached in `svc._market_data`, because `/api/portfolio` does not fetch live quotes on demand.
6. **MATHEMATICAL INDICATOR DEFECT**: `_rsi` in `agents/feature_engine/indicators.py` returns `NaN` instead of 100.0 on monotonic rally candles and `NaN` on flat price series due to division-by-zero treatment.
7. **RELIABILITY DEFECT**: Forex rates endpoint `/api/forex/rates` times out due to sequential synchronous 8-second rate-limit sleeps across 15 currency pairs (totaling ~120s latency).

---

## 2. System Inventory & Discovery

### 2.1 Environments & Build Identifiers
- **Frontend Edge CDN**: Cloudflare Workers (`tradeai-pro.tradingpro.workers.dev`) serving `dashboard/index.html` and `dashboard/app.js` (asset hash `v=ee3_4`).
- **Production Backend API**: Render Web Service (`https://trading-api-n1wt.onrender.com`), running `uvicorn server:app`.
- **Local Staging Environment**: Local Python 3.12.10 environment (`http://localhost:8000/`) with SQLite WAL database (`trading.db`).
- **Database Storage**: Supabase PostgreSQL (`postgresql://postgres...`) for persistent trade outcomes and SQLite (`trading.db`, 88,494 OHLCV rows, 758 trade outcomes, 3,138 context snapshots).

### 2.2 Route & Endpoint Inventory
Total registered routes in FastAPI: **110 routes**
- **Core Health & System**: `/api/health`, `/api/ready`, `/api/system/data-health`, `/api/health/providers`.
- **Dashboard & Intelligence**: `/api/dashboard/overview`, `/api/signals/all`, `/api/signals/winrate`, `/api/crypto/meme`, `/api/market/{symbol}`, `/api/signal/{symbol}`.
- **Autonomous & Paper Execution**: `/api/autonomous/status`, `/api/autonomous/feed`, `/api/autonomous/run-cycle`, `/api/autonomous/toggle`, `/api/paper/trade`, `/api/paper/open`, `/api/paper/close/{trade_id}`, `/api/paper/capital`, `/api/paper/reset`, `/api/paper/scorecard`.
- **Scanners**:
  - *Intraday*: `/api/intraday/scan`, `/api/intraday/latest`, `/api/intraday/stats`, `/api/intraday/live-quote/{symbol}`.
  - *SwingSpectrum*: `/api/swing/shortlist`, `/api/swing-scanner`, `/api/swing/history`, `/api/swing/scan-now`, `/api/swing/symbol/{symbol}`.
  - *Breakouts & Index Movers*: `/api/breakouts`, `/api/index-mover/{index_name}`.
- **Forex**: `/api/forex/rates`, `/api/forex/signals`, `/api/forex/intelligence`, `/api/forex/sessions`, `/api/forex/ohlcv/{symbol}`.
- **Crypto Cockpit**: `/crypto`, `/api/crypto/news`, `/api/crypto/intelligence`, `/api/crypto/prediction`.
- **Context & News**: `/api/context/v3`, `/api/context/events`, `/api/context/market-mood`, `/api/nse/news`, `/api/nse/prediction`.
- **WebSocket Streaming**: `/ws/prices` broadcasting price updates and server keep-alive pings every 20s.

---

## 3. Feature Coverage Matrix

| Feature / Control | Expected Behavior | API / Data Handler | Observed Behavior | Status | Evidence |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Cockpit Tab Switching** | Switches between 15 tabs smoothly | `window.switchTab(tab)` | All 15 tabs activate and render display: block | **PASS** | Evaluated via Chrome DevTools |
| **Theme Toggle** | Toggles Dark / Light themes | `themeToggle` click | Toggles `data-theme` between "dark" and "light" | **PASS** | DevTools evaluation |
| **Trader Notes / Journal** | Persists private notes locally | `localStorage['tradeai_trader_notes']` | Debounced save to localStorage and reloads on boot | **PASS** | Text verified in localStorage |
| **Symbol Search** | Real-time symbol lookup across NSE & Crypto | `/api/search/symbols?q=...` | Handles lowercase, whitespace, invalid symbols gracefully | **PASS** | RELIANCE, tcs, NIFTY, XYZNOTREAL123 tested |
| **Signal Filters & Sorting** | Filter by BUY/SELL/ALL/High Conf; Sort by QS/Conf | DOM filtering in `app.js` | Filters cards correctly; sort updates ranking | **PASS** | DOM element assertions |
| **Active Positions Card** | Shows running paper trades with live LTP & P&L | `/api/paper/open`, `renderOverviewActiveTrades` | Positions render, but negative P&L omits minus sign | **FAIL** | Line 7790 `app.js` (`pnlSign = pnl >= 0 ? '+' : ''`) |
| **Crypto Decimal Precision** | Small-price crypto formatted with sufficient decimals | `fmtPrice` in `app.js` | DOGE entry, SL, TP all round to `$0.07`, collapsing R:R to `—` | **FAIL** | Line 9315 `app.js` (`minimumFractionDigits: 2`) |
| **Overview Win Rate Stat** | Displays win rate with win/loss trade count | `/api/dashboard/overview` | Shows `55.6%` win rate but `0W / 9L • 9 trades` | **FAIL** | Line 695 `api/routes.py` omits `winning_trades` |
| **Portfolio MTM Calculation** | Real-time mark-to-market for open positions | `/api/portfolio` | Shows MTM: +₹0 and current_price == entry when not pre-cached | **FAIL** | Line 3428 `api/routes.py` defaults to `entry` |
| **Multi-Asset P&L Currency** | Converts foreign currencies before aggregation | `paper_trading.py` | Sums USD crypto P&L directly with INR equity P&L | **FAIL** | Row 4 & 10 in `trade_outcomes_pg` summed raw |
| **Forex Rates & Sessions** | Displays active global sessions & live rates | `/api/forex/sessions`, `/api/forex/rates` | Sessions work; `/api/forex/rates` times out due to sequential sleeps | **FAIL** | HTTP 504 timeout on `/api/forex/rates` |
| **RSI Indicator Calculation** | Computes Wilder's 14-period RSI | `agents.feature_engine.indicators._rsi` | Returns `NaN` on monotonic rallies and flat series | **FAIL** | Python calculation returns `NaN` |
| **Autonomous Run Cycle** | Executes single paper cycle on demand | `/api/autonomous/run-cycle` | Runs cycle, evaluates rules, respects max 8 slots | **PASS** | Local cycle verified |
| **Autonomous Pause/Resume** | Halts new entries while monitoring open exits | `/api/autonomous/toggle` | `evaluate_signals` halts; `manage_open_trades` continues | **PASS** | Code inspection lines 119 & 256 `autonomous_daemon.py` |
| **JSON & CSV Export Buttons** | Exports currently filtered signals table | `exportDecisionPackCsv` in `app.js` | Only exports `top_setups` (2 items), not the active signals | **FAIL** | Line 6720 in `dashboard/app.js` |
| **Sidebar Health Badge** | Reflects real-time system & data freshness status | Static HTML in `index.html` | Hardcoded "System Healthy" never updates on failures | **FAIL** | Line 3782 in `dashboard/index.html` |

---

## 4. Bug Register

### BUG-001: Multi-Currency P&L Pollution (USD Crypto Summed Directly with INR Equities)
- **Severity**: **CRITICAL**
- **Affected Surface**: `agents/execution/paper_trading.py`, `api/routes.py`, Overview Dashboard, Portfolio, Scorecard.
- **Root Cause**: `PaperTradingEngine` does not convert asset quote currencies to the account base currency (`INR`). A crypto trade on `ETH/USDT` with entry $2265.96 and exit $2292.95 generates a P&L of `-$295.88 USD`. This is added directly to Rupee capital: `₹1,00,000 + 306.44 (Reliance) - 295.88 (ETH) = ₹1,00,010.56`.
- **Actual Impact**: A $295.88 USD loss is equivalent to ~₹25,000 INR, but is accounted for as only ₹295.88 INR. Portfolio equity, drawdown, Sharpe ratio, and profit factor are completely distorted.
- **Recommended Fix**: Add a quote currency field to `PaperTrade`. Multiply crypto P&L by the prevailing `USD/INR` exchange rate (e.g., from `GlobalMacroFetcher` or Forex feed) before adding to capital and realized P&L.

### BUG-002: Loss-Making Active Positions Omit Negative Sign in P&L Amount
- **Severity**: **HIGH**
- **Affected Surface**: `dashboard/app.js` line 7790 (`renderOverviewActiveTrades`).
- **Reproducible Steps**:
  1. Open dashboard overview when an open trade is in a floating loss (e.g., DRREDDY, INDUSINDBK).
  2. Observe the P&L pill on the active trade card.
  3. Notice `₹135.91 (-0.60%)` and `₹1598.05 (-6.38%)` displayed with positive currency formatting despite negative percentages and red styling.
- **Root Cause**:
  Line 7790 in `dashboard/app.js`:
  ```javascript
  const pnlSign = pnl >= 0 ? '+' : '';
  ...
  <span id="posPnlVal-${t.id}">${pnlSign}${currSym}${Math.abs(pnl).toFixed(2)}</span>
  ```
  When `pnl < 0`, `pnlSign` evaluates to `''` (empty string). `Math.abs(pnl)` strips the minus sign, resulting in `₹135.91`.
- **Recommended Fix**:
  Change line 7790 to:
  ```javascript
  const pnlSign = pnl < 0 ? '-' : (pnl > 0 ? '+' : '');
  ```

### BUG-003: Hardcoded 2-Decimal Rounding Collapses Low-Priced Crypto Geometry
- **Severity**: **HIGH**
- **Affected Surface**: `dashboard/app.js` line 9315 (`fmtPrice`).
- **Reproducible Steps**:
  1. Observe DOGE/USDT, SHIB/USDT, or PEPE/USDT cards on the dashboard.
  2. Notice Entry: `$0.07`, Stop Loss: `$0.07`, Target: `$0.07`, R:R: `—`.
- **Root Cause**:
  ```javascript
  const fmtPrice = (v) => v == null ? '—' : `${curr}${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  ```
  Forces exactly 2 decimal places for all instruments. For DOGE trading at $0.0685 with SL $0.0712 and TP $0.0631, all three prices round to `$0.07`.
- **Recommended Fix**:
  Use dynamic precision based on magnitude:
  ```javascript
  const fmtPrice = (v) => {
      if (v == null || isNaN(v)) return '—';
      const num = Number(v);
      const decimals = num < 0.01 ? 6 : (num < 1 ? 4 : 2);
      return `${curr}${num.toLocaleString('en-IN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}`;
  };
  ```

### BUG-004: Missing `winning_trades` in Overview Endpoint Causes `0W / 9L` Display
- **Severity**: **HIGH**
- **Affected Surface**: `api/routes.py` lines 695–705, `dashboard/app.js` lines 2705–2708.
- **Reproducible Steps**:
  1. Load Overview Cockpit.
  2. Observe the "MODEL WIN RATE" card: header says `55.6%`, but subtext says `0W / 9L • 9 trades • Small Sample`.
- **Root Cause**:
  In `api/routes.py`:
  ```python
  "portfolio_summary": {
      "capital": stats.get("total_equity", stats.get("capital", 100000.0)),
      "cash_capital": stats.get("capital", 100000.0),
      "total_pnl": stats.get("total_pnl", 0.0),
      "win_rate": stats.get("win_rate", 0.0),
      "total_trades": stats.get("total_trades", 0),
      # "winning_trades" and "losing_trades" are missing!
  }
  ```
  In `dashboard/app.js`:
  ```javascript
  const wins = Number(pSummary.winning_trades ?? 0);
  const losses = Number(pSummary.losing_trades ?? Math.max(0, closedCount - wins));
  ```
  Since `winning_trades` is omitted, `wins` evaluates to 0, and `losses` evaluates to 9.
- **Recommended Fix**: Add `"winning_trades": stats.get("winning_trades", 0)` and `"losing_trades": stats.get("losing_trades", 0)` to `portfolio_summary` in `api/routes.py`.

### BUG-005: Portfolio Page Defaults Uncached Symbols to Entry Price and Zero MTM
- **Severity**: **MEDIUM**
- **Affected Surface**: `api/routes.py` line 3428 (`get_portfolio`).
- **Root Cause**: `get_portfolio()` calls `engine.get_open_trades()` without passing `current_prices`. If an open trade's symbol is not currently present in `svc._market_data`, it executes:
  ```python
  current_close = entry
  unrealized = 0.0
  ```
  Unlike `/api/paper/open`, `/api/portfolio` does not fetch live fallback quotes from Binance or Yahoo.
- **Recommended Fix**: Align `get_portfolio()` with `get_open_paper_trades_endpoint()` to query live prices on demand for all open position symbols.

### BUG-006: Division-by-Zero in Wilder's RSI Produces `NaN` on Monotonic Rallies
- **Severity**: **MEDIUM**
- **Affected Surface**: `agents/feature_engine/indicators.py` line 38 (`_rsi`).
- **Root Cause**:
  ```python
  rs = avg_gain / avg_loss.replace(0, np.nan)
  rsi = 100.0 - (100.0 / (1.0 + rs))
  ```
  When price increases on every bar, `avg_loss == 0`. Replacing 0 with `NaN` causes `rs` and `rsi` to become `NaN` instead of standard 100.0. For flat price series, `avg_gain == 0` and `avg_loss == 0`, also producing `NaN`.
- **Recommended Fix**:
  ```python
  rs = np.where(avg_loss == 0, np.where(avg_gain > 0, np.inf, 1.0), avg_gain / avg_loss)
  rsi = np.where(np.isinf(rs), 100.0, 100.0 - (100.0 / (1.0 + rs)))
  ```

### BUG-007: Sequential Sleep in Forex Rate Fetcher Causes API Timeouts
- **Severity**: **MEDIUM**
- **Affected Surface**: `agents/data_ingestion/forex_fetcher.py` line 67 (`_wait_for_rate_limit`), `api/routes.py` line 8003.
- **Root Cause**: `_wait_for_rate_limit()` enforces an 8.0-second blocking sleep between calls. Fetching rates across 15 forex pairs takes `15 * 8s = 120s`. Frontend HTTP clients with standard 5s–10s timeouts consistently time out with 504 Gateway Timeout.
- **Recommended Fix**: Batch requests via Twelve Data's multi-symbol endpoint (`/price?symbol=EUR/USD,GBP/USD...`) or use keyless yFinance fallback concurrently via `asyncio.gather`.

### BUG-008: Export Buttons Do Not Export Active Signals Table
- **Severity**: **MEDIUM**
- **Affected Surface**: `dashboard/app.js` line 6720 (`exportDecisionPackCsv`).
- **Root Cause**: The CSV button in the signal ribbon calls `exportDecisionPackCsv()`, which only reads `_lastCockpitData.top_setups`. If no setups are in that bucket, it throws an alert and exports nothing, completely ignoring the 8+ visible signals in the main table.
- **Recommended Fix**: Modify `exportDecisionPackCsv` to export `_allSignals` filtered by the user's active filter and search query.

---

## 5. Market Data Freshness & Provider Validation

| Asset / Instrument | Venue / Segment | Advertised Feed | Observed Delay / Staleness | Data Provider | Precision / Units | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **NIFTY 50** | NSE Cash Index | 15m Delayed | EOD / Last close (23,715.90) | Yahoo Finance (`^NSEI`) | 2 decimals / pts | **VERIFIED** |
| **BANKNIFTY** | NSE Cash Index | 15m Delayed | EOD / Last close (55,656.00) | Yahoo Finance (`^NSEBANK`) | 2 decimals / pts | **VERIFIED** |
| **RELIANCE** | NSE Cash Equity | 15m Delayed | EOD / Last close (₹1,266.10) | Yahoo Finance (`RELIANCE.NS`) | 2 decimals / ₹ | **VERIFIED** |
| **BTC/USDT** | Binance Spot | Real-time live | < 1 second delay ($80,283.31) | Binance Public API (`/api/v3`) | 2 decimals / $ | **VERIFIED** |
| **DOGE/USDT** | Binance Spot | Real-time live | < 1 second delay ($0.0685) | Binance Public API (`/api/v3`) | Display truncated | **DEFECT** (BUG-003) |
| **USD/INR** | Currency Major | Daily / 4H | EOD / Cash hours only | Twelve Data / yFinance | 4 decimals | **VERIFIED** |
| **Brent Crude** | Commodity Futures | 15m Delayed | 15m delayed ($96.03) | Yahoo Finance (`BZ=F`) | 2 decimals / $/bbl | **VERIFIED** |
| **US 10Y Yield** | US Treasuries | 15m Delayed | 15m delayed (4.89%) | Yahoo Finance (`^TNX`) | 2 decimals / % | **VERIFIED** |

---

## 6. Model Claims & Self-Learning Validation

1. **Badge Claim Reconciled**: The dashboard displays `🧠 Ensemble 4/4 • Calibrated`.
   - Inspection of `models/` confirms that 4 model artifacts exist on disk: `xgb_model.pkl` (958 KB), `lgbm_model.pkl` (80 KB), `best_model.pt` (859 KB LSTM), and `logistic_model.pkl` (801 bytes), along with `calibrator.pkl` (1.6 KB) and `meta_learner.pkl` (1.1 KB).
   - In `agents/ai_engine/ensemble.py`, line 927 hardcodes `"statistical_heuristic": True` even when all 4 models are fitted.
   - `model_meta.json` reveals the LSTM model achieved a validation accuracy of **50.88%** (virtually indistinguishable from a coin flip).
2. **Self-Learning Recalibration**:
   - `ANALYSE_EVERY_N_TRADES` is configured to 30.
   - `get_learning_stats()` reports 9 genuine forward trades, 21 trades until recalibration, state: `COLD_START`.
   - Inspection of `_restore_history_from_db()` in `agents/ai_engine/ensemble.py` lines 121–131 revealed a methodological defect: every model in `_history` is assigned the identical outcome (`is_win`) from `trade_outcomes`, regardless of individual model votes.
3. **Confidence vs. Win Probability**:
   - Out-of-sample calibration audit reveals **MISALIGNED** calibration status (`high_minus_low: -74.7%`). Trades in the 60–70% confidence bucket had a 75% win rate, while trades in the 80–90% confidence bucket had a 0% win rate.

---

## 7. Site-Specific Regression Targets Findings

1. **Calibrated / Four-Model Badge vs. Heuristic**: Confirmed. All 4 model files exist and load, but `"statistical_heuristic": True` remains hardcoded in `get_model_status()` payload.
2. **Portfolio Positions vs. Zero Totals on Overview**: Confirmed. When uncached open trades load on Overview, `unrealized_pnl` returns 0.0 (`MTM: +₹0`) because `get_portfolio()` does not query live quotes.
3. **Loss-making Trades Without Minus Sign**: Confirmed and isolated. Line 7790 of `dashboard/app.js` sets `pnlSign = pnl >= 0 ? '+' : ''`, dropping the negative sign for losses.
4. **Current Price Equaling Entry Across Positions**: Confirmed. Line 3428 of `api/routes.py` falls back to `current_close = entry` when symbols are absent from `svc._market_data`.
5. **Low-Priced Crypto Rounding to Same Value**: Confirmed. Line 9315 of `dashboard/app.js` enforces `maximumFractionDigits: 2`, rounding DOGE entry, SL, and TP to `$0.07`.
6. **LIVE / System Healthy Badges Alongside Stale Data**: Confirmed. Sidebar status badge `<span class="sidebar-status-title">System Healthy</span>` is static HTML that never reacts to backend errors.
7. **Ambiguous Time-Only Signal Timestamps**: Confirmed. Line 9611 of `dashboard/app.js` formats timestamps with `{hour: '2-digit', minute: '2-digit'}`, dropping date and timezone context.
8. **Summary / Filter Counts Disagreeing with Exports**: Confirmed. `exportDecisionPackCsv()` exports `top_setups` (2 items) rather than the visible signal rows (8 items).

---

## 8. Prioritized Remediation Plan

| Priority | Defect / Task | Affected Files | Retest Criteria |
| :--- | :--- | :--- | :--- |
| **P0** | Implement Currency Conversion for Crypto Trades | `agents/execution/paper_trading.py`, `api/routes.py` | Run mixed INR/USD synthetic trade ledger; verify P&L sums accurately in base currency. |
| **P0** | Fix Negative Sign Display on Active Loss Trades | `dashboard/app.js` (line 7790) | Load negative position; verify `-₹135.91` renders with explicit minus sign. |
| **P1** | Dynamic Decimal Precision for Crypto Assets | `dashboard/app.js` (line 9315) | Inspect DOGE/USDT; verify entry/SL/TP show 4 decimal places and valid R:R. |
| **P1** | Add `winning_trades` and `losing_trades` to Overview API | `api/routes.py` (lines 695–705) | Load Overview; verify `5W / 4L • 9 trades` matches `55.6%` win rate. |
| **P1** | Dynamic Price Resolution for Portfolio Page | `api/routes.py` (lines 3386–3430) | Open Portfolio tab with uncached symbols; verify live LTP and floating MTM update. |
| **P2** | Fix Monotonic and Flat Edge Cases in `_rsi` | `agents/feature_engine/indicators.py` (line 38) | Run unit test with monotonic increasing prices; verify RSI equals 100.0, not `NaN`. |
| **P2** | Optimize Forex Rate Fetching and Rate Limits | `agents/data_ingestion/forex_fetcher.py` (line 67) | Call `/api/forex/rates`; verify response completes in < 2.0s without timing out. |
| **P2** | Fix Signal Table Export Scope in CSV/JSON | `dashboard/app.js` (line 6720) | Click "Export signals CSV"; verify downloaded CSV contains all filtered signals. |
| **P3** | Make Sidebar "System Healthy" Reactive | `dashboard/index.html`, `dashboard/app.js` | Disconnect network or trigger 500 error; verify badge changes to "Degraded / Offline". |
| **P3** | Include Date in Signal Timestamps | `dashboard/app.js` (line 9611) | Verify signal cards show explicit date and time (e.g., `05 Nov, 08:00 pm IST`). |

---

## 9. Remaining Questions & Gaps

1. **Sub-topics Not Fully Verified**:
   - Live broker integration: Marked **N/A / BLOCKED by design** as product requirements mandate zero live broker connections (strict paper trading only).
   - Post-30-trade auto-recalibration: Marked **UNTESTED in production** because only 9 genuine forward trades have closed to date (`COLD_START` state, 21 trades remaining).
2. **Areas Where Evidence is Inconclusive**:
   - The impact of Cloudflare Turnstile / Bot Management challenges on the Render backend when accessed by automated client-side fetchers during heavy traffic.
3. **Leads for the Next Investigator / Worker**:
   - Investigate why `trade_outcomes_pg` currently has 12 records while `get_learning_stats()` reports 9 genuine forward trades (3 quarantined as `LEGACY_INCOMPLETE`).
   - Prioritize patching BUG-001 (currency mixing) and BUG-002 (negative sign omission) before any real capital or public paper-trading launch.
