"""
Multi-Currency Accounting & FX Conversion Engine for TradeAI PRO.

Ensures strict institutional-grade separation between native instrument
quote currencies (INR, USDT, USD, etc.) and account base currency (INR).
Uses Decimal arithmetic for monetary calculations to eliminate floating-point drift.
"""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass
import time

from config.logger import get_logger

log = get_logger(__name__)

# Quantization boundaries
INR_QUANT = Decimal("0.01")
NATIVE_CRYPTO_QUANT = Decimal("0.00000001")
NATIVE_FOREX_QUANT = Decimal("0.00001")


class MissingFXRateError(Exception):
    """Raised when an FX rate cannot be resolved and no valid fallback exists."""
    pass


@dataclass
class FXRateRecord:
    from_currency: str
    to_currency: str
    rate: Decimal
    source: str
    timestamp: str  # ISO UTC
    is_stale: bool = False
    conversion_path: str = ""


def resolve_instrument_identity(symbol: str) -> Tuple[str, str, str]:
    """
    Resolve (asset_class, quote_currency, base_currency) for any instrument.
    Account base currency is canonical INR.
    Preserves USDT as USDT (never silently renames USDT to USD).
    """
    s = str(symbol or "").strip().upper()
    base_curr = "INR"

    if s.endswith("USDT") or "/USDT" in s or s.endswith("-USDT"):
        return "CRYPTO", "USDT", base_curr
    elif s.endswith("USD") or "/USD" in s:
        # Check if forex pair or crypto
        if any(fx in s for fx in ("EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY")) and "/" in s:
            return "FOREX", "USD", base_curr
        return "CRYPTO", "USD", base_curr
    elif "/INR" in s or s.endswith("INR"):
        if "/" in s:
            return "FOREX", "INR", base_curr
        return "EQUITY", "INR", base_curr
    elif "/" in s:
        # Generic forex or crypto pair
        parts = s.split("/")
        quote = parts[1] if len(parts) > 1 else "INR"
        return "FOREX" if any(p in ("EUR", "GBP", "JPY", "USD", "AUD", "CAD") for p in parts) else "CRYPTO", quote, base_curr
    else:
        # Indian equity (NSE/BSE)
        return "EQUITY", "INR", base_curr


