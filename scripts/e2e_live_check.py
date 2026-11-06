import time
import requests
import json

EDGE_URL = "https://tradeai-pro.tradingpro.workers.dev"
ORIGIN_URL = "https://trading-api-n1wt.onrender.com"

def check_all():
    results = {}
    s = requests.Session()
    
    # 1. Origin Health
    t0 = time.time()
    try:
        r_origin = s.get(f"{ORIGIN_URL}/api/health/live", timeout=15)
        results["origin_health"] = {
            "status": r_origin.status_code,
            "latency_ms": round((time.time() - t0) * 1000, 1),
            "body": r_origin.json() if r_origin.status_code == 200 else r_origin.text[:100]
        }
    except Exception as e:
        results["origin_health"] = {"error": str(e)}

    # 2. Edge Health & Proxy
    t0 = time.time()
    try:
        r_edge = s.get(f"{EDGE_URL}/api/health/live", timeout=15)
        results["edge_health"] = {
            "status": r_edge.status_code,
            "latency_ms": round((time.time() - t0) * 1000, 1),
            "proxy": r_edge.headers.get("x-edge-proxy"),
            "cf_cache": r_edge.headers.get("cf-cache-status"),
            "cache_control": r_edge.headers.get("cache-control")
        }
    except Exception as e:
        results["edge_health"] = {"error": str(e)}

    # 3. Public Market Overview Intelligence (Cold vs Warm Edge)
    t0 = time.time()
    r_pub1 = s.get(f"{EDGE_URL}/api/overview/intelligence")
    lat1 = round((time.time() - t0) * 1000, 1)
    
    time.sleep(0.5)
    t0 = time.time()
    r_pub2 = s.get(f"{EDGE_URL}/api/overview/intelligence")
    lat2 = round((time.time() - t0) * 1000, 1)

    results["overview_intelligence"] = {
        "call_1": {"status": r_pub1.status_code, "latency_ms": lat1, "cf_cache": r_pub1.headers.get("cf-cache-status"), "cache_control": r_pub1.headers.get("cache-control")},
        "call_2": {"status": r_pub2.status_code, "latency_ms": lat2, "cf_cache": r_pub2.headers.get("cf-cache-status"), "age": r_pub2.headers.get("age"), "cache_control": r_pub2.headers.get("cache-control")}
    }

    # 4. Public Signals
    t0 = time.time()
    r_sig = s.get(f"{EDGE_URL}/api/signals/all")
    results["signals_all"] = {
        "status": r_sig.status_code,
        "latency_ms": round((time.time() - t0) * 1000, 1),
        "cf_cache": r_sig.headers.get("cf-cache-status"),
        "count": len(r_sig.json().get("signals", [])) if r_sig.status_code == 200 else 0
    }

    # 5. Sensitive Private Endpoints (Strict BYPASS & No-Store Policy)
    private_routes = ["/api/portfolio", "/api/trades", "/api/risk/state"]
    results["private_routes_shield"] = {}
    for pr in private_routes:
        t0 = time.time()
        r_pr = s.get(f"{EDGE_URL}{pr}")
        results["private_routes_shield"][pr] = {
            "status": r_pr.status_code,
            "cf_cache": r_pr.headers.get("cf-cache-status"),
            "cache_control": r_pr.headers.get("cache-control"),
            "pragma": r_pr.headers.get("pragma"),
            "latency_ms": round((time.time() - t0) * 1000, 1)
        }

    # 6. Scanners & Data Endpoints
    scanners = ["/api/breakouts", "/api/swing/shortlist", "/api/market/mood"]
    results["scanners"] = {}
    for sc in scanners:
        r_sc = s.get(f"{EDGE_URL}{sc}")
        results["scanners"][sc] = {
            "status": r_sc.status_code,
            "cf_cache": r_sc.headers.get("cf-cache-status")
        }

    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    check_all()
