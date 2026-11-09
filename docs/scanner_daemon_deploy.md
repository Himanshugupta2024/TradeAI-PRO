# Running the autonomous scanner on an always-on host

The scan/entry/management cycle was driven by the GitHub Actions `schedule` cron,
which drops runs on low-traffic public repositories — on 2026-11-09 it delivered
zero runs for the whole session even though the cron asks for one every 20
minutes. Result: frozen marks, no entries, no stop updates.

`scripts/scanner_daemon.py` is the intended always-on worker. It holds a
PostgreSQL advisory lock (`pg_try_advisory_lock`), so running it alongside a
GitHub-triggered cycle cannot double-execute trades.

Target: **worker age < 20 minutes during market hours, 99% of sessions.**

## Option A — Oracle Cloud free ARM VM (recommended, zero cost)

Free tier: 4 OCPU / 24 GB RAM Ampere A1, always free. This box can comfortably
run the daemon plus a small Postgres, or just the daemon against Supabase.

```bash
# 1. On the VM (Ubuntu 22.04+)
sudo apt update && sudo apt install -y python3.12 python3.12-venv git

# 2. Code + dependencies
git clone https://github.com/Himanshugupta2024/TradeAI-PRO.git ~/trading
cd ~/trading
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 3. Secrets (same DATABASE_URL as the Render service — this is critical,
#    the daemon writes the snapshots the API reads)
cp .env.example .env
nano .env          # set DATABASE_URL, ADMIN_API_KEY, optional keys

# 4. Smoke-test one cycle before installing the service
.venv/bin/python scripts/scanner_daemon.py --once   # if supported, else run and Ctrl-C after a cycle
```

```bash
# 5. Install the unit that already lives in the repo
sudo cp deploy/tradeai-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradeai-daemon
systemctl status tradeai-daemon --no-pager
journalctl -u tradeai-daemon -f
```

Edit `deploy/tradeai-daemon.service` first if your paths/user differ
(`User=`, `WorkingDirectory=`, `EnvironmentFile=`).

Verify it is doing real work:

```bash
# Lock held? (one row, is_active true while a cycle runs)
psql "$DATABASE_URL" -c "select job_name, run_id, started_at, status from distributed_locks;"

# Fresh snapshot written?
psql "$DATABASE_URL" -c "select state_key, updated_at from runtime_state where state_key='autonomous_worker:latest_status';"
```

Then check the API from anywhere:

```bash
curl -s https://tradeai-pro.tradingpro.workers.dev/api/autonomous/status | jq '.worker.timestamp, .watchdog.status'
```

## Option B — Fly.io / any small VPS

Same commands; only the unit manager differs. A 512 MB shared-cpu-1x Fly machine
is enough for the daemon alone (the web API stays on Render).

## After the daemon is live

1. Keep `.github/workflows/autonomous_cycle.yml` as a **backup** dispatcher — the
   advisory lock makes concurrent runs safe. The Cloudflare Worker cron keeps the
   Render process warm; `CYCLE_SCHEDULER_ENABLED` on Render also dispatches every
   20 minutes when `GITHUB_TOKEN` is present.
2. Monitor from the dashboard: the "WORKER" pill in the Autonomous Decision Feed
   must stay green during market hours.
3. If you later move the web API too, set `BACKGROUND_REFRESH_ENABLED=true` and
   give it at least 0.5 CPU — the free 0.1 CPU instance serialises eight cockpit
   requests into 8–15 second waits.
