"""
TradeAI-PRO — Prediction Outcome Tracker & Audit Store.
======================================================
Stores, persists, and resolves multi-horizon forecasts against actual market prices:
- Tracks: symbol, timestamp, reference_price, predicted_range, predicted_direction, realized_price, error, model_version
- Horizons: 'intraday', 'next_day', 'swing'
- Multi-tier storage: In-memory circular buffer + persistent JSON audit store in data/
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TRACKER_STORE_PATH = Path("data") / "prediction_tracking_audit.json"
_MAX_BUFFER_SIZE = 500

_PREDICTION_RECORDS: deque[Dict[str, Any]] = deque(maxlen=_MAX_BUFFER_SIZE)


def _load_persisted_records() -> None:
    """Load previously persisted prediction records on startup."""
    global _PREDICTION_RECORDS
    if _PREDICTION_RECORDS:
        return
    if _TRACKER_STORE_PATH.exists():
        try:
            with open(_TRACKER_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data[-_MAX_BUFFER_SIZE:]:
                        _PREDICTION_RECORDS.append(item)
            logger.info("Loaded %d persisted prediction tracking records", len(_PREDICTION_RECORDS))
        except Exception as e:
            logger.warning("Could not load prediction tracking audit: %s", e)


def _save_persisted_records() -> None:
    """Flush circular buffer to persistent JSON file."""
    try:
        _TRACKER_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_TRACKER_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(list(_PREDICTION_RECORDS), f, indent=2)
    except Exception as e:
        logger.warning("Could not persist prediction tracking records: %s", e)


def record_prediction(
    symbol: str,
    reference_price: float,
    predicted_range: Tuple[float, float],
    predicted_direction: str,
    probabilities: Dict[str, float],
    horizon: str = "swing",
    model_version: str = "v1.2.0-ensemble-6layer",
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Log an active prediction into the audit tracker.
    """
    _load_persisted_records()
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    clean_sym = symbol.strip().upper().replace(".NS", "")

    record = {
        "id": f"{clean_sym}_{horizon}_{int(datetime.now().timestamp() * 1000)}",
        "symbol": clean_sym,
        "timestamp": ts,
        "horizon": horizon.lower(),
        "model_version": model_version,
        "reference_price": round(float(reference_price), 2),
        "predicted_lower": round(float(predicted_range[0]), 2),
        "predicted_upper": round(float(predicted_range[1]), 2),
        "predicted_direction": predicted_direction.upper(),
        "probabilities": probabilities,
        "realized_price": None,
        "prediction_error": None,
        "directional_hit": None,
        "resolved_at": None,
        "status": "OPEN",
    }

    _PREDICTION_RECORDS.append(record)
    _save_persisted_records()
    return record


def update_realized_outcome(
    symbol: str,
    realized_price: float,
    horizon: Optional[str] = None,
) -> int:
    """
    Update open predictions for a symbol with actual realized market price and compute error.
    """
    _load_persisted_records()
    clean_sym = symbol.strip().upper().replace(".NS", "")
    resolved_count = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for rec in _PREDICTION_RECORDS:
        if rec["symbol"] == clean_sym and rec["status"] == "OPEN":
            if horizon and rec["horizon"] != horizon.lower():
                continue

            ref = float(rec["reference_price"])
            real = round(float(realized_price), 2)
            err = round(abs(real - ref), 2)

            # Check directional correctness
            pred_dir = rec["predicted_direction"]
            if pred_dir == "UP":
                hit = real > ref
            elif pred_dir == "DOWN":
                hit = real < ref
            else:
                # SIDEWAYS: within predicted range
                hit = rec["predicted_lower"] <= real <= rec["predicted_upper"]

            rec["realized_price"] = real
            rec["prediction_error"] = err
            rec["directional_hit"] = hit
            rec["resolved_at"] = now_iso
            rec["status"] = "RESOLVED"
            resolved_count += 1

    if resolved_count > 0:
        _save_persisted_records()

    return resolved_count


def get_prediction_history(
    symbol: Optional[str] = None,
    horizon: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Retrieve filtered prediction records."""
    _load_persisted_records()
    records = list(_PREDICTION_RECORDS)

    if symbol:
        clean_sym = symbol.strip().upper().replace(".NS", "")
        records = [r for r in records if r["symbol"] == clean_sym]

    if horizon:
        h = horizon.lower()
        records = [r for r in records if r["horizon"] == h]

    return records[-limit:]


def compute_tracking_accuracy(horizon: Optional[str] = None) -> Dict[str, Any]:
    """Compute aggregate accuracy metrics on resolved predictions."""
    _load_persisted_records()
    resolved = [r for r in _PREDICTION_RECORDS if r["status"] == "RESOLVED"]

    if horizon:
        resolved = [r for r in resolved if r["horizon"] == horizon.lower()]

    if not resolved:
        return {
            "total_evaluated": 0,
            "directional_accuracy_pct": 50.0,
            "mean_absolute_error": 0.0,
            "status": "NO_RESOLVED_DATA",
        }

    hits = sum(1 for r in resolved if r.get("directional_hit"))
    errors = [float(r["prediction_error"]) for r in resolved if r.get("prediction_error") is not None]

    accuracy = round((hits / len(resolved)) * 100.0, 1)
    mae = round(float(sum(errors) / len(errors)), 2) if errors else 0.0

    return {
        "total_evaluated": len(resolved),
        "directional_hits": hits,
        "directional_accuracy_pct": accuracy,
        "mean_absolute_error": mae,
        "horizon": horizon or "all",
        "status": "ACCURACY_COMPUTED",
    }
