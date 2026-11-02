# TradeAI-PRO — Known Issues & Technical Debt Tracker

## 1. Upstream & Dependency Quirks
- **yFinance / Pandas 3.x Compatibility**:
  - `yfinance` 1.2.0 issues deprecation warnings with `pandas` 3.x regarding `inplace` and `copy_on_write`.
  - *Mitigation*: Narrowly targeted warning filters registered in `api/app.py` without masking critical runtime exceptions.
- **Render Free Tier Ephemeral Disks**:
  - Free web services lose SQLite data across cold restarts unless synced.
  - *Mitigation*: `RuntimeState` abstracts persistence with PostgreSQL/Supabase backing and atomic local SQLite mirror.

## 2. Market Ingestion Rate Limits
- **Twelve Data (Forex)**: Free tier has 8 requests/minute credit limit.
  - *Mitigation*: Bounded batching, 300s cache TTL, and strict paper-trading framing.
- **Yahoo Finance**: Occasional 429 rate limit during high-frequency parallel requests.
  - *Mitigation*: `MarketDataRouter` with single-flight locking, semaphore batching (max 5 concurrent calls), and fallback to cached data.

## 3. UI Auto-Refresh Alignment
- Previous client-side refresh was tied to local computer time instead of IST market hours.
- *Status*: Resolved with UTC+330 minute explicit IST conversion in `dashboard/app.js`.
