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
- Prefers free or keyless data paths first for news and market context (RSS, public HTTP); treats paid API keys as optional add-ons.

## Learned Workspace Facts

- Session focus has centered on NSE equities plus crypto (e.g. Binance/CCXT pipeline); forex was set aside in favor of digital-asset work in at least one major thread.
- FastAPI app is imported as `server:app`; HTTP/WebSocket routes and shared service state live under the `api/` package with a thin `server.py` bootstrap.
- Two `nse_fetcher.py` files are intentional: `agents.market_intelligence` for dashboard/API usage and `agents.data_ingestion` for batch ingestion; see `docs/nse_fetcher_modules.md`.
- Default automated test runs skip live-API integration tests: `pytest -m "not integration"`.
- Trading flow vocabulary uses `LONG`/`SHORT`/`SKIP`/`HOLD` aligned across signal generation, runners, backtester, and paper execution (avoid relying only on legacy `BUY`/`SELL`).
- GitHub Actions CI runs Python tests and a `tradvix-app` job (install, lint, build).
- Full options-chain open interest and similar derivatives figures need a broker or NSE-capable feed; dynamic ATM or strike logic alone does not make OI trustworthy.
- Market context for signals is built in `agents/data_ingestion/context_fetcher.py` (`ContextFetcher`): blends 24h and 7d news-style sentiment into `context_score`; NSE may use MarketAux when configured or keyless RSS-style fallbacks; crypto uses free endpoints plus RSS; sentiment is heuristic, not guaranteed edge.
- Swing setups are produced by `agents/market_intelligence/swing_scanner.py` (confluence and dedupe; guardrails covered by swing scanner tests).

