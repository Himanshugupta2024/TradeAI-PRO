# TradeAI PRO — Data & Machine Learning Pipeline Reference (P5)

> **Document Status**: Complete & Verified (Audit Phase 5)  
> **Repository Context**: Multi-Asset Real-Time Market Intelligence & Quantitative Execution  
> **Operating Constraint**: Read-Only reverse-engineering audit. All claims backed by exact `path/to/file.py:LINE` citations.

---

## 1. Provider Chain per Asset Class

The data ingestion tier coordinates multi-provider routing, in-memory caching, disk/database persistence, and failover across Equities, Crypto, and Forex.

### 1.1 Architecture & Failover Order

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           CENTRAL DATA ROUTING TIER                             │
└─────────────────────────────────────────────────────────────────────────────────┘
                                       │
     ┌─────────────────────────────────┼────────────────────────────────┐
     ▼                                 ▼                                ▼
[NSE EQUITIES & INDICES]           [CRYPTO]                          [FOREX]
 1. Memory L1 Cache (<1ms)        1. CCXT Binance Spot             1. Memory Cache
    cache/memory.py:45               agents/.../binance_fetcher.py    forex_fetcher.py:31
 2. Shoonya WebSocket (Box C)     2. Binance Futures REST          2. Twelve Data API (Free)
    FEED_SERVICE_URL                 binance_fetcher.py:182           (800/d, 8/min rate limit)
    data/providers/shoonya_feed.py   (Meme/Perp fallback)             forex_fetcher.py:150
 3. TradingView India Scanner     3. Yahoo Finance Crypto          3. Keyless yFinance Fallback
    data/providers/tradingview.py    binance_fetcher.py:239           (EURUSD=X, USDINR=X)
 4. Yahoo Finance Chart API       4. Memory Cache / None              forex_fetcher.py:200
    data/providers/yahoo.py:183                                    4. None
 5. Stale L1 Cache Fallback
    cache/memory.py:74
 6. None
```

### 1.2 Asset Class Specifications

| Asset Class | Primary Provider | Fallback 1 | Fallback 2 | Cache Mechanism | Failure / Terminal State | Router Code Reference | Component Classification |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **NSE Equities (Quotes / Ticks)** | Shoonya WebSocket via Box C feed service (`ShoonyaFeedProvider`) | TradingView Scanner Vectorized POST (`TradingViewProvider`) | Yahoo Query1 Chart API (`YahooProvider`) | L1 `MemoryCache` (3s quote TTL, 30s stale grace) | Returns `None` | `data/router.py:36-79` (`MarketDataRouter.get_quote`) | **WIRED** & **DEPLOYED** (TV/Yahoo live; Shoonya active when `FEED_SERVICE_URL` set) |
| **NSE Equities (Batch OHLCV 5m / 1d)** | `ShoonyaFeedProvider` (`data/providers/shoonya_feed.py:135`) | `YahooProvider` (`data/providers/yahoo.py:284` query1 + yf.download) | Per-symbol fallback to `agents.market_intelligence.nse_fetcher::fetch_ohlcv` | `_htf_daily_cache` (6h TTL in `api/service.py:1724`) + persistent SQLite/Supabase `read_cached_ohlcv` (`db/ohlcv_cache.py:34`) | Returns empty `{}` or drops symbol | `data/router.py:166-194` (`get_batch_ohlcv`) & `api/service.py:1715-1749` | **WIRED** & **DEPLOYED** |
| **NSE Official Bhavcopy (EOD Archive)** | `https://nsearchives.nseindia.com` Public Archives (`agents/data_ingestion/nse_bhavcopy.py:75`) | Previous trading day archive fallback (`agents/data_ingestion/nse_bhavcopy.py:126`) | None | Local SQLite table `bhavcopy` in `db/bhavcopy.db` (`agents/data_ingestion/nse_bhavcopy.py:42`) | Raises `RuntimeError("No Bhavcopy available")` | `agents/data_ingestion/nse_bhavcopy.py:75-148` | **WIRED** & **DEPLOYED** |
| **Crypto (Spot & Memes)** | CCXT Binance Spot (`agents/data_ingestion/binance_fetcher.py:201`) | Binance Futures Public REST (`_fetch_binance_futures_fallback` at line 182) | Yahoo Crypto (`_fetch_yahoo_crypto_fallback` at line 239) | In-memory `_SPOT_MARKETS_CACHE` (1800s TTL at line 20) | Returns empty `pd.DataFrame()` | `agents/data_ingestion/binance_fetcher.py:159-240` | **WIRED** & **DEPLOYED** |
| **Forex (Majors & INR Pairs)** | Twelve Data REST API (`agents/data_ingestion/forex_fetcher.py:150`) | Keyless yFinance (`_fetch_ohlcv_yfinance` at line 200 via `_SYMBOL_YF_MAP`) | None | In-memory `_ohlcv_cache` (`forex_fetcher.py:31`) + JSON API usage budget tracker (`DAILY_API_USAGE_FILE`) | Returns empty `pd.DataFrame()` | `agents/data_ingestion/forex_fetcher.py:1-240` | **WIRED** & **DEPLOYED** |

