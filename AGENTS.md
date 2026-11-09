<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.

## Learned User Preferences

- Prefers trading-bot and platform explanations in Hinglish (Roman script, professional tone) when the conversation is in that style.
- Expects explicit clarity on which metrics are live or exchange-sourced versus synthetic or placeholder, especially when discussing real capital.
- Prefers free or keyless data paths first for news and market context (RSS, public HTTP); treats paid API keys as optional add-ons; for the context layer specifically, paid MarketAux/RapidAPI paths stay off unless `CONTEXT_USE_PAID_APIS` is enabled with keys.
- For high-volatility crypto (including meme-style names), prefers product framing as market data plus risk education rather than buy recommendations.
- For SwingSpectrum, expects a daily auto-suggested shortlist framed as "morning-buy / before-close-exit" candidates (positional-intraday framing on daily/EOD bars), not a raw scanner dump.
- For the Forex module, treats "paper trading only, NO live broker integration ever, zero-cost free APIs only" as a hard non-negotiable product rule, not just a default.
- Ships on Render and expects deployment notes to call out free-tier limits (no persistent disk on `plan: free` web services, idle sleep, and small-RAM/OOM risk during heavy ML stack imports).
- Expects agents to run shell commands locally and verify outcomes rather than only suggesting steps.
- Adheres strictly to the 'unslop' contract (from https://github.com/theclaymethod/unslop, installed in user skills): avoids robotic AI tells, throat-clearing, generic filler, inflated buzzwords ("tapestry", "delve", "testament to", "game-changer"), sycophancy, and moralizing codas; preserves concrete facts, technical definitions, exact numbers, and direct human-like engineering tone.

## Learned Workspace Facts

- Session focus centers on NSE equities and crypto (e.g. Binance/CCXT); a Twelve Data–backed forex path (`agents/data_ingestion/forex_fetcher.py`, `agents/market_intelligence/forex_scanner.py`) supports the dashboard Forex tab alongside those assets (paper-only execution framing).
- FastAPI app is imported as `server:app`; HTTP/WebSocket routes and shared service state live under the `api/` package with a thin `server.py` bootstrap.
- Two `nse_fetcher.py` files are intentional: `agents.market_intelligence` for dashboard/API usage and `agents.data_ingestion` for batch ingestion; see `docs/nse_fetcher_modules.md`.
- Default automated test runs skip live-API integration tests: `pytest -m "not integration"`.
- Trading flow vocabulary uses `LONG`/`SHORT`/`SKIP`/`HOLD` aligned across signal generation, runners, backtester, and paper execution (avoid relying only on legacy `BUY`/`SELL`).
- GitHub Actions CI runs Python tests and a `tradvix-app` job (install, lint, build).
- Options/PCR dashboard UI and related FastAPI routes (`/api/pcr`, `/api/broker/status`) plus broker-only option-chain code were removed per user direction; trustworthy OI still needs a broker or NSE-capable feed if that surface returns.
- Market context for signals is built in `agents/data_ingestion/context_fetcher.py` (`ContextFetcher`): blends 24h and 7d news-style sentiment into `context_score` (tunable via `CONTEXT_NEWS_WEIGHT_24HR` / `CONTEXT_NEWS_WEIGHT_7D`); default NSE path is Google News RSS, crypto uses public endpoints plus RSS buckets; MarketAux and RapidAPI calendar extras only when `CONTEXT_USE_PAID_APIS=true` with keys; sentiment is heuristic, not guaranteed edge.
- Swing setups come from `agents/market_intelligence/swing_scanner.py` (SwingSpectrum: confluence, dedupe, daily/EOD-style bars—not intraday LTP); per-symbol lookup uses `/api/swing/symbol/{symbol}` with OHLCV fallback via `fetch_arbitrary_symbol`, including a `{SYM}.NS` yFinance bypass when mapped `fetch_ohlcv` fails (index-style names skip the `.NS` suffix); when OHLCV resolves but nothing qualifies, the dashboard may still show the generic `No swing setups passed filters (R:R, regime gate, bar age, pattern rules) for {SYMBOL}` string, while the JSON response can carry a more specific `message` plus `swing_scan` stage counters (`raw_pattern_count`, `after_rr_count`, `after_regime_count`, `blocked`, etc.) so empty results distinguish “no pattern matched” from R:R or regime gating.
- Render Blueprint (`render.yaml`): web service `trading-api` runs `uvicorn server:app` with `healthCheckPath: /api/health`; free-tier web cannot attach persistent disks (ephemeral SQLite in Blueprint unless Postgres is wired); Blueprint sets `OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS` / `NUMEXPR_NUM_THREADS` to `1`; `requirements.txt` pins `xgboost>=2.0.0,<2.1.0` on Linux to avoid pip pulling `nvidia-nccl-cu12`; `api/app.py` lifespan runs `init_db()` via `await asyncio.to_thread(init_db)`.
- Dashboard static delivery: `FileResponse` + `_DASH_HTML_HEADERS` (`Cache-Control: no-store, must-revalidate`) for `/`, `/landing`, `/crypto` in `api/routes.py`; `dashboard/crypto.html` bar charts use `measureCanvasLayoutWidth` plus `ResizeObserver` on `#coinDetailChartWrap`; `/ws/prices` sends a lightweight server JSON ping about every 20 seconds; `dashboard/app.js` reconnects WebSockets with exponential backoff and jitter (`_wsReconnectBackoffMs`, `readyState < 2`); `/favicon.ico` is served from `STATIC_DIR` for the API host; optional `SELF_PING_ENABLED` HTTP self-ping in `api/app.py` uses `httpx` (see `requirements.txt`).
- Forex (Twelve Data, paper-only framing) config lives in `config/settings.py`: `FOREX_SYMBOLS` = INR pairs (`USD/INR`, `EUR/INR`, `GBP/INR`, `JPY/INR`) + majors (`EUR/USD`, `GBP/USD`, `USD/JPY`, `AUD/USD`, `USD/CAD`); `FOREX_TIMEFRAMES = ["1h", "4h", "1day"]` with swing primary `4h`+`1day`; `FOREX_SESSIONS` maps IST windows for `ASIAN`/`LONDON`/`NEWYORK`/`NSE_INR` (09:00–17:00 IST) with per-session best-pair hints; INR pairs follow NSE hours so show inactive outside IST cash hours; Twelve Data free tier has a daily cap + tight per-minute credit budget, so keep blocking work off the FastAPI event loop (`asyncio.to_thread`) and pace/batch calls; Twelve Data HTTP uses underscore symbol form (`EUR/USD` → `EUR_USD`), convert at the fetcher boundary.
- Windows Local Server Binding: When running Uvicorn locally on Windows 11, bind host to `0.0.0.0` (not `127.0.0.1`) so that browser traffic resolving `localhost` to IPv6 `[::1]:8000` does not trigger connection refused (`WSAECONNREFUSED`).
- Cloudflare Pages Python Pin: Build runner uses `asdf` which defaults to Python 3.14+ if unset, breaking binary wheels (`numba`, `pandas`). Keep `.python-version` pinned to `3.12.7` alongside `runtime.txt`.
- Model Bundle & Cloud Limits Contract:
  - Models are trained externally (GitHub Actions cron, Kaggle GPU, Google Colab) using `scripts/train_and_publish.py` and published directly to Supabase `runtime_state` key `model_bundle:v1` via `api.model_sync.publish_bundle_to_db`.
  - Bundle gates strictly enforce: whitelisted model filenames only, max 8 MB per file, max 12 MB total decoded gzip size.
  - Render Web Service (`plan: free`) remains on `LIGHTWEIGHT_API_MODE=true` to honor the 512MB RAM cap; live scoring uses statistical fallbacks, while full pickle sync is gated behind admin `POST /api/models/sync`.
- External Model Training Workflow:
  - Notebook: `notebooks/TradeAI_Model_Trainer.ipynb` reads `DATABASE_URL` via `kaggle_secrets.UserSecretsClient` or `google.colab.userdata.get('DATABASE_URL')`.
  - Automation: `.github/workflows/model_train_scheduled.yml` executes weekly (Sunday 00:00 UTC) with repo secret `DATABASE_URL`.

