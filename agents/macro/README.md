# Macro Intelligence Layer (Edge Engine)

The macro intelligence layer provides fundamental/macro economic scoring that sits ON TOP of technical signal generation. It aggregates free data sources to produce an EdgeScore (-100 to +100) that modifies technical signal confidence.

## Phase 1: Macro Data Layer (Completed)

### Components

#### MacroFetcher
Fetches macro indicators from FRED API (Federal Reserve Economic Data):
- GDP growth (YoY %)
- CPI inflation (YoY %)
- Unemployment rate (%)
- Fed Funds rate (%)
- 10-year Treasury yield (%)

**Features:**
- 24-hour cache with JSON fallback
- Stale cache fallback on API failure
- Timezone-aware datetime handling
- Type-safe with full type hints

#### MacroScorer
Scores macro environment from -100 (risk-off) to +100 (risk-on):
- Strong growth + low inflation + low rates = +100 (risk-on)
- Recession + high inflation + high rates = -100 (risk-off)
- Neutral conditions = 0

**Scoring Logic:**
- GDP: >3% YoY = +100, <0% = -100
- CPI: <3% YoY = +100, >5% = -100
- Unemployment: <4% = +100, >8% = -100
- Fed Funds: <2% = +100, >5% = -100
- 10Y Treasury: <2% = +100, >5% = -100

Requires at least 3 of 5 indicators available.

## Setup

### 1. Get FRED API Key (Free)
1. Visit https://fred.stlouisfed.org/docs/api/api_key.html
2. Sign up (no credit card required)
3. Copy your API key

### 2. Configure Environment
Add to `.env`:
```bash
FRED_API_KEY=your_api_key_here
```

### 3. Usage Example

```python
from agents.macro.macro_fetcher import MacroFetcher
from agents.macro.macro_scorer import MacroScorer

# Initialize
fetcher = MacroFetcher()
scorer = MacroScorer(fetcher=fetcher)

# Fetch indicators
indicators = fetcher.fetch_indicators()
print(f"GDP Growth: {indicators.gdp_growth_yoy}%")
print(f"CPI Inflation: {indicators.cpi_yoy}%")
print(f"Unemployment: {indicators.unemployment_rate}%")

# Get macro score
score = scorer.get_macro_score()
print(f"Macro Score: {score}")  # -100 to +100
```

## Cache Behavior

### Cache Files
- `macro_cache.json` - Stores fetched indicators

### Cache TTL
- 24 hours (86400 seconds)
- Configurable via `EDGE_MACRO_CACHE_TTL` in settings

### Fallback Strategy
1. Try cache first (if fresh)
2. If cache stale, fetch from API
3. If API fails and `EDGE_FALLBACK_ENABLED=True`, use stale cache
4. If no cache and no API, return empty indicators (all None)

## Testing

Run tests:
```bash
pytest tests/test_macro_basic.py -v
```

Tests cover:
- Serialization/deserialization
- Cache hit/miss behavior
- Stale cache fallback
- Risk-on scoring
- Risk-off scoring
- Insufficient indicators handling

## Next Steps (Phase 2+)

- [ ] COT (Commitment of Traders) data integration
- [ ] Retail sentiment scoring (Myfxbook/Dukascopy)
- [ ] Risk gauge (VIX, BTC, Nifty, USD/JPY, Gold)
- [ ] Seasonality analysis
- [ ] Carry spread calculation
- [ ] EdgeScore engine orchestration
- [ ] Signal generator integration
- [ ] API endpoints
- [ ] Dashboard UI components

## Design Principles

- **Zero cost**: All APIs are free (FRED, CFTC, public sentiment)
- **Paper trading only**: No real capital at risk
- **Non-breaking**: All new code in `agents/macro/`, existing modules untouched
- **Cache-first**: All external API calls cached with fallback
- **Type-safe**: Full type hints on all functions
- **Logging only**: No print statements

## Configuration

All settings in `config/settings.py`:

```python
# FRED API
FRED_API_KEY: str = os.getenv("FRED_API_KEY", "")

# Cache TTLs
EDGE_MACRO_CACHE_TTL: int = 86400  # 24 hours

# Scoring thresholds
MACRO_MIN_INDICATORS: int = 3
MACRO_STRONG_GROWTH_GDP: float = 3.0
MACRO_LOW_INFLATION_CPI: float = 3.0
MACRO_HIGH_INFLATION_CPI: float = 5.0
MACRO_LOW_RATES_FED: float = 2.0
MACRO_HIGH_RATES_FED: float = 5.0

# FRED Series IDs
FRED_SERIES_GDP: str = "GDPC1"
FRED_SERIES_CPI: str = "CPIAUCSL"
FRED_SERIES_UNEMPLOYMENT: str = "UNRATE"
FRED_SERIES_FED_FUNDS: str = "DFF"
FRED_SERIES_TREASURY_10Y: str = "DGS10"
```