---

## 2. Feature Engineering

The feature pipeline converts raw OHLCV bars into a 173-dimensional feature matrix. All tabular features are explicitly designed to be stationary and scale-free, allowing models trained on large-cap equities to generalize to mid-caps, indices, or crypto without retuning price anchors.

### 2.1 Complete Feature Inventory (173 Features)

| Feature Subset | Count | Feature Names | Stationarity / Transformation | Computing Module |
| :--- | :--- | :--- | :--- | :--- |
| **Base Momentum Oscillators** | 9 | `RSI_14`, `MACD`, `MACD_signal`, `MACD_hist`, `ADX_14`, `StochRSI_K`, `StochRSI_D`, `MFI_14`, `OBV_ROC5` | **Stationary**: Bounded oscillators [0, 100] or rate-of-change ratios. OBV is 5-bar percentage change (`OBV.pct_change(5)*100`), not cumulative raw volume. | `agents/feature_engine/indicators.py:227-287` & `agents/ai_engine/xgboost_model.py:59-69` |
| **Normalized Price-Relative** | 5 | `ATR_14_pct`, `BB_pct`, `ema20_pct`, `ema50_pct`, `ema200_pct` | **Stationary**: Dimensionless percentages: `ATR/close*100`, Bollinger Band position `(close - lower)/(upper - lower)`, EMA % distances `(close - EMA)/EMA*100`. | `agents/ai_engine/xgboost_model.py:70-75` & `indicators.py:242-269` |
| **Multi-Horizon Momentum** | 3 | `ret_5`, `ret_10`, `ret_20` | **Stationary**: Log/percentage returns over 5, 10, and 20 candles (`close.pct_change(N)*100`). | `agents/ai_engine/xgboost_model.py:76-79` |
| **Volume Dynamics** | 1 | `volume_ratio` | **Stationary**: Bar volume divided by 20-period moving average volume (`vol / vol_avg_20`). | `agents/ai_engine/xgboost_model.py:81` & `indicators.py:300` |
| **Market Regime & Context** | 1 | `htf_trend_enc` | **Stationary**: Discrete categorical encoding (-1: Bearish, 0: Neutral, 1: Bullish) derived from daily HTF trend alignment. | `agents/ai_engine/xgboost_model.py:83` & `agents/feature_engine/trend.py:65` |
| **Candle Geometry** | 3 | `body_ratio`, `upper_wick_pct`, `lower_wick_pct` | **Stationary**: Ratios bounded [0, 1] relative to the candle's total high-low span `(high - low)`. | `agents/ai_engine/xgboost_model.py:85-87` & `indicators.py:326-334` |
| **Ichimoku Cloud** | 5 | `ichimoku_cloud_pos`, `ichimoku_tk_cross`, `ichimoku_cloud_thick`, `ichimoku_vs_kijun`, `ichimoku_vs_tenkan` | **Stationary**: Normalized cloud position (-1, 0, 1), Tenkan/Kijun cross signal, cloud thickness as % of close, and percentage distance from Kijun/Tenkan lines. | `agents/feature_engine/advanced_features.py:36-103` & `agents/ai_engine/xgboost_model.py:92-97` |
| **Pivot & Structural Levels** | 4 | `pivot_pos`, `pivot_r1_dist`, `pivot_s1_dist`, `pivot_fib_zone` | **Stationary**: Discrete position relative to classic pivots and percentage distances `(R1 - close)/close*100`, `(S1 - close)/close*100`. | `agents/feature_engine/advanced_features.py:105-184` & `agents/ai_engine/xgboost_model.py:98-102` |
| **Advanced Volatility** | 4 | `vol_ratio`, `vol_zscore`, `vol_regime`, `realized_vol_20` | **Stationary**: Garman-Klass volatility, Parkinson volatility, 20-period realized volatility, and rolling volatility z-scores. | `agents/feature_engine/advanced_features.py:264-331` & `agents/ai_engine/xgboost_model.py:103-107` |
| **Price Action Dynamics** | 5 | `swing_high_dist`, `swing_low_dist`, `price_percentile`, `hh_streak`, `range_expansion` | **Stationary**: Percentage distance to 20-bar swing high/low, percentile of close within 50-bar rolling window, consecutive higher-high count, and range expansion ratio. | `agents/feature_engine/advanced_features.py:333-405` & `agents/ai_engine/xgboost_model.py:108-113` |
| **Heikin-Ashi Trend** | 3 | `ha_body_ratio`, `ha_direction`, `ha_trend_streak` | **Stationary**: Normalized Heikin-Ashi body ratio, direction (-1 or +1), and consecutive directional bar streak. | `agents/feature_engine/advanced_features.py:407-476` & `agents/ai_engine/xgboost_model.py:114-117` |
| **Temporal Cyclical Encoding** | 2 | `session`, `dow_sin` | **Stationary**: Integer session identifier (0: Pre, 1: Morning, 2: Midday, 3: Closing) and sine transformation of day of week `sin(2*pi*dow/5)`. | `agents/feature_engine/advanced_features.py:186-262` & `agents/ai_engine/xgboost_model.py:119-121` |
| **PyTorch LSTM Embeddings** | 128 | `lstm_h_0` through `lstm_h_127` | **Stationary**: Latent hidden representation from 2-layer LSTM trained on normalized 10-bar lookback sequences (`best_model.pt`). Defaulted to 0.0 when PyTorch disabled. | `agents/ai_engine/lstm_model.py:22` & `agents/ai_engine/xgboost_model.py:124-275` |

