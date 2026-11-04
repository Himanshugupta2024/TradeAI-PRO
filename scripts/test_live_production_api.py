"""
Production API Verification Script for TradeAI PRO
Queries all live production endpoints on Render: https://trading-api-n1wt.onrender.com/
"""

import time
import json
import httpx
from datetime import datetime

BASE_URL = "https://trading-api-n1wt.onrender.com"

# Candidate routes and representative test parameters
PARAM_MAPPINGS = {
    "{symbol}": "RELIANCE",
    "{symbol:path}": "RELIANCE",
    "{index_name}": "NIFTY",
    "{scan_id}": "1",
}

# Routes to test
ROUTES_TO_TEST = [
    # Core Health & Status
    ("/api/health", {}),
    ("/api/health/live", {}),
    ("/api/health/ready", {}),
    ("/api/health/providers", {}),
    ("/api/ready", {}),
    ("/api/stats", {}),
    
    # Overview & Dashboard
    ("/api/dashboard/overview", {}),
    ("/api/overview/intelligence", {}),
    ("/api/symbols", {}),
    ("/api/sectors", {}),
    
    # Markets: NSE
    ("/api/nse/dashboard", {}),
    ("/api/nse/news", {}),
    ("/api/nse/prediction", {}),
    ("/api/market/mood", {}),
    ("/api/market/RELIANCE", {}),
    ("/api/search/symbols?q=TCS", {}),
    ("/api/search/quote/TCS", {}),
    
    # Markets: Crypto
    ("/api/crypto/intelligence", {}),
    ("/api/crypto/news", {}),
    ("/api/crypto/prediction", {}),
    ("/api/crypto/meme", {}),
    
    # Markets: Forex
    ("/api/forex/intelligence", {}),
    ("/api/forex/rates", {}),
    ("/api/forex/sessions", {}),
    ("/api/forex/signals", {}),
    ("/api/forex/signal/EUR_USD", {}),
    ("/api/forex/ohlcv/EUR_USD?timeframe=1h", {}),
    
    # Trading: Trade Flow, Portfolio, Performance
    ("/api/tradeflow", {}),
    ("/api/paper/open", {}),
    ("/api/portfolio", {}),
    ("/api/trades?limit=50", {}),
    ("/api/performance", {}),
    ("/api/paper/scorecard", {}),
    ("/api/risk/state", {}),
    
    # Autonomous Engine
    ("/api/autonomous/status", {}),
    ("/api/autonomous/feed", {}),
    
    # Scanners: Top Setups / Edge
    ("/api/edge/top-setups", {}),
    ("/api/edge/score/RELIANCE", {}),
    ("/api/edge/score/ETH/USDT", {}),
    ("/api/edge/research/RELIANCE", {}),
    ("/api/screener/opportunities", {}),
    ("/api/screener/gainers", {}),
    ("/api/screener/losers", {}),
    
    # Scanners: Intraday
    ("/api/intraday/latest", {}),
    ("/api/intraday/stats", {}),
    ("/api/intraday/live-quote/RELIANCE", {}),
    
    # Scanners: Swing
    ("/api/morning-scan", {}),
    ("/api/swing-scanner", {}),
    ("/api/swing/shortlist", {}),
    ("/api/swing/stats", {}),
    ("/api/swing/history", {}),
    ("/api/swing/symbol/RELIANCE", {}),
    ("/api/swing-debug", {}),
    
    # Scanners: Breakouts & Index Movers
    ("/api/breakouts", {}),
    ("/api/index-mover/NIFTY", {}),
    
    # Intelligence: Scorecard, AI Learning, Macro, Risk
    ("/api/analytics/scorecard", {}),
    ("/api/analytics/signals", {}),
    ("/api/learning/stats", {}),
    ("/api/learning/outcomes", {}),
    ("/api/ai/ensemble/status", {}),
    ("/api/ensemble/status", {}),
    ("/api/ensemble/validate", {}),
    ("/api/ai/weights", {}),
    ("/api/retrain/status", {}),
    ("/api/edge/macro-heatmap", {}),
    ("/api/macro/intelligence", {}),
    ("/api/macro/sector-impact", {}),
    ("/api/edge/risk-gauge", {}),
    ("/api/signals/all", {}),
    ("/api/signals/leaderboard", {}),
    ("/api/signals/winrate", {}),
    ("/api/signal/RELIANCE", {}),
    ("/api/regime/all", {}),
    ("/api/regime/RELIANCE", {}),
    ("/api/context/market-mood", {}),
    ("/api/context/events", {}),
    ("/api/context/v3", {}),
    ("/api/context/RELIANCE", {}),
    ("/api/nlp/summary", {}),
    ("/api/telegram/status", {}),
    ("/api/backtest/RELIANCE", {}),
]

