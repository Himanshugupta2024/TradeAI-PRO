import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
try:
    import pandas_ta as ta
except Exception:  # pragma: no cover - runtime environment dependency guard
    ta = None
import yfinance as yf
from config.logger import get_logger
from db.connection import SessionLocal
from db.models import MorningScan

log = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MORNING_SCAN_HOUR = 9
MORNING_SCAN_MINUTE = 0

TOP_200_NSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", "HUL.NS", "ITC.NS", "SBIN.NS",
    "BHARTIARTL.NS", "BAJFINANCE.NS", "KOTAKBANK.NS", "LT.NS", "AXISBANK.NS", "ASIANPAINT.NS", "DMART.NS",
    "MARUTI.NS", "SUNPHARMA.NS", "BAJAJFINSV.NS", "TITAN.NS", "ULTRACEMCO.NS", "WIPRO.NS", "ADANIENT.NS",
    "TATASTEEL.NS", "ONGC.NS", "ADANIPORTS.NS", "NTPC.NS", "POWERGRID.NS", "HCLTECH.NS", "M&M.NS",
    "TVSMOTOR.NS", "COALINDIA.NS", "JSWSTEEL.NS", "BAJAJ-AUTO.NS", "NESTLEIND.NS", "GRASIM.NS",
    "HDFCLIFE.NS", "TECHM.NS", "DIVISLAB.NS", "BRITANNIA.NS", "APOLLOHOSP.NS", "DRREDDY.NS", "HINDALCO.NS",
    "INDUSINDBK.NS", "EICHERMOT.NS", "CIPLA.NS", "TATACONSUM.NS", "SBILIFE.NS", "UPL.NS", "HEROMOTOCO.NS",
    "PIDILITIND.NS", "GODREJCP.NS", "DABUR.NS", "SHREECEM.NS", "CHOLAFIN.NS", "TVSMOTOR.NS", "HAVELLS.NS",
    "ZYDUSLIFE.NS", "TRENT.NS", "SIEMENS.NS", "ABB.NS", "HAL.NS", "BEL.NS", "AMBUJACEM.NS", "CUMMINSIND.NS",
    "CGPOWER.NS", "DIXON.NS", "POLYCAB.NS", "MAXHEALTH.NS", "JINDALSTEL.NS", "BOSCHLTD.NS", "LODHA.NS",
    "TORNTPHARM.NS", "LTIM.NS", "SHRIRAMFIN.NS", "DLF.NS", "SRF.NS", "MUTHOOTFIN.NS", "GODREJPROP.NS",
    "TATACOMM.NS", "ICICIPRULI.NS", "PIIND.NS", "SUZLON.NS", "ZFCVINDIA.NS", "IDEA.NS", "VEDL.NS",
    "JSWENERGY.NS", "YESBANK.NS", "BHEL.NS", "AUBANK.NS", "IDFCFIRSTB.NS", "PNB.NS", "BANKBARODA.NS",
    "CANBK.NS", "UNIONBANK.NS", "AWL.NS", "ATGL.NS", "MRF.NS", "LUPIN.NS", "GLENMARK.NS", "ZYDUSLIFE.NS",
    "AUROPHARMA.NS", "BIOCON.NS", "ALKEM.NS", "LAURUSLABS.NS", "VOLTAS.NS", "CROMPTON.NS", "BATAINDIA.NS",
    "RELAXO.NS", "IRCTC.NS", "IEX.NS", "CDSL.NS", "MCX.NS", "BSE.NS", "CAMS.NS", "ANGELONE.NS",
    "PFC.NS", "RECLTD.NS", "M&MFIN.NS", "LICHSGFIN.NS", "NAM-INDIA.NS", "HDFCAMC.NS", "UTIAMC.NS",
    "NHPC.NS", "SJVN.NS", "NLCINDIA.NS", "TORNTPOWER.NS", "TATAPOWER.NS", "CESC.NS", "INDIGO.NS",
    "PAYTM.NS", "NYKAA.NS", "POLICYBZR.NS", "DELHIVERY.NS", "SWIGGY.NS", "VBL.NS", "TATAELXSI.NS",
    "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "KPITTECH.NS", "SONACOMS.NS", "MOTHERSON.NS",
    "MRPL.NS", "CHENNPETRO.NS", "HINDPETRO.NS", "BPCL.NS", "IOC.NS", "GAIL.NS", "PETRONET.NS", "IGL.NS",
    "MGL.NS", "GUJGASLTD.NS", "AARTIIND.NS", "DEEPAKNTR.NS", "TATACHEM.NS", "NAVINFLUOR.NS", "CHAMBLFERT.NS",
    "COROMANDEL.NS", "GNFC.NS", "GSFC.NS", "FACT.NS", "NFL.NS", "RCF.NS", "IPCALAB.NS", "AJANTPHARM.NS",
    "CONCOR.NS", "IRFC.NS", "RVNL.NS", "IRCON.NS", "RAILTEL.NS", "TIINDIA.NS", "ESCORTS.NS", "ASHOKLEY.NS",
    "PRESTIGE.NS", "OBEROIRLTY.NS", "PHOENIXLTD.NS", "BRIGADE.NS", "SOBHA.NS", "MAHLIFE.NS", "METROPOLIS.NS",
    "METROPOLIS.NS", "SYNGENE.NS", "ASTRAL.NS", "FINCABLES.NS", "KEI.NS", "APARINDS.NS", "KEC.NS",
    "RAYMOND.NS", "TRIDENT.NS", "THERMAX.NS", "PAGEIND.NS", "KPRMILL.NS", "CUPID.NS",
    "REDINGTON.NS", "BLUEDART.NS", "DEEPAKNTR.NS", "EQUITASBNK.NS", "UJJIVANSFB.NS", "PRECWIRE.NS",
    "BANDHANBNK.NS", "RBLBANK.NS", "FEDERALBNK.NS", "CUB.NS", "KARURVYSYA.NS", "IDBI.NS", "MAHABANK.NS"
]