### 2.2 Stationary Feature Contract vs. Raw Price Leakage

- **Zero Raw Price Levels**: No raw absolute prices (`open`, `high`, `low`, `close` in rupees/dollars) or cumulative volume values are provided to tree models (`agents/ai_engine/xgboost_model.py:132-134`).
- **Feature Alignment & Sanitization**: Both `predict_row` and `predict_batch` in `xgboost_model.py:390-410` and `lightgbm_model.py:376-388` enforce strict column realignment via `.reindex(columns=self.feature_cols, fill_value=0.0)` and sanitize NaNs via `np.nan_to_num(X, nan=0.0)`. Passing raw 173-feature arrays to models fitted on 45 base+advanced features triggers automatic reindexing to prevent shape mismatch or silent fallback.

---

## 3. Label Generation & Transaction Cost Modeling

Training labels avoid naive binary "next candle is green" classification in favor of outcome-based Triple-Barrier labeling (Marcos López de Prado).

### 3.1 Triple-Barrier Parameters & Logic

Defined in `agents/feature_engine/label_generator.py:192-339` (`triple_barrier_labels`):

```
       ▲ Price
       │                                     Upper Barrier (Target / TP)
 ──────┼───────────────────────────────────  entry + (ATR * sl_atr_mult * rr_ratio)
       │              ▲
       │             / \   ▲
       │            /   \ / \   WIN (+1.0)
 Entry ┼───────────●     ▼   ●──────────────
       │          /
       │         /                           Lower Barrier (Stop / SL)
 ──────┼────────●──────────────────────────  entry - (ATR * sl_atr_mult)
       │        LOSS (-1.0)
       │
       └───────────────────────────────────► Time (Bars)
                │◄────── max_bars ────────►│
                     (Vertical Barrier: 0.0)
```

1. **Upper Barrier (Profit Target)**:
   $$\text{Target} = \text{Entry} \pm (\text{ATR} \times \text{sl\_atr\_mult} \times \text{rr\_ratio})$$
   - Default Risk:Reward ratio `rr_ratio = 2.0` (`label_generator.py:197`).
   - Default stop multiplier `sl_atr_mult = 1.0` (`label_generator.py:196`).
2. **Lower Barrier (Stop Loss)**:
   $$\text{Stop} = \text{Entry} \mp (\text{ATR} \times \text{sl\_atr\_mult})$$
3. **Vertical Barrier (Holding Timeout)**:
   - Evaluated across `max_bars = 10` bars forward (`label_generator.py:195`).
   - In 1R:2R outcome labeling (`label_1r2r_outcome` at `label_generator.py:32`), default lookahead is `forward_bars = 5`.
