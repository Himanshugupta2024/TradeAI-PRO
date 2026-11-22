"""
TradeAI Pro — Shoonya Token Master.
Downloads and caches NSE equity and index symbol tokens from Shoonya.
Maps user-facing symbols (e.g., RELIANCE, NIFTY 50) to exchange tokens.
"""
from __future__ import annotations

import io
import json
import logging
import os
import urllib.request
import zipfile
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Fallback tokens for common benchmark indices and high-weight NSE equities
# Used when offline, during tests, or before the initial zip download completes.
DEFAULT_FALLBACK_TOKENS: Dict[str, str] = {
    # Benchmark Indices
    "NIFTY 50": "26000",
    "NIFTY": "26000",
    "NIFTY50": "26000",
    "NIFTY BANK": "26009",
    "BANKNIFTY": "26009",
    # Top NSE Equities
    "RELIANCE": "2885",
    "TCS": "11536",
    "HDFCBANK": "1333",
    "INFY": "1594",
    "ICICIBANK": "4963",
    "SBIN": "3045",
    "BHARTIARTL": "10604",
    "ITC": "1660",
    "KOTAKBANK": "1922",
    "LT": "11483",
    "HINDUNILVR": "1394",
    "AXISBANK": "5900",
    "TATAMOTORS": "3456",
    "BAJFINANCE": "317",
    "MARUTI": "10999",
    "SUNPHARMA": "3351",
    "TITAN": "3506",
    "ASIANPAINT": "236",
    "WIPRO": "3787",
    "ULTRACEMCO": "11532",
    "TATASTEEL": "3499",
    "NTPC": "11630",
    "POWERGRID": "14977",
    "M&M": "2031",
}

SHOONYA_NSE_SYMBOLS_URL = "https://api.shoonya.com/NSE_symbols.txt.zip"


class TokenMaster:
    """Manages mapping between trading symbols and Shoonya exchange tokens."""

    def __init__(self, cache_file: Optional[str] = None):
        self.cache_file = cache_file or os.path.join(
            os.path.dirname(__file__), "..", "data", "cache", "shoonya_tokens.json"
        )
        self.symbol_to_token: Dict[str, str] = dict(DEFAULT_FALLBACK_TOKENS)
        self.token_to_symbol: Dict[str, str] = {
            f"NSE|{tok}": sym for sym, tok in DEFAULT_FALLBACK_TOKENS.items()
        }
        # Explicit canonical names for reverse index mapping
        self.token_to_symbol["NSE|26000"] = "NIFTY 50"
        self.token_to_symbol["NSE|26009"] = "NIFTY BANK"
        self.last_updated: Optional[str] = None
        self._load_cached_tokens()

    def normalize_symbol(self, symbol: str) -> str:
        """Strip exchange affixes, spaces, and case variants."""
        s = symbol.strip().upper()
        if s.endswith(".NS"):
            s = s[:-3]
        if s.endswith("-EQ"):
            s = s[:-3]
        return s

    def get_token(self, symbol: str) -> Optional[str]:
        """Look up exchange token for a symbol."""
        norm = self.normalize_symbol(symbol)
        if norm in self.symbol_to_token:
            return self.symbol_to_token[norm]
        # Check raw symbol
        if symbol in self.symbol_to_token:
            return self.symbol_to_token[symbol]
        return None

    def get_symbol_from_token(self, exchange: str, token: str) -> Optional[str]:
        """Reverse look up symbol from exchange token."""
        key = f"{exchange.upper()}|{token}"
        return self.token_to_symbol.get(key)

    def register_token(self, symbol: str, token: str, exchange: str = "NSE") -> None:
        """Manually register or override a token mapping."""
        norm = self.normalize_symbol(symbol)
        self.symbol_to_token[norm] = str(token)
        self.token_to_symbol[f"{exchange.upper()}|{token}"] = norm

    def refresh_from_shoonya(self, timeout: float = 15.0) -> int:
        """
        Download and parse official NSE_symbols.txt.zip from Shoonya API.
        Extracts all standard equity (-EQ) and index tokens.
        """
        try:
            req = urllib.request.Request(
                SHOONYA_NSE_SYMBOLS_URL,
                headers={"User-Agent": "TradeAI-Feed-TokenMaster/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()

            with zipfile.ZipFile(io.BytesIO(content)) as z:
                first_file = z.namelist()[0]
                with z.open(first_file) as f:
                    import csv
                    # Read decoded text
                    text_stream = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
                    reader = csv.DictReader(text_stream)

                    count = 0
                    for row in reader:
                        exch = (row.get("Exchange") or "").strip()
                        if exch != "NSE":
                            continue

                        token = (row.get("Token") or "").strip()
                        instrument = (row.get("Instrument") or "").strip()
                        symbol = (row.get("Symbol") or "").strip()
                        tsym = (row.get("TradingSymbol") or "").strip()

                        if not token or not symbol:
                            continue

                        # Match standard equities and indices
                        if instrument in ("EQ", "INDEX") or tsym.endswith("-EQ") or "INDEX" in tsym:
                            norm = self.normalize_symbol(symbol)
                            self.symbol_to_token[norm] = token
                            self.token_to_symbol[f"NSE|{token}"] = norm
                            count += 1

            logger.info("Successfully refreshed %d Shoonya NSE tokens", count)
            self._save_cached_tokens()
            return count
        except Exception as exc:
            logger.warning("Failed to refresh Shoonya token master: %s (using fallbacks)", exc)
            return len(self.symbol_to_token)

    def _load_cached_tokens(self) -> None:
        """Load persisted tokens from local disk cache if available."""
        if not self.cache_file or not os.path.exists(self.cache_file):
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self.symbol_to_token.update(data.get("symbol_to_token", {}))
                    self.token_to_symbol.update(data.get("token_to_symbol", {}))
                    self.last_updated = data.get("last_updated")
        except Exception as exc:
            logger.debug("Could not read token cache file: %s", exc)

    def _save_cached_tokens(self) -> None:
        """Persist tokens to disk cache for fast cold-starts."""
        if not self.cache_file:
            return
        try:
            from datetime import datetime, timezone
            os.makedirs(os.path.dirname(os.path.abspath(self.cache_file)), exist_ok=True)
            self.last_updated = datetime.now(timezone.utc).isoformat()
            payload = {
                "last_updated": self.last_updated,
                "symbol_to_token": self.symbol_to_token,
                "token_to_symbol": self.token_to_symbol,
            }
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception as exc:
            logger.debug("Could not save token cache file: %s", exc)
