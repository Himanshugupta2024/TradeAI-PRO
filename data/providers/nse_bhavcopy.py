"""
TradeAI-PRO V2 — Official NSE UDiFF Bhavcopy Provider.

Fetches official end-of-day (EOD) market data directly from NSE India archives.
- 100% keyless, free, and authoritative.
- Covers entire NSE universe (2,000+ equities, Series EQ) in a single ~1.5MB zip.
- Automatically backtracks on weekends and exchange holidays to the latest active trading day.
- Streams and filters columns in-memory to keep RAM bounded below 15MB.
"""
from __future__ import annotations

import io
import logging
import os
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Base cache directory for daily bhavcopies
_CACHE_DIR = Path(".cache") / "bhavcopy"


class NSEBhavcopyProvider:
    """Official NSE UDiFF daily bhavcopy fetcher and parser."""

    name: str = "nse_bhavcopy"

    # NSE Archive UDiFF URL template (YYYYMMDD)
    # e.g. BhavCopy_NSE_CM_0_0_0_20261030_F_0000.csv.zip
    URL_TEMPLATE = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip"
    FALLBACK_URL_TEMPLATE = "https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip"

    HTTP_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.nseindia.com/",
    }

    def __init__(self, cache_dir: Path | None = None, request_timeout: float = 12.0):
        self.cache_dir = cache_dir or _CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_timeout = request_timeout

    def _get_cache_path(self, d: date) -> Path:
        return self.cache_dir / f"bhavcopy_{d.strftime('%Y%m%d')}.csv.gz"

    def fetch_daily_bhavcopy(
        self,
        target_date: date | None = None,
        max_backtrack_days: int = 5,
    ) -> pd.DataFrame:
        """
        Fetch official NSE EOD bhavcopy DataFrame for target_date.
        If target_date falls on a weekend or holiday, backtracks up to max_backtrack_days
        until an official active trading day bhavcopy is found.
        """
        cur_date = target_date or date.today()

        for attempt in range(max_backtrack_days + 1):
            check_date = cur_date - timedelta(days=attempt)
            # Skip weekends upfront
            if check_date.weekday() >= 5:  # 5=Saturday, 6=Sunday
                continue

            cache_file = self._get_cache_path(check_date)
            if cache_file.exists():
                try:
                    df = pd.read_csv(cache_file, compression="gzip")
                    logger.debug("[NSEBhavcopy] Loaded %s from local cache (%d rows)", check_date, len(df))
                    return df
                except Exception as read_err:
                    logger.warning("[NSEBhavcopy] Corrupt cache file %s, refetching: %s", cache_file, read_err)
                    cache_file.unlink(missing_ok=True)

            # Attempt network download
            df = self._download_and_parse(check_date)
            if df is not None and not df.empty:
                # Save to local cache
                try:
                    df.to_csv(cache_file, index=False, compression="gzip")
                except Exception as save_err:
                    logger.debug("[NSEBhavcopy] Cache write failed for %s: %s", check_date, save_err)
                return df

        logger.error("[NSEBhavcopy] Failed to locate valid bhavcopy within %d backtrack days from %s", max_backtrack_days, cur_date)
        return pd.DataFrame()

    def _download_and_parse(self, check_date: date) -> pd.DataFrame | None:
        """Stream and parse the UDiFF zip file for check_date."""
        date_str = check_date.strftime("%Y%m%d")
        urls = [
            self.URL_TEMPLATE.format(date_str=date_str),
            self.FALLBACK_URL_TEMPLATE.format(date_str=date_str),
        ]

        for url in urls:
            try:
                resp = requests.get(url, headers=self.HTTP_HEADERS, timeout=self.request_timeout)
                if resp.status_code == 404:
                    logger.debug("[NSEBhavcopy] 404 Not Found for %s at %s", check_date, url)
                    continue
                if resp.status_code != 200 or len(resp.content) < 50:
                    continue

                # Stream-decompress zip file in-memory
                with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                    csv_names = [n for n in z.namelist() if n.endswith(".csv")]
                    if not csv_names:
                        continue

                    # Read only required columns to strictly bound memory <15MB
                    # UDiFF column names:
                    # TradDt, SctySrs, TckrSymb, OpnPric, HghPric, LwPric, ClsPric, PrvsClsgPric, TtlTradgVol, TtlTrfVal
                    with z.open(csv_names[0]) as csv_file:
                        df = pd.read_csv(
                            csv_file,
                            usecols=lambda c: c in (
                                "TradDt", "SctySrs", "TckrSymb",
                                "OpnPric", "HghPric", "LwPric", "ClsPric",
                                "PrvsClsgPric", "TtlTradgVol", "TtlTrfVal",
                            ),
                            dtype={
                                "TckrSymb": "str",
                                "SctySrs": "str",
                                "OpnPric": "float64",
                                "HghPric": "float64",
                                "LwPric": "float64",
                                "ClsPric": "float64",
                                "PrvsClsgPric": "float64",
                                "TtlTradgVol": "float64",
                                "TtlTrfVal": "float64",
                            },
                        )

                        # Filter strictly for Series 'EQ' (Standard mainline equities)
                        if "SctySrs" in df.columns:
                            df = df[df["SctySrs"] == "EQ"].copy()

                        # Rename to standard internal schema
                        df = df.rename(columns={
                            "TradDt": "date",
                            "TckrSymb": "symbol",
                            "OpnPric": "open",
                            "HghPric": "high",
                            "LwPric": "low",
                            "ClsPric": "close",
                            "PrvsClsgPric": "prev_close",
                            "TtlTradgVol": "volume",
                            "TtlTrfVal": "turnover",
                        })

                        # Basic sanity check
                        df = df.dropna(subset=["symbol", "close"])
                        df = df[(df["close"] > 0) & (df["high"] >= df["low"]) & (df["low"] > 0)]
                        logger.info("[NSEBhavcopy] Successfully parsed %s: %d EQ symbols", check_date, len(df))
                        return df

            except Exception as exc:
                logger.debug("[NSEBhavcopy] Download attempt failed for %s (%s): %s", check_date, url, exc)
                continue

        return None

    def get_symbol_eod(self, symbol: str, target_date: date | None = None) -> dict[str, Any] | None:
        """Fetch single symbol EOD data from the latest bhavcopy."""
        df = self.fetch_daily_bhavcopy(target_date=target_date)
        if df.empty:
            return None

        clean_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
        row = df[df["symbol"] == clean_sym]
        if row.empty:
            return None

        item = row.iloc[0]
        return {
            "symbol": clean_sym,
            "date": str(item.get("date")),
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
            "prev_close": float(item.get("prev_close", item["close"])),
            "volume": float(item.get("volume", 0)),
            "turnover": float(item.get("turnover", 0)),
            "change_pct": round(
                ((float(item["close"]) - float(item.get("prev_close", item["close"]))) /
                 max(1e-6, float(item.get("prev_close", item["close"])))) * 100.0,
                2,
            ),
        }

    def get_batch_eod(self, symbols: list[str], target_date: date | None = None) -> dict[str, dict[str, Any]]:
        """Fetch multiple symbols in one vectorized dataframe lookup."""
        if not symbols:
            return {}

        df = self.fetch_daily_bhavcopy(target_date=target_date)
        if df.empty:
            return {}

        clean_map = {s.replace(".NS", "").replace(".BO", "").strip().upper(): s for s in symbols}
        matched = df[df["symbol"].isin(clean_map.keys())]

        results: dict[str, dict[str, Any]] = {}
        for _, row in matched.iterrows():
            sym = str(row["symbol"])
            orig_sym = clean_map.get(sym, sym)
            prev_c = float(row.get("prev_close", row["close"]))
            close_c = float(row["close"])
            results[orig_sym] = {
                "symbol": sym,
                "date": str(row.get("date")),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": close_c,
                "prev_close": prev_c,
                "volume": float(row.get("volume", 0)),
                "turnover": float(row.get("turnover", 0)),
                "change_pct": round(((close_c - prev_c) / max(1e-6, prev_c)) * 100.0, 2),
            }

        return results