4. **Conservative Same-Bar Conflict Arbitration**:
   - If a single bar touches both the Upper and Lower barriers (e.g. wide candle / high volatility), **the stop loss takes precedence**:
     ```python
     # agents/feature_engine/label_generator.py:304-310
     if stop_hit:
         label, barrier, bars, exit_price = -1.0, "stop", k, stop
         break
     if target_hit:
         label, barrier, bars, exit_price = 1.0, "target", k, target
         break
     ```
   - Same-bar touches are marked as `LOSS (-1.0)` to guarantee pessimistic backtesting and training.
5. **Timeout Handling**:
   - If neither barrier is touched before `max_bars` expires, the position is closed at the vertical barrier (`exit_price = close[i + max_bars]`).
   - Label is set to `0.0` (TIMEOUT / VERTICAL) (`label_generator.py:292`).
   - When training binary classifiers, `prepare_trade_outcome_targets` treats timeouts as losses (`label == 0 -> 0`) to penalize capital-stagnant trades (`agents/ai_engine/time_split.py:166`).

### 3.2 Indian Market Cost Simulation

Defined in `agents/ai_engine/backtest_validator.py:68-112` (`calculate_indian_transaction_costs`):

```python
# Statutory and exchange friction schedule
buy_val = entry_price * shares
sell_val = exit_price * shares
turnover = buy_val + sell_val

brokerage_buy  = min(20.0, buy_val * 0.0003)    # min(₹20, 0.03% turnover)
brokerage_sell = min(20.0, sell_val * 0.0003)   # min(₹20, 0.03% turnover)
total_brokerage = round(brokerage_buy + brokerage_sell, 2)

# STT: 0.1% on delivery (both sides) or 0.025% on intraday (sell side)
stt = round(turnover * 0.001, 2) if is_delivery else round(sell_val * 0.00025, 2)

exchange_charges = round(turnover * 0.0000345, 2)  # NSE equity: 0.00345%
gst = round((total_brokerage + exchange_charges) * 0.18, 2)  # GST: 18%
stamp_duty = round(buy_val * 0.00015, 2)            # Stamp duty: 0.015% on buy
slippage = round(turnover * slippage_pct, 2)        # Slippage: 0.05% default
```

In automated training (`scripts/train_and_publish.py:87`), a flat `round_trip_cost_pct = 0.0025` (0.25% cost drag) is added to the target distance hurdle to ensure positive net edge after friction.

---

## 4. Model Training & Anti-Leakage Protocol

Training runs externally (GitHub Actions `.github/workflows/model_train_scheduled.yml`, Kaggle GPU, or Google Colab) to preserve Render Free tier resources.

### 4.1 Chronological 4-Way Split with Embargo

Defined in `agents/ai_engine/time_split.py:101-160` (`chronological_four_way_split`):

```
┌─────────────────┬───────────────────┬─────────────────┬─────────────────┐
│   SPLIT 1 (50%) │   SPLIT 2 (25%)   │ SPLIT 3 (12.5%) │ SPLIT 4 (12.5%) │
│ Base Train Data │ Meta-Learner(OOF) │   Calibration   │ Blind Test Data │
└─────────────────┴───────────────────┴─────────────────┴─────────────────┘
                  ▲                   ▲                 ▲
                  │                   │                 │
            embargo_bars        embargo_bars      embargo_bars
               (>= 5)              (>= 5)            (>= 5)
```

- **Anti-Leakage Contract**: `max(split_i_indices) + embargo_bars < min(split_i+1_indices)`.
- Eliminates overlapping multi-candle lookahead labels between training, meta-learning, calibration, and test partitions (`agents/ai_engine/time_split.py:36`).

### 4.2 Scaler Fit Isolation

Defined in `agents/ai_engine/trainer.py:214-265` (`fit_scaler`):

- **Train-Only Fitting**: Scaler parameters ($\mu, \sigma$) are fitted **strictly on the first 80% (train partition)** of the feature matrix (`_resolve_fit_end` at line 193). Fitting across the full series is prohibited by contract (`trainer.py:224-227`).
- **Artifact Path**: Persisted to `models/scaler_params.json` (`trainer.py:294`).
- **Version Tag**: Marked with `FEATURE_SET_VERSION = "scale_free_v1"` (`trainer.py:35`). Mismatched feature counts or outdated versions cause `load_persisted_scaler` to reject the artifact (`trainer.py:318-340`).

### 4.3 Database Model Bundle Publishing

Defined in `api/model_sync.py:33-145` and `scripts/train_and_publish.py:220-230`:

