# TradeAI-PRO

Python trading stack with a FastAPI backend, static dashboard, and an optional React SPA (**tradvix-app**).

## Prerequisites

- Python **3.11+** (3.12 recommended; use a separate venv for PyTorch if you need LSTM — see [requirements.txt](requirements.txt) comments)
- Node.js **20+** (CI uses 22) for **tradvix-app** only

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # macOS/Linux

pip install -r requirements.txt
cp .env.example .env       # then edit .env
```

## Run the API + built-in dashboard

The server loads agents, serves JSON under `/api/*`, WebSocket at `/ws/prices`, and static files from the [dashboard/](dashboard/) folder (`/`, `/landing`, `/crypto`, `/static/...`).

```bash
# From repo root
python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Or:

```bash
python server.py
```

## Tradvix React app vs dashboard

| UI | Location | How to run | Served by |
|----|----------|------------|-----------|
| **Dashboard** | [dashboard/](dashboard/) | Use FastAPI above | Same process as API (`server:app`) |
| **Tradvix** | [tradvix-app/](tradvix-app/) | `cd tradvix-app && npm ci && npm run dev` | Vite dev server (default port 5173); build output is standalone static assets |

Use the dashboard for the main trading UI wired to this repo’s API. Use **tradvix-app** for experiments or a separate product shell; point it at your API base URL in its frontend config if you integrate it.

## Tests

```bash
pytest -m "not integration"   # default: skip tests that hit real APIs
pytest                        # run everything, including `integration`-marked tests
```

## Deploy On Render

This repo includes a Render Blueprint config in `render.yaml` for FastAPI (`server:app`).

1. Push this repo to GitHub.
2. In Render, click **New +** -> **Blueprint** and select your repository.
3. Render will read `render.yaml` and create the web service automatically.
4. Add any optional API keys in Render Environment (for example `MARKETAUX_API_KEY`, `RAPIDAPI_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
5. Deploy.

Notes:
- Start command uses `uvicorn server:app --host 0.0.0.0 --port $PORT`.
- SQLite is persisted on a mounted disk at `/var/data` via `DATABASE_URL=sqlite:////var/data/trading.db`.
- Health check path is `/api/health`.

## Project layout (short)

- [agents/](agents/) — ingestion, features, signals, execution, backtester, analytics
- [api/](api/) — FastAPI app factory and routers (import entry remains `server:app`)
- [config/](config/) — settings (`python-dotenv`)
- [db/](db/) — SQLAlchemy models and session
- [tests/](tests/) — pytest

See [PROJECT_ARCHITECTURE_FLOW.md](PROJECT_ARCHITECTURE_FLOW.md) for deeper architecture notes.

NSE data: two `nse_fetcher` modules are intentional — [docs/nse_fetcher_modules.md](docs/nse_fetcher_modules.md).
