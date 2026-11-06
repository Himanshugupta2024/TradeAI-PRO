# -*- coding: utf-8 -*-
"""
Verification script for all items in the user's checklist against live production:
https://tradeai-pro.tradingpro.workers.dev and https://trading-api-n1wt.onrender.com
"""

import urllib.request
import json
import ssl

ctx = ssl.create_default_context()
BASE = "https://tradeai-pro.tradingpro.workers.dev"

def check(url, method="GET", headers=None):
    hdrs = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as res:
            return res.getcode(), dict(res.headers), res.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:
        return 0, {}, str(e).encode()

print("="*60)
print("TRADEAI PRO PRODUCTION AUDIT & CHECKLIST VERIFICATION")
print("="*60)

# 1. Site load & Security Headers
c, h, b = check(f"{BASE}/")
print(f"1. Root (/) Status: {c}")
sec_headers = {
    'CSP': h.get('Content-Security-Policy', h.get('content-security-policy')),
    'HSTS': h.get('Strict-Transport-Security', h.get('strict-transport-security')),
    'X-Content-Type-Options': h.get('X-Content-Type-Options', h.get('x-content-type-options')),
    'X-Frame-Options': h.get('X-Frame-Options', h.get('x-frame-options')),
    'Referrer-Policy': h.get('Referrer-Policy', h.get('referrer-policy')),
}
for k, v in sec_headers.items():
    print(f"   - Header {k}: {'PRESENT (' + v[:40] + '...)' if v else 'MISSING'}")

# 2. Legal links
c_leg, h_leg, b_leg = check(f"{BASE}/legal")
print(f"2. /legal route: {c_leg} (length: {len(b_leg)})")

# 3. API Docs disabled in prod
c_doc, _, _ = check(f"{BASE}/docs")
c_oa, _, _ = check(f"{BASE}/openapi.json")
print(f"3. /docs status: {c_doc} | /openapi.json status: {c_oa} (Expected 404 in prod)")

# 4. SPA deep link (/performance)
c_spa, _, b_spa = check(f"{BASE}/performance")
print(f"4. SPA /performance: {c_spa} (Returned HTML: {'<!DOCTYPE html>' in b_spa.decode('utf-8', errors='ignore')})")

# 5. Missing asset 404 test
c_miss, _, b_miss = check(f"{BASE}/static/does-not-exist-xyz.png")
print(f"5. Missing asset (/static/does-not-exist-xyz.png): {c_miss}")

# 6. Morning scan shape test
c_ms, _, b_ms = check(f"{BASE}/api/morning-scan")
try:
    ms_json = json.loads(b_ms.decode())
    print(f"6. /api/morning-scan: {c_ms} | Keys: {list(ms_json.keys())}")
except Exception as e:
    print(f"6. /api/morning-scan: {c_ms} | Non-JSON response")

# 7. /api/paper/open structure
c_po, _, b_po = check(f"{BASE}/api/paper/open")
try:
    po_json = json.loads(b_po.decode())
    print(f"7. /api/paper/open: {c_po} | Keys: {list(po_json.keys())}")
    print(f"   unpriced_positions present? {'unpriced_positions' in po_json}")
    print(f"   pnl_status present? {'pnl_status' in po_json}")
except Exception as e:
    print(f"7. /api/paper/open: {c_po} | Non-JSON response")

# 8. /track-record and /api/track-record
c_tr_page, _, b_tr_page = check(f"{BASE}/track-record")
c_tr_api, _, b_tr_api = check(f"{BASE}/api/track-record")
print(f"8. /track-record page status: {c_tr_page}")
print(f"   /api/track-record status: {c_tr_api}")

# 9. Health & Worker Run
c_hlth, _, b_hlth = check(f"{BASE}/api/health")
try:
    hlth_json = json.loads(b_hlth.decode())
    print(f"9. /api/health: {c_hlth}")
    print(f"   worker_health: {hlth_json.get('worker_health')}")
    print(f"   feed state: {hlth_json.get('feed', {}).get('state')}")
    print(f"   symbols_loaded: {hlth_json.get('symbols_loaded')}")
except Exception as e:
    print(f"9. /api/health: {c_hlth} | Error parsing JSON")

# 10. Portfolio & Closed Trades
c_port, _, b_port = check(f"{BASE}/api/portfolio")
try:
    port_json = json.loads(b_port.decode())
    print(f"10. /api/portfolio: {c_port}")
    print(f"    total_open: {port_json.get('total_open')}")
    print(f"    total_closed: {port_json.get('total_closed')}")
except Exception as e:
    print(f"10. /api/portfolio: {c_port}")

# 11. Section 6B: Next-Day Picks live status
c_ndp, _, b_ndp = check(f"{BASE}/api/research/next-day-picks")
try:
    ndp_json = json.loads(b_ndp.decode())
    print(f"11. Section 6B Next-Day Picks: {c_ndp} | report_id: {ndp_json.get('report_id')} | picks: {len(ndp_json.get('picks', []))}")
except Exception as e:
    print(f"11. Section 6B Next-Day Picks: {c_ndp}")

print("="*60)