- **Supabase Persistence Key**: `runtime_state` table under `key = "model_bundle:v1"` (`api/model_sync.py:33`).
- **Packaging**: Files compressed via gzip (level 9) and base64 encoded (`api/model_sync.py:65`).
- **Whitelisted Artifacts**: Only allowed filenames are accepted: `xgb_model.pkl`, `lgbm_model.pkl`, `logistic_model.pkl`, `logistic_scaler.pkl`, `meta_learner.pkl`, `calibrator.pkl`, `scaler_params.json`, `model_meta.json`, `best_model.pt` (`api/model_sync.py:39-50`).
- **Safety Gates**:
  - Max single file size: 8 MB (`MAX_FILE_BYTES = 8 * 1024 * 1024` at `api/model_sync.py:52`).
  - Max total bundle size: 12 MB (`MAX_TOTAL_BYTES = 12 * 1024 * 1024` at `api/model_sync.py:53`).
  - Accuracy Gate: Validation accuracy must be $\ge 0.50$ (`MIN_VAL_ACCURACY = 0.50` at `api/model_sync.py:54`).

---

## 5. Model Serving & Sidecar Routing Architecture

Serving dynamically branches depending on the deployment role (`trading-api` Box A vs `trading-ml` Box B) to stay strictly within the Render 512MB RAM cap.

### 5.1 Serving Topology & Routing Conditions

```
                                [Inference Request]
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │ agents/ai_engine/ensemble.py:384      │
                     │ def predict_advanced(...)             │
                     └───────────────────────────────────────┘
                                         │
           ┌─────────────────────────────┴─────────────────────────────┐
           ▼                                                           ▼
[Box B: Dedicated ML Sidecar]                               [Box A: Core Gateway / Web]
os.getenv("LIGHTWEIGHT_API_MODE") == "false"                os.getenv("LIGHTWEIGHT_API_MODE") == "true"
           │                                                           │
           ▼                                                           ▼
Load ModelRegistry:                                         Check Remote ML Sidecar:
- XGBoost (xgb_model.pkl)                                   api/ml_client.py:70 infer_remote_sync()
- LightGBM (lgbm_model.pkl)                                            │
- Logistic (logistic_model.pkl)                       ┌────────────────┴────────────────┐
- MetaLearner (meta_learner.pkl)                      ▼                                 ▼
- Platt Calibrator (calibrator.pkl)           [Sidecar Responded 200]           [Sidecar Unreachable / Error]
           │                                          │                                 │
           ▼                                          ▼                                 ▼
Execute Model Ensemble & Return               Return Sidecar Inference          Execute Statistical Fallback:
source: "LOCAL_ENSEMBLE"                      source: "ML_SIDECAR"              - fallback_predictor.py:413
                                                                                - anomaly_detector.py:445
                                                                                source: "FALLBACK_STATISTICAL"
```

### 5.2 Exact Code Branch Conditions

In `agents/ai_engine/ensemble.py:418-460`:

```python
# 1. Check if lightweight API mode or ML Sidecar is configured
if _is_lightweight_mode() or bool(os.environ.get("ML_SIDECAR_URL", "").strip()):
    ml_sidecar_url = os.environ.get("ML_SIDECAR_URL", "").strip()
    
    # 2. Remote ML Sidecar Delegation (Box A -> Box B)
    if ml_sidecar_url and os.environ.get("SERVICE_ROLE") != "ml_sidecar":
        try:
            from api.ml_client import infer_remote_sync
            sidecar_pred = infer_remote_sync(symbol=symbol, df=df)
            if sidecar_pred and isinstance(sidecar_pred, dict) and "confidence" in sidecar_pred:
                sidecar_pred["source"] = "ML_SIDECAR"
                return sidecar_pred
        except Exception as sc_err:
            log.debug("[Ensemble] Remote ML sidecar fallback: %s", sc_err)

    # 3. Statistical Fallback (Render 512MB RAM cap safety)
    # Triggered when sidecar is disabled or unreachable
    res = {
        "direction": direction,
        "confidence": round(conf, 1),
        "prob_up": prob_up,
        "prob_down": prob_down,
        "model_contributions": {
            "statistical": 1.0,
            "xgboost": 0.0,
            "lightgbm": 0.0,
            "lstm": 0.0,
            "logistic": 0.0,
        },
    }
    return res
```

### 5.3 Dynamic Ensemble Weights

