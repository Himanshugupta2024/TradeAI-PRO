# Where the autonomous cycle actually runs

Three independent triggers now exist. All of them execute the same
`scripts/autonomous_cycle.py` on a GitHub runner, and the PostgreSQL advisory
lock makes overlapping runs safe.

| Trigger | Cadence | Depends on | Role |
|---|---|---|---|
| Cloudflare Worker cron (`src/worker.js`) | every 20 min, 09:15–15:30 IST, Mon–Fri | Cloudflare only | **primary** |
| Render `cycle_scheduler` | every 20 min in session | Render awake + `GITHUB_TOKEN` | backup |
| GitHub `schedule` cron | best effort | GitHub's scheduler | fallback (drops runs) |

## Why the Cloudflare cron is the primary

GitHub's `schedule` trigger silently drops runs on low-traffic repositories. On
2026-11-09 the workflow was configured for ~20 runs during the session and
delivered **zero**: the feed stayed on Friday's bars, positions showed stale
marks, and no entries were taken.

Cloudflare's cron is precise (it fires every 5 minutes for the keep-warm ping),
and the worker only needs an `actions:write` GitHub token to dispatch the
workflow directly. It does not require Render to be awake, so a cold or sleeping
backend can no longer stop the scheduler.

Configuration (already applied):

```bash
# The token is a fine-grained PAT with "Actions: Read and write" on the repo.
wrangler secret put GH_PAT
wrangler deploy
```

`shouldDispatchCycle()` in `src/worker.js` is pure and unit-tested
(`tests/test_worker_scheduled.js`, tests 7–9): it checks IST weekday, the
09:15–15:30 window, and the 20-minute cadence so the 5-minute cron only
dispatches on the :00/:20/:40 boundaries.

## Verification

```bash
# Runs dispatched by the scheduler appear as workflow_dispatch events
curl -s "https://api.github.com/repos/Himanshugupta2024/TradeAI-PRO/actions/workflows/autonomous_cycle.yml/runs?per_page=5" \
  | jq '.workflow_runs[] | {created_at, event, conclusion}'

# And the feed should show today's decisions within ~3 minutes of each dispatch
curl -s https://tradeai-pro.tradingpro.workers.dev/api/autonomous/feed?limit=5 | jq '.feed[].action'
```

## Optional: dedicated always-on host

`scripts/scanner_daemon.py` + `deploy/tradeai-daemon.service` can run the cycle
on an Oracle Cloud free ARM VM (as described in
[scanner_daemon_deploy.md](scanner_daemon_deploy.md)). That removes GitHub from
the execution path entirely, but it is **not required** while the Cloudflare cron
is dispatching reliably.