# Ensure important symbols are always present even if not in the legacy top-200 list.
for _sym in ("PRECWIRE.NS",):
    if _sym not in TOP_200_NSE:
        TOP_200_NSE.append(_sym)

opportunity_stocks = []

TOP_NSE_30_BASE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BAJFINANCE", "BHARTIARTL",
    "PRECWIRE", "VOLTAMP", "KAYNES", "CUMMINSIND", "HAVELLS",
    "ABB", "SIEMENS", "BHEL", "POWERGRID", "NTPC",
    "TATAPOWER", "ADANIGREEN", "WIPRO", "HCLTECH", "TECHM",
    "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP",
]


def _with_ns_suffix(symbol: str) -> str:
    clean = (symbol or "").strip().upper()
    if not clean:
        return ""
    return clean if clean.endswith(".NS") else f"{clean}.NS"


MORNING_SCAN_SYMBOLS = [_with_ns_suffix(sym) for sym in TOP_NSE_30_BASE]


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _safe_float(value) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _prepare_indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    if ta is None:
        return pd.DataFrame()

    frame = df.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)

    rename_map = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    }
    frame = frame.rename(columns=rename_map)
    required = {"close", "volume"}
    if not required.issubset(frame.columns):
        return pd.DataFrame()

    frame = frame.dropna(subset=["close", "volume"])
    if len(frame) < 220:
        return pd.DataFrame()

    frame["rsi_14"] = ta.rsi(frame["close"], length=14)
    macd_df = ta.macd(frame["close"], fast=12, slow=26, signal=9)
    if macd_df is None or macd_df.empty:
        return pd.DataFrame()

    macd_cols = list(macd_df.columns)
    macd_line_col = next((c for c in macd_cols if c.startswith("MACD_")), None)
    macd_signal_col = next((c for c in macd_cols if c.startswith("MACDs_")), None)
    if not macd_line_col or not macd_signal_col:
        return pd.DataFrame()

    frame["macd"] = macd_df[macd_line_col]
    frame["macd_signal"] = macd_df[macd_signal_col]
    frame["ema_20"] = ta.ema(frame["close"], length=20)
    frame["ema_50"] = ta.ema(frame["close"], length=50)
    frame["ema_200"] = ta.ema(frame["close"], length=200)
    frame["avg_volume_20"] = frame["volume"].rolling(window=20).mean()
    frame["volume_ratio"] = frame["volume"] / frame["avg_volume_20"].replace(0, pd.NA)

    return frame.dropna(
        subset=[
            "rsi_14", "macd", "macd_signal", "ema_20", "ema_50", "ema_200", "volume_ratio"
        ]
    )


def _calculate_confidence_score(rsi: float, volume_ratio: float, close: float, ema_50: float, macd: float, macd_signal: float) -> float:
    # Weighted score: RSI quality (20), volume conviction (30), trend strength (25), MACD spread (25).
    rsi_quality = 1.0 - (abs(rsi - 44.0) / 14.0)
    rsi_component = _clamp(rsi_quality, 0.0, 1.0) * 20.0

    volume_component = _clamp((volume_ratio - 1.5) / 1.5, 0.0, 1.0) * 30.0

    trend_pct = ((close - ema_50) / ema_50) if ema_50 else 0.0
    trend_component = _clamp(trend_pct / 0.08, 0.0, 1.0) * 25.0

    macd_spread = macd - macd_signal
    macd_component = _clamp(macd_spread / max(close * 0.004, 1e-6), 0.0, 1.0) * 25.0

    return round(_clamp(rsi_component + volume_component + trend_component + macd_component, 0.0, 100.0), 2)