When running full local ML (Box B or offline backtest), weights dynamically adjust based on recent 20-trade accuracy (`agents/ai_engine/ensemble.py:90-171`):
- Base prior: `statistical: 0.15`, `xgboost: 0.35`, `lightgbm: 0.35`, `logistic: 0.15`.
- Dynamic multiplier: Recent accuracy $< 40\% \rightarrow 0.50\times$, $> 65\% \rightarrow 1.25\times$, $> 75\% \rightarrow 1.50\times$ (`ensemble.py:146-168`).
- Min/Max guardrails: No model drops below 5% or exceeds 60% total weight (`ensemble.py:192-225`).

---

## 6. Calibration & Drift Detection

Raw ensemble confidence scores are uncalibrated heuristics. Probability calibration maps model outputs to empirical win probabilities so that Kelly sizing and risk controls reflect real expectancy.

### 6.1 Platt Scaling (Sigmoid) Calibrator

Defined in `agents/ai_engine/calibrator.py:35-125` (`ProbabilityCalibrator`):

- **Method**: Sigmoid / Platt scaling via `sklearn.calibration.CalibratedClassifierCV(FrozenEstimator(base_estimator), method="sigmoid")` (`calibrator.py:77`).
- **Holdout Fit**: Fitted on partition 3 of `chronological_four_way_split` (`time_split.py:111`), completely isolated from both base model training and meta-learner training.
- **Artifact Path**: `models/calibrator.pkl` (`calibrator.py:32`).
- **Validation Metrics**:
  - Brier Score: $\text{BS} = \frac{1}{N} \sum_{i=1}^N (p_i - y_i)^2$ (Benchmark target $< 0.22$, `calibrator.py:11`).
  - Base-rate Brier: $\text{BS}_{\text{ref}} = \bar{y}(1 - \bar{y})$ (`calibrator.py:346`).
  - Brier Skill Score: $\text{BSS} = 1 - \frac{\text{BS}}{\text{BS}_{\text{ref}}}$ (`calibrator.py:347`).
  - Expected Calibration Error (ECE): Evaluated across 5 probability bins [0.0, 0.2, 0.4, 0.6, 0.8, 1.0] (`calibrator.py:351-360`).

### 6.2 Live Calibration Drift Detection

Defined in `agents/ai_engine/calibrator.py:258-382` (`check_live_calibration_drift`):

- **Input Field Extraction**:
  - Evaluates closed trades from `trade_outcomes_pg`.
  - Strictly extracts `p_calibrated` (0.0 to 1.0) or `calibrated_trade_win_probability` (0 to 100) (`calibrator.py:240-255`).
  - **Rejects `confidence`**: The heuristic `confidence` field is explicitly skipped because it is an uncalibrated 0–100 heuristic score (`calibrator.py:232-236`).
- **Trigger Conditions**:
  - Sample size threshold: Minimum 20 closed trades (`min_sample_size = 20` at `calibrator.py:261`).
  - ECE Drift Threshold: $\text{ECE} > 0.10$ (10% calibration gap) (`calibrator.py:260`).
- **Action & Recommendation**:
  - When $\text{ECE} > 0.10$: Status transitions to `"DRIFT_DETECTED"` and recommendation outputs:  
    `"TRIGGER_RETRAIN: Intra-week probability drift detected (>10%). Platt calibrator needs re-fitting."` (`calibrator.py:364`).
  - When $\text{ECE} \le 0.10$: Status is `"CALIBRATION_HEALTHY"` (`calibrator.py:368`).

---

## 7. Self-Learning Feedback Loop

The platform employs a Bayesian self-learning feedback loop that continuously tunes signal weights and regime-specific multipliers based on actual closed paper trade outcomes.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        SELF-LEARNING ARCHITECTURE                      │
└────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                       [Closed Paper Trade Event]
                                    │
                                    ▼
                  agents/analytics/feedback_loop.py:196
                  TradeOutcomeLogger.log_closed_trade()
                                    │
                                    ▼
                   Writes to: PostgreSQL / Supabase
                   Table: trade_outcomes_pg (db/models.py:553)
                                    │
       ┌────────────────────────────┴────────────────────────────┐
       │ (Every 30 trades inline)                                │ (Daily at 15:45 IST)
       ▼                                                         ▼
FeedbackAnalyser.run()                               api/learning_scheduler.py:47
- Queries trade_outcomes_pg                          should_run_review()
- Evaluates PAPER_FORWARD only                       - AutonomousDaemon.check_daily_eod_review()
- Computes win rates by signal & regime                          │
       │                                                         │
       └────────────────────────────┬────────────────────────────┘
                                    │
                                    ▼
                  agents/analytics/feedback_loop.py:611
                  WeightAdapter.update_weights()
                  - Bayesian Shrinkage toward 50% prior
                  - Step-size cap: max 0.15 per cycle
                                    │
       ┌────────────────────────────┴────────────────────────────┐
       ▼                                                         ▼
