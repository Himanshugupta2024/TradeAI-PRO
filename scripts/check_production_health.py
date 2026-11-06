"""
Production health probe for TradeAI PRO.

Checks the things that actually broke in production, not just "is the site up":

* API health: database connected, symbols loaded, worker recently ran
* signal freshness: timestamps are current and old rows are flagged stale
* setup geometry: no entry == stop == target in the top setups
* assets: a missing .css must return 404, not the SPA shell with 200
* docs: /openapi.json must be closed
* headers: CSP present on the HTML

Exit codes: 0 healthy, 1 degraded, 2 critical.

    python scripts/check_production_health.py
    python scripts/check_production_health.py --json
    python scripts/check_production_health.py --webhook https://hooks.slack.com/...

Designed to run from a GitHub Actions cron or any uptime monitor.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.data_honesty import signal_age_minutes, validate_setup_levels  # noqa: E402

DEFAULT_BASE = "https://tradeai-pro.tradingpro.workers.dev"
MAX_SIGNAL_AGE_MIN = 45.0


def _fetch(url: str, timeout: float = 15.0, accept: str = "*/*"):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "tradeai-health/1.0", "Accept": accept},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), ""
    except Exception as exc:  # timeout, DNS, TLS
        return 0, {}, str(exc)


def _fetch_with_retry(
    url: str,
    accept: str = "*/*",
    delays: tuple[float, ...] = (0.0, 10.0, 25.0),
) -> tuple[int, dict, str]:
    """Retry transient Render cold-start responses (0/429/502/503/504)."""
    result: tuple[int, dict, str] = (0, {}, "")
    for delay in delays:
        if delay:
            time.sleep(delay)
        result = _fetch(url, accept=accept)
        status = result[0]
        if status == 200:
            return result
        if status not in (0, 429, 502, 503, 504):
            return result
    return result


def _result(level: str, check: str, detail: str) -> dict:
    return {"level": level, "check": check, "detail": detail}


def evaluate_health(status: int, payload: dict) -> list[dict]:
    results: list[dict] = []
    if status != 200:
        return [_result("critical", "health_http", f"HTTP {status}")]

    results.append(_result("ok", "health_http", "200"))
    if payload.get("healthy") is not True:
        results.append(_result("critical", "health_flag", f"healthy={payload.get('healthy')}"))

    database = payload.get("database") or {}
    if not database.get("connected"):
        results.append(_result("critical", "database", "not connected"))
    else:
        results.append(_result("ok", "database", "connected"))

    symbols = int(payload.get("symbols_loaded") or 0)
    if symbols <= 0:
        results.append(_result("critical", "symbols", "0 symbols loaded"))
    else:
        results.append(_result("ok", "symbols", f"{symbols} symbols"))

    worker = str(payload.get("worker_health") or "UNKNOWN").upper()
    if worker in ("NO_RECENT_RUN", "STALE", "DOWN"):
        results.append(_result("warn", "worker", worker))
    else:
        results.append(_result("ok", "worker", worker))

    return results


def evaluate_signals(
    status: int,
    payload: dict,
    now: datetime | None = None,
    max_age_min: float = MAX_SIGNAL_AGE_MIN,
) -> list[dict]:
    if status != 200:
        return [_result("critical", "signals_http", f"HTTP {status}")]

    signals = payload.get("signals") or []
    if not signals:
        return [_result("warn", "signals", "no signals returned")]

    results = [_result("ok", "signals", f"{len(signals)} signals")]
    old = 0
    wrong_flag = 0
    unknown = 0

    for signal in signals:
        age = signal_age_minutes(signal.get("timestamp"), now=now)
        if age is None:
            unknown += 1
            continue
        if age > max_age_min:
            old += 1
            if signal.get("stale") is not True:
                wrong_flag += 1

    if old:
        results.append(_result("warn", "signal_age", f"{old}/{len(signals)} older than {max_age_min:g} min"))
    if wrong_flag:
        results.append(
            _result("critical", "stale_flag", f"{wrong_flag} old signals reported stale=false")
        )
    if unknown:
        results.append(_result("warn", "signal_timestamp", f"{unknown} missing timestamps"))
    if not old and not unknown:
        results.append(_result("ok", "signal_age", "all fresh"))

    return results


def evaluate_setups(status: int, payload: dict) -> list[dict]:
    if status != 200:
        return [_result("critical", "setups_http", f"HTTP {status}")]

    setups = payload.get("top_setups") or []
    if not setups:
        return [_result("warn", "setups", "no top setups")]

    bad = 0
    for setup in setups:
        ok, reason = validate_setup_levels(
            setup.get("entry"),
            setup.get("stop_loss"),
            setup.get("target"),
            setup.get("signal", "LONG"),
        )
        if not ok:
            bad += 1

    if bad:
        return [_result("critical", "setups", f"{bad}/{len(setups)} have invalid risk geometry")]
    return [_result("ok", "setups", f"{len(setups)} valid setups")]


def evaluate_asset_probe(status: int, content_type: str) -> list[dict]:
    if status == 404:
        return [_result("ok", "asset_404", "missing asset returns 404")]
    if status == 200 and "text/html" in (content_type or "").lower():
        return [_result("critical", "asset_404", "missing asset returned the SPA shell (200 HTML)")]
    return [_result("warn", "asset_404", f"unexpected status {status} ({content_type})")]


def evaluate_docs(status: int) -> list[dict]:
    if status == 404:
        return [_result("ok", "docs", "openapi closed")]
    if status == 200:
        return [_result("warn", "docs", "openapi.json is public")]
    return [_result("warn", "docs", f"unexpected status {status}")]


def evaluate_headers(headers: dict) -> list[dict]:
    lowered = {str(k).lower(): v for k, v in headers.items()}
    if not lowered.get("content-security-policy"):
        return [_result("warn", "csp", "content-security-policy missing")]
    if not lowered.get("strict-transport-security"):
        return [_result("warn", "hsts", "strict-transport-security missing")]
    return [_result("ok", "headers", "CSP + HSTS present")]


def summarize(results: list[dict]) -> tuple[int, str]:
    levels = [item["level"] for item in results]
    if "critical" in levels:
        return 2, "CRITICAL"
    if "warn" in levels:
        return 1, "DEGRADED"
    return 0, "HEALTHY"


def run(base_url: str, max_age_min: float) -> tuple[int, str, list[dict]]:
    base = base_url.rstrip("/")
    results: list[dict] = []

    status, headers, body = _fetch_with_retry(f"{base}/api/health", accept="application/json")
    try:
        health_payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        health_payload = {}

    # Render's free tier spins down when idle. The first request after a wake
    # can beat the DB pool by a few seconds, so retry once before calling it a
    # database outage.
    if not (health_payload.get("database") or {}).get("connected"):
        time.sleep(20)
        status, headers, body = _fetch_with_retry(f"{base}/api/health", accept="application/json")
        try:
            health_payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            health_payload = {}
        health_payload["_retried_after_cold_start"] = True

    results += evaluate_health(status, health_payload)

    status, _, body = _fetch_with_retry(f"{base}/api/signals/all", accept="application/json")
    try:
        signals_payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        signals_payload = {}
    results += evaluate_signals(status, signals_payload, max_age_min=max_age_min)

    status, _, body = _fetch_with_retry(f"{base}/api/overview/intelligence", accept="application/json")
    try:
        overview_payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        overview_payload = {}
    results += evaluate_setups(status, overview_payload)

    status, headers_asset, _ = _fetch_with_retry(f"{base}/static/health-probe-missing.css")
    results += evaluate_asset_probe(status, headers_asset.get("Content-Type", ""))

    status, _, _ = _fetch_with_retry(f"{base}/openapi.json", accept="application/json")
    results += evaluate_docs(status)

    status, home_headers, _ = _fetch_with_retry(f"{base}/", accept="text/html")
    if status != 200:
        results.append(_result("critical", "home", f"HTTP {status}"))
    else:
        results += evaluate_headers(home_headers)

    code, label = summarize(results)
    return code, label, results


def _post_webhook(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "tradeai-health/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except Exception as exc:
        print(f"webhook failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeAI PRO production health probe")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--max-signal-age-min", type=float, default=MAX_SIGNAL_AGE_MIN)
    parser.add_argument("--json", action="store_true", help="print JSON only")
    parser.add_argument("--webhook", help="POST the JSON summary to this URL")
    args = parser.parse_args(argv)

    code, label, results = run(args.base_url, args.max_signal_age_min)
    payload = {
        "status": label,
        "exit_code": code,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "checks": results,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{label}  ({len(results)} checks)")
        for item in results:
            marker = {"ok": "OK  ", "warn": "WARN", "critical": "FAIL"}[item["level"]]
            print(f"  [{marker}] {item['check']}: {item['detail']}")

    if args.webhook:
        _post_webhook(args.webhook, payload)

    return code


if __name__ == "__main__":
    raise SystemExit(main())
