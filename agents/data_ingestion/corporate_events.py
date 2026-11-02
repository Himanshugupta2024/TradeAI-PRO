"""
NSE Corporate Announcements & Event Risk Engine — TradeAI-PRO Data Brain V3
Identifies high-impact binary events (earnings results, board meetings, CFO/CEO exits, SEBI actions)
to prevent risky trades right before earnings announcements, acting as an event risk circuit gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Cache for corporate announcements: map symbol -> list of event dicts
_events_by_symbol: dict[str, list[dict[str, Any]]] = {}
_cache_timestamp: float = 0.0
_CACHE_TTL_SECONDS: float = 1800.0  # 30 minutes TTL

HIGH_RISK_KEYWORDS = [
    "financial results",
    "quarterly results",
    "audited results",
    "unaudited results",
    "board meeting",
    "dividend",
    "bonus issue",
    "stock split",
    "merger",
    "amalgamation",
    "cfo resignation",
    "ceo resignation",
    "chief financial officer",
    "chief executive officer",
    "resignation",
    "auditor resignation",
    "sebi",
    "forensic",
    "investigation",
    "insolvency",
]

LOW_RISK_KEYWORDS = [
    "investor presentation",
    "earnings call transcript",
    "trading window",
    "loss of share certificate",
    "share certificate",
    "postal ballot",
    "press release",
    "allotment of equity shares",
]


def _evaluate_event_risk(subject: str, details: str = "") -> str:
    """Classify risk level of a single corporate announcement."""
    text = f"{subject} {details}".lower()
    for kw in HIGH_RISK_KEYWORDS:
        if kw in text:
            return "HIGH_RISK"
    for kw in LOW_RISK_KEYWORDS:
        if kw in text:
            return "LOW_RISK"
    return "INFORMATIONAL"


def _fetch_announcements_sync() -> dict[str, list[dict[str, Any]]]:
    """Fetch corporate announcements from NSE public feed."""
    events_map: dict[str, list[dict[str, Any]]] = {}
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
            },
        )
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        items = data if isinstance(data, list) else data.get("data", [])
        for item in items:
            sym = str(item.get("symbol", "")).strip().upper()
            if not sym:
                continue
            subj = str(item.get("desc", item.get("subject", ""))).strip()
            details = str(item.get("attchmntText", item.get("details", ""))).strip()
            broadcast = item.get("an_dt", item.get("broadcastDate", ""))
            risk = _evaluate_event_risk(subj, details)

            entry = {
                "subject": subj,
                "broadcast_time": broadcast,
                "risk_level": risk,
            }
            if sym not in events_map:
                events_map[sym] = []
            events_map[sym].append(entry)

    except Exception as exc:
        log.debug("[CorporateEvents] NSE announcements fetch error: %s", exc)

    return events_map


async def refresh_corporate_events(force: bool = False) -> None:
    """Refresh corporate announcements in background."""
    global _events_by_symbol, _cache_timestamp
    now = time.time()
    if not force and _events_by_symbol and (now - _cache_timestamp < _CACHE_TTL_SECONDS):
        return

    try:
        res = await asyncio.to_thread(_fetch_announcements_sync)
        if res:
            _events_by_symbol = res
            _cache_timestamp = now
            log.info("[CorporateEvents] Refreshed announcements for %d symbols", len(res))
    except Exception as exc:
        log.warning("[CorporateEvents] Refresh failed: %s", exc)


def check_symbol_event_risk(symbol: str) -> dict[str, Any]:
    """
    Check if a symbol has active or upcoming high-impact event risk.
    Returns structured risk evaluation and gate verdict.
    """
    clean_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
    events = _events_by_symbol.get(clean_sym, [])

    high_risk_events = [e for e in events if e.get("risk_level") == "HIGH_RISK"]
    has_high_risk = len(high_risk_events) > 0

    return {
        "symbol": clean_sym,
        "has_event_risk": has_high_risk,
        "risk_level": "HIGH_RISK" if has_high_risk else ("LOW_RISK" if events else "CLEAR"),
        "event_risk_penalty": 10 if has_high_risk else 0,
        "hard_skip": has_high_risk,
        "recent_events_count": len(events),
        "high_risk_events": high_risk_events[:3],
        "latest_announcement": events[0].get("subject") if events else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
