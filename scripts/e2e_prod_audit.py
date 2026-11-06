import urllib.request
import urllib.error
import json
import sys

WORKER_BASE = "https://tradeai-pro.tradingpro.workers.dev"
RENDER_BASE = "https://trading-api-n1wt.onrender.com"

results = []

def log_check(name, status, detail=""):
    results.append({"name": name, "status": status, "detail": detail})
    icon = "[PASS]" if status == "PASS" else "[FAIL]"
    print(f"{icon} {name:50s}: {status} - {detail}")

def check_url(url, method="GET", headers=None, expected_status=200, check_fn=None):
    req_headers = {"User-Agent": "TradeAI-Audit/1.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body = resp.read() if method != "HEAD" else b""
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if status != expected_status:
                log_check(f"{method} {url}", "FAIL", f"Expected HTTP {expected_status}, got {status}")
                return
            if check_fn:
                ok, reason = check_fn(status, resp_headers, body)
                log_check(f"{method} {url}", "PASS" if ok else "FAIL", reason)
            else:
                log_check(f"{method} {url}", "PASS", f"HTTP {status}")
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read()
        resp_headers = {k.lower(): v for k, v in e.headers.items()}
        if status == expected_status:
            if check_fn:
                ok, reason = check_fn(status, resp_headers, body)
                log_check(f"{method} {url}", "PASS" if ok else "FAIL", reason)
            else:
                log_check(f"{method} {url}", "PASS", f"HTTP {status}")
        else:
            log_check(f"{method} {url}", "FAIL", f"Expected HTTP {expected_status}, got HTTP {status}")
    except Exception as ex:
        log_check(f"{method} {url}", "FAIL", f"Exception: {ex}")

print("\n=== 1. EDGE WORKER SECURITY HEADERS & INTEGRITY ===")

def verify_homepage(status, headers, body):
    text = body.decode("utf-8", errors="ignore")
    csp = headers.get("content-security-policy", "")
    hsts = headers.get("strict-transport-security", "")
    xcto = headers.get("x-content-type-options", "")
    xfo = headers.get("x-frame-options", "")
    has_brent = "74.25" in text
    has_sebi = "NOT SEBI REGISTERED" in text
    has_legal_anchors = "/legal#terms" in text and "/legal#privacy" in text
    
    missing = []
    if not csp: missing.append("CSP")
    if not hsts: missing.append("HSTS")
    if xcto != "nosniff": missing.append("X-Content-Type-Options")
    if xfo != "DENY": missing.append("X-Frame-Options")
    if has_brent: missing.append("Found placeholder $74.25")
    if not has_sebi: missing.append("Missing NOT SEBI REGISTERED")
    if not has_legal_anchors: missing.append("Missing direct /legal# anchors")
    
    if missing:
        return False, f"Issues: {', '.join(missing)}"
    return True, f"All headers present, no fake values, legal anchors direct"

check_url(f"{WORKER_BASE}/", "GET", expected_status=200, check_fn=verify_homepage)

print("\n=== 2. SPA DEEP-LINKS & FALLBACK ===")

def verify_spa(status, headers, body):
    ct = headers.get("content-type", "")
    csp = headers.get("content-security-policy", "")
    hsts = headers.get("strict-transport-security", "")
    if "text/html" not in ct:
        return False, f"Content-Type not text/html ({ct})"
    if not csp or not hsts:
        return False, "Missing CSP or HSTS on SPA fallback"
    return True, f"HTTP {status} text/html with CSP+HSTS"

check_url(f"{WORKER_BASE}/performance", "GET", headers={"Accept": "text/html"}, expected_status=200, check_fn=verify_spa)
check_url(f"{WORKER_BASE}/performance", "HEAD", expected_status=200, check_fn=verify_spa)
check_url(f"{WORKER_BASE}/portfolio", "GET", expected_status=200, check_fn=verify_spa)
check_url(f"{WORKER_BASE}/learning", "GET", expected_status=200, check_fn=verify_spa)
check_url(f"{WORKER_BASE}/crypto", "GET", expected_status=200, check_fn=verify_spa)

print("\n=== 3. LEGAL & VENDOR ASSETS ===")
check_url(f"{WORKER_BASE}/legal", "GET", expected_status=200)
check_url(f"{WORKER_BASE}/legal.html", "GET", expected_status=200)
check_url(f"{WORKER_BASE}/static/honesty.js", "GET", expected_status=200)
check_url(f"{WORKER_BASE}/static/vendor/chart.umd.min.js", "GET", expected_status=200)

print("\n=== 4. PROXIED API ENDPOINTS VIA WORKER ===")

def verify_api_json(status, headers, body):
    try:
        data = json.loads(body.decode("utf-8"))
        proxy_hdr = headers.get("x-edge-proxy", "")
        return True, f"HTTP 200 JSON ok, X-Edge-Proxy: {proxy_hdr}"
    except Exception as e:
        return False, f"JSON parse error: {e}"

check_url(f"{WORKER_BASE}/api/health", "GET", expected_status=200, check_fn=verify_api_json)
check_url(f"{WORKER_BASE}/api/overview/intelligence", "GET", expected_status=200, check_fn=verify_api_json)
check_url(f"{WORKER_BASE}/api/signals/all", "GET", expected_status=200, check_fn=verify_api_json)
check_url(f"{WORKER_BASE}/api/autonomous/status", "GET", expected_status=200, check_fn=verify_api_json)
check_url(f"{WORKER_BASE}/api/autonomous/feed", "GET", expected_status=200, check_fn=verify_api_json)
check_url(f"{WORKER_BASE}/api/research/next-day-picks", "GET", expected_status=200, check_fn=verify_api_json)

print("\n=== 5. ORIGIN API DOCS LOCKDOWN ===")
check_url(f"{RENDER_BASE}/docs", "GET", expected_status=404)
check_url(f"{RENDER_BASE}/openapi.json", "GET", expected_status=404)
check_url(f"{RENDER_BASE}/redoc", "GET", expected_status=404)
check_url(f"{RENDER_BASE}/api/health", "GET", expected_status=200)

print("\n=== SUMMARY ===")
passed = sum(1 for r in results if r["status"] == "PASS")
failed = sum(1 for r in results if r["status"] == "FAIL")
print(f"Total Checks: {len(results)} | Passed: {passed} | Failed: {failed}")
if failed > 0:
    sys.exit(1)
print("ALL LIVE PRODUCTION ENDPOINTS VERIFIED SUCCESSFULLY!")
