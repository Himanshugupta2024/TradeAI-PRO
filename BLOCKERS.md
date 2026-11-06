# BLOCKERS.md — Operational Blockers

## Blocker 1: GitHub Account Billing Lock Disables All GitHub Actions Workflows

### Exact Failure Message (from GitHub Check-Run Annotations)
> `"The job was not started because your account is locked due to a billing issue."`
- **Impacted Workflows:**
  - `autonomous_cycle.yml` ("Autonomous Paper Trading Cycle")
  - `keep_alive.yml` ("Production Backend Keep-Alive")
  - `nightly_swing_scan.yml` ("Nightly SwingSpectrum Scan")
  - `ci.yml` ("CI")
- **Diagnostic Evidence:**
  - Run IDs: `42263640482`, `42263517893`, `42261805922`, `42274727879`
  - GitHub Check Run ID: `128049764045`
  - Check Run Annotation:
    ```json
    {
      "path": ".github",
      "annotation_level": "failure",
      "message": "The job was not started because your account is locked due to a billing issue."
    }
    ```
  - Runner Name: `""` (no runner ever assigned; job terminates in ~2 seconds at startup with 0 steps executed).
  - Billable Duration: `0 ms`.

### Downstream Consequences on Production
1. **Render Idle Sleep:** Because `keep_alive.yml` fails at startup, Render free tier web service sleeps after 15 minutes of inactivity, producing 20–70s cold starts.
2. **Autonomous Worker Stoppage:** Because `autonomous_cycle.yml` does not execute, `/api/autonomous/status` reports `worker_health: "NO_RECENT_RUN"`, `last_scan_time: null`.
3. **Nightly Scans Skipped:** `nightly_swing_scan.yml` does not run, leaving swing opportunities unrefreshed until manual on-demand triggers.

### Required Action by Account Owner
- Navigate to GitHub Settings $\rightarrow$ Billing and Plans $\rightarrow$ Payment information or Spending limits for GitHub Actions.
- Settle outstanding balance or adjust spending limit so Actions runners can be allocated.

### In-Code Mitigation (Phase 1.3)
- Because GitHub Actions is blocked by an account-level billing lock, add a Cloudflare Worker Cron Trigger in `wrangler.jsonc` paired with a `scheduled(event, env, ctx)` handler in `src/worker.js`.
- The Cloudflare Worker will execute on Cloudflare's edge independently of GitHub Actions, pinging the Render backend health endpoint every 5 minutes to prevent idle sleep.
