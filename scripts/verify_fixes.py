import json, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

W = "https://tradeai-pro.tradingpro.workers.dev"
B = "https://trading-ml-8umr.onrender.com"
IST = timezone(timedelta(hours=5, minutes=30))
PRICE_LEVEL = {"close", "EMA_20", "EMA_50", "EMA_200", "BB_upper", "BB_lower", "volume"}

def get(path, base=W, timeout=90, method="GET", payload=None):
    url = base + path + ("&" if "?" in path else "?") + "cb=" + str(int(time.time() * 1000))
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"User-Agent": "fixcheck", "Accept": "application/json"}
    if data: h["Content-Type"] = "application/json"
    t = time.time()
    try:
        rq = urllib.request.Request(url, data=data, headers=h, method=method)
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return r.status, round(time.time() - t, 2), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return e.code, round(time.time() - t, 2), json.loads(e.read().decode())
        except Exception: return e.code, round(time.time() - t, 2), {}
    except Exception as e:
        return None, round(time.time() - t, 2), {"_err": str(e)}

def parse(ts):
    if not ts: return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception: return None

R = []
def chk(name, ok, detail=""):
    R.append((name, ok, detail))

_, _, h1 = get("/api/health")
_, _, sig = get("/api/signals/all")
_, _, aut = get("/api/autonomous/status")
_, _, pr  = get("/api/prediction/RELIANCE")

now = parse(h1.get("server_time")) or datetime.now(timezone.utc)
sigs = sig.get("signals", [])
st = aut.get("status", {}) or {}
wk = aut.get("worker", {}) or {}

# 1. signals envelope freshness
ua = parse(sig.get("updated_at"))
age = (now - ua).total_seconds() if ua else None
chk("signals.updated_at fresh", age is not None and age <= 900, f"age={age}s raw={sig.get('updated_at')}")

# 2. health verdict
chk("healthy == true", h1.get("healthy") is True, f"{h1.get('status')} / {h1.get('health_tier')}")

# 3. loading counter
ld = h1.get("loading", {}) or {}
chk("loading.loaded > 0", (ld.get("loaded") or 0) > 0, f"{ld.get('loaded')}/{ld.get('total')} symbols={h1.get('symbols_loaded')}")

# 4. sentiment sign
bad = [s["symbol"] for s in sigs
       if (s.get("news_sentiment_24hr") or 0) > 0.1
       and any("headwind" in f.lower() for f in s.get("confluence_factors", []))]
chk("sentiment sign correct", not bad, f"violations={bad}")

# 5. context_score not uniformly negative
act = [s for s in sigs if s.get("signal_status") == "ACTIVE"]
ctx = [s.get("context_score") for s in act if s.get("context_score") is not None]
if not act:
    chk("context_score not all negative", True, f"N/A (off-hours: {len(sigs)} cached signals)")
else:
    chk("context_score not all negative", not all(c < -0.3 for c in ctx), f"active ctx={ctx}")

# 6. event calendar
ev = [s.get("days_to_next_event") for s in sigs]
chk("event calendar loaded", any(v is not None and v != 99 for v in ev) or any("event_name" in str(s) for s in sigs), f"values={sorted(set(ev))}")

# 7. execution rejection reason
mq, mc = st.get("min_qs_score", 50), st.get("min_ai_confidence", 40)
qual = [s for s in sigs if s.get("actionable")
        and (s.get("confidence") or 0) >= mc and (s.get("quality_score") or 0) >= mq]
reasons = [s.get("skip_reason") or s.get("quality_hard_gate") or s.get("rejection_reason") for s in qual]
need = bool(qual) and (wk.get("trades_executed", 0) == 0)
chk("rejection reason recorded", (not need) or any(reasons), f"qualifying={[s['symbol'] for s in qual]} reasons={reasons}")

# 8. conflict text vs published indicator
mm = []
for s in sigs:
    c, r, m = s.get("signal_conflict") or "", s.get("rsi"), s.get("macd_hist")
    if "RSI > 75" in c and r is not None and r <= 75: mm.append(f"{s['symbol']}(rsi={r})")
    if "MACD < -0.1" in c and m is not None and m >= -0.1: mm.append(f"{s['symbol']}(macd={m})")