Supabase runtime_state Table                              Local JSON Sidecar
- learned_weights:v1                                      config/learned_weights.json
- learned_weights_2d:v1                                   (atomic_write_json)
```

### 7.1 Feedback Components & Database Tables

| Component | Class / Function | file:line | Tables Read | Tables / Paths Written | Trigger Condition |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Trade Outcome Logger** | `TradeOutcomeLogger.log_closed_trade` | `agents/analytics/feedback_loop.py:196` | None | `trade_outcomes_pg` (`db/models.py:553`) | Triggered on every closed paper trade. Enforces unique constraint on `trade_id` (`feedback_loop.py:170`). |
| **Feedback Analyser** | `FeedbackAnalyser.run` | `agents/analytics/feedback_loop.py:359` | `trade_outcomes_pg` (`TradeOutcome.environment == 'PAPER_FORWARD'`) | None (In-memory insights dict) | Queries last 500 closed paper trades, computes win rate, profit factor, and expectancy by signal type and regime (`feedback_loop.py:375`). |
| **Weight Adapter** | `WeightAdapter.update_weights` | `agents/analytics/feedback_loop.py:611` | `runtime_state` key `learned_weights:v1` or `config/learned_weights.json` | `runtime_state` keys `learned_weights:v1` & `learned_weights_2d:v1` (`feedback_loop.py:765`), plus `config/learned_weights.json` (`feedback_loop.py:778`) | Inline after every 30 closed trades (`ANALYSE_EVERY_N_TRADES = 30` at line 44) or via daily EOD review. |
| **EOD Learning Supervisor** | `run_review_scheduler_loop` | `api/learning_scheduler.py:92` | `runtime_state` key `eod_learning_review:v1` | `runtime_state` key `eod_learning_review:v1` | Async background loop running every 60s. Fires once per trading day between 15:45 and 16:30 IST (`api/learning_scheduler.py:36`). |
| **Daily Scorecard** | `generate_daily_scorecard` | `db/daily_scorecard.py:108` | `trade_outcomes_pg` | `daily_scorecards` table (`db/models.py:590`) | Triggered during EOD review; writes daily metrics (trades, win rate, PnL, best/worst signal). |

### 7.2 Bayesian Shrinkage & Adaptation Guardrails

Defined in `agents/analytics/feedback_loop.py:615-695`:

1. **Cold Start Gate**:
   - If total closed paper trades $< 30$, weights remain strictly 1.0 (`COLD_START`). No weight modification occurs (`feedback_loop.py:636-653`).
2. **Dynamic Adaptation Bounds**:
   - Sample $30 - 99$ trades: conservative bounds $[0.75, 1.25]$.
   - Sample $100 - 249$ trades: expanded bounds $[0.50, 1.75]$.
   - Sample $250+$ trades: mature bounds $[0.30, 2.50]$ (`feedback_loop.py:655-660`).
3. **Bayesian Shrinkage toward 50% Prior**:
   $$\text{ShrunkWR} = \frac{(\text{RealizedWR} \times \text{SampleStrength}) + (50.0 \times \text{PriorStrength})}{\text{SampleStrength} + \text{PriorStrength}}$$
   Where `WEIGHT_PRIOR_STRENGTH = 12.0` (`feedback_loop.py:48`).
4. **Step Size Cap**:
   - Maximum weight change per adaptation cycle is strictly clamped to $\pm 0.15$ (`WEIGHT_MAX_STEP_PER_UPDATE = 0.15` at line 689).
5. **2D Regime Adaptation**:
   - Updates the full 2D cross-product matrix (4 regimes $\times$ 6 signal types) in `learned_weights_2d:v1`, boosting signal weights that demonstrate positive mathematical expectancy in specific market regimes (`feedback_loop.py:697-741`).

---

## 8. Data & ML Component Verification Summary

| Component | file:line | Classification | Verification Status | Operational Notes |
| :--- | :--- | :--- | :--- | :--- |
| `MarketDataRouter` | `data/router.py:19` | **WIRED & DEPLOYED** | Verified in `api/service.py:1718` | Primary quote & candle coordinator with multi-provider failover. |
| `ShoonyaFeedProvider` | `data/providers/shoonya_feed.py:20` | **WIRED & DEPLOYED** | Verified in `data/router.py:236` | Activated when `FEED_SERVICE_URL` is set; connects to Box C. |
| `TradingViewProvider` | `data/providers/tradingview.py:28` | **WIRED & DEPLOYED** | Verified in `data/router.py:238` | Quote-only scanner provider; candles return empty list by design. |
| `YahooProvider` | `data/providers/yahoo.py:32` | **WIRED & DEPLOYED** | Verified in `data/router.py:239` | Primary OHLCV provider for Box A batch download. |
| `NSEBhavcopyFetcher` | `agents/data_ingestion/nse_bhavcopy.py:75` | **WIRED & DEPLOYED** | Verified in `scripts/nightly_swing_scan.py:45` | Downloads authoritative EOD Bhavcopy from NSE public archives. |
| `BinanceFetcher` | `agents/data_ingestion/binance_fetcher.py:159` | **WIRED & DEPLOYED** | Verified in `api/service.py:1772` | CCXT spot with futures REST fallback and Yahoo crypto fallback. |
| `ForexFetcher` | `agents/data_ingestion/forex_fetcher.py:150` | **WIRED & DEPLOYED** | Verified in `agents/market_intelligence/forex_scanner.py:42` | Twelve Data free tier with 8s gap and keyless yFinance fallback. |
| `FeatureExtractor` | `agents/ai_engine/xgboost_model.py:128` | **WIRED & DEPLOYED** | Verified in `scripts/train_and_publish.py:52` | Extracts 173 total features; strictly scale-free & stationary. |
| `TripleBarrierLabels` | `agents/feature_engine/label_generator.py:192` | **WIRED & DEPLOYED** | Verified in `agents/ai_engine/trainer.py:150` | 1R:2R triple-barrier labeling with conservative same-bar stop arbitration. |
| `TimeSeriesSplitWithEmbargo` | `agents/ai_engine/time_split.py:27` | **WIRED & DEPLOYED** | Verified in `scripts/train_and_publish.py:123` | Purged & embargoed time-series CV; prevents lookahead leakage. |
| `FitScaler` | `agents/ai_engine/trainer.py:214` | **WIRED & DEPLOYED** | Verified in `scripts/train_and_publish.py:118` | Fits mean/std on 80% train partition only; saves to `models/scaler_params.json`. |
| `ModelBundleSync` | `api/model_sync.py:73` | **WIRED & DEPLOYED** | Verified in `scripts/train_and_publish.py:221` | Base64-gzip bundle synced to Supabase `model_bundle:v1`. |
| `ModelRegistry` | `agents/ai_engine/model_registry.py:18` | **WIRED & DEPLOYED** | Verified in `ml_server.py:33` | Singleton warming up all predictors off the main thread. |
| `EnsemblePredictor` | `agents/ai_engine/ensemble.py:384` | **WIRED & DEPLOYED** | Verified in `ml_server.py:131` | Full ensemble with dynamic weights and remote sidecar delegation. |
| `MLSidecarClient` | `api/ml_client.py:70` | **WIRED & DEPLOYED** | Verified in `agents/ai_engine/ensemble.py:423` | Sync/async HTTP client with connection pooling and 6s timeout. |
| `ProbabilityCalibrator` | `agents/ai_engine/calibrator.py:35` | **WIRED & DEPLOYED** | Verified in `agents/ai_engine/model_registry.py:109` | Platt scaling calibrator yielding empirical win probabilities. |
| `CalibrationDriftDetector` | `agents/ai_engine/calibrator.py:258` | **WIRED & DEPLOYED** | Verified in `api/routers/admin.py:820` | ECE drift detection over closed trade outcomes (rejects heuristic confidence). |
| `TradeOutcomeLogger` | `agents/analytics/feedback_loop.py:168` | **WIRED & DEPLOYED** | Verified in `agents/execution/paper_trading.py:840` | Logs closed trade outcomes to `trade_outcomes_pg`. |
| `FeedbackAnalyser` | `agents/analytics/feedback_loop.py:359` | **WIRED & DEPLOYED** | Verified in `agents/analytics/feedback_loop.py:623` | Evaluates win rates & expectancy on `PAPER_FORWARD` trades. |
| `WeightAdapter` | `agents/analytics/feedback_loop.py:611` | **WIRED & DEPLOYED** | Verified in `api/learning_scheduler.py:89` | Bayesian weight shrinkage written to `learned_weights:v1`. |
| `LearningScheduler` | `api/learning_scheduler.py:92` | **WIRED & DEPLOYED** | Verified in `api/app.py:175` | In-process asyncio task running daily EOD review at 15:45 IST. |
