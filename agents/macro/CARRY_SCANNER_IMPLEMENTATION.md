# Carry Scanner Implementation

## Overview

The `carry_scanner.py` module implements carry trade analysis for forex pairs using interest rate differentials from FRED API. It provides a carry bonus (-10 to +10) that is added to the EdgeScore formula, but only applies in risk-on market environments.

## Key Features

### 1. Forex-Only Logic
- Only processes forex pairs (format: "XXX/YYY" with 3-letter currency codes)
- Returns 0 for crypto (BTC/USDT), NSE stocks (RELIANCE), and other non-forex symbols
- Validates both base and quote currencies are in supported currency list

### 2. Risk-On Environment Filter
- Carry bonus only applies when `risk_regime == "RISK_ON"`
- Returns 0 in RISK_OFF or NEUTRAL environments
- Rationale: Carry trades are unwound during risk-off periods (flight to safety)

### 3. Interest Rate Differential Calculation
Uses FRED API to fetch central bank policy rates for 7 currencies:
- **USD**: Federal Funds Rate (DFF) - daily
- **EUR**: ECB Deposit Facility Rate (ECBDFR)
- **GBP**: Bank of England Official Bank Rate (BOERUKQ)
- **JPY**: Japan Policy Rate (IRSTCB01JPM156N) - monthly
- **AUD**: Australia Cash Rate (IRSTCB01AUM156N) - monthly
- **CAD**: Canada Overnight Rate (IRSTCB01CAM156N) - monthly
- **INR**: India Repo Rate (IRSTCB01INM156N) - monthly

Carry differential = base_rate - quote_rate

### 4. Carry Bonus Scoring (Risk-On Only)

| Differential | Bonus | Description |
|--------------|-------|-------------|
| ≥ +3.0% | +10 | Strong positive carry |
| +1.0% to +3.0% | +5 to +10 | Weak positive carry (linear) |
| -1.0% to +1.0% | 0 | Neutral carry |
| -3.0% to -1.0% | -5 to -10 | Weak negative carry (linear) |
| ≤ -3.0% | -10 | Strong negative carry |

### 5. 24-Hour Cache with Fallback
- Cache file: `carry_cache.json` (configurable via `EDGE_CARRY_CACHE_FILE`)
- TTL: 24 hours (86400 seconds, configurable via `EDGE_CARRY_CACHE_TTL`)
- Cache structure: `{symbol: CarryData}` dict
- Fallback: Uses stale cache on API failure if `EDGE_FALLBACK_ENABLED=True`
- Cache updates risk_regime dynamically (may change more frequently than rates)

## Usage Example

```python
from agents.macro.carry_scanner import CarryScanner
from agents.macro.risk_gauge import RiskGauge

# Initialize scanner
scanner = CarryScanner()

# Get current risk regime
risk_gauge = RiskGauge()
risk_score = risk_gauge.get_risk_score()
indicators = risk_gauge.fetch_risk_indicators()
risk_regime = indicators.risk_regime  # "RISK_ON" | "RISK_OFF" | "NEUTRAL"

# Calculate carry bonus for forex pairs
eur_usd_carry = scanner.get_carry_score("EUR/USD", risk_regime)
# Returns: -10 to +10 if RISK_ON, 0 otherwise

# Non-forex symbols always return 0
btc_carry = scanner.get_carry_score("BTC/USDT", risk_regime)
# Returns: 0 (not a forex pair)

reliance_carry = scanner.get_carry_score("RELIANCE", risk_regime)
# Returns: 0 (not a forex pair)
```

## Integration with EdgeScore

The carry bonus is added to the EdgeScore formula:

```
EdgeScore = (macro×0.30 + cot×0.25 + sentiment×0.20 + risk×0.10 + momentum×0.15) 
            + seasonal_bonus + carry_bonus
```

Where:
- Base score: -100 to +100 (weighted average of 5 components)
- Seasonal bonus: -20 to +20
- Carry bonus: -10 to +10 (from this module)
- Final EdgeScore: -130 to +130 (clamped to -100 to +100 in practice)

## Configuration

All configuration is in `config/settings.py`:

```python
# FRED API key (free from stlouisfed.org)
FRED_API_KEY: str = os.getenv("FRED_API_KEY", "")

# Cache settings
EDGE_CARRY_CACHE_FILE: str = "carry_cache.json"
EDGE_CARRY_CACHE_TTL: int = 86400  # 24 hours
EDGE_FALLBACK_ENABLED: bool = True
```

## Supported Forex Pairs

Based on `FOREX_SYMBOLS` in settings.py:
- **INR Pairs**: USD/INR, EUR/INR, GBP/INR, JPY/INR
- **Major Pairs**: EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD

All pairs use currencies with FRED interest rate data available.

## Error Handling

1. **No FRED API Key**: Uses cache-only mode, logs warning
2. **API Failure**: Falls back to stale cache if `EDGE_FALLBACK_ENABLED=True`
3. **Missing Currency Data**: Returns 0 carry bonus, logs warning
4. **Invalid Symbol Format**: Returns 0 (not a forex pair)
5. **Cache Corruption**: Logs error, continues without cache

## Testing

Basic functionality verified:
- ✅ Forex pair detection (EUR/USD = True, BTC/USDT = False)
- ✅ Forex pair parsing (EUR/USD → base=EUR, quote=USD)
- ✅ Carry bonus calculation (+4% RISK_ON = +10, +4% RISK_OFF = 0)
- ✅ Non-forex filtering (BTC/USDT always returns 0)
- ✅ Risk regime filtering (RISK_OFF/NEUTRAL always returns 0)

## Implementation Notes

1. **Daily vs Monthly Rates**: Some FRED series are daily (USD, EUR, GBP), others monthly (JPY, AUD, CAD, INR). The module fetches the latest available value regardless of frequency.

2. **Rate Staleness**: Interest rates change infrequently (monthly central bank meetings), so 24-hour cache is appropriate. The risk_regime is updated dynamically even with cached rate data.

3. **Carry Trade Logic**: Positive carry (buying high-yield currency) is profitable in risk-on environments when investors seek yield. In risk-off environments, carry trades are unwound as investors flee to safe havens (USD, JPY, CHF).

4. **Linear Interpolation**: Weak carry bonuses (1-3% differential) use linear interpolation to provide smooth scoring between thresholds.

5. **Zero-Cost Design**: Uses free FRED API (no paid data sources), consistent with Edge Engine design principles.

## Future Enhancements

Potential improvements (not in current scope):
- Add CHF (Swiss Franc) support if FRED series available
- Add NZD (New Zealand Dollar) support
- Implement carry trade risk metrics (volatility-adjusted carry)
- Add historical carry performance tracking
- Implement dynamic threshold adjustment based on market conditions
