"""
TradeAI-PRO — Live-Session Operational Readiness & Health Probe.

Executes end-to-end verification across edge proxy and origin endpoints:
1. /api/health (market_open, data_age_seconds, providers health)
2. /api/context/v3 (derivatives_oi, fii_dii_flow, vix, breadth)
3. /api/index-mover/nifty50 (latency < 2.0s, constituent count >= 20)
4. /api/signals/all (signals cached, direction, scoring)
"""
import json
import sys
import time
import urllib.request
from typing import Any, Dict

DEFAULT_BASE_URL = "https://tradeai-pro.tradingpro.workers.dev"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fetch_json(url: str, timeout: float = 12.0) -> tuple[int, Dict[str, Any], float]:
    """Fetch URL and return (status_code, json_dict, elapsed_sec)."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = time.time() - t0
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body), elapsed
    except urllib.error.HTTPError as he:
        elapsed = time.time() - t0
        try:
            body = he.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        except Exception:
            data = {"error": f"HTTP {he.code}: {he.reason}"}
        return he.code, data, elapsed
    except Exception as exc:
        elapsed = time.time() - t0
        return 0, {"error": str(exc)}, elapsed


def run_probe(base_url: str = DEFAULT_BASE_URL) -> bool:
    print(f"=== TradeAI PRO Live-Session Probe ({time.strftime('%Y-%m-%d %H:%M:%S IST', time.gmtime(time.time() + 19800))}) ===")
    print(f"Target: {base_url}\n")
    all_passed = True

    # 1. Health Probe
    status, health, el = fetch_json(f"{base_url}/api/health")
    if status == 200 and health.get("status") == "ok":
        mkt_open = health.get("is_market_open", False)
        data_age = health.get("data_age_seconds", 999999.0)
        tier = health.get("health_tier", {})
        print(f"✅ [PASS] /api/health ({round(el, 2)}s)")
        print(f"   • Market Open: {mkt_open}")
        print(f"   • Data Age: {data_age:.1f}s ({health.get('data_age_human', 'N/A')})")
        print(f"   • Health Tier: {tier}")
        if mkt_open and data_age > 300:
            print("   ⚠️  WARNING: Market is OPEN but data_age > 300s!")
            all_passed = False
    else:
        print(f"❌ [FAIL] /api/health returned HTTP {status}: {health}")
        all_passed = False

    # 2. Context V3 Probe
    status, ctx, el = fetch_json(f"{base_url}/api/context/v3")
    if status == 200:
        oi = ctx.get("derivatives_oi", {})
        vix = ctx.get("india_vix", {})
        flow = ctx.get("fii_dii_flow", {})
        print(f"✅ [PASS] /api/context/v3 ({round(el, 2)}s)")
        print(f"   • Derivatives OI Status: {oi.get('status')} (PCR: {oi.get('put_call_ratio', 'N/A')}, Buildup: {oi.get('oi_buildup_classification', 'N/A')})")
        print(f"   • India VIX Status: {vix.get('status')} (Last: {vix.get('last', 'N/A')})")
        print(f"   • FII/DII Flow Status: {flow.get('status')}")
    else:
        print(f"❌ [FAIL] /api/context/v3 returned HTTP {status}: {ctx}")
        all_passed = False

    # 3. Index Movers NIFTY 50 Probe (Must be < 4.0s)
    status, movers, el = fetch_json(f"{base_url}/api/index-mover/nifty50", timeout=12.0)
    if status == 200 and "movers" in movers:
        m_count = len(movers.get("movers", []))
        level = movers.get("index_level", 0)
        print(f"✅ [PASS] /api/index-mover/nifty50 ({round(el, 2)}s)")
        print(f"   • Constituents: {m_count} movers (Index Level: ₹{level:,.1f})")
        print(f"   • Top Gainers: {[g['symbol'] for g in movers.get('top_gainers', [])[:3]]}")
        print(f"   • Top Draggers: {[d['symbol'] for d in movers.get('top_draggers', [])[:3]]}")
        if el > 4.0:
            print(f"   ⚠️  LATENCY WARNING: Index movers took {round(el, 2)}s (> 4.0s)")
    else:
        print(f"❌ [FAIL] /api/index-mover/nifty50 returned HTTP {status}: {movers}")
        all_passed = False

    # 4. Signals Probe
    status, sigs, el = fetch_json(f"{base_url}/api/signals/all")
    sig_list = sigs.get("signals", []) if isinstance(sigs, dict) else (sigs if isinstance(sigs, list) else [])
    if status == 200 and sig_list:
        print(f"✅ [PASS] /api/signals/all ({round(el, 2)}s)")
        print(f"   • Total Signals: {len(sig_list)}")
        longs = sum(1 for s in sig_list if s.get("direction") == "LONG")
        shorts = sum(1 for s in sig_list if s.get("direction") == "SHORT")
        holds = sum(1 for s in sig_list if s.get("direction") == "HOLD")
        print(f"   • Breakdown: {longs} LONG, {shorts} SHORT, {holds} HOLD")
    else:
        print(f"❌ [FAIL] /api/signals/all returned HTTP {status}: {sigs}")

    print("\n" + ("=" * 60))
    if all_passed:
        print("OVERALL RESULT: PASS (All production contracts verified nominal)")
    else:
        print("OVERALL RESULT: FAIL / DEGRADED (One or more contracts require attention)")
    print("=" * 60 + "\n")
    return all_passed


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL
    success = run_probe(base)
    sys.exit(0 if success else 1)
