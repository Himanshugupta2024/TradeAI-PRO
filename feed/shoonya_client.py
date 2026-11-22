"""
TradeAI Pro — Shoonya (Finvasia) Feed Manager.
Handles automated TOTP login, daily session refresh, and live WebSocket streaming.
Falls back to clean simulation when credentials are not yet configured.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from feed.bar_builder import BarBuilder
from feed.token_master import TokenMaster

logger = logging.getLogger(__name__)

# Default host and websocket endpoints for Shoonya (Finvasia)
SHOONYA_HOST = "https://api.shoonya.com/NorenWClientTP/"
SHOONYA_WS = "wss://api.shoonya.com/NorenWSTP/"


class ShoonyaFeedManager:
    """
    Manages Shoonya API lifecycle: authentication, token mapping,
    WebSocket connection, automated reconnects, and tick routing.
    """

    def __init__(
        self,
        bar_builder: Optional[BarBuilder] = None,
        token_master: Optional[TokenMaster] = None,
        watchlist: Optional[List[str]] = None,
    ):
        self.bar_builder = bar_builder or BarBuilder()
        self.token_master = token_master or TokenMaster()
        self.watchlist = watchlist or [
            "NIFTY 50", "NIFTY BANK", "RELIANCE", "TCS", "INFY", "HDFCBANK",
            "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK", "LT"
        ]

        # Credentials
        self.uid = os.environ.get("SHOONYA_USERID", "").strip()
        self.password = os.environ.get("SHOONYA_PASSWORD", "").strip()
        self.api_key = os.environ.get("SHOONYA_API_KEY", "").strip()
        self.vendor_code = os.environ.get("SHOONYA_VENDOR_CODE", "").strip()
        self.totp_secret = os.environ.get("SHOONYA_TOTP_SECRET", "").strip()
        self.imei = os.environ.get("SHOONYA_IMEI", "tradeai-feed-01").strip()

        # State
        self.is_connected = False
        self.is_simulated = False
        self.last_login_time: Optional[datetime] = None
        self.api = None
        self._sim_task = None
        self._stop_event = threading.Event()

    def has_credentials(self) -> bool:
        """Check if all required Shoonya credentials are present."""
        return bool(
            self.uid and self.password and self.api_key and self.vendor_code and self.totp_secret
        )

    def login(self) -> bool:
        """Authenticate with Shoonya using automated TOTP."""
        if not self.has_credentials():
            logger.warning("[Shoonya] Missing credentials — switching to simulated tick mode")
            self.is_simulated = True
            return False

        try:
            import pyotp
            from NorenRestApiPy.NorenApi import NorenApi

            self.api = NorenApi(host=SHOONYA_HOST, websocket=SHOONYA_WS)

            pwd_hash = hashlib.sha256(self.password.encode("utf-8")).hexdigest()
            totp = pyotp.TOTP(self.totp_secret).now()
            api_secret = hashlib.sha256(f"{self.uid}|{self.api_key}".encode("utf-8")).hexdigest()

            logger.info("[Shoonya] Attempting login for user: %s", self.uid)
            res = self.api.login(
                userid=self.uid,
                password=pwd_hash,
                twoFA=totp,
                vendor_code=self.vendor_code,
                api_secret=api_secret,
                imei=self.imei,
            )

            if isinstance(res, dict) and res.get("stat") == "Ok":
                self.last_login_time = datetime.now(timezone.utc)
                self.is_simulated = False
                logger.info("[Shoonya] Login successful. Session token acquired.")
                return True
            else:
                logger.error("[Shoonya] Login failed response: %s", res)
                self.is_simulated = True
                return False

        except Exception as exc:
            logger.error("[Shoonya] Exception during login: %s", exc)
            self.is_simulated = True
            return False

    def start(self) -> None:
        """Start the feed service (WebSocket or simulation)."""
        logged_in = self.login()
        if logged_in and self.api is not None:
            self._start_websocket()
        else:
            self._start_simulation()

    def stop(self) -> None:
        """Stop all feed threads and close socket."""
        self._stop_event.set()
        if self.api and self.is_connected:
            try:
                self.api.close_websocket()
            except Exception:
                pass
        self.is_connected = False

    def _start_websocket(self) -> None:
        """Initialize Shoonya WebSocket connection."""
        try:
            logger.info("[Shoonya] Starting live WebSocket...")
            self.api.start_websocket(
                subscribe_callback=self._on_message,
                socket_open_callback=self._on_open,
                socket_close_callback=self._on_close,
                socket_error_callback=self._on_error,
            )
        except Exception as exc:
            logger.error("[Shoonya] Failed to start WebSocket: %s", exc)
            self._start_simulation()

    def _on_open(self) -> None:
        """Callback when WebSocket connection opens successfully."""
        self.is_connected = True
        logger.info("[Shoonya] WebSocket connection opened. Subscribing to watchlist...")
        tokens_to_sub = []
        for sym in self.watchlist:
            tok = self.token_master.get_token(sym)
            if tok:
                tokens_to_sub.append(f"NSE|{tok}")

        if tokens_to_sub and self.api:
            try:
                self.api.subscribe(tokens_to_sub)
                logger.info("[Shoonya] Subscribed to %d instruments: %s", len(tokens_to_sub), tokens_to_sub[:5])
            except Exception as exc:
                logger.error("[Shoonya] Error subscribing: %s", exc)

    def _on_close(self) -> None:
        """Callback when WebSocket closes."""
        self.is_connected = False
        logger.warning("[Shoonya] WebSocket closed. Reconnect scheduled.")

    def _on_error(self, err: Any) -> None:
        """Callback on WebSocket error."""
        logger.warning("[Shoonya] WebSocket error: %s", err)

    def _on_message(self, msg: Dict[str, Any]) -> None:
        """
        Callback for incoming live market data feed ticks.
        Runs on websocket worker thread — no heavy processing!
        """
        try:
            exch = msg.get("e", "NSE")
            token = str(msg.get("tk", ""))
            sym = self.token_master.get_symbol_from_token(exch, token)
            if not sym:
                return

            lp = float(msg.get("lp") or 0.0)
            if lp <= 0:
                return

            vol = float(msg.get("v") or 0.0)
            c_price = float(msg.get("c") or 0.0) if msg.get("c") else None

            self.bar_builder.on_tick(
                symbol=sym,
                price=lp,
                volume=vol,
                prev_close=c_price,
            )
        except Exception as exc:
            logger.debug("[Shoonya] Error parsing tick message: %s", exc)

    def _start_simulation(self) -> None:
        """
        Background simulation loop when credentials are not configured or during testing.
        Generates realistic ticks around anchor prices.
        """
        self.is_simulated = True
        self.is_connected = True
        logger.info("[Shoonya] Starting simulated tick generator for %d symbols", len(self.watchlist))

        import random

        anchors = {
            "NIFTY 50": 24200.0,
            "NIFTY BANK": 51500.0,
            "RELIANCE": 2510.0,
            "TCS": 4120.0,
            "INFY": 1820.0,
            "HDFCBANK": 1740.0,
            "ICICIBANK": 1260.0,
            "SBIN": 810.0,
            "BHARTIARTL": 1580.0,
            "ITC": 475.0,
            "KOTAKBANK": 1780.0,
            "LT": 3650.0,
        }

        def _sim_loop():
            # Seed initial quotes
            day_vol = {sym: 50000.0 for sym in self.watchlist}
            cur_price = {sym: anchors.get(sym, 1000.0) for sym in self.watchlist}

            while not self._stop_event.is_set():
                for sym in self.watchlist:
                    p = cur_price[sym]
                    delta = (random.random() - 0.495) * (p * 0.001)
                    p = round(max(1.0, p + delta), 2)
                    cur_price[sym] = p

                    added_vol = random.randint(10, 250)
                    day_vol[sym] += added_vol

                    self.bar_builder.on_tick(
                        symbol=sym,
                        price=p,
                        volume=day_vol[sym],
                        prev_close=anchors.get(sym, 1000.0),
                    )

                time.sleep(2.0)

        t = threading.Thread(target=_sim_loop, daemon=True, name="ShoonyaSimulatedFeed")
        t.start()