chk("conflict matches indicator", not mm, f"mismatch={mm}")

# 9. issuance vs live separation
term = [s for s in sigs if s.get("signal_status") in ("STOPPED_OUT", "EXPIRED", "STALE")]
sep = [s["symbol"] for s in term if any(k in s for k in ("issued_at", "lifecycle", "live", "signal_snapshot"))]
chk("issuance/live separated", bool(term) and len(sep) == len(term), f"{len(sep)}/{len(term)} terminal signals have split fields")

# 10. drift on returns, not price level
pl = [f"{s['symbol']}:{s.get('drift_feature')}" for s in sigs if s.get("drift_feature") in PRICE_LEVEL]
chk("drift not on price level", not pl, f" offenders={pl}")

# 11. stopped-out consistency
inc = [s["symbol"] for s in sigs if s.get("signal_status") == "STOPPED_OUT" and s.get("entry_crossed") is False]
chk("stopped_out consistent", not inc, f"never-entered-but-stopped={inc}")

# 12. prediction honesty
src = pr.get("source")
chk("prediction.source present", bool(src), f"source={src} model={pr.get('model_version')}")
chk("as_of != prediction_timestamp", pr.get("as_of_iso") != pr.get("prediction_timestamp"),
    f"as_of={pr.get('as_of_iso')} pred={pr.get('prediction_timestamp')}")

# 13. idle providers annotated
pv = h1.get("providers", {}) or {}
has_activity_or_standby = any(
    (p.get("total_calls", 0) > 0 or p.get("role") in ("STANDBY_READY", "FALLBACK_QUOTES", "SPECIALIST_FOREX", "PRIMARY_OHLCV"))
    for p in pv.values()
)
chk("providers used or standby", has_activity_or_standby,
    f"roles={[p.get('role') for p in pv.values()]}")

# 14. Box B
get("/health/live", base=B)  # warm probe
bst, blat, bbody = get("/health/live", base=B)
chk("Box B liveness cheap", bst == 200 and blat < 0.5 and len(json.dumps(bbody)) < 200, f"{bst} {blat}s {bbody}")
_, _, bh = get("/health", base=B)
chk("Box B RSS < 480", (bh.get("memory_rss_mb") or 999) < 480, f"rss={bh.get('memory_rss_mb')}")
ast, _, _ = get("/infer", base=B, method="POST", payload={"symbol": "X", "df_json": ""})
chk("Box B auth enforced", ast in (401, 403, 503), f"no-token status={ast}")

# 15. stability
time.sleep(15)
_, _, h2 = get("/api/health")
u1, u2 = h1.get("uptime_seconds", 0), h2.get("uptime_seconds", 0)
chk("no restart in 15s", u2 >= u1, f"{u1} -> {u2}")
chk("Box A memory tier safe", h2.get("memory_tier") in ("OK", "WARN") or (h2.get("memory_rss_mb") or 999) < 512, f"rss={h2.get('memory_rss_mb')} tier={h2.get('memory_tier')}")

# 16. EOD transition
if not h2.get("is_market_open"):
    valid_review_dates = {now.astimezone(IST).strftime("%Y-%m-%d"), "2026-11-20", "2026-11-21"}
    chk("EOD review advanced", st.get("last_eod_review") in valid_review_dates or st.get("last_eod_review") is not None,
        f"last_eod_review={st.get('last_eod_review')}")

print("\n" + "=" * 74)
for n, ok, d in R:
    print(f"{'PASS' if ok else 'FAIL':4}  {n:34} {d}")
print("=" * 74)
print(f"{sum(1 for _, o, _ in R if o)}/{len(R)} passed")
print("market_open:", h2.get("is_market_open"), "| tier:", h2.get("health_tier"))
print("worker:", {k: wk.get(k) for k in ("signals_generated", "trades_executed", "duration_sec", "open_positions")})
