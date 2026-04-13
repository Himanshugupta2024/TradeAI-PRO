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
- Ships on Render and expects deployment notes to call out free-tier limits (no persistent disk on `plan: free` web services, idle sleep, and small-RAM/OOM risk during heavy ML stack imports).

## Learned Workspace Facts

- Session focus has centered on NSE equities plus crypto (e.g. Binance/CCXT pipeline); forex was set aside in favor of digital-asset work in at least one major thread.
- FastAPI app is imported as `server:app`; HTTP/WebSocket routes and shared service state live under the `api/` package with a thin `server.py` bootstrap.
- Two `nse_fetcher.py` files are intentional: `agents.market_intelligence` for dashboard/API usage and `agents.data_ingestion` for batch ingestion; see `docs/nse_fetcher_modules.md`.
- Default automated test runs skip live-API integration tests: `pytest -m "not integration"`.
- Trading flow vocabulary uses `LONG`/`SHORT`/`SKIP`/`HOLD` aligned across signal generation, runners, backtester, and paper execution (avoid relying only on legacy `BUY`/`SELL`).
- GitHub Actions CI runs Python tests and a `tradvix-app` job (install, lint, build).
- Options/PCR dashboard UI and related FastAPI routes (`/api/pcr`, `/api/broker/status`) plus broker-only option-chain code were removed per user direction; trustworthy OI still needs a broker or NSE-capable feed if that surface returns.
- Market context for signals is built in `agents/data_ingestion/context_fetcher.py` (`ContextFetcher`): blends 24h and 7d news-style sentiment into `context_score` (tunable via `CONTEXT_NEWS_WEIGHT_24HR` / `CONTEXT_NEWS_WEIGHT_7D`); default NSE path is Google News RSS, crypto uses public endpoints plus RSS buckets; MarketAux and RapidAPI calendar extras only when `CONTEXT_USE_PAID_APIS=true` with keys; sentiment is heuristic, not guaranteed edge.
- Swing setups are produced by `agents/market_intelligence/swing_scanner.py` (confluence and dedupe; guardrails covered by swing scanner tests).
- Render Blueprint (`render.yaml`): web service `trading-api` runs `uvicorn server:app` with `healthCheckPath: /api/health`; free-tier web cannot attach persistent disks (ephemeral SQLite in Blueprint unless Postgres is wired); Blueprint sets `OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS` / `NUMEXPR_NUM_THREADS` to `1`; `requirements.txt` pins `xgboost>=2.0.0,<2.1.0` on Linux to avoid pip pulling `nvidia-nccl-cu12`; `api/app.py` lifespan runs `init_db()` via `await asyncio.to_thread(init_db)`.
- Dashboard HTML for `/`, `/landing`, and `/crypto` uses `FileResponse` with `_DASH_HTML_HEADERS` in `api/routes.py` (`Cache-Control: no-store, must-revalidate`); `dashboard/crypto.html` bar charts use `measureCanvasLayoutWidth` plus `ResizeObserver` on `#coinDetailChartWrap` so canvas sizing stays correct after layout.
- The `/ws/prices` handler in `api/routes.py` sends a lightweight server JSON ping about every 20 seconds for proxy/Render LB idle timeouts; optional `SELF_PING_ENABLED` HTTP self-ping in `api/app.py` uses `httpx` (listed in `requirements.txt`).

