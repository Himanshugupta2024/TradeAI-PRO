# TradeAI-PRO — Known Issues & Technical Debt Tracker

## 1. Upstream & Dependency Quirks
- **yFinance / Pandas 3.x Compatibility**:
  - `yfinance` 1.2.0 issues deprecation warnings with `pandas` 3.x regarding `inplace` and `copy_on_write`.
  - *Mitigation*: Narrowly targeted warning filters registered in `api/app.py` without masking critical runtime exceptions.
- **Render Free Tier Ephemeral Disks**:
  - Free web services lose SQLite data across cold restarts unless synced.
  - *Mitigation*: `RuntimeState` abstracts persistence with PostgreSQL/Supabase backing and atomic local SQLite mirror.

## 2. Market Ingestion Rate Limits
- **Twelve Data (Forex)**: Free tier has 8 requests/minute credit limit.
  - *Mitigation*: Bounded batching, 300s cache TTL, and strict paper-trading framing.
- **Yahoo Finance**: Occasional 429 rate limit during high-frequency parallel requests.
  - *Mitigation*: `MarketDataRouter` with single-flight locking, semaphore batching (max 5 concurrent calls), and fallback to cached data.

## 3. Recently Resolved Critical Bugs & Regressions
- **Self-Learning Bankroll Static ₹1,00,000 Display**:
  - *Root Cause*: `PaperTradingEngine` only tracked realized cash; open positions' floating mark-to-market (MTM) was omitted from `statCapital`. The UI hero tile lacked sub-labels and interactive controls.
  - *Fix*: Implemented `get_portfolio_equity()` computing live `total_equity = cash + unrealized_pnl`, added endpoints `/api/paper/capital` and `/api/paper/reset`, and created interactive Self-Learning modal with custom seed adjustment and instant scan triggers.
- **XGBoost & LightGBM Feature Slicing Mismatch (45 vs 173)**:
  - *Root Cause*: Adding 128 LSTM embedding columns caused tabular models trained on 45 base features to fail runtime prediction with shape mismatch.
  - *Fix*: Guardrailed `predict_row()` in both models to slice `features[self.feature_cols]` matching trained input dimensions.
- **AI Ensemble Stuck at 3/4 ML (Uncalibrated)**:
  - *Root Cause*: Logistic baseline, Sigmoid Platt Calibrator, and Stacking Meta-Learner lacked serialized production models.
  - *Fix*: Trained Logistic Baseline on 11,839 real candles, calibrated Sigmoid classifier, fitted Meta-Learner with interaction terms, upgrading status to `🧠 Ensemble 4/4 • Calibrated`.
- **Dynamic Weight Adaptation Disconnected from History**:
  - *Root Cause*: `_ModelPerformanceTracker` started with empty in-memory deques on reboot.
  - *Fix*: Restored rolling outcome window from SQLite `trade_outcomes` (519 real closed paper trades), enabling persistent adaptive weighting.
