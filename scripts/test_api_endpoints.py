"""
TradeAI-PRO — Complete REST & WebSocket Endpoint Verification
Tests all major API routes and WebSocket telemetry with FastAPI TestClient.
"""

import sys
import os
import json
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from server import app
from api import service as svc
from config import settings

# Pre-populate cache flags so test endpoints return fast cached responses without blocking on network
svc._swing_cache["real_data"] = True
svc._swing_cache["symbols_scanned"] = 10
svc._swing_cache["setups"] = []

client = TestClient(app)

print("=" * 80)
print("TRADEAI-PRO API & WEBSOCKET ENDPOINT VERIFICATION")
print("=" * 80)

endpoints_to_test = [
    ("GET", "/api/health", 200),
    ("GET", "/api/dashboard/overview", 200),
    ("GET", "/api/overview/intelligence", 200),
    ("GET", "/api/signals/winrate", 200),
    ("GET", "/api/swing-scanner", 200),
    ("GET", "/api/swing/shortlist", 200),
    ("GET", "/api/forex/sessions", 200),
    ("GET", "/api/forex/rates", 200),
    ("GET", "/api/crypto/intelligence", 200),
    ("GET", "/api/crypto/meme", 200),
    ("GET", "/api/edge/top-setups", 200),
    ("GET", "/api/edge/risk-gauge", 200),
    ("GET", "/api/paper/open", 200),
    ("GET", "/api/paper/scorecard", 200),
    ("GET", "/api/autonomous/status", 200),
    ("GET", "/api/autonomous/feed", 200),
    ("GET", "/api/telegram/status", 200),
    ("GET", "/", 200),
    ("GET", "/crypto", 200),
]

api_results = {}

for method, path, expected_status in endpoints_to_test:
    t0 = time.perf_counter()
    try:
        resp = client.get(path)
        dur = (time.perf_counter() - t0) * 1000
        
        is_pass = resp.status_code == expected_status
        status_text = "PASS" if is_pass else "FAIL"
        print(f"  [{status_text}] {method:<4} {path:<32} -> Status {resp.status_code} ({dur:6.1f}ms)")
        api_results[path] = {
            "status": status_text,
            "status_code": resp.status_code,
            "latency_ms": round(dur, 1)
        }
    except Exception as exc:
        print(f"  [FAIL] {method:<4} {path:<32} -> Error: {exc}")
        api_results[path] = {"status": "FAIL", "error": str(exc)}

# Test Protected Admin Route Security
print("\nTesting Protected Route Security...")
t0 = time.perf_counter()
unauth_resp = client.post("/api/refresh")
dur = (time.perf_counter() - t0) * 1000
is_protected = unauth_resp.status_code in [401, 403]
print(f"  [{'PASS' if is_protected else 'FAIL'}] POST /api/refresh without API Key -> Status {unauth_resp.status_code} (Protected: {is_protected}) ({dur:.1f}ms)")
api_results["/api/refresh_auth"] = {"status": "PASS" if is_protected else "FAIL", "status_code": unauth_resp.status_code}

# Test WebSocket
print("\nTesting WebSocket Telemetry (/ws/prices)...")
try:
    with client.websocket_connect("/ws/prices") as ws:
        data = ws.receive_json()
        print(f"  [PASS] WS /ws/prices connected and received frame: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        api_results["/ws/prices"] = {"status": "PASS", "first_frame": type(data).__name__}
except Exception as exc:
    print(f"  [FAIL] WS /ws/prices error: {exc}")
    api_results["/ws/prices"] = {"status": "FAIL", "error": str(exc)}

print("\n" + "=" * 80)
print("API VERIFICATION SUMMARY:")
print(json.dumps(api_results, indent=2))
print("=" * 80)