def _build_reason(rsi: float, volume_ratio: float, close: float, ema_50: float, macd: float, macd_signal: float) -> str:
    return (
        f"RSI {rsi:.1f} in buy zone, volume {volume_ratio:.2f}x avg, "
        f"price above EMA50 ({close:.2f}>{ema_50:.2f}), MACD bullish ({macd:.3f}>{macd_signal:.3f})."
    )


def _save_morning_scan_results(scan_time: datetime, rows: list[dict]) -> None:
    session = SessionLocal()
    try:
        for idx, row in enumerate(rows, start=1):
            session.add(
                MorningScan(
                    scan_time=scan_time,
                    scan_date=scan_time.date(),
                    rank=idx,
                    symbol=row["symbol"],
                    entry_price=row["entry_price"],
                    stop_loss=row["stop_loss"],
                    target_price=row["target"],
                    confidence_score=row["confidence_score"],
                    reason=row["reason"],
                    close=row["close"],
                    rsi=row["rsi"],
                    macd=row["macd"],
                    macd_signal=row["macd_signal"],
                    ema_20=row["ema_20"],
                    ema_50=row["ema_50"],
                    ema_200=row["ema_200"],
                    volume=row["volume"],
                    avg_volume_20=row["avg_volume_20"],
                    volume_ratio=row["volume_ratio"],
                )
            )
        session.commit()
    except Exception as exc:
        session.rollback()
        log.error("Morning scan DB write failed: %s", exc)
        raise
    finally:
        session.close()


