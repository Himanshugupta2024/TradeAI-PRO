# Risk Gauge Implementation Summary

## Overview

Implemented `risk_gauge.py` as part of Task 5.2 of the Edge Engine spec. This module measures the global market risk-on/risk-off environment using 5 key indicators.

## Components Implemented

### 1. RiskIndicators Dataclass
- Stores all risk indicator data
- Fields: VIX, VIX change %, BTC trend, Nifty trend, USD/JPY level/trend, Gold trend, risk regime
- JSON serialization/deserialization support

### 2. RiskGauge Class
- Main class for fetching and scoring risk indicators
- Configurable cache TTL (default: 1 hour)
- Fallback to stale cache on API failure

### 3. Data Sources (All Free)
- **VIX**: Yahoo Finance (^VIX) - volatility index
- **BTC**: Yahoo Finance (BTC-USD) - crypto risk appetite
- **Nifty 50**: Yahoo Finance (^NSEI) - Indian equity market
- **USD/JPY**: Twelve Data API (with Yahoo Finance fallback) - safe-haven flows
- **Gold**: Yahoo Finance (GC=F) - traditional safe-haven

## Key Methods

### `get_risk_score() -> float`
Calculates overall risk score from -100 (risk-off) to +100 (risk-on):
- VIX < 15 = risk-on, VIX > 30 = risk-off
- BTC/Nifty rising = risk-on
- USD/JPY/Gold rising = risk-off (inverted)
- Requires minimum 3 of 5 indicators

### `fetch_risk_indicators() -> RiskIndicators`
Fetches latest data from all sources:
- Uses cache if fresh (< 1 hour old)
- Falls back to stale cache on API failure
- Calculates risk regime (RISK_ON/RISK_OFF/NEUTRAL)

### `calculate_trend(prices: list[float]) -> str`
Calculates trend from price series:
- Uses 7-day moving average
- Returns "RISING" if current > MA by >2%
- Returns "FALLING" if current < MA by >2%
- Returns "FLAT" otherwise

## Risk Regime Logic

Risk regime determined by majority vote (>60% agreement):
- **RISK_ON**: VIX low, BTC/Nifty rising, USD/JPY/Gold falling
- **RISK_OFF**: VIX high, BTC/Nifty falling, USD/JPY/Gold rising
- **NEUTRAL**: Mixed signals

## Cache Implementation

- Cache file: `risk_cache.json` (configurable via settings)
- TTL: 3600 seconds (1 hour, configurable)
- Fallback enabled by default
- JSON format with ISO timestamp

## Testing Results

✅ All components tested and working:
- Trend calculation (rising/falling/flat)
- Risk indicator fetching (5/5 indicators)
- Risk score calculation (within [-100, +100] range)
- Cache functionality (hit/miss behavior)
- Fallback to stale cache on failure

## Example Output

```
VIX: 18.19 (7d change: -23.80%)
BTC Trend: RISING
Nifty Trend: FLAT
USD/JPY: 158.80 (Trend: FLAT)
Gold Trend: FLAT
Risk Regime: NEUTRAL
Risk Score: +31.5 (MODERATE RISK-ON)
```

## Integration Points

The RiskGauge can be integrated with:
1. EdgeScoreEngine (Phase 5) - contributes 10% weight to EdgeScore
2. Signal confidence modifier - adjust confidence based on risk regime
3. Dashboard UI - display risk gauge meter
4. API endpoints - `/api/edge/risk-gauge`

## Dependencies

- `yfinance>=0.2.30` (already in requirements.txt)
- `requests` (already in requirements.txt)
- `config.settings` (TWELVE_DATA_API_KEY, cache settings)

## Configuration

All settings in `config/settings.py`:
- `EDGE_RISK_CACHE_FILE`: Cache file path
- `EDGE_RISK_CACHE_TTL`: Cache TTL in seconds
- `EDGE_FALLBACK_ENABLED`: Enable fallback to stale cache
- `TWELVE_DATA_API_KEY`: Optional API key for USD/JPY (falls back to Yahoo Finance)

## Notes

- Zero cost: all data sources are free
- No API keys required (Twelve Data is optional)
- Robust error handling with fallbacks
- Type-safe with full type hints
- Logging only (no print statements)
- Cache-first design minimizes API calls
