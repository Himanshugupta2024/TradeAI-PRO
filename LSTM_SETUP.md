# LSTM Setup Guide

## Why a Separate Environment?

PyTorch requires Python <= 3.12. This project uses Python 3.14 for the main server.
The solution: a dedicated `venv_torch` virtual environment with Python 3.12 for LSTM training.

## One-Time Setup (Already Done)

```powershell
# Python 3.12 install + venv creation + PyTorch install — already completed
# venv_torch exists at E:\trading\venv_torch\
```

## Training LSTM

```powershell
# Activate the PyTorch environment
cd E:\trading
.\venv_torch\Scripts\activate

# Train on BTC/USDT 1h data
python train_lstm.py

# Train on all crypto symbols
python train_lstm.py --all-symbols

# Custom settings
python train_lstm.py --symbol ETH/USDT --timeframe 4h --epochs 100
```

## Model Files

| File | Description |
|------|-------------|
| `models/best_model.pt` | Trained LSTM weights |
| `models/model_meta.json` | Training metadata (accuracy, epochs) |
| `models/xgb_model.pkl` | XGBoost model |
| `models/lgbm_model.pkl` | LightGBM model |

## Running the Full Ensemble

The server (Python 3.14) uses XGBoost + LightGBM + Statistical + Sentiment.
For LSTM in predictions, run from venv_torch:

```powershell
.\venv_torch\Scripts\activate
python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/ensemble/status` | Model status + Fear-Greed index |
| `GET /api/ensemble/validate?symbol=BTC/USDT&n_folds=5` | Walk-forward accuracy |
| `POST /api/retrain?symbols=BTC/USDT,ETH/USDT&timeframe=4h` | Retrain XGBoost + LightGBM |
| `GET /api/retrain/status` | Check retrain job progress |

## Current Model Performance

| Model | Val Accuracy | Training Data |
|-------|-------------|---------------|
| XGBoost | 55.62% | BTC+ETH+SOL+BNB 4H 730 days |
| LightGBM | 52.96% | BTC+ETH+SOL+BNB 4H 730 days |
| LSTM | 50.9% | BTC/USDT 1H 365 days |
| Statistical | N/A (rule-based) | — |
