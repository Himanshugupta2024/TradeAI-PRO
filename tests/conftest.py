"""Pytest global configuration and cleanup fixtures."""

import os
import sys
import threading

import pytest
from db.connection import SessionLocal
from db.models import RuntimeState


def _purge_runtime_state() -> None:
    """Delete test-scoped runtime_state rows (best effort, never fatal)."""
    try:
        session = SessionLocal()
        session.query(RuntimeState).filter(
            (RuntimeState.state_key.like("%pytest%"))
            | (RuntimeState.state_key.like("%Temp%"))
            | (RuntimeState.state_key.like("%tmp%"))
            | (RuntimeState.state_key == "paper_trading_state:default")
        ).delete(synchronize_session=False)
        session.commit()
        session.close()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def clean_test_runtime_state():
    """Ensure test runs remain completely isolated and do not leave stale state in runtime_state or leaky env vars."""
    from config import settings
    saved_env = {
        k: os.environ.get(k)
        for k in (
            "LIGHTWEIGHT_API_MODE",
            "INITIAL_LOAD_ENABLED",
            "BACKGROUND_REFRESH_ENABLED",
            "ADMIN_API_KEY",
            "RENDER",
            "ENVIRONMENT",
            "IS_PRODUCTION",
        )
    }
    saved_admin_key = getattr(settings, "ADMIN_API_KEY", None)
    yield
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if saved_admin_key is not None:
        settings.ADMIN_API_KEY = saved_admin_key
    # Only touch the service singleton when the test already imported it.
    # Importing api.service here pulled in the whole data stack (and its
    # module-level side effects) for every single teardown.
    svc = sys.modules.get("api.service")
    try:
        if svc is not None and getattr(svc, "_engine", None) is not None:
            svc._engine._daily_pnl.clear()
            svc._engine._consecutive_losses = 0
            svc._engine.last_rejection_reason = ""
    except Exception:
        pass

    # Purge in a bounded background thread: a read-only or lock-contended
    # database used to block every teardown until the driver's busy timeout
    # expired, which made the whole suite look like it had hung.
    if os.getenv("PYTEST_SKIP_DB_CLEANUP"):
        return
    purger = threading.Thread(target=_purge_runtime_state, name="pytest-state-purge", daemon=True)
    purger.start()
    purger.join(timeout=3.0)