def run_morning_scan_once(symbols: list[str] | None = None, period: str = "15mo") -> list[dict]:
    if ta is None:
        log.error("Morning scan skipped: pandas-ta is not installed. Install with `pip install pandas-ta`.")
        return []

    symbols = symbols or MORNING_SCAN_SYMBOLS
    normalized_symbols = [_with_ns_suffix(sym) for sym in symbols if _with_ns_suffix(sym)]

    matched: list[dict] = []
    for symbol in normalized_symbols:
        try:
            df = yf.download(
                symbol,
                period=period,
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if df is None or df.empty:
                log.warning("Morning scan: no data for %s", symbol)
                continue

            frame = _prepare_indicator_frame(df)
            if frame.empty:
                log.debug("Morning scan: insufficient indicator data for %s", symbol)
                continue

            last = frame.iloc[-1]
            close = _safe_float(last.get("close"))
            volume = _safe_float(last.get("volume"))
            avg_volume_20 = _safe_float(last.get("avg_volume_20"))
            volume_ratio = _safe_float(last.get("volume_ratio"))
            rsi = _safe_float(last.get("rsi_14"))
            macd = _safe_float(last.get("macd"))
            macd_signal = _safe_float(last.get("macd_signal"))
            ema_20 = _safe_float(last.get("ema_20"))
            ema_50 = _safe_float(last.get("ema_50"))
            ema_200 = _safe_float(last.get("ema_200"))

            if None in [close, volume, avg_volume_20, volume_ratio, rsi, macd, macd_signal, ema_20, ema_50, ema_200]:
                continue

            if not (30.0 <= rsi <= 58.0):
                continue
            if not (volume_ratio > 1.5):
                continue
            if not (close > ema_50):
                continue
            if not (macd > macd_signal):
                continue

            entry_price = round(close, 2)
            stop_loss = round(entry_price * 0.93, 2)
            target = round(entry_price * 1.15, 2)
            confidence_score = _calculate_confidence_score(rsi, volume_ratio, close, ema_50, macd, macd_signal)
            reason = _build_reason(rsi, volume_ratio, close, ema_50, macd, macd_signal)

            matched.append(
                {
                    "symbol": symbol.replace(".NS", ""),
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "target": target,
                    "confidence_score": confidence_score,
                    "reason": reason,
                    "close": round(close, 2),
                    "rsi": round(rsi, 2),
                    "macd": round(macd, 6),
                    "macd_signal": round(macd_signal, 6),
                    "ema_20": round(ema_20, 2),
                    "ema_50": round(ema_50, 2),
                    "ema_200": round(ema_200, 2),
                    "volume": float(volume),
                    "avg_volume_20": float(avg_volume_20),
                    "volume_ratio": round(volume_ratio, 3),
                }
            )
        except Exception as exc:
            log.error("Morning scan processing failed for %s: %s", symbol, exc)

    top_results = sorted(matched, key=lambda x: x["confidence_score"], reverse=True)[:10]

    scan_time = datetime.now(tz=IST).replace(second=0, microsecond=0)
    if top_results:
        _save_morning_scan_results(scan_time=scan_time, rows=top_results)
        log.info("Morning scan saved %d matched symbols.", len(top_results))
    else:
        log.info("Morning scan completed with no matches.")
    return top_results


def _seconds_until_next_ist_9am(now: datetime | None = None) -> float:
    current = now or datetime.now(tz=IST)
    target = datetime.combine(current.date(), time(MORNING_SCAN_HOUR, MORNING_SCAN_MINUTE), tzinfo=IST)
    if current >= target:
        target = target + timedelta(days=1)
    return max((target - current).total_seconds(), 1.0)


async def start_morning_scan_task():
    while True:
        try:
            delay = _seconds_until_next_ist_9am()
            next_run = datetime.now(tz=IST) + timedelta(seconds=delay)
            log.info("Morning scan scheduler sleeping %.0f seconds; next run at %s IST", delay, next_run.strftime("%Y-%m-%d %H:%M:%S"))
            await asyncio.sleep(delay)
            log.info("Starting daily 9:00 AM IST morning scan...")
            await asyncio.to_thread(run_morning_scan_once)
        except Exception as exc:
            log.error("Morning scan scheduler error: %s", exc)
            await asyncio.sleep(60)

def calculate_rsi(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def _download_single_symbol_daily(symbol: str) -> pd.DataFrame:
    """Fallback fetch when bulk download misses a symbol in the MultiIndex payload."""
    try:
        one = yf.download(
            symbol,
            period="1mo",
            interval="1d",
            threads=False,
            progress=False,
            auto_adjust=False,
        )
        if one is None or one.empty:
            return pd.DataFrame()
        if isinstance(one.columns, pd.MultiIndex):
            one.columns = one.columns.get_level_values(0)
        return one
    except Exception as exc:
        log.debug("Single-symbol fallback failed for %s: %s", symbol, exc)
        return pd.DataFrame()

def scan_opportunities():
    global opportunity_stocks
    log.info("Downloading screener data for 200 NSE stocks...")
    
    try:
        data = yf.download(
            " ".join(TOP_200_NSE),
            period="1mo",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception as e:
        log.error(f"Screener download failed: {e}")
        return

    new_opportunities = []
    
    for symbol in TOP_200_NSE:
        try:
            if len(TOP_200_NSE) == 1:
                df = data
            else:
                if symbol not in data.columns.levels[0]:
                    df = _download_single_symbol_daily(symbol)
                else:
                    df = data[symbol]

            if df is None or df.empty or 'Close' not in df.columns:
                continue
                
            df = df.dropna(subset=['Close'])
            if len(df) < 20: 
                continue

            last_close = float(df['Close'].iloc[-1])
            last_vol = float(df['Volume'].iloc[-1])
            
            avg_vol_20 = float(df['Volume'].rolling(20).mean().iloc[-1])
            vol_spike = last_vol > (2 * avg_vol_20)

            high_20 = float(df['High'].rolling(20).max().iloc[-2])
            breakout = last_close > high_20

            rsi = float(calculate_rsi(df['Close']).iloc[-1])
            
            signal_type = None
            if rsi < 35:
                signal_type = "OVERSOLD_BUY"
            elif rsi > 70:
                signal_type = "OVERBOUGHT_SELL"
            elif breakout:
                signal_type = "BREAKOUT"
            elif vol_spike:
                signal_type = "VOLUME_SPIKE"

            if signal_type:
                new_opportunities.append({
                    "symbol": symbol.replace(".NS", ""),
                    "price": round(last_close, 2),
                    "rsi": round(rsi, 1) if pd.notna(rsi) else None,
                    "avg_vol": int(avg_vol_20) if pd.notna(avg_vol_20) else 0,
                    "vol": int(last_vol),
                    "signal": signal_type,
                    "breakout": bool(breakout),
                    "vol_spike": bool(vol_spike)
                })
        except Exception as e:
            continue

    if new_opportunities:
        # Sort by most interesting: breakout + vol spike -> deep oversold -> deep overbought
        opportunity_stocks = sorted(
            new_opportunities, 
            key=lambda x: (
                0 if x['breakout'] and x['vol_spike'] else 
                1 if x['rsi'] and x['rsi'] < 30 else
                2 if x['rsi'] and x['rsi'] > 75 else 
                3
            )
        )[:5]
        log.info(f"Screener found {len(new_opportunities)} opportunities. Top 5 saved.")

async def start_screener_task():
    while True:
        try:
            log.info("Running 5-min Stock Screener...")
            # In a real app we'd use run_in_executor but yf.download uses its own threads normally
            await asyncio.to_thread(scan_opportunities)
        except Exception as e:
            log.error(f"Screener loop error: {e}")
            
        await asyncio.sleep(300)
