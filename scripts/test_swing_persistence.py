"""
End-to-end smoke test for Milestone 2 — swing scan persistence.

Simulates a full refresh → persist → hydrate cycle WITHOUT hitting live market
data or the real pattern scanner. Uses a tmp SQLite file so your real
trading.db is never touched.

Run from the repo root:

    python scripts/test_swing_persistence.py

Success output = one line per stage, ending with "OK — all stages passed".
Exit code 0 on success, 1 on any assertion failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Point DATABASE_URL at a temp file BEFORE importing anything that touches the DB.
_tmp = tempfile.NamedTemporaryFile(prefix="swing_smoke_", suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp.name).as_posix()}"

import pandas as pd  # noqa: E402

from db.connection import init_db  # noqa: E402
from db import swing_store  # noqa: E402
from api import service as svc  # noqa: E402


def _dummy_setup(symbol: str, grade: str = "A") -> dict:
    return {
        "symbol": symbol,
        "pattern": "20-Day Range Breakout",
        "direction": "LONG",
        "strength": 78,
        "entry_zone": 100.0,
        "stop_loss": 95.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "risk_reward": 2.5,
        "timeframe": "3-7 days",
        "grade": grade,
        "description": "smoke-test setup",
    }


async def main() -> int:
    print(f"[smoke] DATABASE_URL = {os.environ['DATABASE_URL']}")
    init_db()
    print("[smoke] OK stage 1 - init_db (tables created)")

    # Stub out live data + pattern scanner inside the service.
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0], "volume": [1_000, 1_100, 1_200]})
    svc._daily_data_cache = {"RELIANCE.NS": df, "TCS.NS": df}

    def _fake_scan(*_args, **_kwargs) -> list[dict]:
        return [
            _dummy_setup("RELIANCE.NS", "A"),
            _dummy_setup("TCS.NS", "B"),
            _dummy_setup("INFY.NS", "C"),
        ]

    svc.swing_scan_all = _fake_scan  # type: ignore[assignment]
    svc._build_swing_market_context = lambda: {        # type: ignore[assignment]
        "index_symbol": "NIFTY 50",
        "index_regime": "TRENDING",
        "index_trend": "BULLISH",
    }

    ok = await svc._recompute_swing_cache(timeout_sec=10.0, trigger="manual")
    assert ok is True, "recompute failed"
    assert len(svc._swing_cache["setups"]) == 3, "cache should have 3 setups"
    print(f"[smoke] OK stage 2 - recompute + persist (setups={len(svc._swing_cache['setups'])})")

    # Verify the row landed in SQLite.
    latest = swing_store.load_latest_scan()
    assert latest is not None, "no scan persisted"
    assert latest["total_setups"] == 3
    assert latest["grade_a_count"] == 1
    assert latest["grade_b_count"] == 1
    assert latest["grade_c_count"] == 1
    assert latest["trigger"] == "manual"
    assert latest["market_context"]["index_trend"] == "BULLISH"
    print(
        f"[smoke] OK stage 3 - DB round-trip "
        f"(scan_id={latest['id']}, A={latest['grade_a_count']} "
        f"B={latest['grade_b_count']} C={latest['grade_c_count']})"
    )

    # Simulate a fresh process by wiping the in-memory cache + hydrating.
    svc._swing_cache = {
        "setups": [],
        "total_found": 0,
        "symbols_scanned": 0,
        "real_data": False,
        "updated_at": None,
        "market_context": None,
    }
    hydrated = await svc._hydrate_swing_cache_from_db()
    assert hydrated is True, "hydration should succeed"
    assert len(svc._swing_cache["setups"]) == 3, "cache should be re-populated from DB"
    assert svc._swing_cache["real_data"] is True
    assert svc._swing_cache["hydrated_from_db"] is True
    print(
        f"[smoke] OK stage 4 - hydrate from DB "
        f"(setups={len(svc._swing_cache['setups'])}, trend={svc._swing_cache['market_context']['index_trend']})"
    )

    # Verify per-setup outcome rows were created.
    open_rows = swing_store.fetch_open_outcomes()
    assert len(open_rows) == 3, f"expected 3 OPEN outcome rows, got {len(open_rows)}"
    print(f"[smoke] OK stage 5 - outcome rows ({len(open_rows)} OPEN)")

    # Second scan should create a second scan + second set of outcome rows.
    await svc._recompute_swing_cache(timeout_sec=10.0, trigger="periodic")
    all_scans = swing_store.list_recent_scans(limit=10)
    assert len(all_scans) == 2, f"expected 2 scans, got {len(all_scans)}"
    print(f"[smoke] OK stage 6 - multiple scans persist ({len(all_scans)} rows)")

    print("\n[smoke] OK - all stages passed")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except AssertionError as e:
        print(f"\n[smoke] FAIL - {e}")
        rc = 1
    except Exception as e:   # pragma: no cover
        print(f"\n[smoke] ERROR - {type(e).__name__}: {e}")
        rc = 1
    finally:
        # Best-effort cleanup of the temp DB.
        try:
            Path(_tmp.name).unlink(missing_ok=True)
        except Exception:
            pass
    sys.exit(rc)
