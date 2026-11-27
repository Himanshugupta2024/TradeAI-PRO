"""
Regression tests for worker cycle, daemon gates, intraday targets,
nightly scan IST holiday calendar, Render config, and workflow hardening.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


# ── 1. scripts/autonomous_cycle.py ────────────────────────────────────────────

def _make_ohlcv(rows: int = 60, trend: str = "UP", freq: str = "5min") -> pd.DataFrame:
    if freq == "1D":
        # Historical daily bars ending before today's intraday session (point-in-time valid)
        ts = pd.date_range(end="2026-11-19 00:00:00+05:30", periods=rows, freq="1D")
        span = (100.0, 180.0) if trend == "UP" else (180.0, 100.0)
    else:
        ts = pd.date_range("2026-11-20 09:15:00+05:30", periods=rows, freq=freq)
        span = (100.0, 125.0) if trend == "UP" else (125.0, 100.0)
    if trend in ("UP", "DOWN"):
        base = np.linspace(span[0], span[1], rows)
    else:
        base = 100.0 + np.sin(np.linspace(0, 4 * np.pi, rows))
    return pd.DataFrame({
        "timestamp": ts,
        "open": base - 0.2,
        "high": base + 0.6,
        "low": base - 0.6,
        "close": base,
        "volume": np.full(rows, 50_000.0),
    })


def test_autonomous_cycle_passes_regime_htf_preserves_skip_and_bounds_concurrency(monkeypatch):
    from scripts import autonomous_cycle
    from agents.feature_engine.regime_detector import MarketRegime
    import agents.execution.market_control as mc
    import agents.execution.session_guard as sg

    monkeypatch.setattr(autonomous_cycle, "init_db", lambda: None)
    monkeypatch.setattr(autonomous_cycle, "is_nse_market_open", lambda dt=None: (True, "SESSION_ACTIVE"))
    monkeypatch.setattr(sg, "is_nse_market_open", lambda dt=None: (True, "SESSION_ACTIVE"))
    monkeypatch.setattr(mc, "is_crypto_forex_enabled", lambda: False)
    monkeypatch.setattr(autonomous_cycle, "evaluate_data_freshness", lambda df, sym: ("LIVE", 60.0))
    monkeypatch.setattr(autonomous_cycle, "record_autonomous_decision_to_db", lambda **kwargs: {})
    monkeypatch.setattr(autonomous_cycle, "detect_regime", lambda df: MarketRegime.TRENDING)

    lock_acquires = []

    class _StubLockMgr:
        def __init__(self, default_duration_sec=480, heartbeat_timeout_sec=180):
            self.default_duration_sec = default_duration_sec

        def acquire(self, job_name, run_id, duration_sec=None, meta=None):
            lock_acquires.append(duration_sec)
            return True

        def heartbeat(self, job_name, run_id, extend_sec=120):
            return True

        def release(self, job_name, run_id, status="COMPLETED", meta=None):
            return True

    monkeypatch.setattr(autonomous_cycle, "DistributedLockManager", _StubLockMgr)

    class _StubDaemon:
        enabled = False
        max_positions = 8
        def manage_open_trades(self, *args, **kwargs):
            return []
        def record_decision(self, *args, **kwargs):
            return {}
        def check_daily_eod_review(self):
            return None
        def _get_regime_thresholds(self, regime):
            return 50.0, 40.0

    class _StubEngine:
        capital = 100_000.0
        open_trades = []

    monkeypatch.setattr(autonomous_cycle, "get_autonomous_daemon", lambda: _StubDaemon())
    monkeypatch.setattr(autonomous_cycle, "PaperTradingEngine", lambda: _StubEngine())

    persisted_state = {}
    monkeypatch.setattr(autonomous_cycle, "set_runtime_state", lambda k, v: persisted_state.__setitem__(k, v))

    import db.intraday_store as intraday_store
    monkeypatch.setattr(intraday_store, "save_scan", lambda *args, **kwargs: None)

    in_flight = 0
    max_in_flight = 0

    def _fake_fetch_ohlcv(sym, period="5d", interval="5m"):
        nonlocal in_flight, max_in_flight
        if interval == "5m":
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
            time.sleep(0.03)
            in_flight -= 1
            return _make_ohlcv(60, trend="UP", freq="5min")
        return _make_ohlcv(60, trend="UP", freq="1D")

    monkeypatch.setattr(autonomous_cycle, "fetch_ohlcv", _fake_fetch_ohlcv)

    captured_calls = []

    def _fake_generate_signals(df, symbol="", regime=None, **kwargs):
        captured_calls.append({
            "symbol": symbol,
            "regime": regime,
            "has_htf": "htf_trend" in df.columns,
            "htf_val": str(df["htf_trend"].iloc[-1]) if "htf_trend" in df.columns else None,
        })
        out = df.copy()
        out["signal"] = "HOLD"
        out["signal_type"] = "HOLD"
        out["quality_score"] = 0.0
        out["confidence"] = 0.0
        out["skip_reason"] = ""
        out["quality_hard_gate"] = ""
        out["quality_grade"] = ""
        out["signal_conflict"] = ""
        if symbol == "RELIANCE":
            out.iloc[-1, out.columns.get_loc("signal")] = "SKIP"
            out["_raw_direction"] = "LONG"
            out.iloc[-1, out.columns.get_loc("quality_score")] = 48.0
            out.iloc[-1, out.columns.get_loc("confidence")] = 55.0
            out.iloc[-1, out.columns.get_loc("skip_reason")] = "QUALITY_GRADE_C"
            out["stop_loss"] = 118.0
            out["target"] = 132.0
        return out

    monkeypatch.setattr(autonomous_cycle, "generate_signals", _fake_generate_signals)

    symbols = "RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN,ITC,LT"
    summary = asyncio.run(
        autonomous_cycle.run_single_autonomous_cycle(
            universe_arg=symbols,
            force_scan=True,
            dry_run=True,
        )
    )

    assert summary["ok"] is True
    assert summary["symbols_scanned"] == 8
    assert lock_acquires == [660]
    assert 1 < max_in_flight <= 4, f"Expected bounded concurrency in (1, 4], got {max_in_flight}"

    assert len(captured_calls) == 8
    for call in captured_calls:
        assert call["regime"] == MarketRegime.TRENDING
        assert call["has_htf"] is True
        assert call["htf_val"] == "BULLISH"

    sig_rows = persisted_state["latest_signals:v1"]["signals"]
    by_sym = {row["symbol"]: row for row in sig_rows}
    assert by_sym["RELIANCE"]["signal"] == "SKIP"
    assert by_sym["RELIANCE"]["signal_status"] == "SKIP"
    assert by_sym["RELIANCE"]["skip_reason"] == "QUALITY_GRADE_C"
    assert by_sym["RELIANCE"]["direction"] == "LONG"
    assert by_sym["RELIANCE"]["stop_loss"] == 118.0
    assert by_sym["RELIANCE"]["target"] == 132.0
    assert by_sym["TCS"]["signal"] == "HOLD"
    assert by_sym["TCS"]["signal_status"] == "NO_SIGNAL"


# ── 2. agents/execution/autonomous_daemon.py ──────────────────────────────────

def test_daemon_blocks_zero_qs_score_and_flags_missing_unparseable_timestamps(tmp_path):
    from agents.execution.autonomous_daemon import AutonomousExecutionDaemon
    from agents.execution.paper_trading import PaperTradingEngine

    daemon = AutonomousExecutionDaemon(max_positions=5, min_qs_score=50.0, min_ai_confidence=40.0)
    daemon.enabled = True
    daemon.decision_log = []

    blocked = []
    daemon._record_gate_block = lambda sym, d, e, sl, tp, reason, reg: blocked.append((sym, reason))

    engine = PaperTradingEngine(
        initial_capital=100_000.0,
        log_file=str(tmp_path / "trades.json"),
        state_key="test_zero_qs",
    )
    engine.trades = []
    engine.open_trades = []

    # 1. qs_score == 0.0 must be rejected by GATE_LOW_QS_SCORE
    executed = daemon.evaluate_signals(
        signals=[{
            "symbol": "INFY",
            "signal": "LONG",
            "entry": 1500.0,
            "stop_loss": 1400.0,
            "target": 1700.0,
            "quant_score": 0.0,
            "confidence": 85.0,
            "regime": "TRENDING",
        }],
        current_prices={"INFY": {"close": 1500.0}},
        engine=engine,
    )
    assert executed == []
    assert blocked == [("INFY", "GATE_LOW_QS_SCORE")]

    # 2. Missing / unparseable timestamps must return True (stale/unknown)
    assert daemon._is_feed_stale("BTC/USDT", None)[0] is True
    assert daemon._is_feed_stale("BTC/USDT", "")[0] is True
    assert daemon._is_feed_stale("RELIANCE", "not-a-timestamp")[0] is True

    # 3. Unparseable timestamp passed via signal is rejected by evaluate_signals
    executed_bad_ts = daemon.evaluate_signals(
        signals=[{
            "symbol": "TCS",
            "signal": "LONG",
            "entry": 3500.0,
            "stop_loss": 3300.0,
            "target": 3900.0,
            "quant_score": 80.0,
            "confidence": 80.0,
            "timestamp": "corrupt-ts",
        }],
        current_prices={"TCS": {"close": 3500.0, "timestamp": "corrupt-ts"}},
        engine=engine,
    )
    assert executed_bad_ts == []
    assert daemon.decision_log[0]["action"] == "SKIP_STALE_DATA"


# ── 3. agents/intraday_engine/strategies.py ───────────────────────────────────

def test_intraday_risk_wrapped_signal_non_formulaic_rr():
    from agents.intraday_engine.strategies import _risk_wrapped_signal

    # Case A: Strong bullish momentum with prev_day CPR resistance
    ts_day = pd.date_range("2025-06-10 09:15:00", periods=25, freq="5min")
    closes_a = np.linspace(100.0, 108.0, 25)
    df_a = pd.DataFrame({
        "timestamp": ts_day,
        "open": closes_a - 0.15,
        "high": closes_a + 0.45,
        "low": closes_a - 0.35,
        "close": closes_a,
        "volume": np.full(25, 100_000.0),
    })

    # Case B: Modest volatility with different RSI / ATR profile
    closes_b = np.array([100.0, 100.4, 99.8, 100.3, 100.9, 100.2, 100.6, 101.2, 100.8, 101.3,
                         101.0, 101.5, 101.1, 101.6, 101.2, 101.8, 101.4, 101.9, 101.7, 102.2,
                         101.9, 102.4, 102.0, 102.5, 102.8])
    df_b = pd.DataFrame({
        "timestamp": ts_day,
        "open": closes_b - 0.2,
        "high": closes_b + 0.7,
        "low": closes_b - 0.6,
        "close": closes_b,
        "volume": np.full(25, 100_000.0),
    })

    # Case C: Prior-day CPR structure providing an explicit overhead R1 target
    prev_day = pd.DataFrame({
        "timestamp": pd.date_range("2025-06-09 09:15:00", periods=10, freq="5min"),
        "open": np.full(10, 103.0),
        "high": np.full(10, 109.5),
        "low": np.full(10, 100.5),
        "close": np.full(10, 105.0),
        "volume": np.full(10, 80_000.0),
    })

    sig_a = _risk_wrapped_signal(
        symbol="RELIANCE", strategy="ORB", direction="LONG", score=80.0,
        entry=float(df_a["close"].iloc[-1]), rationale="test A", df=df_a,
    )
    sig_b = _risk_wrapped_signal(
        symbol="INFY", strategy="MOMENTUM", direction="LONG", score=78.0,
        entry=float(df_b["close"].iloc[-1]), rationale="test B", df=df_b,
    )
    sig_c = _risk_wrapped_signal(
        symbol="TCS", strategy="CPR", direction="LONG", score=82.0,
        entry=float(df_b["close"].iloc[-1]), rationale="test C", df=df_b, prev_day_df=prev_day,
    )
    sig_short = _risk_wrapped_signal(
        symbol="SBIN", strategy="VWAP", direction="SHORT", score=77.0,
        entry=float(df_b["close"].iloc[-1]), rationale="test SHORT", df=df_b, prev_day_df=prev_day,
    )

    for s in (sig_a, sig_b, sig_c, sig_short):
        assert s is not None
        assert s.risk_reward >= 1.5

    assert sig_a.stop_loss < sig_a.entry < sig_a.target_1 < sig_a.target_2
    assert sig_short.target_2 < sig_short.target_1 < sig_short.entry < sig_short.stop_loss

    rrs = {round(s.risk_reward, 3) for s in (sig_a, sig_b, sig_c, sig_short)}
    assert len(rrs) > 1, f"Expected non-formulaic varying R:Rs, got {rrs}"
    assert rrs != {2.0}, f"R:Rs collapsed to constant 2.0: {rrs}"


# ── 4. scripts/nightly_swing_scan.py ──────────────────────────────────────────

def test_nightly_swing_scan_uses_canonical_nse_holidays_and_ist(monkeypatch):
    from scripts import nightly_swing_scan

    assert not hasattr(nightly_swing_scan, "NSE_HOLIDAYS_2026")
    # 2026-06-26 is Muharram in config/nse_holidays_2026.json (stale list had 06-25)
    assert nightly_swing_scan.is_nse_trading_day(date(2026, 6, 26)) is False
    # 2026-02-17 was wrongly in stale list, is a normal Tuesday in canonical config
    assert nightly_swing_scan.is_nse_trading_day(date(2026, 2, 17)) is True
    # 2026-11-24 is Guru Nanak Jayanti
    assert nightly_swing_scan.is_nse_trading_day(date(2026, 11, 24)) is False
    # Weekend check
    assert nightly_swing_scan.is_nse_trading_day(date(2026, 11, 28)) is False

    # 2026-11-23 22:07 UTC is already 2026-11-24 03:37 IST (Guru Nanak Jayanti)
    monkeypatch.setattr(nightly_swing_scan, "_today_ist", lambda: date(2026, 11, 24))
    assert nightly_swing_scan.is_nse_trading_day() is False


# ── 5. render.yaml, verify_box_b.py & .github/workflows/*.yml ─────────────────

def test_render_yaml_and_verify_box_b_security_and_universe():
    from agents.market_intelligence.nse_fetcher import NSE_SYMBOLS
    from scripts.autonomous_cycle import TIER1_NSE

    render_text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "tradeai-secret-" not in render_text

    parsed = yaml.safe_load(render_text)
    services = {svc["name"]: svc for svc in parsed["services"]}

    for name in ("trading-api", "trading-ml", "trading-feed"):
        envs = {e["key"]: e for e in services[name]["envVars"]}
        assert envs["ENVIRONMENT"]["value"] == "production"

    api_envs = {e["key"]: e for e in services["trading-api"]["envVars"]}
    assert int(api_envs["NSE_SYMBOL_LIMIT"]["value"]) == 40
    assert api_envs["ML_INTERNAL_TOKEN"] == {"key": "ML_INTERNAL_TOKEN", "sync": False}
    assert api_envs["FEED_INTERNAL_TOKEN"] == {"key": "FEED_INTERNAL_TOKEN", "sync": False}

    ml_envs = {e["key"]: e for e in services["trading-ml"]["envVars"]}
    assert ml_envs["ML_INTERNAL_TOKEN"] == {"key": "ML_INTERNAL_TOKEN", "sync": False}

    feed_envs = {e["key"]: e for e in services["trading-feed"]["envVars"]}
    assert feed_envs["FEED_INTERNAL_TOKEN"] == {"key": "FEED_INTERNAL_TOKEN", "sync": False}

    top_40 = list(NSE_SYMBOLS.keys())[:40]
    assert [s for s in TIER1_NSE if s not in top_40] == []

    verify_box_b_src = (ROOT / "scripts" / "verify_box_b.py").read_text(encoding="utf-8")
    assert "tradeai-secret-" not in verify_box_b_src


def test_github_workflows_hardened():
    wf_dir = ROOT / ".github" / "workflows"

    for name in ("autonomous_cycle.yml", "model_train_scheduled.yml", "nightly_swing_scan.yml", "keep_alive.yml"):
        raw = (wf_dir / name).read_text(encoding="utf-8")
        doc = yaml.safe_load(raw)
        for job in doc["jobs"].values():
            for step in job["steps"]:
                run_cmd = step.get("run", "")
                assert "${{ github.event." not in run_cmd, (
                    f"{name} step '{step.get('name')}' interpolates github.event.* directly in run:"
                )

    auto_doc = yaml.safe_load((wf_dir / "autonomous_cycle.yml").read_text(encoding="utf-8"))
    assert auto_doc["jobs"]["run-cycle"]["timeout-minutes"] == 12

    keep_raw = (wf_dir / "keep_alive.yml").read_text(encoding="utf-8")
    assert "https://trading-api-n1wt.onrender.com/api/health/live" in keep_raw
    assert "'https://trading-api-n1wt.onrender.com/api/health'" not in keep_raw

    ci_raw = (wf_dir / "ci.yml").read_text(encoding="utf-8")
    assert "--experimental-default-type=module" not in ci_raw
    assert "tests/test_tab_error_remediation.js" in ci_raw
    assert '"ml_server"' in ci_raw
    assert '"feed_server"' in ci_raw


def test_autonomous_cycle_preserves_signals_snapshot_when_empty():
    """Off-hours cycles producing 0 scanned signals must not overwrite latest_signals:v1 with []."""
    src = (ROOT / "scripts" / "autonomous_cycle.py").read_text(encoding="utf-8")
    anchor = "persisted_sigs = all_scanned_signals if all_scanned_signals else candidate_signals"
    assert anchor in src
    block = src.split(anchor, 1)[1][:450]
    assert "if persisted_sigs:" in block
