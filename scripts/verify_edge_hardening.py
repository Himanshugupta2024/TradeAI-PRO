import time
import json
import statistics
import requests

BASE_URL = "https://tradeai-pro.tradingpro.workers.dev"

def test_headers_and_privacy():
    print("=== 1. TESTING PUBLIC VS PRIVATE ROUTING & HEADERS ===")
    s = requests.Session()
    
    # 1. Public Endpoint
    pub_url = f"{BASE_URL}/api/overview/intelligence"
    r1 = s.get(pub_url)
    print(f"[Public Route] GET {pub_url}")
    print(f"  Status: {r1.status_code}")
    print(f"  CF-Cache-Status: {r1.headers.get('cf-cache-status')}")
    print(f"  Cache-Control: {r1.headers.get('cache-control')}")
    print(f"  X-Edge-Proxy: {r1.headers.get('x-edge-proxy')}")
    print(f"  Age: {r1.headers.get('age')}")
    
    # Immediate second call to test HIT
    time.sleep(0.5)
    r2 = s.get(pub_url)
    print(f"  Repeat Status: {r2.status_code}")
    print(f"  Repeat CF-Cache-Status: {r2.headers.get('cf-cache-status')}")
    print(f"  Repeat Cache-Control: {r2.headers.get('cache-control')}")
    print(f"  Repeat Age: {r2.headers.get('age')}")

    # 2. Private Endpoint (Portfolio)
    priv_url = f"{BASE_URL}/api/portfolio"
    rp = s.get(priv_url)
    print(f"\n[Private Route] GET {priv_url}")
    print(f"  Status: {rp.status_code}")
    print(f"  CF-Cache-Status: {rp.headers.get('cf-cache-status')} (Expect: BYPASS)")
    print(f"  Cache-Control: {rp.headers.get('cache-control')} (Expect: private, no-store)")
    print(f"  Pragma: {rp.headers.get('pragma')}")

def test_swr_and_latency_distribution():
    print("\n=== 2. MEASURING MULTI-SAMPLE LATENCIES & STATS (20 SAMPLES) ===")
    s = requests.Session()
    url = f"{BASE_URL}/api/signals/all"
    
    latencies = []
    statuses = []
    
    for i in range(20):
        t0 = time.time()
        r = s.get(url)
        elapsed = (time.time() - t0) * 1000
        latencies.append(elapsed)
        statuses.append(r.headers.get('cf-cache-status', 'NONE'))
        time.sleep(0.2)
        
    p50 = statistics.median(latencies)
    lat_sorted = sorted(latencies)
    p95_idx = int(len(lat_sorted) * 0.95)
    p95 = lat_sorted[min(p95_idx, len(lat_sorted)-1)]
    min_lat = min(latencies)
    max_lat = max(latencies)
    
    hits = statuses.count("HIT")
    stales = statuses.count("STALE")
    misses = statuses.count("MISS")
    
    print(f"Samples: {len(latencies)}")
    print(f"Status distribution: {hits} HITS | {stales} STALES | {misses} MISSES")
    print(f"Min: {min_lat:.1f}ms | Median (p50): {p50:.1f}ms | p95: {p95:.1f}ms | Max: {max_lat:.1f}ms")

if __name__ == "__main__":
    test_headers_and_privacy()
    test_swr_and_latency_distribution()
