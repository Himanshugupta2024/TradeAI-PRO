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


def calculate_position_size(
    capital: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
) -> float:
    """
    Calculate position size based on fixed percent risk.

    Formula: qty = (capital * risk_pct) / |entry - stop_loss|

    Args:
        capital: Current account capital
        risk_pct: Percent of capital to risk (e.g., 2.0 = 2%)
        entry: Entry price
        stop_loss: Stop-loss price

    Returns:
        Quantity (number of units) to trade. Returns 0 if invalid inputs.
    """
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
