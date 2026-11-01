"""Roundtrip tests for db.intraday_store."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import connection as db_conn
from db import models as db_models
from db import intraday_store


@pytest.fixture()
def isolated_intraday_db(tmp_path, monkeypatch):
    db_file = tmp_path / "intraday_test.db"
    url = f"sqlite:///{db_file.as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db_models.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_conn, "engine", engine, raising=False)
    monkeypatch.setattr(db_conn, "SessionLocal", TestSession, raising=False)
    monkeypatch.setattr(intraday_store, "SessionLocal", TestSession, raising=False)
    yield
    try:
        engine.dispose()
    except Exception:
        pass


def test_intraday_save_and_load(isolated_intraday_db):
    sigs = [
        {"symbol": "X", "strategy": "ORB", "direction": "LONG", "score": 55.0, "entry": 1.0, "stop_loss": 0.9, "target": 1.1, "rationale": "t"},
    ]
    scan_id = intraday_store.save_scan(sigs, symbols_scanned=5, trigger="test")
    assert scan_id is not None
    latest = intraday_store.load_latest_scan()
    assert latest is not None
    assert latest["signal_count"] == 1
    assert latest["signals"][0]["symbol"] == "X"
