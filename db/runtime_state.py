"""Helpers for runtime key-value state persisted in SQLite."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from db.connection import SessionLocal, init_db
from db.models import RuntimeState

_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    init_db()
    _schema_ready = True


def get_runtime_state(key: str, default: Any = None) -> Any:
    """Read a JSON-decoded runtime state object by key."""
    _ensure_schema()
    session = SessionLocal()
    try:
        row = session.get(RuntimeState, key)
        if row is None:
            return default
        return json.loads(row.state_json)
    except Exception:
        return default
    finally:
        session.close()


def set_runtime_state(key: str, value: Any) -> None:
    """Upsert a runtime state object by key using JSON encoding."""
    _ensure_schema()
    session = SessionLocal()
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        row = session.get(RuntimeState, key)
        if row is None:
            row = RuntimeState(state_key=key, state_json=encoded, updated_at=datetime.now(timezone.utc))
            session.add(row)
        else:
            row.state_json = encoded
            row.updated_at = datetime.now(timezone.utc)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
