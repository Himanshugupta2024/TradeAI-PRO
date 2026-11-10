"""
Position Sizing Module.

Calculates optimal trade quantity based on risk management rules.
Three methods:
  1. Fixed-percent risk: Risk X% of capital per trade
  2. Kelly Criterion: Statistically optimal bet fraction
  3. Regime/volatility-adjusted risk: Scale risk by market regime and ATR ratio

``compute_risk_pct`` is the single entry point used by the paper-trading engine:
it composes regime scaling, ATR vol-targeting, a fractional-Kelly cap and a hard
per-trade ceiling, and reports which rule produced the final number.
"""

import math
from typing import Iterable

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

    # MarketRegime enum values are uppercase ("HIGH_VOLATILITY"); callers may
    # also pass the raw string. Normalize so neither form silently misses the
    # scaling branch (this was dead code before the normalization).
    raw = regime.value if hasattr(regime, "value") else str(regime)
    regime_name = raw.strip().lower()

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


def compute_risk_pct(
    base_risk_pct: float,
    regime=None,
    current_atr: float = 0.0,
    historical_atr: float = 0.0,
    kelly_fraction: float = None,
    kelly_fractionality: float = 0.5,
    kelly_floor_ratio: float = 0.25,
    max_risk_pct: float = None,
) -> dict:
    """
    Compose every risk-scaling rule into one auditable risk percentage.

    Order of operations:
      1. Regime scaling   — HIGH_VOLATILITY ×0.5, TRENDING ×1.25
      2. ATR vol-target   — risk / clamp(current_atr / historical_atr, 0.5, 3.0)
      3. Fractional Kelly — cap at ``100 × f* × fractionality`` (never raises risk)
      4. Hard ceiling     — clamp to ``max_risk_pct`` if provided

    The Kelly cap is a de-risking guard, not an accelerator: with a 1.25% base
    risk it only binds when the realized edge is thin or negative, in which case
    risk shrinks toward ``kelly_floor_ratio × base_risk_pct`` instead of dropping
    to zero (a zero here would be misread downstream as a sizing failure).

    Args:
        base_risk_pct: Configured per-trade risk (e.g. 1.25 = 1.25% of capital).
        regime: MarketRegime enum value or name string, or None.
        current_atr: Current ATR, or 0.0 to skip vol-targeting.
        historical_atr: Rolling average ATR, or 0.0 to skip vol-targeting.
        kelly_fraction: Raw Kelly f* (fraction of capital), or None when the
            closed-trade sample is too small to trust.
        kelly_fractionality: Fraction of Kelly to apply (0.5 = half-Kelly).
        kelly_floor_ratio: Floor for the Kelly cap as a ratio of base risk.
        max_risk_pct: Absolute per-trade ceiling in percent.

    Returns:
        dict with keys: risk_pct, base_pct, regime_pct, vol_pct, kelly_cap_pct,
        binding ("base" | "regime" | "volatility" | "kelly" | "max_cap").
    """
    base = float(base_risk_pct)
    if not math.isfinite(base) or base <= 0:
        return {
            "risk_pct": 0.0,
            "base_pct": base if math.isfinite(base) else 0.0,
            "regime_pct": 0.0,
            "vol_pct": 0.0,
            "kelly_cap_pct": None,
            "binding": "invalid_base",
        }

    regime_pct = regime_adjusted_risk_pct(base, regime)
    binding = "regime" if abs(regime_pct - base) > 1e-9 else "base"

    vol_pct = regime_pct
    if current_atr > 0 and historical_atr > 0:
        vol_pct = volatility_adjusted_risk_pct(regime_pct, current_atr, historical_atr)
        if abs(vol_pct - regime_pct) > 1e-9:
            binding = "volatility"

    risk_pct = vol_pct
    kelly_cap_pct = None
    if kelly_fraction is not None and math.isfinite(float(kelly_fraction)):
        raw_cap = 100.0 * float(kelly_fraction) * max(float(kelly_fractionality), 0.0)
        floor_pct = base * max(float(kelly_floor_ratio), 0.0)
        kelly_cap_pct = round(max(raw_cap, floor_pct), 4)
        if kelly_cap_pct < risk_pct - 1e-9:
            risk_pct = kelly_cap_pct
            binding = "kelly"

    if max_risk_pct is not None and max_risk_pct > 0 and risk_pct > max_risk_pct:
        risk_pct = float(max_risk_pct)
        binding = "max_cap"

    return {
        "risk_pct": round(max(risk_pct, 0.0), 4),
        "base_pct": round(base, 4),
        "regime_pct": round(regime_pct, 4),
        "vol_pct": round(vol_pct, 4),
        "kelly_cap_pct": kelly_cap_pct,
        "binding": binding,
    }


def calculate_position_size(
    capital: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    current_atr: float = 0.0,
    historical_atr: float = 0.0,
    max_leverage: float = None,
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

    # Enforce purchasing power / cash equity bound if max_leverage is set
    if max_leverage is not None and max_leverage > 0 and entry > 0:
        max_allowed_qty = (capital * max_leverage) / entry
        qty = min(qty, max_allowed_qty)

    log.debug(
        "Position size: ₹%.2f capital × %.1f%% risk = ₹%.2f risk → %.4f units (risk/unit=%.2f)",
        capital, risk_pct, risk_amount, qty, risk_per_unit,
    )
    return round(qty, 6)


def edge_stats_from_pnls(
    pnls: Iterable[float],
    lookback: int = 60,
    min_sample: int = 30,
) -> dict | None:
    """
    Empirical edge statistics from realized closed-trade PnLs (base currency).

    Uses the most recent ``lookback`` decided trades (breakeven trades excluded)
    so a regime change is reflected instead of being diluted by ancient history.

    Args:
        pnls: Realized PnL per closed trade.
        lookback: Max number of recent decided trades to aggregate.
        min_sample: Minimum decided trades required to trust the estimate.

    Returns:
        dict with sample, win_rate, avg_win, avg_loss, payoff_ratio,
        expectancy and profit_factor — or None when the sample is too small
        (or when one side of the distribution is missing entirely).
    """
    values: list[float] = []
    for pnl in pnls:
        try:
            value = float(pnl)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)

    if lookback and lookback > 0:
        values = values[-int(lookback):]

    wins = [v for v in values if v > 1e-9]
    losses = [v for v in values if v < -1e-9]
    decided = len(wins) + len(losses)

    if decided < max(int(min_sample), 1) or not wins or not losses:
        return None

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = gross_profit / len(wins)
    avg_loss = gross_loss / len(losses)
    if avg_win <= 0 or avg_loss <= 0:
        return None

    win_rate = len(wins) / decided
    expectancy = win_rate * avg_win - (1.0 - win_rate) * avg_loss

    return {
        "sample": decided,
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "payoff_ratio": round(avg_win / avg_loss, 4),
        "expectancy": round(expectancy, 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
    }


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
