"""
Position Sizing Module.

Calculates optimal trade quantity based on risk management rules.
Two methods:
  1. Fixed-percent risk: Risk X% of capital per trade
  2. Kelly Criterion: Statistically optimal bet fraction
  3. Regime-adjusted risk: Scale risk by current market regime
"""

from config import settings
from config.logger import get_logger

log = get_logger(__name__)


def regime_adjusted_risk_pct(base_risk: float, regime=None) -> float:
    """
    Scale the base risk percentage based on market regime.

    HIGH_VOLATILITY → 50% of base (protect capital in choppy markets)
    TRENDING        → 125% of base (exploit momentum)
    Default         → base unchanged

    Args:
        base_risk: Base risk percent (e.g. 2.0 = 2%)
        regime: MarketRegime enum value, or None

    Returns:
        Adjusted risk percent.
    """
    if regime is None:
        return base_risk

    regime_name = regime.value if hasattr(regime, "value") else str(regime)

    if regime_name == "high_volatility":
        adjusted = base_risk * 0.5
        log.debug("HIGH_VOLATILITY: risk reduced %.2f%% → %.2f%%", base_risk, adjusted)
    elif regime_name == "trending":
        adjusted = base_risk * 1.25
        log.debug("TRENDING: risk increased %.2f%% → %.2f%%", base_risk, adjusted)
    else:
        adjusted = base_risk

    return round(adjusted, 4)


def volatility_adjusted_risk_pct(base_risk: float, current_atr: float, historical_atr: float) -> float:
    """
    Scale the base risk percentage using the ATR ratio.
    If current volatility (ATR) is double the historical average, position size halves.
    """
    if historical_atr <= 0 or current_atr <= 0:
        return base_risk
    
    atr_ratio = current_atr / historical_atr
    
    # Cap the ratio to avoid extreme edge cases (e.g., flash crashes)
    atr_ratio = max(0.5, min(atr_ratio, 3.0))
    
    adjusted = base_risk / atr_ratio
    log.debug("Volatility Adjustment: Ratio %.2f (%.2f / %.2f) -> Risk %.2f%% to %.2f%%", 
              atr_ratio, current_atr, historical_atr, base_risk, adjusted)
    return round(adjusted, 4)


def calculate_position_size(
    capital: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    current_atr: float = 0.0,
    historical_atr: float = 0.0,
) -> float:
    """
    Calculate position size based on fixed percent risk, optionally adjusted for volatility.

    Formula: qty = (capital * adjusted_risk_pct) / |entry - stop_loss|

    Args:
        capital: Current account capital
        risk_pct: Percent of capital to risk (e.g., 2.0 = 2%)
        entry: Entry price
        stop_loss: Stop-loss price
        current_atr: Optional current ATR for volatility scaling
        historical_atr: Optional historical average ATR for volatility scaling

    Returns:
        Quantity (number of units) to trade. Returns 0 if invalid inputs.
    """
    # Apply volatility scaling if ATR data is provided
    if current_atr > 0 and historical_atr > 0:
        risk_pct = volatility_adjusted_risk_pct(risk_pct, current_atr, historical_atr)

    risk_per_unit = abs(entry - stop_loss)
    if risk_per_unit <= 0 or capital <= 0 or risk_pct <= 0:
        log.warning("Invalid sizing inputs: capital=%.2f, risk=%.2f%%, risk/unit=%.2f",
                     capital, risk_pct, risk_per_unit)
        return 0.0

    risk_amount = capital * (risk_pct / 100.0)
    qty = risk_amount / risk_per_unit

    log.debug(
        "Position size: ₹%.2f capital × %.1f%% risk = ₹%.2f risk → %.4f units (risk/unit=%.2f)",
        capital, risk_pct, risk_amount, qty, risk_per_unit,
    )
    return round(qty, 6)


def kelly_criterion(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    max_fraction: float = 0.25,
) -> float:
    """
    Kelly Criterion — optimal bet fraction for long-term growth.

    Formula: f* = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win

    Capped at max_fraction to prevent over-betting with limited data.

    Args:
        win_rate: Historical win probability (0–1)
        avg_win: Average winning trade amount (positive)
        avg_loss: Average losing trade amount (positive, absolute value)
        max_fraction: Maximum fraction to cap at (default: 25%)

    Returns:
        Fraction of capital to risk (0 to max_fraction).
        Returns 0 if the edge is negative or inputs are invalid.
    """
    if avg_win <= 0 or avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0

    # Kelly formula
    b = avg_win / avg_loss  # Payoff ratio (odds)
    p = win_rate
    q = 1 - p

    kelly_f = (p * b - q) / b

    if kelly_f <= 0:
        log.debug("Kelly fraction negative (%.3f) — no edge detected", kelly_f)
        return 0.0

    # Cap at max_fraction for safety
    capped = min(kelly_f, max_fraction)
    log.debug(
        "Kelly: win_rate=%.1f%%, b=%.2f → f*=%.3f (capped=%.3f)",
        win_rate * 100, b, kelly_f, capped,
    )
    return round(capped, 4)


def calculate_forex_position(
    symbol: str,
    entry: float,
    sl: float,
    account_balance: float,
    risk_pct: float = None,
) -> dict:
    """
    Calculate forex position size in standard lots.

    Uses fixed-percent risk model:
        risk_amount = account_balance * risk_pct
        lot_size    = risk_amount / (sl_pips * pip_value_inr)

    Lot size is capped at FOREX_MAX_LOT_SIZE (1.0) and floored at 0.01 (micro lot).

    Args:
        symbol:          Forex pair e.g. "EUR/USD"
        entry:           Entry price
        sl:              Stop-loss price
        account_balance: Account size in INR
        risk_pct:        Fraction to risk per trade (default: settings.FOREX_RISK_PCT_PER_TRADE)

    Returns:
        dict with keys: lot_size, sl_pips, risk_amount_inr
    """
    from agents.data_ingestion.forex_fetcher import get_pip_value

    if risk_pct is None:
        risk_pct = settings.FOREX_RISK_PCT_PER_TRADE

    pip_value = get_pip_value(symbol)
    sl_pips = abs(entry - sl) / pip_value if pip_value > 0 else 0.0

    if sl_pips <= 0 or account_balance <= 0:
        log.warning(
            "[FOREX] Invalid position sizing inputs: symbol=%s entry=%.5f sl=%.5f balance=%.2f",
            symbol, entry, sl, account_balance,
        )
        return {"lot_size": 0.01, "sl_pips": 0.0, "risk_amount_inr": 0.0}

    risk_amount = account_balance * risk_pct
    # Approximate pip value per lot: 10 INR for INR pairs, ~840 INR for non-INR (1 pip ≈ $10 ≈ ₹840)
    inr_pairs = {"USD/INR", "EUR/INR", "GBP/INR", "JPY/INR"}
    pip_value_inr = 10.0 if symbol in inr_pairs else 840.0

    lot_size = risk_amount / (sl_pips * pip_value_inr)
    lot_size = min(lot_size, settings.FOREX_MAX_LOT_SIZE)
    lot_size = max(lot_size, 0.01)

    log.debug(
        "[FOREX] Position: %s entry=%.5f sl=%.5f → sl_pips=%.1f lot_size=%.2f risk=₹%.2f",
        symbol, entry, sl, sl_pips, lot_size, risk_amount,
    )
    return {
        "lot_size": round(lot_size, 2),
        "sl_pips": round(sl_pips, 1),
        "risk_amount_inr": round(risk_amount, 2),
    }