def run_tests():
    print(f"=== Starting Production API Verification against {BASE_URL} ===")
    results = []
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 TradeAI-Verifier/1.0",
        "Accept": "application/json"
    }
    with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
        # First verify basic connectivity
        try:
            t0 = time.perf_counter()
            r_root = client.get(f"{BASE_URL}/api/health")
            t_root = round((time.perf_counter() - t0) * 1000, 1)
            print(f"[ROOT HEALTH] /api/health -> {r_root.status_code} in {t_root}ms")
        except Exception as e:
            print(f"[FATAL] Cannot connect to {BASE_URL}: {e}")
            return

        for path, params in ROUTES_TO_TEST:
            time.sleep(0.4)
            url = f"{BASE_URL}{path}"
            t_start = time.perf_counter()
            status = 0
            err = None
            keys = []
            sample_data = None
            latency = 0.0
            
            try:
                resp = client.get(url, params=params)
                latency = round((time.perf_counter() - t_start) * 1000, 1)
                status = resp.status_code
                
                content_type = resp.headers.get("content-type", "")
                if "application/json" in content_type:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            keys = list(data.keys())
                            # Sample check for nulls or errors
                            sample_data = {k: ("<list len=" + str(len(v)) + ">") if isinstance(v, list) else ("<dict>" if isinstance(v, dict) else v) for k, v in list(data.items())[:6]}
                        elif isinstance(data, list):
                            keys = [f"list[{len(data)}]"]
                            if len(data) > 0 and isinstance(data[0], dict):
                                sample_data = {"first_item_keys": list(data[0].keys())}
                    except Exception as je:
                        err = f"JSON parse error: {je}"
                else:
                    sample_data = f"Non-JSON response ({content_type}): {resp.text[:100]}"
                    
            except httpx.TimeoutException:
                latency = round((time.perf_counter() - t_start) * 1000, 1)
                status = 408
                err = "Request timed out (>30s)"
            except Exception as ex:
                latency = round((time.perf_counter() - t_start) * 1000, 1)
                status = 500
                err = str(ex)
                
            res_entry = {
                "path": path,
                "status": status,
                "latency_ms": latency,
                "keys": keys,
                "sample": sample_data,
                "error": err
            }
            results.append(res_entry)
            
            icon = "PASS" if status == 200 else ("WARN" if status < 400 else "FAIL")
            print(f"[{icon:4s}] {status:3d} | {latency:6.1f}ms | {path:40s} | keys={keys[:5]}")
            if err:
                print(f"       ERROR: {err}")
                
    output_file = "scripts/production_api_results.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump({
            "tested_at": datetime.now().isoformat(),
            "base_url": BASE_URL,
            "total_tested": len(results),
            "passed": len([r for r in results if r["status"] == 200]),
            "failed": len([r for r in results if r["status"] != 200]),
            "results": results
        }, f, indent=2)
    print(f"\nSaved detailed API results to {output_file}")
    print(f"Summary: {len([r for r in results if r['status'] == 200])}/{len(results)} endpoints returned HTTP 200")

if __name__ == "__main__":
    run_tests()
