"""
NSE Official Bhavcopy EOD Data Ingestion Engine.
Downloads official End-of-Day settlement data directly from the National Stock Exchange of India
(nsearchives.nseindia.com) including cash settlement prices, traded volumes, and institutional
delivery metrics (DELIV_QTY, DELIV_PER).
"""
from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
BHAVCOPY_DIR = Path("data/bhavcopy")
DEFAULT_TIMEOUT_SEC = 10

# Base URL for official NSE full security bhavdata
NSE_BHAVDATA_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _clean_numeric(series: pd.Series) -> pd.Series:
    """Sanitize numeric columns removing commas, whitespace, and hyphens."""
    return pd.to_numeric(
        series.astype(str).str.replace(",", "").str.strip().replace("-", "nan"),
        errors="coerce",
    )


class NSEBhavcopyFetcher:
    """
    Downloads, parses, caches, and indexes official daily NSE Bhavcopy settlement files.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or BHAVCOPY_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache: Dict[str, pd.DataFrame] = {}

    def get_target_date_str(self, dt: datetime) -> str:
        """Format datetime to NSE URL format: DDMMYYYY (e.g. 20112026)."""
        return dt.strftime("%d%m%Y")

    def get_iso_date_str(self, dt: datetime) -> str:
        """Format datetime to ISO format YYYY-MM-DD."""
        return dt.strftime("%Y-%m-%d")

    def fetch_bhavcopy_for_date(self, target_date: datetime) -> Optional[pd.DataFrame]:
        """
        Fetch and parse Bhavcopy for a specific date.
        Checks local disk cache first, then downloads from NSE archive CDN if missing.
        """
        iso_str = self.get_iso_date_str(target_date)
        if iso_str in self._memory_cache:
            return self._memory_cache[iso_str]

        cache_file = self.cache_dir / f"sec_bhavdata_{iso_str}.parquet"
        csv_cache_file = self.cache_dir / f"sec_bhavdata_{iso_str}.csv"

        # 1. Load from disk cache if present
        if cache_file.exists():
            try:
                df = pd.read_parquet(cache_file)
                self._memory_cache[iso_str] = df
                return df
            except Exception as exc:
                logger.warning("Failed to read parquet cache %s: %s", cache_file, exc)

        if csv_cache_file.exists():
            try:
                df = pd.read_csv(csv_cache_file)
                self._memory_cache[iso_str] = df
                return df
            except Exception as exc:
                logger.warning("Failed to read CSV cache %s: %s", csv_cache_file, exc)

        # 2. Download from official NSE archive CDN
        date_str = self.get_target_date_str(target_date)
        url = NSE_BHAVDATA_URL.format(date_str=date_str)
        logger.info("[NSE Bhavcopy] Downloading official file from %s", url)

        try:
            resp = requests.get(url, headers=NSE_HEADERS, timeout=DEFAULT_TIMEOUT_SEC)
            if resp.status_code != 200 or len(resp.content) < 100:
                logger.debug("[NSE Bhavcopy] Date %s not available (HTTP %s, len=%d)", iso_str, resp.status_code, len(resp.content))
                return None

            df = self.parse_raw_csv(resp.text, target_date=target_date)
            if df is not None and not df.empty:
                # Save to disk cache
                try:
                    df.to_parquet(cache_file, index=False)
                except Exception:
                    df.to_csv(csv_cache_file, index=False)
                self._memory_cache[iso_str] = df
                logger.info("[NSE Bhavcopy] Successfully parsed %d equity symbols for date %s", len(df), iso_str)
                return df

        except Exception as exc:
            logger.warning("[NSE Bhavcopy] Network exception downloading date %s: %s", iso_str, exc)

        return None

    def fetch_latest_bhavcopy(
        self, start_date: Optional[datetime] = None, max_lookback_days: int = 7
    ) -> Tuple[Optional[pd.DataFrame], Optional[datetime]]:
        """
        Discover and load the latest available completed Bhavcopy, looking back
        up to `max_lookback_days` to skip weekends and exchange holidays.
        """
        current_dt = start_date or datetime.now(_IST)

        # If today during market hours (before 18:30 IST), today's Bhavcopy is not yet published
        if current_dt.hour < 18 or (current_dt.hour == 18 and current_dt.minute < 30):
            current_dt = current_dt - timedelta(days=1)

        for day_offset in range(max_lookback_days):
            candidate_dt = current_dt - timedelta(days=day_offset)
            # Skip Saturday and Sunday outright
            if candidate_dt.weekday() in (5, 6):
                continue

            df = self.fetch_bhavcopy_for_date(candidate_dt)
            if df is not None and not df.empty:
                logger.info(
                    "[NSE Bhavcopy] Found authoritative completed settlement for %s (%d symbols)",
                    self.get_iso_date_str(candidate_dt),
                    len(df),
                )
                return df, candidate_dt

        logger.error("[NSE Bhavcopy] No Bhavcopy found within %d days lookback", max_lookback_days)
        return None, None

    @staticmethod
    def parse_raw_csv(csv_text: str, target_date: Optional[datetime] = None) -> Optional[pd.DataFrame]:
        """
        Parse raw NSE sec_bhavdata CSV string into normalized DataFrame.
        Filters for standard Equity series (EQ).
        """
        try:
            df = pd.read_csv(io.StringIO(csv_text))
            df.columns = [c.strip().upper() for c in df.columns]

            # Standardize symbol & series
            if "SYMBOL" not in df.columns or "SERIES" not in df.columns:
                logger.error("Missing required columns in Bhavcopy CSV: %s", df.columns.tolist())
                return None

            df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
            df["SERIES"] = df["SERIES"].astype(str).str.strip().str.upper()

            # Keep only standard cash equities
            df = df[df["SERIES"] == "EQ"].copy()
            if df.empty:
                return None

            # Numeric columns
            df["prev_close"] = _clean_numeric(df["PREV_CLOSE"])
            df["open"] = _clean_numeric(df["OPEN_PRICE"])
            df["high"] = _clean_numeric(df["HIGH_PRICE"])
            df["low"] = _clean_numeric(df["LOW_PRICE"])
            df["close"] = _clean_numeric(df["CLOSE_PRICE"])
            df["last"] = _clean_numeric(df["LAST_PRICE"]) if "LAST_PRICE" in df.columns else df["close"]
            df["volume"] = _clean_numeric(df["TTL_TRD_QNTY"])
            df["turnover_lacs"] = _clean_numeric(df["TURNOVER_LACS"]) if "TURNOVER_LACS" in df.columns else 0.0
            df["delivery_qty"] = _clean_numeric(df["DELIV_QTY"]) if "DELIV_QTY" in df.columns else 0.0
            df["delivery_pct"] = _clean_numeric(df["DELIV_PER"]) if "DELIV_PER" in df.columns else 0.0

            # Fill missing delivery info
            df["delivery_qty"] = df["delivery_qty"].fillna(0.0)
            df["delivery_pct"] = df["delivery_pct"].fillna(0.0)

            # Metadata
            date_val = target_date.strftime("%Y-%m-%d") if target_date else datetime.now(_IST).strftime("%Y-%m-%d")
            df["date"] = date_val
            df["timestamp"] = pd.to_datetime(date_val + " 15:30:00+05:30")

            # Final normalized schema
            normalized = df[[
                "SYMBOL", "SERIES", "date", "timestamp", "open", "high", "low", "close",
                "prev_close", "last", "volume", "delivery_qty", "delivery_pct", "turnover_lacs"
            ]].copy()
            normalized.rename(columns={"SYMBOL": "symbol", "SERIES": "series"}, inplace=True)
            normalized.set_index("symbol", drop=False, inplace=True)
            return normalized

        except Exception as exc:
            logger.error("Error parsing Bhavcopy CSV: %s", exc)
            return None

    def get_symbol_quote(self, symbol: str, target_date: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """Retrieve single symbol settlement quote and delivery metrics."""
        clean_sym = symbol.strip().upper().replace(".NS", "").replace("-EQ", "")
        if target_date:
            df = self.fetch_bhavcopy_for_date(target_date)
        else:
            df, _ = self.fetch_latest_bhavcopy()

        if df is None or clean_sym not in df.index:
            return None

        row = df.loc[clean_sym]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]

        return row.to_dict()


# Global module singleton
bhavcopy_fetcher = NSEBhavcopyFetcher()


def get_latest_nse_bhavcopy() -> Tuple[Optional[pd.DataFrame], Optional[datetime]]:
    """Helper to get latest authoritative Bhavcopy."""
    return bhavcopy_fetcher.fetch_latest_bhavcopy()


def get_bhavcopy_symbol_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Helper to get single symbol quote with delivery metrics from Bhavcopy."""
    return bhavcopy_fetcher.get_symbol_quote(symbol)
