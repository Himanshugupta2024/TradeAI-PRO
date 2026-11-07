"""Pytest global configuration and cleanup fixtures."""

import os
import pytest
from db.connection import SessionLocal
from db.models import RuntimeState


@pytest.fixture(autouse=True)
def clean_test_runtime_state():
    """Ensure test runs remain completely isolated and do not leave stale state in runtime_state or leaky env vars."""
    saved_env = {
        k: os.environ.get(k)
        for k in ("LIGHTWEIGHT_API_MODE", "INITIAL_LOAD_ENABLED", "BACKGROUND_REFRESH_ENABLED")
    }
    yield
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        session = SessionLocal()
        session.query(RuntimeState).filter(
            (RuntimeState.state_key.like("%pytest%"))
            | (RuntimeState.state_key.like("%Temp%"))
            | (RuntimeState.state_key.like("%tmp%"))
        ).delete(synchronize_session=False)
        session.commit()
        session.close()
    except Exception:
        pass
