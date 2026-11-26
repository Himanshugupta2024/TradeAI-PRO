"""
Verification script for Box B (ML Inference Sidecar).
Runs pre-inference health check, authenticated /infer test, and measures post-inference peak RSS.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
import pandas as pd

token = os.environ.get("ML_INTERNAL_TOKEN", "").strip()
if not token:
    print("ERROR: ML_INTERNAL_TOKEN environment variable is required.", file=sys.stderr)
    sys.exit(1)
base_url = os.environ.get("BOX_B_URL", "https://trading-ml-8umr.onrender.com").rstrip("/")

print(f"=== Verifying Box B: {base_url} ===")

# 1. Pre-inference health check
print("\n--- 1. Pre-inference Health ---")
req_pre = urllib.request.Request(f"{base_url}/health")
try:
    with urllib.request.urlopen(req_pre, timeout=20) as resp:
        pre_health = json.loads(resp.read().decode())
        print("Status:", pre_health.get("status"))
        print("Pre-inference RSS (MB):", pre_health.get("memory_rss_mb"))
except Exception as e:
    print("Health check failed:", e)
    sys.exit(1)

# 2. Authenticated /infer call
print("\n--- 2. Authenticated /infer ---")
base_time = datetime(2026, 11, 22, 9, 15)
dates = [base_time + timedelta(minutes=5 * i) for i in range(70)]
df = pd.DataFrame({
    "timestamp": dates,
    "open": [100.0 + i * 0.1 for i in range(70)],
    "high": [102.0 + i * 0.1 for i in range(70)],
    "low": [99.0 + i * 0.1 for i in range(70)],
    "close": [101.0 + i * 0.1 for i in range(70)],
    "volume": [1000 + i * 10 for i in range(70)],
})
df_json = df.to_json(orient="split", date_format="iso")

req_infer = urllib.request.Request(
    f"{base_url}/infer",
    data=json.dumps({"symbol": "RELIANCE", "df_json": df_json, "offline": True, "force_refresh": True}).encode(),
    headers={
        "Content-Type": "application/json",
        "X-Internal-Token": token,
    },
)

t0 = time.perf_counter()
try:
    with urllib.request.urlopen(req_infer, timeout=25) as resp:
        infer_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        res = json.loads(resp.read().decode())
        print(f"HTTP Status: {resp.status} (in {infer_latency_ms}ms)")
        print("Source:", res.get("source"))
        print("Direction:", res.get("direction"))
        print("Confidence:", res.get("confidence"))
        print("Calibrated Win Prob:", res.get("calibrated_trade_win_probability"))
        print("Model votes:", list((res.get("model_votes") or {}).keys()))
except urllib.error.HTTPError as e:
    print(f"HTTP Error: {e.code} - {e.read().decode()}")
    sys.exit(1)
except Exception as e:
    print("Inference failed:", e)
    sys.exit(1)

# 3. Post-inference health check (PEAK RSS)
print("\n--- 3. Post-inference Health (Peak RSS) ---")
time.sleep(0.5)
req_post = urllib.request.Request(f"{base_url}/health")
try:
    with urllib.request.urlopen(req_post, timeout=20) as resp:
        post_health = json.loads(resp.read().decode())
        rss = post_health.get("memory_rss_mb")
        print(f"Post-inference Peak RSS: {rss} MB")
        if rss and rss < 480.0:
            print(f"VERDICT: PASS (RSS {rss} MB < 480 MB cap)")
        else:
            print(f"VERDICT: FAIL (RSS {rss} MB exceeds 480 MB)")
except Exception as e:
    print("Post-inference health check failed:", e)
