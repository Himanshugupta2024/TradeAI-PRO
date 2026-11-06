import requests
import statistics
import time

s = requests.Session()

print('=== 1. SENSITIVE ROUTE AUDIT ===')
for route in ['/api/portfolio', '/api/trades', '/api/risk/state']:
    r = s.get('https://tradeai-pro.tradingpro.workers.dev' + route)
    print(f"{route} -> Status: {r.status_code} | CF-Cache: {r.headers.get('cf-cache-status')} | Cache-Control: {r.headers.get('cache-control')}")

print('\n=== 2. PUBLIC ROUTE HEADER CONSISTENCY (MISS vs HIT) ===')
url = 'https://tradeai-pro.tradingpro.workers.dev/api/signals/winrate'
r_miss = s.get(url, headers={'Cache-Control': 'no-cache'})
print(f"MISS -> CF-Cache: {r_miss.headers.get('cf-cache-status')} | Cache-Control: {r_miss.headers.get('cache-control')}")
time.sleep(0.5)
r_hit = s.get(url)
print(f"HIT  -> CF-Cache: {r_hit.headers.get('cf-cache-status')} | Cache-Control: {r_hit.headers.get('cache-control')} | Age: {r_hit.headers.get('age')}s")

print('\n=== 3. LATENCY DISTRIBUTION FROM INDIA (20 SAMPLES) ===')
target = 'https://tradeai-pro.tradingpro.workers.dev/api/signals/all'
latencies = []
for i in range(20):
    t0 = time.time()
    r = s.get(target)
    elapsed = (time.time() - t0) * 1000
    latencies.append(elapsed)
    time.sleep(0.1)

lats_sorted = sorted(latencies)
p50 = statistics.median(latencies)
p90 = lats_sorted[int(len(lats_sorted) * 0.90)]
p95 = lats_sorted[int(len(lats_sorted) * 0.95)]
print(f"Samples: 20 | Min: {min(latencies):.1f}ms | p50 (Median): {p50:.1f}ms | p90: {p90:.1f}ms | p95: {p95:.1f}ms | Max: {max(latencies):.1f}ms")
