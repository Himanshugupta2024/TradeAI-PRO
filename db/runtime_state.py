"""
Helpers for runtime key-value state persisted in database (PostgreSQL/SQLite).
Provides reliable state reads, atomic upserts, and explicit failure signaling.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError, ObjectDeletedError

from db.connection import SessionLocal, init_db
from db.models import RuntimeState

logger = logging.getLogger(__name__)

_schema_ready = False


class DatabaseStateError(RuntimeError):
    """Raised when database connectivity or operations fail during runtime state access."""
    pass


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    try:
        init_db()
        _schema_ready = True
    except Exception as exc:
        logger.error("Failed to ensure database schema for runtime state: %s", exc)
        raise DatabaseStateError(f"Database schema initialization failed: {exc}") from exc


def get_runtime_state(key: str, default: Any = None) -> Any:
    """
    Read a JSON-decoded runtime state object by key.
    
    Returns:
      - default if the key does not exist in the database.
      - parsed JSON object if found.
      
    Raises:
      - DatabaseStateError if a database connection or query error occurs.
    """
    _ensure_schema()
    session = SessionLocal()
    try:
        row = session.get(RuntimeState, key)
        if row is None:
            return default
        return json.loads(row.state_json)
    except json.JSONDecodeError as jde:
        logger.error("Corrupt JSON in runtime state for key '%s': %s", key, jde)
        return default
    except (SQLAlchemyError, Exception) as exc:
        logger.exception("Database error while reading runtime state for key '%s': %s", key, exc)
        raise DatabaseStateError(f"Database error while reading runtime state for key '{key}': {exc}") from exc
    finally:
        session.close()


def get_runtime_states(keys: Sequence[str], session: Optional[Any] = None) -> dict[str, Any]:
    """
    Read multiple JSON-decoded runtime state objects by key in a single round-trip query.
    If session is provided, reuses it without closing.
    """
    if not keys:
        return {}
    _ensure_schema()
    close_session = False
    if session is None:
        session = SessionLocal()
        close_session = True
    try:
        stmt = select(RuntimeState).where(RuntimeState.state_key.in_(list(keys)))
        rows = session.execute(stmt).scalars().all()
        result = {}
        for row in rows:
            try:
                result[row.state_key] = json.loads(row.state_json)
            except json.JSONDecodeError as jde:
                logger.error("Corrupt JSON in runtime state for key '%s': %s", row.state_key, jde)
        return result
    except (SQLAlchemyError, Exception) as exc:
        logger.exception("Database error while reading runtime states for keys %s: %s", keys, exc)
        raise DatabaseStateError(f"Database error while reading runtime states: {exc}") from exc
    finally:
        if close_session:
            session.close()


def set_runtime_state(key: str, value: Any) -> None:
    """
    Upsert a runtime state object by key using atomic JSON encoding.
    
    Raises:
      - DatabaseStateError if persistence fails.
    """
    _ensure_schema()
    session = SessionLocal()
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        row = session.get(RuntimeState, key)
        if row is None:
            row = RuntimeState(
                state_key=key,
                state_json=encoded,
                updated_at=datetime.now(timezone.utc),
            )
            session.add(row)
        else:
            row.state_json = encoded
            row.updated_at = datetime.now(timezone.utc)
        try:
            session.commit()
        except (StaleDataError, ObjectDeletedError):
            session.rollback()
            session.expunge_all()
            existing = session.get(RuntimeState, key)
            if existing is None:
                session.add(
                    RuntimeState(
                        state_key=key,
                        state_json=encoded,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
            else:
                existing.state_json = encoded
                existing.updated_at = datetime.now(timezone.utc)
            session.commit()
    except Exception as exc:
        session.rollback()
        logger.exception("Database error while writing runtime state for key '%s': %s", key, exc)
        raise DatabaseStateError(f"Database error while writing runtime state for key '{key}': {exc}") from exc
    finally:
        session.close()
