"""Pytest global configuration and cleanup fixtures."""

import pytest
from db.connection import SessionLocal
from db.models import RuntimeState


@pytest.fixture(autouse=True)
def clean_test_runtime_state():
    """Ensure test runs remain completely isolated and do not leave stale state in runtime_state."""
    yield
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
