"""
TradeAI Pro — Shoonya NFO Master.
Parses, caches, and indexes National Stock Exchange Futures & Options (NFO) symbol masters.
Maps underlyings (e.g. NIFTY, RELIANCE) to strike series, option types (CE/PE), expiries, and tokens.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import urllib.request
import zipfile
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Standard NSE index and stock options strike intervals (steps)
STRIKE_STEPS: Dict[str, float] = {
    "NIFTY": 50.0,
    "NIFTY 50": 50.0,
    "NIFTY50": 50.0,
    "BANKNIFTY": 100.0,
    "NIFTY BANK": 100.0,
    "RELIANCE": 20.0,
    "TCS": 20.0,
    "HDFCBANK": 10.0,
    "INFY": 10.0,
    "ICICIBANK": 10.0,
    "SBIN": 5.0,
    "BHARTIARTL": 10.0,
    "ITC": 5.0,
    "KOTAKBANK": 10.0,
    "LT": 20.0,
    "AXISBANK": 10.0,
    "MARUTI": 50.0,
    "TATAMOTORS": 10.0,
    "BAJFINANCE": 50.0,
}

SHOONYA_NFO_SYMBOLS_URL = "https://api.shoonya.com/NFO_symbols.txt.zip"


class NFOMaster:
    """Manages indexing and lookup of NFO derivative contracts."""

    def __init__(self, cache_file: Optional[str] = None):
        self.cache_file = cache_file or os.path.join(
            os.path.dirname(__file__), "..", "data", "cache", "shoonya_nfo_tokens.json"
        )
        # Map: symbol -> list of contracts dict
        self.contracts: Dict[str, List[Dict]] = {}
        # Map: token -> contract dict
        self.token_to_contract: Dict[str, Dict] = {}
        self._load_local_cache()

    def get_strike_step(self, symbol: str) -> float:
        """Return typical strike step for symbol, defaulting to 10.0."""
        sym = symbol.strip().upper().replace(".NS", "")
        return STRIKE_STEPS.get(sym, 10.0)

    def get_atm_strike(self, symbol: str, spot: float, step: Optional[float] = None) -> float:
        """Compute the nearest At-The-Money (ATM) strike price for spot."""
        s = step or self.get_strike_step(symbol)
        if s <= 0:
            return round(spot, 2)
        return round(round(spot / s) * s, 2)

    def register_contract(
        self,
        symbol: str,
        token: str,
        strike: float,
        option_type: str,
        expiry: str,
        lot_size: int = 1,
        trading_symbol: str = "",
    ) -> None:
        """Register a single contract into index."""
        sym = symbol.strip().upper().replace(".NS", "")
        opt_type = option_type.strip().upper()
        item = {
            "symbol": sym,
            "token": str(token),
            "strike": float(strike),
            "option_type": opt_type,
            "expiry": expiry,
            "lot_size": lot_size,
            "trading_symbol": trading_symbol or f"{sym}{expiry}{strike}{opt_type}",
        }
        if sym not in self.contracts:
            self.contracts[sym] = []
        self.contracts[sym].append(item)
        self.token_to_contract[str(token)] = item

    def get_contracts(self, symbol: str, expiry: Optional[str] = None) -> List[Dict]:
        """Get all option contracts for an underlying symbol, optionally filtered by expiry."""
        sym = symbol.strip().upper().replace(".NS", "")
        c_list = self.contracts.get(sym, [])
        if expiry:
            return [c for c in c_list if c.get("expiry") == expiry]
        return c_list

    def get_contract_by_token(self, token: str) -> Optional[Dict]:
        return self.token_to_contract.get(str(token))

    def _load_local_cache(self) -> bool:
        """Load cached contract tokens if available."""
        if not os.path.exists(self.cache_file):
            return False
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for sym, items in data.items():
                    for it in items:
                        self.register_contract(
                            symbol=it.get("symbol", sym),
                            token=it.get("token", ""),
                            strike=it.get("strike", 0.0),
                            option_type=it.get("option_type", "CE"),
                            expiry=it.get("expiry", ""),
                            lot_size=it.get("lot_size", 1),
                            trading_symbol=it.get("trading_symbol", ""),
                        )
                return True
        except Exception as exc:
            logger.warning("[NFOMaster] Failed to load local cache: %s", exc)
        return False

    def save_cache(self) -> None:
        """Persist registered contracts to disk cache."""
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.contracts, f, indent=2)
        except Exception as exc:
            logger.warning("[NFOMaster] Could not save cache: %s", exc)

    def refresh_from_shoonya(self, timeout: float = 8.0) -> bool:
        """
        Download and parse NFO_symbols.txt.zip from Finvasia / Shoonya.
        Format: Exchange|Token|LotSize|Symbol|TradingSymbol|Expiry|Instrument|OptionType|StrikePrice|TickSize
        """
        try:
            req = urllib.request.Request(
                SHOONYA_NFO_SYMBOLS_URL,
                headers={"User-Agent": "TradeAI-Pro/1.0 (Feed-Worker)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()

            with zipfile.ZipFile(io.BytesIO(content)) as z:
                names = z.namelist()
                if not names:
                    return False
                with z.open(names[0]) as txt_file:
                    text_data = txt_file.read().decode("utf-8", errors="ignore")

            lines = text_data.splitlines()
            if not lines:
                return False

            count = 0
            for line in lines[1:]:  # skip header
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 9:
                    exchange = parts[0]
                    token = parts[1]
                    lot_size = int(parts[2]) if parts[2].isdigit() else 1
                    symbol = parts[3]
                    trading_symbol = parts[4]
                    expiry = parts[5]
                    instrument = parts[6]
                    opt_type = parts[7]
                    try:
                        strike = float(parts[8])
                    except ValueError:
                        continue

                    # Only index standard stock/index options (OPTIDX, OPTSTK)
                    if opt_type in ("CE", "PE") and strike > 0:
                        self.register_contract(
                            symbol=symbol,
                            token=token,
                            strike=strike,
                            option_type=opt_type,
                            expiry=expiry,
                            lot_size=lot_size,
                            trading_symbol=trading_symbol,
                        )
                        count += 1

            logger.info("[NFOMaster] Indexed %d NFO contracts across %d underlyings", count, len(self.contracts))
            self.save_cache()
            return True
        except Exception as exc:
            logger.warning("[NFOMaster] Failed to refresh from Shoonya: %s", exc)
            return False
