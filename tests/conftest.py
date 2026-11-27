"""Pytest global configuration and cleanup fixtures.

Test isolation contract
-----------------------
``config.settings`` resolves ``DATABASE_URL`` from the environment at *import*
time, and ``db.connection`` builds its engine from that value at import time
too. Import order therefore decides which database a test writes to. Before
this module did the rewrite below, ``pytest`` imported the project against
whatever ``.env`` contained (``sqlite:///./trading.db``), so the suite wrote
into the developer's real database: learned weights, ``runtime_state`` rows and
seeded ``trade_outcomes_pg`` fixtures all landed in production-shaped data. The
tests still passed, which is exactly why it went unnoticed.

This module now points ``DATABASE_URL`` at a throwaway SQLite file *before*
anything from the project is imported, then creates the schema there.

Opt out with ``PYTEST_ALLOW_REAL_DB=1`` to run against the configured database
(useful when debugging against a copy of production data). Even then the
runtime-state purge fixture below still runs.
"""

import atexit
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

_TEMP_DB_DIR = None
_ARTIFACT_DIR = None

if os.getenv("PYTEST_ALLOW_REAL_DB") != "1":
    _TEMP_DB_DIR = tempfile.mkdtemp(prefix="trading-pytest-db-")
    _TEMP_DB_PATH = os.path.join(_TEMP_DB_DIR, "test_trading.db")
    os.environ["DATABASE_URL"] = "sqlite:///" + _TEMP_DB_PATH.replace("\\", "/")
    # ENVIRONMENT=test keeps the SQLite-in-production guard in db.connection
    # quiet and tells api.security that an admin key is not mandatory.
    os.environ["ENVIRONMENT"] = "test"

from db.connection import SessionLocal, engine, init_db  # noqa: E402
from db.models import RuntimeState  # noqa: E402


def _create_test_schema() -> None:
    """Create every table in the throwaway database (or the real one on opt-out)."""
    try:
        init_db()
    except Exception as exc:  # pragma: no cover - surfaces as test errors instead
        print(f"[conftest] init_db() failed: {exc}", file=sys.stderr)
        raise


def _drop_temp_db() -> None:
    """Dispose the engine before deleting the file (Windows keeps it locked)."""
    if not _TEMP_DB_DIR:
        return
    try:
        engine.dispose()
    except Exception:
        pass
    shutil.rmtree(_TEMP_DB_DIR, ignore_errors=True)


_create_test_schema()
atexit.register(_drop_temp_db)


@pytest.fixture(scope="session", autouse=True)
def isolate_runtime_artifacts():
    """Point runtime artifacts at a temp dir for the whole session.

    The database rewrite above stops tests writing *rows* to the real database,
    but `agents.analytics.feedback_loop._WEIGHTS_PATH` is a plain module global
    pointing at `config/learned_weights.json`. Any test that drives
    `FeedbackAnalyser.run_daily_eod_review()` therefore stamps the repository's
    learned-weights sidecar with its own synthetic insights (observed: a
    COLD_START / zero-trade snapshot replacing the trained one). Patching once
    here covers tests that never knew they touched it.
    """
    global _ARTIFACT_DIR
    if os.getenv("PYTEST_ALLOW_REAL_DB") == "1":
        yield
        return

    import agents.analytics.feedback_loop as feedback_loop

    _ARTIFACT_DIR = tempfile.mkdtemp(prefix="trading-pytest-artifacts-")
    original_path = feedback_loop._WEIGHTS_PATH
    feedback_loop._WEIGHTS_PATH = Path(_ARTIFACT_DIR) / "learned_weights.json"
    try:
        yield
    finally:
        feedback_loop._WEIGHTS_PATH = original_path
        shutil.rmtree(_ARTIFACT_DIR, ignore_errors=True)


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
    try:
        pb = sys.modules.get("data.providers.base")
        if pb is not None and hasattr(pb, "reset_shared_provider_health"):
            pb.reset_shared_provider_health()
    except Exception:
        pass
    try:
        bh = sys.modules.get("agents.data_ingestion.binance_http")
        if bh is not None and hasattr(bh, "reset_binance_geo_cooldown"):
            bh.reset_binance_geo_cooldown()
    except Exception:
        pass
    try:
        mc = sys.modules.get("api.ml_client")
        if mc is not None and hasattr(mc, "reset_ml_sidecar_state"):
            mc.reset_ml_sidecar_state()
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
