# Stock Market Predictor — Best Results Improvement Plan

## Phase 7: Advanced ML Prediction Improvements

Status: several building blocks exist in-repo; optional deps (e.g. Optuna, SHAP) are not yet required in `requirements.txt`.

### Steps:

- [x] Step 1: `agents/feature_engine/advanced_features.py` — Ichimoku, pivots, time features, volatility, Heikin-Ashi
- [ ] Step 2: Modify `agents/feature_engine/indicators.py` — integrate advanced features (verify wiring vs current pipeline)
- [ ] Step 3: Expand `agents/ai_engine/xgboost_model.py` — 22 → 45+ features + Optuna tuning
- [x] Step 4: `agents/ai_engine/lightgbm_model.py` — LightGBM predictor module present
- [ ] Step 5: Modify `agents/ai_engine/ensemble.py` — add LightGBM + dynamic weights (confirm production defaults)
- [x] Step 6: `train_models.py` — unified training script present
- [ ] Step 7: Update `requirements.txt` — add lightgbm, optuna, shap when those paths are active in training

## Previous Phases (Completed):

- [x] Phase 1: Signal bias fix (penalty-only scoring)
- [x] Phase 2: Threshold tuning
- [x] Phase 3: Screener cache fallback + tradeflow timeout
- [x] Phase 4: Frontend retry states
- [x] Phase 5: Progressive startup (non-blocking)
- [x] Phase 6: Market Tab Fixes (Gainers/Losers + Sector Heatmap)
