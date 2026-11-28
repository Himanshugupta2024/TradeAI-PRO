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
from zoneinfo import ZoneInfo

from feed.bar_builder import BarBuilder
from feed.token_master import TokenMaster

logger = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")

# Default host and websocket endpoints for Shoonya (Finvasia)
SHOONYA_HOST = "https://api.shoonya.com/NorenWClientAPI/"
SHOONYA_WS = "wss://api.shoonya.com/NorenWSAPI/"


DEFAULT_LIVE_WATCHLIST: List[str] = [
    "NIFTY 50", "NIFTY BANK", "RELIANCE", "TCS", "INFY", "HDFCBANK",
    "ICICIBANK", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "ITC",
    "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI", "TITAN", "WIPRO",
    "TATAMOTORS", "SUNPHARMA", "ULTRACEMCO", "ADANIENT", "ADANIPORTS",
    "ADANIPOWER", "ADANIGREEN", "TATASTEEL", "JSWSTEEL", "HINDALCO",
    "COALINDIA", "NTPC", "POWERGRID", "ONGC", "BPCL", "IOC", "GAIL",
    "TECHM", "HCLTECH", "DRREDDY", "CIPLA", "SWIGGY",
]


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
        nfo_master: Optional[Any] = None,
        fno_engine: Optional[Any] = None,
    ):
        self.bar_builder = bar_builder or BarBuilder()
        self.token_master = token_master or TokenMaster()
        self.watchlist = watchlist or list(DEFAULT_LIVE_WATCHLIST)
        self.nfo_master = nfo_master
        self.fno_engine = fno_engine

        # Credentials
        self.uid = os.environ.get("SHOONYA_USERID", "").strip()
        self.password = os.environ.get("SHOONYA_PASSWORD", "").strip()
        self.api_key = os.environ.get("SHOONYA_API_KEY", "").strip()
        self.vendor_code = os.environ.get("SHOONYA_VENDOR_CODE", "").strip()
        self.totp_secret = os.environ.get("SHOONYA_TOTP_SECRET", "").strip()
        self.imei = os.environ.get("SHOONYA_IMEI", "tradeai-feed-01").strip()
        self.usertoken = os.environ.get("SHOONYA_USERTOKEN", "").strip()

        # State
        self.is_connected = False
        self.is_simulated = False
        self.last_login_time: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.api = None
        self._sim_task = None
        self._sim_stop_event = threading.Event()
        self._stop_event = threading.Event()
        self._reconnect_lock = threading.Lock()
        self._reconnecting = False
        self._reconnect_attempts = 0
        self._watchdog_started = False
        self._fno_rotator_started = False
        self._fno_lock = threading.Lock()
        self._fno_last_refresh: Dict[str, float] = {}

    def has_credentials(self) -> bool:
        """Check if all required Shoonya credentials are present."""
        return bool(
            self.uid and self.password and self.api_key and self.vendor_code and self.totp_secret
        )

    def login(self) -> bool:
        """Authenticate with Shoonya QuickAuth and preserve exact broker rejection emsg."""
        if not self.has_credentials():
            logger.warning("[Shoonya] Missing credentials — switching to simulated tick mode")
            self.is_simulated = True
            return False

        try:
            import json
            import pyotp
            import requests
            from NorenRestApiPy.NorenApi import NorenApi

            self.api = NorenApi(host=SHOONYA_HOST, websocket=SHOONYA_WS)

            totp_clean = "".join((self.totp_secret or "").split())
            if totp_clean.isdigit():
                if len(totp_clean) != 6:
                    self.last_error = "Invalid 2FA: enter a 6-digit OTP or Base32 TOTP secret"
                    logger.error("[Shoonya] %s", self.last_error)
                    self.is_simulated = True
                    return False
                totp = totp_clean
            else:
                totp = pyotp.TOTP(totp_clean.upper()).now()

            logger.info("[Shoonya] Attempting login for user: %s (vc: %s)", self.uid, self.vendor_code)
            pwd_hash = hashlib.sha256(self.password.encode("utf-8")).hexdigest()
            app_hash = hashlib.sha256(f"{self.uid}|{self.api_key}".encode("utf-8")).hexdigest()
            values = {
                "source": "API",
                "apkversion": "1.0.0",
                "uid": self.uid,
                "pwd": pwd_hash,
                "factor2": totp,
                "vc": self.vendor_code,
                "appkey": app_hash,
                "imei": self.imei,
            }
            auth_url = f"{SHOONYA_HOST.rstrip('/')}/QuickAuth"
            resp = requests.post(auth_url, data=f"jData={json.dumps(values)}", timeout=15)

            res = None
            try:
                res = resp.json()
            except Exception:
                res = None

            if isinstance(res, dict) and res.get("stat") == "Ok" and res.get("susertoken"):
                self.api.set_session(
                    userid=self.uid,
                    password=self.password,
                    usertoken=res["susertoken"],
                )
                self.usertoken = str(res["susertoken"])
                self.last_login_time = datetime.now(timezone.utc)
                self.is_simulated = False
                self.last_error = None
                logger.info("[Shoonya] Login successful. Session token acquired.")
                return True

            emsg = ""
            if isinstance(res, dict):
                emsg = str(res.get("emsg") or res.get("message") or "").strip()
            if not emsg:
                snippet = (getattr(resp, "text", "") or "").strip()[:160]
                emsg = f"Shoonya HTTP {resp.status_code}: {snippet}" if snippet else f"Shoonya HTTP {resp.status_code} login rejected"

            self.last_error = emsg
            logger.error("[Shoonya] Login failed response (HTTP %s): %s", resp.status_code, emsg)
            self.is_simulated = True
            return False

        except Exception as exc:
            self.last_error = str(exc)
            logger.error("[Shoonya] Exception during login: %s", exc)
            self.is_simulated = True
            return False

    def masked_uid(self) -> str:
        uid = (self.uid or "").strip()
        if not uid:
            return ""
        if len(uid) <= 4:
            return uid[0] + "*" * (len(uid) - 1)
        return uid[:2] + "*" * (len(uid) - 4) + uid[-2:]

    def stop_simulation(self) -> None:
        """Stop the synthetic tick thread cleanly when switching to a live broker session."""
        self._sim_stop_event.set()
        self.is_simulated = False

    @staticmethod
    def _extract_auth_code(raw: str) -> str:
        """Extract raw OAuth authorization code from a query token or full redirect URL."""
        text = (raw or "").strip()
        if not text:
            return ""
        if "code=" in text:
            try:
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(text)
                qs = parse_qs(parsed.query)
                if qs.get("code") and qs["code"][0].strip():
                    return qs["code"][0].strip()
                # Handle bare query fragments such as "?code=xyz" or "code=xyz&state=..."
                raw_qs = parse_qs(text.lstrip("?"))
                if raw_qs.get("code") and raw_qs["code"][0].strip():
                    return raw_qs["code"][0].strip()
            except Exception:
                pass
        return text

    def login_interactive(
        self,
        *,
        userid: str = "",
        password: str = "",
        api_key: str = "",
        vendor_code: str = "",
        totp: str = "",
        usertoken: str = "",
        auth_code: str = "",
    ) -> tuple[bool, str]:
        """Apply operator-supplied credentials/OAuth/OTP/token in memory and start live Shoonya WS."""
        if userid.strip():
            self.uid = userid.strip()
        if password.strip():
            self.password = password.strip()
        if api_key.strip():
            self.api_key = api_key.strip()
        if vendor_code.strip():
            self.vendor_code = vendor_code.strip()
        if totp.strip():
            self.totp_secret = totp.strip()
        if not self.uid and self.vendor_code.upper().endswith("_U"):
            self.uid = self.vendor_code[:-2]

        self.last_error = None
        logged_in = False
        if usertoken.strip():
            logged_in = self.login_with_token(usertoken.strip())
        elif auth_code.strip():
            clean_code = self._extract_auth_code(auth_code)
            if not self.uid or not self.api_key:
                missing = [k for k, v in (("userid", self.uid), ("api_key (Secret Code)", self.api_key)) if not v]
                msg = f"Missing required Shoonya OAuth fields: {', '.join(missing)}"
                self.last_error = msg
                return False, msg
            token = self.exchange_auth_code(clean_code)
            if not token:
                return False, self.last_error or "Shoonya OAuth GenAcsTok exchange failed"
            logged_in = self.login_with_token(token)
        else:
            if not self.has_credentials():
                missing = [
                    name
                    for name, val in (
                        ("userid", self.uid),
                        ("password", self.password),
                        ("api_key", self.api_key),
                        ("vendor_code", self.vendor_code),
                        ("totp", self.totp_secret),
                    )
                    if not val
                ]
                msg = f"Missing required Shoonya fields: {', '.join(missing)}"
                self.last_error = msg
                return False, msg
            logged_in = self.login()

        if not logged_in or self.api is None:
            return False, self.last_error or "Shoonya authentication failed"

        self.stop_simulation()
        self.stop_ws_only()
        self._reconnect_attempts = 0
        self._start_websocket()
        if self.has_credentials():
            self._start_session_watchdog()
        self._start_fno_rotator()
        return True, f"Shoonya live session active for {self.masked_uid() or 'operator'}"

    def login_with_token(self, usertoken: str) -> bool:
        """Authenticate using an active 24-hour Shoonya user token."""
        try:
            from NorenRestApiPy.NorenApi import NorenApi

            self.api = NorenApi(host=SHOONYA_HOST, websocket=SHOONYA_WS)
            uid_val = self.uid or (self.vendor_code[:-2] if self.vendor_code.upper().endswith("_U") else self.vendor_code)
            self.api.set_session(userid=uid_val, password=self.password, usertoken=usertoken)
            self.usertoken = usertoken
            self.last_login_time = datetime.now(timezone.utc)
            self.is_simulated = False
            self.last_error = None
            logger.info("[Shoonya] User session token set successfully for user: %s", uid_val)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("[Shoonya] Failed to set user token: %s", exc)
            return False

    def exchange_auth_code(self, auth_code: str) -> Optional[str]:
        """Exchange OAuth auth_code from browser redirect for 24-hour susertoken."""
        import json
        import requests

        code_clean = self._extract_auth_code(auth_code)
        if not code_clean:
            self.last_error = "Missing Shoonya OAuth authorization code"
            return None

        client_id = self.vendor_code or f"{self.uid}_U"
        uid_val = self.uid or (client_id[:-2] if client_id.upper().endswith("_U") else client_id)
        raw_str = f"{client_id}{self.api_key}{code_clean}"
        checksum = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

        url = f"{SHOONYA_HOST.rstrip('/')}/GenAcsTok"
        payload = {
            "uid": uid_val,
            "code": code_clean,
            "appkey": checksum,
        }
        try:
            res = requests.post(url, data={"jData": json.dumps(payload)}, timeout=15)
            data = None
            try:
                data = res.json()
            except Exception:
                data = None

            if res.status_code == 200 and isinstance(data, dict) and data.get("stat") == "Ok" and data.get("susertoken"):
                self.usertoken = str(data["susertoken"])
                self.last_error = None
                logger.info("[Shoonya] Successfully exchanged OAuth auth_code for susertoken")
                return self.usertoken

            emsg = ""
            if isinstance(data, dict):
                emsg = str(data.get("emsg") or data.get("message") or "").strip()
            if not emsg:
                snippet = (getattr(res, "text", "") or "").strip()[:160]
                emsg = f"GenAcsTok HTTP {res.status_code}: {snippet}" if snippet else f"GenAcsTok HTTP {res.status_code} failed"
            self.last_error = emsg
            logger.error("[Shoonya] OAuth exchange failed (HTTP %s): %s", res.status_code, emsg)
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("[Shoonya] Exception during OAuth exchange: %s", exc)
        return None

    def start(self) -> None:
        """Start the feed service (WebSocket or simulation)."""
        logged_in = False
        if self.usertoken:
            logged_in = self.login_with_token(self.usertoken)
        # Fall back to automated TOTP login whenever credentials exist and usertoken is missing/invalid.
        if not logged_in and self.has_credentials():
            logged_in = self.login()

        if logged_in and self.api is not None:
            self._start_websocket()
            self._start_fno_rotator()
        else:
            self._start_simulation()

        if self.has_credentials():
            self._start_session_watchdog()

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
            if self.has_credentials():
                self._schedule_reconnect()
            else:
                self._start_simulation()

    def _on_open(self) -> None:
        """Callback when WebSocket connection opens successfully."""
        self.is_connected = True
        self.is_simulated = False
        self._reconnect_attempts = 0
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
        if self._stop_event.is_set():
            return
        logger.warning("[Shoonya] WebSocket closed. Scheduling automated reconnect...")
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Spawn a deduplicated daemon reconnect loop with exponential backoff and TOTP re-login."""
        if self._stop_event.is_set() or (not self.has_credentials() and not self.usertoken):
            return

        with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True

        def _reconnect_worker():
            backoffs = (2.0, 5.0, 15.0, 30.0, 60.0)
            try:
                while not self._stop_event.is_set() and not self.is_connected:
                    delay = backoffs[min(self._reconnect_attempts, len(backoffs) - 1)]
                    self._reconnect_attempts += 1
                    logger.info("[Shoonya] Reconnect attempt #%d in %.1fs...", self._reconnect_attempts, delay)
                    if self._stop_event.wait(delay):
                        return

                    # Re-authenticate via TOTP whenever session may be missing, old, or repeated WS closes occurred
                    need_fresh_login = (
                        self.api is None
                        or self._reconnect_attempts >= 2
                        or self.last_login_time is None
                        or (datetime.now(timezone.utc) - self.last_login_time).total_seconds() > 36000
                    )
                    ok = True
                    if need_fresh_login and self.has_credentials():
                        ok = self.login()
                    elif need_fresh_login and self.usertoken:
                        ok = self.login_with_token(self.usertoken)

                    if ok and self.api is not None:
                        try:
                            self.api.start_websocket(
                                subscribe_callback=self._on_message,
                                socket_open_callback=self._on_open,
                                socket_close_callback=self._on_close,
                                socket_error_callback=self._on_error,
                            )
                            # Give socket_open_callback a moment to fire before deciding to retry
                            if self._stop_event.wait(3.0):
                                return
                            if self.is_connected:
                                logger.info("[Shoonya] Live WebSocket reconnected cleanly.")
                                return
                        except Exception as exc:
                            logger.warning("[Shoonya] Reconnect WebSocket start failed: %s", exc)
            finally:
                with self._reconnect_lock:
                    self._reconnecting = False

        t = threading.Thread(target=_reconnect_worker, daemon=True, name="ShoonyaReconnect")
        t.start()

    def _needs_daily_relogin(self, now_utc: Optional[datetime] = None) -> bool:
        """Return True on weekdays from 08:45 IST onward if session was acquired on an earlier IST day."""
        if not self.has_credentials():
            return False
        now_u = now_utc or datetime.now(timezone.utc)
        now_ist = now_u.astimezone(_IST)
        if now_ist.weekday() >= 5:
            return False
        if (now_ist.hour, now_ist.minute) < (8, 45):
            return False
        if self.last_login_time is None:
            return True
        last_ist = self.last_login_time.astimezone(_IST)
        return last_ist.date() < now_ist.date()

    def _start_session_watchdog(self) -> None:
        """Daily morning TOTP refresh and stalled-feed recovery watchdog."""
        if self._watchdog_started:
            return
        self._watchdog_started = True

        def _watchdog_loop():
            while not self._stop_event.wait(60.0):
                try:
                    if self._needs_daily_relogin():
                        logger.info("[Shoonya] Performing daily IST pre-market TOTP session refresh...")
                        self.stop_ws_only()
                        if self.login() and self.api is not None:
                            self._start_websocket()
                    elif self.has_credentials() and (self.is_simulated or not self.is_connected):
                        self._schedule_reconnect()
                except Exception as exc:
                    logger.debug("[Shoonya] Watchdog tick error: %s", exc)

        t = threading.Thread(target=_watchdog_loop, daemon=True, name="ShoonyaSessionWatchdog")
        t.start()

    def stop_ws_only(self) -> None:
        """Close active WebSocket without tripping permanent shutdown stop_event."""
        if self.api and self.is_connected:
            try:
                self.api.close_websocket()
            except Exception:
                pass
        self.is_connected = False

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

    def refresh_fno_underlying(self, symbol: str, spot: float = 0.0, count: int = 10) -> bool:
        """Fetch near-expiry CE/PE option chain from Shoonya REST and populate FNOAnalyticsEngine."""
        if not self.api or self.is_simulated or not self.nfo_master or not self.fno_engine:
            return False

        from feed.nfo_master import normalize_nfo_underlying

        canonical = symbol.strip().upper().replace(".NS", "")
        nfo_sym = normalize_nfo_underlying(canonical)
        expiry = self.nfo_master.get_nearest_expiry(nfo_sym)
        if not expiry:
            return False

        resolved_spot = float(spot or 0.0)
        if resolved_spot <= 0:
            snap = self.bar_builder.get_snapshot([canonical, nfo_sym])
            quotes = (snap or {}).get("quotes") or {}
            q_row = quotes.get(canonical) or quotes.get(nfo_sym) or {}
            resolved_spot = float(q_row.get("lp") or 0.0)

        atm_slice = self.nfo_master.get_strike_slice(nfo_sym, spot=resolved_spot, half_width=1, expiry=expiry)
        if not atm_slice:
            return False

        ref_contract = atm_slice[0]
        rows: list = []
        with self._fno_lock:
            try:
                res = self.api.get_option_chain(
                    "NFO",
                    str(ref_contract.get("trading_symbol") or ""),
                    str(ref_contract.get("strike") or ""),
                    count=max(2, int(count)),
                )
                if isinstance(res, dict) and res.get("stat") == "Ok" and isinstance(res.get("values"), list):
                    rows = res["values"]
            except Exception as exc:
                logger.debug("[Shoonya] get_option_chain failed for %s: %s", nfo_sym, exc)

            # Fallback to slice get_quotes if get_option_chain omitted values
            if not rows and hasattr(self.api, "get_quotes"):
                broad_slice = self.nfo_master.get_strike_slice(nfo_sym, spot=resolved_spot, half_width=5, expiry=expiry)
                for contract in broad_slice:
                    tok = str(contract.get("token") or "")
                    if not tok:
                        continue
                    try:
                        q = self.api.get_quotes("NFO", tok)
                        if isinstance(q, dict) and q.get("stat") == "Ok":
                            rows.append({
                                "tsym": contract.get("trading_symbol"),
                                "strprc": contract.get("strike"),
                                "optt": contract.get("option_type"),
                                "lp": q.get("lp"),
                                "oi": q.get("oi"),
                                "v": q.get("v"),
                            })
                    except Exception:
                        continue

        if not rows:
            return False

        chain = self.fno_engine.get_or_create_chain(canonical)
        updated = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            tsym = str(row.get("tsym") or "").upper()
            optt = str(row.get("optt") or "").upper()
            if optt not in ("CE", "PE"):
                if "CE" in tsym:
                    optt = "CE"
                elif "PE" in tsym:
                    optt = "PE"
                else:
                    continue
            try:
                strike = float(row.get("strprc") or row.get("strike") or 0.0)
            except (TypeError, ValueError):
                continue
            if strike <= 0:
                continue
            try:
                ltp = float(row.get("lp") or 0.0)
            except (TypeError, ValueError):
                ltp = 0.0
            try:
                oi = int(float(row.get("oi") or 0))
            except (TypeError, ValueError):
                oi = 0
            try:
                vol = int(float(row.get("v") or 0))
            except (TypeError, ValueError):
                vol = 0
            chain.update_by_strike(
                strike=strike,
                opt_type=optt.lower(),
                ltp=ltp if ltp > 0 else None,
                oi=oi if oi >= 0 else None,
                volume=vol if vol >= 0 else None,
            )
            updated += 1

        if updated > 0:
            self.fno_engine.mark_updated(canonical)
            self._fno_last_refresh[canonical] = time.time()
            return True
        return False

    def _start_fno_rotator(self) -> None:
        """Rotate across indexed NFO underlyings during NSE session with polite REST pacing."""
        if self._fno_rotator_started or not self.nfo_master or not self.fno_engine:
            return
        self._fno_rotator_started = True

        def _rotator_loop():
            idx = 0
            priority_map = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
            while not self._stop_event.wait(2.0):
                try:
                    if not self.api or self.is_simulated or not self.is_connected:
                        continue
                    now_ist = datetime.now(_IST)
                    if now_ist.weekday() >= 5 or not ((9, 10) <= (now_ist.hour, now_ist.minute) <= (15, 35)):
                        continue
                    underlyings = self.nfo_master.list_underlyings()
                    if not underlyings:
                        continue
                    raw_u = underlyings[idx % len(underlyings)]
                    idx += 1
                    sym = priority_map.get(raw_u, raw_u)
                    last_ts = self._fno_last_refresh.get(sym, 0.0)
                    if time.time() - last_ts < 90.0:
                        continue
                    self.refresh_fno_underlying(sym, count=10)
                except Exception as exc:
                    logger.debug("[Shoonya] NFO rotator step error: %s", exc)

        t = threading.Thread(target=_rotator_loop, daemon=True, name="ShoonyaFnoRotator")
        t.start()

    def _start_simulation(self) -> None:
        """
        Background simulation loop when credentials are not configured or during testing.
        Generates realistic ticks around anchor prices.
        """
        self._sim_stop_event.clear()
        self.is_simulated = True
        self.is_connected = True
        logger.info("[Shoonya] Starting simulated tick generator for %d symbols", len(self.watchlist))

        import random

        anchors = {
            "NIFTY 50": 25885.0,
            "NIFTY BANK": 53000.0,
            "RELIANCE": 1396.0,
            "TCS": 2640.0,
            "INFY": 1183.0,
            "HDFCBANK": 862.0,
            "ICICIBANK": 1290.0,
            "SBIN": 840.0,
            "BHARTIARTL": 1620.0,
            "ITC": 480.0,
            "KOTAKBANK": 1760.0,
            "LT": 3600.0,
        }

        def _sim_loop():
            # Seed initial quotes
            day_vol = {sym: 50000.0 for sym in self.watchlist}
            cur_price = {sym: anchors.get(sym, 1000.0) for sym in self.watchlist}

            while not self._stop_event.is_set() and not self._sim_stop_event.is_set():
                for sym in self.watchlist:
                    anchor = anchors.get(sym, 1000.0)
                    p = cur_price[sym]
                    # Zero-drift mean-reverting random walk clamped to ±3% of anchor
                    mean_reversion = (anchor - p) * 0.02
                    noise = (random.random() - 0.5) * (p * 0.001)
                    p = round(max(anchor * 0.97, min(anchor * 1.03, p + noise + mean_reversion)), 2)
                    cur_price[sym] = p

                    added_vol = random.randint(10, 250)
                    day_vol[sym] += added_vol

                    self.bar_builder.on_tick(
                        symbol=sym,
                        price=p,
                        volume=day_vol[sym],
                        prev_close=anchor,
                    )

                time.sleep(2.0)

        t = threading.Thread(target=_sim_loop, daemon=True, name="ShoonyaSimulatedFeed")
        t.start()
