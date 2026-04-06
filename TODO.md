# Stock Market Predictor — Best Results Improvement Plan

## Phase 7: Advanced ML Prediction Improvements

### Steps:
- [ ] Step 1: Create `agents/feature_engine/advanced_features.py` — Ichimoku, Pivot Points, Time features, Volatility, Heikin-Ashi
- [ ] Step 2: Modify `agents/feature_engine/indicators.py` — integrate advanced features
- [ ] Step 3: Expand `agents/ai_engine/xgboost_model.py` — 22 → 45+ features + Optuna tuning
- [ ] Step 4: Create `agents/ai_engine/lightgbm_model.py` — LightGBM predictor
- [ ] Step 5: Modify `agents/ai_engine/ensemble.py` — add LightGBM + dynamic weights
- [ ] Step 6: Create `train_models.py` — unified training script
- [ ] Step 7: Update `requirements.txt` — add lightgbm, optuna, shap

## Previous Phases (Completed):
- [x] Phase 1: Signal bias fix (penalty-only scoring)
- [x] Phase 2: Threshold tuning
- [x] Phase 3: Screener cache fallback + tradeflow timeout
- [x] Phase 4: Frontend retry states
- [x] Phase 5: Progressive startup (non-blocking)
- [x] Phase 6: Market Tab Fixes (Gainers/Losers + Sector Heatmap)
