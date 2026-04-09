# NSE fetcher modules (two files)

The repository intentionally has **two** different `nse_fetcher.py` modules. They are **not** duplicates; imports must target the correct package.

## `agents/market_intelligence/nse_fetcher.py` (dashboard / live API)

- **Role:** Primary surface for the FastAPI app and market UI: `NSE_SYMBOLS`, `fetch_ohlcv`, gainers/losers, sector helpers, symbol search, validation.
- **Data:** Uses yFinance with optional SQLite OHLCV caching (`db.ohlcv_cache`).
- **When to import from here:** Anything in the API layer, scanners, or dashboard that needs the canonical symbol map or intraday/live-style OHLCV.

## `agents/data_ingestion/nse_fetcher.py` (ingestion layer)

- **Role:** Focused historical OHLCV fetch for the data-ingestion pipeline; timeout-wrapped yFinance calls and project logging conventions.
- **When to import from here:** Batch ingestion, ETL, or training pipelines that should stay decoupled from dashboard-specific symbol tables.

## Rule of thumb

- **User-facing path / server routes:** `agents.market_intelligence.nse_fetcher`
- **Offline / batch ingestion:** `agents.data_ingestion.nse_fetcher`

If a feature needs both, prefer calling **one** module from the other via a thin adapter in a single place rather than copying helpers, so behavior does not drift.
