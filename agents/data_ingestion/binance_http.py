"""
Shared public Binance REST endpoint selector and geo-block cooldown helper.

Render US egress servers frequently hit HTTP 451 (Unavailable For Legal Reasons)
on api.binance.com and fapi.binance.com. Routing Spot endpoints to Binance's public
data-api.binance.vision mirror first and latching per-host cooldowns avoids repeated
blocked calls and log spam while preserving zero-cost keyless public data access.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

BINANCE_SPOT_BASES: tuple[str, ...] = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
)
DEFAULT_GEO_COOLDOWN_SECONDS = 3600.0
DEFAULT_RATE_COOLDOWN_SECONDS = 300.0

_COOLDOWN_LOCK = threading.Lock()
_HOST_COOLDOWN_UNTIL: dict[str, float] = {}
_LOG_THROTTLE_UNTIL: dict[str, float] = {}


def _host_key(host_or_url: str) -> str:
    raw = (host_or_url or "").strip()
    if "://" in raw:
        parsed = urlparse(raw)
        return (parsed.netloc or raw).lower()
    return raw.split("/")[0].lower()


def reset_binance_geo_cooldown() -> None:
    """Clear in-memory host cooldowns and throttles (intended for unit tests)."""
    with _COOLDOWN_LOCK:
        _HOST_COOLDOWN_UNTIL.clear()
        _LOG_THROTTLE_UNTIL.clear()


def is_host_in_cooldown(host_or_url: str, *, now: float | None = None) -> bool:
    """Return True when the given Binance host is currently in geo/rate cooldown."""
    key = _host_key(host_or_url)
    if not key:
        return False
    cur = time.monotonic() if now is None else now
    with _COOLDOWN_LOCK:
        until = _HOST_COOLDOWN_UNTIL.get(key, 0.0)
        return cur < until


def mark_host_geo_blocked(
    host_or_url: str,
    *,
    status_code: int | None = 451,
    ttl_sec: float | None = None,
    now: float | None = None,
) -> float:
    """Record a geo-block or rate-limit cooldown for a host; return monotonic expiry."""
    key = _host_key(host_or_url)
    if not key:
        return 0.0
    if ttl_sec is None:
        ttl_sec = DEFAULT_GEO_COOLDOWN_SECONDS if status_code in (403, 451) else DEFAULT_RATE_COOLDOWN_SECONDS
    cur = time.monotonic() if now is None else now
    until = cur + max(1.0, float(ttl_sec))
    with _COOLDOWN_LOCK:
        _HOST_COOLDOWN_UNTIL[key] = max(_HOST_COOLDOWN_UNTIL.get(key, 0.0), until)
        return _HOST_COOLDOWN_UNTIL[key]


def extract_http_status(exc: Exception) -> int | None:
    """Extract numeric HTTP status code from urllib, requests, or httpx exceptions."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if isinstance(sc, int):
            return sc
    return None


def log_geo_cooldown_once(
    log: logging.Logger,
    key: str,
    message: str,
    *args: Any,
    cooldown_sec: float = DEFAULT_GEO_COOLDOWN_SECONDS,
) -> None:
    """Log geo-block notices at most once per cooldown interval."""
    cur = time.monotonic()
    with _COOLDOWN_LOCK:
        if cur < _LOG_THROTTLE_UNTIL.get(key, 0.0):
            return
        _LOG_THROTTLE_UNTIL[key] = cur + max(1.0, cooldown_sec)
    log.warning(message, *args)


def spot_candidate_urls(path_and_query: str, *, include_blocked: bool = False) -> list[str]:
    """Build ordered Binance spot REST URLs preferring unblocked data-api.binance.vision."""
    suffix = path_and_query if path_and_query.startswith("/") else f"/{path_and_query}"
    urls: list[str] = []
    for base in BINANCE_SPOT_BASES:
        if include_blocked or not is_host_in_cooldown(base):
            urls.append(f"{base}{suffix}")
    return urls


def _default_urlopen_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "TradeAI-Pro/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_binance_spot_json(
    path_and_query: str,
    *,
    timeout: float = 8.0,
    fetcher: Callable[[str, float], Any] | None = None,
    log: logging.Logger | None = None,
    context_label: str = "spot",
) -> Any | None:
    """
    Fetch JSON from Binance public spot REST endpoints across candidate hosts.

    Marks HTTP 403/418/429/451 hosts in cooldown so US-egress deployments avoid
    repeated blocked requests and log spam. Returns None when every host fails.
    """
    urls = spot_candidate_urls(path_and_query)
    if not urls:
        return None
    fn = fetcher or _default_urlopen_json
    active_log = log or logging.getLogger(__name__)
    for url in urls:
        try:
            return fn(url, timeout)
        except Exception as exc:
            status = extract_http_status(exc)
            if status in (403, 418, 429, 451):
                ttl = DEFAULT_GEO_COOLDOWN_SECONDS if status in (403, 451) else DEFAULT_RATE_COOLDOWN_SECONDS
                mark_host_geo_blocked(url, status_code=status, ttl_sec=ttl)
                log_geo_cooldown_once(
                    active_log,
                    f"binance_{_host_key(url)}_{status}_{context_label}",
                    "Binance %s host %s degraded (HTTP %s); cooling down for %ds.",
                    context_label,
                    _host_key(url),
                    status,
                    int(ttl),
                    cooldown_sec=ttl,
                )
            else:
                active_log.debug("Binance %s fetch error on %s: %s", context_label, url, exc)
    return None
