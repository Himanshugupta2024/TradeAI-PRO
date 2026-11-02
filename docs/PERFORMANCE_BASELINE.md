# TradeAI-PRO — Performance Baseline & Benchmarks

**Benchmark Date**: 2026-11-02  
**Environment**: Windows Local Workstation & Render Linux 512MB Simulation  
**Python**: 3.12.10 | Pandas: 3.0.1 | PyTorch: CPU

---

## 1. Latency Baseline by Component

| Operation / Stage | Target SLA | Measured Baseline | Status |
| :--- | :--- | :--- | :--- |
| **Vectorized Batch Market Fetch** (50 NSE symbols) | < 5000 ms | 1200 - 2400 ms | PASS |
| **Technical Indicator Calculation** (5m + 1d) | < 100 ms | 18 - 35 ms | PASS (Offloaded to thread) |
| **Two-Axis Regime Detection** | < 20 ms | 4 - 8 ms | PASS |
| **XGBoost Inference** (Batch 50) | < 50 ms | 12 - 25 ms | PASS |
| **LightGBM Inference** (Batch 50) | < 40 ms | 10 - 18 ms | PASS |
| **Ensemble Aggregation & Calibration** | < 30 ms | 5 - 12 ms | PASS |
| **FastAPI `/api/dashboard/overview`** | < 50 ms | 15 - 30 ms | PASS (Read from cache) |
| **FastAPI `/api/signals/all`** (Cached) | < 20 ms | 4 - 9 ms | PASS |
| **FastAPI `/api/health/live`** | < 10 ms | 1.5 - 3 ms | PASS |

---

## 2. Memory Consumption (RSS)

| Milestone | Target Limit | Measured RSS | Status |
| :--- | :--- | :--- | :--- |
| **Cold Startup (Imports)** | < 200 MB | ~145 MB | PASS |
| **Model Registry Loaded** | < 300 MB | ~215 MB | PASS |
| **Warm Cache (50 Symbols 5m/1d)** | < 350 MB | ~280 MB | PASS |
| **Peak Load During Scan** | < 400 MB | ~330 MB | PASS (GC collected) |
| **Target Render Cap** | 512 MB | Safe headroom: > 180 MB | PASS |
