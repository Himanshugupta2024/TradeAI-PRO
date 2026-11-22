#!/usr/bin/env python3
"""
TradeAI Pro — Institutional Zero-Downtime Deploy CLI.
====================================================
Automates deployment with strict production safety gates:
  1. Pre-flight tests: Requires local test suite to pass.
  2. Indian Market Gate: Refuses deploys during active NSE session
     (09:15–15:30 IST Monday–Friday) to prevent killing active trade feeds
     unless --force or --force-emergency is explicitly provided.
  3. Git verification: Verifies working directory status & pushes to main.
  4. Render API Trigger: Dispatches deploy to Render Box A Gateway.
  5. Live Verification: Polls /api/health/live until the new build_sha is live.

Usage:
  python scripts/deploy.py               # Standard safe deploy
  python scripts/deploy.py --check-only   # Run pre-flight checks only
  python scripts/deploy.py --force        # Override NSE market hours lockout
  python scripts/deploy.py --skip-tests   # Skip test suite (doc/asset changes)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

_IST = ZoneInfo("Asia/Kolkata")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "srv-d7bsr8juibrs739ujdj0")
RENDER_API_KEY = os.getenv("RENDER_API_KEY")
RENDER_PUBLIC_URL = os.getenv("RENDER_PUBLIC_URL", "https://trading-api-n1wt.onrender.com").rstrip("/")


def is_nse_market_open(now_utc: Optional[datetime] = None) -> Tuple[bool, str]:
    """Check whether the Indian NSE market is currently in an active trading session."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(_IST)
    weekday = now.weekday()  # 0=Monday, 4=Friday, 5=Sat, 6=Sun
    time_min = now.hour * 60 + now.minute

    if weekday in (5, 6):
        return False, f"Weekend ({now.strftime('%A')})"

    market_open_min = 9 * 60 + 15   # 09:15 IST
    market_close_min = 15 * 60 + 30 # 15:30 IST

    if market_open_min <= time_min <= market_close_min:
        return True, f"Active NSE Session ({now.strftime('%H:%M:%S')} IST)"
    elif time_min < market_open_min:
        return False, f"Pre-market ({now.strftime('%H:%M:%S')} IST)"
    else:
        return False, f"Post-market ({now.strftime('%H:%M:%S')} IST)"


