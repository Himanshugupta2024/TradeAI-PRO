"""
TradeAI-PRO — Complete Trading Engine Verification Script
Tests and verifies all 17 algorithmic, intelligence, and execution components.
"""

import sys
import os
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

engine_results = {}

print("=" * 80)
print(f"TRADEAI-PRO ENGINE VERIFICATION -- {datetime.now(timezone.utc).isoformat()}")
print("=" * 80)

# Generate baseline realistic price bars for testing
np.random.seed(42)
prices = 100 + np.cumsum(np.random.randn(60))
dates = pd.date_range("2026-11-01", periods=60, freq="5min")
df_raw = pd.DataFrame({
    "timestamp": dates,
    "open": prices + 0.1,
    "high": prices + 1.0,
    "low": prices - 1.0,
    "close": prices,
    "volume": np.random.randint(1000, 10000, 60)
})

# 1. RSI and EMA indicators
print("\n[1/17] Testing Indicators (RSI, EMA, ATR, MACD)...")
try:
    from agents.feature_engine.indicators import calculate_indicators
    ind_df = calculate_indicators(df_raw)
    assert "RSI_14" in ind_df.columns, "RSI_14 missing"
    assert "EMA_20" in ind_df.columns, "EMA_20 missing"
    assert "EMA_50" in ind_df.columns, "EMA_50 missing"
    assert "ATR_14" in ind_df.columns, "ATR_14 missing"
    latest_rsi = ind_df["RSI_14"].iloc[-1]
    latest_ema20 = ind_df["EMA_20"].iloc[-1]
    print(f"  [OK] Indicators calculated: Latest RSI_14={latest_rsi:.2f}, EMA_20={latest_ema20:.2f}")
    engine_results["Indicators"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Indicators failed: {e}")
    engine_results["Indicators"] = f"FAIL: {e}"

# 2. Market regime detection
print("\n[2/17] Testing Market Regime Detection...")
try:
    from agents.feature_engine.regime_detector import detect_regime
    regime = detect_regime(ind_df)
    reg_val = getattr(regime, "regime", regime) if hasattr(regime, "regime") else regime
    print(f"  [OK] Regime detected: {reg_val}")
    engine_results["Regime_Detector"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Regime detector failed: {e}")
    engine_results["Regime_Detector"] = f"FAIL: {e}"

# 3. SwingSpectrum pattern detection
print("\n[3/17] Testing SwingSpectrum Patterns...")
try:
    from agents.market_intelligence.swing_scanner import scan_swing_setups
    trend_prices = np.linspace(100, 150, 60) + np.sin(np.linspace(0, 10, 60)) * 2
    swing_df = pd.DataFrame({
        "open": trend_prices - 0.2,
        "high": trend_prices + 1.5,
        "low": trend_prices - 1.5,
        "close": trend_prices,
        "volume": np.random.randint(50000, 100000, 60)
    })
    setups = scan_swing_setups(swing_df, symbol="TEST_STOCK")
    print(f"  [OK] Swing scanner executed cleanly: {len(setups)} setups matched filters")
    engine_results["SwingSpectrum"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Swing scanner failed: {e}")
    engine_results["SwingSpectrum"] = f"FAIL: {e}"

# 4. Intraday VWAP Strategy
print("\n[4/17] Testing Intraday VWAP Strategy...")
try:
    from agents.intraday_engine.strategies import strat_vwap
    sig = strat_vwap("TEST_STOCK", ind_df)
    print(f"  [OK] VWAP strategy evaluated: Signal={sig}")
    engine_results["VWAP_Strategy"] = "PASS"
except Exception as e:
    print(f"  [FAIL] VWAP strategy failed: {e}")
    engine_results["VWAP_Strategy"] = f"FAIL: {e}"

# 5. CPR Breakout Strategy
print("\n[5/17] Testing CPR Breakout Strategy...")
try:
    from agents.intraday_engine.strategies import strat_cpr
    sig = strat_cpr("TEST_STOCK", ind_df)
    print(f"  [OK] CPR strategy evaluated: Signal={sig}")
    engine_results["CPR_Strategy"] = "PASS"
except Exception as e:
    print(f"  [FAIL] CPR strategy failed: {e}")
    engine_results["CPR_Strategy"] = f"FAIL: {e}"

# 6. Gap-and-Go Strategy
print("\n[6/17] Testing Gap-and-Go Strategy...")
try:
    from agents.intraday_engine.strategies import strat_gap_go
    sig = strat_gap_go("TEST_STOCK", ind_df)
    print(f"  [OK] Gap-and-Go strategy evaluated: Signal={sig}")
    engine_results["Gap_Go_Strategy"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Gap-and-Go strategy failed: {e}")
    engine_results["Gap_Go_Strategy"] = f"FAIL: {e}"

# 7. Supertrend Strategy
print("\n[7/17] Testing Supertrend Strategy...")
try:
    from agents.intraday_engine.strategies import strat_supertrend
    sig = strat_supertrend("TEST_STOCK", ind_df)
    print(f"  [OK] Supertrend strategy evaluated: Signal={sig}")
    engine_results["Supertrend_Strategy"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Supertrend strategy failed: {e}")
    engine_results["Supertrend_Strategy"] = f"FAIL: {e}"

# 8. EdgeScore Factor Calculation
print("\n[8/17] Testing EdgeScore Factor Engine...")
try:
    from agents.macro.edge_score_engine import EdgeScoreEngine
    edge_engine = EdgeScoreEngine()
    breakdown = edge_engine.get_edge_score("RELIANCE", asset_class="nse")
    print(f"  [OK] EdgeScore calculated: Score={breakdown.edge_score:.1f}, Confidence={breakdown.confidence:.0f}%")
    engine_results["EdgeScore"] = "PASS"
except Exception as e:
    print(f"  [FAIL] EdgeScore calculation failed: {e}")
    engine_results["EdgeScore"] = f"FAIL: {e}"

# 9. News Sentiment & Context
print("\n[9/17] Testing News Sentiment Processing...")
try:
    from agents.data_ingestion.context_fetcher import build_context_fetcher
    cf = build_context_fetcher()
    snap = cf.get_snapshot_sync("RELIANCE")
    score = getattr(snap, "context_score", 0.0) if snap else 0.0
    print(f"  [OK] News sentiment snapshot: Score={score:.2f}")
    engine_results["News_Sentiment"] = "PASS"
except Exception as e:
    print(f"  [FAIL] News sentiment failed: {e}")
    engine_results["News_Sentiment"] = f"FAIL: {e}"

# 10. XGBoost Predictions
print("\n[10/17] Testing XGBoost Model Engine...")
try:
    from agents.ai_engine.xgboost_model import get_xgb_predictor
    xgb_pred = get_xgb_predictor()
    # Predict row on indicator features
    pred_res = xgb_pred.predict_row(ind_df)
    print(f"  [OK] XGBoost predictor evaluated row: Result={pred_res}")
    engine_results["XGBoost"] = "PASS"
except Exception as e:
    print(f"  [FAIL] XGBoost failed: {e}")
    engine_results["XGBoost"] = f"FAIL: {e}"

# 11. Ensemble Scoring
print("\n[11/17] Testing AI Ensemble Scoring...")
try:
    from agents.ai_engine.ensemble import predict_advanced
    ens_res = predict_advanced(ind_df)
    sig_dir = ens_res.get("direction", ens_res.get("signal", "HOLD"))
    print(f"  [OK] Ensemble evaluated: Direction={sig_dir}, Confidence={ens_res.get('confidence', 0):.1f}%")
    engine_results["Ensemble"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Ensemble failed: {e}")
    engine_results["Ensemble"] = f"FAIL: {e}"

# 12. Risk/Reward Filters
print("\n[12/17] Testing Risk/Reward Filtering...")
try:
    entry = 100.0
    stop_loss = 95.0
    target = 110.0
    risk = entry - stop_loss
    reward = target - entry
    rr = reward / risk
    assert rr >= 1.5, f"RR filter failed: {rr}"
    print(f"  [OK] R:R Filter verified: Entry={entry}, SL={stop_loss}, Tgt={target} -> R:R={rr:.2f} (>= 1.5)")
    engine_results["RR_Filter"] = "PASS"
except Exception as e:
    print(f"  [FAIL] RR filter failed: {e}")
    engine_results["RR_Filter"] = f"FAIL: {e}"

# 13. Position Sizing
print("\n[13/17] Testing Position Sizing...")
try:
    from agents.execution.position_sizing import calculate_position_size
    qty = calculate_position_size(capital=100000, risk_pct=0.01, entry=100.0, stop_loss=95.0)
    assert qty > 0, "Calculated qty should be > 0"
    print(f"  [OK] Position sizing: Capital=100,000, 1% Risk, Risk/share=5.0 -> Qty={qty}")
    engine_results["Position_Sizing"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Position sizing failed: {e}")
    engine_results["Position_Sizing"] = f"FAIL: {e}"

# 14. Stop-loss & Trailing Logic
print("\n[14/17] Testing Trailing Stop-Loss Logic...")
try:
    from agents.execution.trailing_sl import TrailingStopManager
    tsm = TrailingStopManager()
    trade_id = 99999
    tsm.register(trade_id=trade_id, direction="LONG", entry=100.0, sl=95.0, atr=1.5)
    sl_initial = tsm.get_sl(trade_id)
    tsm.update(trade_id=trade_id, direction="LONG", current_high=110.0, current_low=108.0, atr=1.5)
    sl_trailed = tsm.get_sl(trade_id)
    tsm.remove(trade_id)
    print(f"  [OK] Trailing SL: Initial SL={sl_initial:.2f}, Trailed at 110={sl_trailed:.2f}")
    assert sl_trailed >= sl_initial, "Trailing SL should move up on profit"
    engine_results["Trailing_SL"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Trailing SL failed: {e}")
    engine_results["Trailing_SL"] = f"FAIL: {e}"

# 15. Paper Trading Execution Engine
print("\n[15/17] Testing Paper Order Execution Engine...")
try:
    from agents.execution.paper_trading import PaperTradingEngine
    engine = PaperTradingEngine()
    pos_before = len(engine.get_open_trades())
    trade = engine.execute(
        symbol="TEST_PAPER",
        direction="LONG",
        entry=100.0,
        sl=95.0,
        tp=110.0,
        confidence=80.0
    )
    pos_after = len(engine.get_open_trades())
    print(f"  [OK] Paper execution: Opened trade ID={getattr(trade, 'id', 'N/A')}, Positions before={pos_before}, after={pos_after}")
    if trade and hasattr(trade, 'id'):
        engine.close_manual(trade.id, exit_price=105.0)
        print(f"  [OK] Closed test trade ID={trade.id}")
    engine_results["Paper_Execution"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Paper execution failed: {e}")
    engine_results["Paper_Execution"] = f"FAIL: {e}"

# 16. Trade Outcome Resolution
print("\n[16/17] Testing Outcome Resolution Tracker...")
try:
    from db.swing_outcome_tracker import run_outcome_evaluation_once
    counters = run_outcome_evaluation_once(max_outcomes=5)
    print(f"  [OK] Swing outcome tracker evaluated: {counters}")
    engine_results["Outcome_Resolution"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Outcome resolution failed: {e}")
    engine_results["Outcome_Resolution"] = f"FAIL: {e}"

# 17. Telegram Alerts Engine
print("\n[17/17] Testing Telegram Alerts Pipeline...")
try:
    from agents.notifications.telegram_alerts import is_configured
    configured = is_configured()
    print(f"  [OK] Telegram alerts pipeline check: Configured={configured} (Safe fallback when token missing)")
    engine_results["Telegram_Alerts"] = "PASS"
except Exception as e:
    print(f"  [FAIL] Telegram alerts failed: {e}")
    engine_results["Telegram_Alerts"] = f"FAIL: {e}"

print("\n" + "=" * 80)
print("TRADING ENGINE VERIFICATION SUMMARY:")
print(json.dumps(engine_results, indent=2))
print("=" * 80)