class FXRateService:
    """
    Singleton service for source-attributed, timestamped FX rate conversion.
    Supports deterministic test rate injection, live yFinance rates, and runtime caching.
    """
    _instance: Optional["FXRateService"] = None

    def __init__(self):
        self._test_rates: Dict[Tuple[str, str], Decimal] = {}
        self._cache: Dict[Tuple[str, str], FXRateRecord] = {}
        self._cache_ttl_sec = 300.0  # 5 minutes TTL

    @classmethod
    def get_instance(cls) -> "FXRateService":
        if cls._instance is None:
            cls._instance = FXRateService()
        return cls._instance

    def set_test_rate(self, from_curr: str, to_curr: str, rate: float | Decimal | str) -> None:
        """Inject a deterministic test FX rate for reproducible test fixtures."""
        fc = from_curr.strip().upper()
        tc = to_curr.strip().upper()
        dec_rate = Decimal(str(rate))
        self._test_rates[(fc, tc)] = dec_rate
        if dec_rate > 0:
            self._test_rates[(tc, fc)] = Decimal("1.0") / dec_rate

    def clear_test_rates(self) -> None:
        """Clear injected test rates."""
        self._test_rates.clear()

    def get_fx_rate(
        self,
        from_curr: str,
        to_curr: str = "INR",
        allow_stale: bool = True,
        max_staleness_sec: float = 3600.0,
    ) -> FXRateRecord:
        """
        Get conversion rate from from_curr to to_curr with timestamp and source attribution.
        Never defaults to 1.0 for non-identical currencies.
        """
        fc = from_curr.strip().upper()
        tc = to_curr.strip().upper()
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Identity rate
        if fc == tc:
            return FXRateRecord(
                from_currency=fc,
                to_currency=tc,
                rate=Decimal("1.0"),
                source="IDENTITY",
                timestamp=now_iso,
                is_stale=False,
                conversion_path=f"{fc} == {tc}",
            )

        # 2. Check injected deterministic test rates
        if (fc, tc) in self._test_rates:
            return FXRateRecord(
                from_currency=fc,
                to_currency=tc,
                rate=self._test_rates[(fc, tc)],
                source="DETERMINISTIC_FIXTURE",
                timestamp=now_iso,
                is_stale=False,
                conversion_path=f"{fc} -> {tc} via test fixture",
            )

        # 3. Check in-memory / runtime cache
        cache_key = (fc, tc)
        if cache_key in self._cache:
            rec = self._cache[cache_key]
            try:
                rec_dt = datetime.fromisoformat(rec.timestamp.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - rec_dt).total_seconds()
                if age < self._cache_ttl_sec:
                    return rec
                elif allow_stale and age < max_staleness_sec:
                    return FXRateRecord(
                        from_currency=rec.from_currency,
                        to_currency=rec.to_currency,
                        rate=rec.rate,
                        source=f"{rec.source} (STALE_{int(age)}s)",
                        timestamp=rec.timestamp,
                        is_stale=True,
                        conversion_path=rec.conversion_path,
                    )
            except Exception:
                pass

        # 4. Resolve USDT -> INR (via USD/INR or direct)
        # Note: In institutional crypto, USDT trades at parity to USD within ±0.15% collar.
        # We attribute this as USDT -> USD (1.0) -> USDINR=X.
        if fc == "USDT" and tc == "INR":
            usd_inr_rec = self._fetch_live_or_fallback_rate("USD", "INR")
            rate = usd_inr_rec.rate  # 1 USDT * 1.0 USD/USDT * USD/INR
            rec = FXRateRecord(
                from_currency=fc,
                to_currency=tc,
                rate=rate,
                source=f"COLLAR_USDT_PARITY * {usd_inr_rec.source}",
                timestamp=usd_inr_rec.timestamp,
                is_stale=usd_inr_rec.is_stale,
                conversion_path=f"USDT -> USD(1.0) -> INR({rate})",
            )
            self._cache[cache_key] = rec
            return rec

        # 5. Resolve USD -> INR or other pairs
        rec = self._fetch_live_or_fallback_rate(fc, tc)
        self._cache[cache_key] = rec
        return rec

    def _fetch_live_or_fallback_rate(self, from_curr: str, to_curr: str) -> FXRateRecord:
        """Attempt live quote fetch from yfinance or runtime_state, with fail-safe error handling."""
        now_iso = datetime.now(timezone.utc).isoformat()
        
        # Check runtime state first
        try:
            from db.runtime_state import get_runtime_state
            cached_rates = get_runtime_state("forex_rates:v1", {}) or {}
            pair_key = f"{from_curr}/{to_curr}"
            if pair_key in cached_rates and float(cached_rates[pair_key]) > 0:
                val = Decimal(str(cached_rates[pair_key]))
                return FXRateRecord(
                    from_currency=from_curr,
                    to_currency=to_curr,
                    rate=val,
                    source="RUNTIME_STATE_SNAPSHOT",
                    timestamp=now_iso,
                    is_stale=False,
                    conversion_path=f"{pair_key} from runtime snapshot",
                )
        except Exception:
            pass

        # Try live yfinance fetch
        yf_ticker = f"{from_curr}{to_curr}=X"
        try:
            import yfinance as yf
            t = yf.Ticker(yf_ticker)
            df = t.history(period="1d", interval="5m")
            if not df.empty and "Close" in df.columns:
                val = Decimal(str(round(float(df["Close"].iloc[-1]), 4)))
                if val > 0:
                    return FXRateRecord(
                        from_currency=from_curr,
                        to_currency=to_curr,
                        rate=val,
                        source="YFINANCE_LIVE",
                        timestamp=now_iso,
                        is_stale=False,
                        conversion_path=f"{yf_ticker} live",
                    )
        except Exception as e:
            log.debug("Live FX fetch failed for %s: %s", yf_ticker, e)

        # Fallback to configured default for USD/INR if allowed
        if from_curr in ("USD", "USDT") and to_curr == "INR":
            fallback = Decimal("85.0")
            return FXRateRecord(
                from_currency=from_curr,
                to_currency=to_curr,
                rate=fallback,
                source="CONFIG_FALLBACK_DEFAULT",
                timestamp=now_iso,
                is_stale=True,
                conversion_path=f"{from_curr}->INR emergency fallback (85.0)",
            )

        raise MissingFXRateError(f"Cannot resolve FX rate for {from_curr} to {to_curr}")


def to_decimal(val: float | int | str | Decimal, default: str = "0.0") -> Decimal:
    """Safely convert any numeric value to Decimal."""
    if isinstance(val, Decimal):
        return val
    try:
        if val is None or (isinstance(val, float) and (val != val or abs(val) == float("inf"))):
            return Decimal(default)
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def quantize_inr(amount: Decimal | float) -> Decimal:
    """Quantize base-currency INR amount to 2 decimal places."""
    dec = to_decimal(amount)
    return dec.quantize(INR_QUANT, rounding=ROUND_HALF_UP)


def quantize_native(amount: Decimal | float, asset_class: str = "EQUITY") -> Decimal:
    """Quantize native currency amounts according to asset class."""
    dec = to_decimal(amount)
    if asset_class == "CRYPTO":
        return dec.quantize(NATIVE_CRYPTO_QUANT, rounding=ROUND_HALF_UP)
    elif asset_class == "FOREX":
        return dec.quantize(NATIVE_FOREX_QUANT, rounding=ROUND_HALF_UP)
    return dec.quantize(INR_QUANT, rounding=ROUND_HALF_UP)