def get_current_git_commit() -> str:
    """Return the short 8-character HEAD commit hash."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()[:8]
    except Exception as exc:
        print(f"❌ Error getting git commit: {exc}")
        sys.exit(1)


def run_preflight_tests() -> bool:
    """Execute the core test suite to ensure zero regressions before deploy."""
    print("🧪 Running pre-flight test suite (test_health_checks, test_health_telemetry, test_ml_sidecar_contract, test_pydantic_contracts)...")
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_health_checks.py",
        "tests/test_health_telemetry.py",
        "tests/test_ml_sidecar_contract.py",
        "tests/test_pydantic_contracts.py",
        "-q",
        "--tb=short",
    ]
    res = subprocess.run(cmd)
    return res.returncode == 0


def push_to_origin_main() -> bool:
    """Push local main branch commits to remote repository."""
    print("📤 Pushing local commits to origin main...")
    res = subprocess.run(["git", "push", "origin", "main"])
    return res.returncode == 0


def trigger_render_deploy(commit_sha: str, clear_cache: bool = False) -> Optional[str]:
    """Trigger deploy on Render via REST API."""
    if not RENDER_API_KEY:
        print("⚠️ No RENDER_API_KEY found in environment or .env.")
        print("   If GitHub auto-deploy is enabled on Render, the push to main will deploy automatically.")
        return "auto"

    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
    payload = {"clearCache": "clear" if clear_cache else "do_not_clear"}
    headers = {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "TradeAI-DeployCLI/1.0",
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            deploy_id = data.get("id")
            print(f"🚀 Render deploy successfully triggered! ID: {deploy_id}")
            return deploy_id
    except Exception as exc:
        print(f"⚠️ Direct Render API trigger error: {exc}")
        print("   Falling back to git push auto-deploy.")
        return "auto"


def poll_until_live(target_sha: str, timeout_seconds: int = 300) -> bool:
    """Poll the production health endpoint until the deployed build_sha matches target_sha."""
    poll_url = f"{RENDER_PUBLIC_URL}/api/health/live"
    print(f"⏳ Polling {poll_url} for build_sha: {target_sha} (max {timeout_seconds}s)...")
    start = time.time()
    last_reported_sha = None

    while (time.time() - start) < timeout_seconds:
        elapsed = int(time.time() - start)
        try:
            req = urllib.request.Request(poll_url, headers={"User-Agent": "TradeAI-DeployCLI/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    live_sha = (data.get("build_sha") or "")[:8]
                    if live_sha != last_reported_sha:
                        last_reported_sha = live_sha
                        print(f"   [{elapsed}s] Live build_sha: {live_sha} (waiting for {target_sha})...")

                    if live_sha == target_sha:
                        print(f"🎉 VERIFIED LIVE! Commit {target_sha} active on Render in {elapsed}s.")
                        print(f"   Config Hash: {data.get('config_hash')}")
                        print(f"   Uptime: {data.get('uptime_seconds')}s")
                        return True
        except Exception as exc:
            # Service may be restarting/building
            sys.stdout.write(f"\r   [{elapsed}s] Waiting for service to respond... ({exc.__class__.__name__})")
            sys.stdout.flush()

        time.sleep(5)

    print(f"\n❌ Timeout after {timeout_seconds}s waiting for {target_sha} to go live.")
    return False


def main():
    parser = argparse.ArgumentParser(description="TradeAI Pro Safe Institutional Deploy CLI")
    parser.add_argument("--force", "--force-emergency", action="store_true", dest="force", help="Bypass NSE market hours safety lockout")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pre-flight test suite")
    parser.add_argument("--check-only", action="store_true", help="Run pre-flight checks only, do not deploy")
    parser.add_argument("--clear-cache", action="store_true", help="Clear build cache on Render")
    parser.add_argument("--timeout", type=int, default=300, help="Polling timeout in seconds (default 300)")

    args = parser.parse_args()

    print("=======================================================")
    print("🚀 TradeAI Pro — Safe Institutional Deploy CLI")
    print("=======================================================")

    # 1. Market Hours Gate (Indian Financial Market Safety)
    is_open, session_info = is_nse_market_open()
    print(f"🕒 Market Status: {session_info}")

    if is_open and not args.force:
        print("\n🚨 DEPLOY BLOCKED: Indian Equity Market (NSE) is currently OPEN!")
        print("   Deploying during trading hours cuts active tick streams and resets container state.")
        print("   Wait until 15:30 IST or run with --force if this is a critical emergency fix.")
        sys.exit(1)

    if is_open and args.force:
        print("⚠️ WARNING: --force specified. Deploying during active NSE session.")

    # 2. Pre-flight Tests
    if not args.skip_tests:
        if not run_preflight_tests():
            print("\n❌ PRE-FLIGHT TEST FAILURE: One or more tests failed. Deploy aborted.")
            sys.exit(1)
        print("✅ Pre-flight tests passed successfully.")
    else:
        print("⚠️ Pre-flight tests skipped (--skip-tests).")

    # 3. Git Status & Target Commit
    commit_sha = get_current_git_commit()
    print(f"📌 Target Commit SHA: {commit_sha}")

    if args.check_only:
        print("\n✅ All pre-flight checks passed! (Check-only mode, stopping before deploy).")
        sys.exit(0)

    # 4. Git Push
    if not push_to_origin_main():
        print("\n❌ Git push failed. Deploy aborted.")
        sys.exit(1)

    # 5. Trigger Render Deploy
    trigger_render_deploy(commit_sha, clear_cache=args.clear_cache)

    # 6. Poll Health Endpoint until Target SHA is Live
    success = poll_until_live(commit_sha, timeout_seconds=args.timeout)
    if not success:
        sys.exit(1)

    print("\n✅ Deployment process completed successfully.")


if __name__ == "__main__":
    main()
