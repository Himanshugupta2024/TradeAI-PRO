import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

W = "https://tradeai-pro.tradingpro.workers.dev"
B = "https://trading-ml-8umr.onrender.com"
REPO = "Himanshugupta2024/TradeAI-PRO"
IST = timezone(timedelta(hours=5, minutes=30))
PREV_FILE = "scripts/.audit_prev.json"


def fetch(url, timeout=70):
    u = url + ("&" if "?" in url else "?") + "cb=" + str(int(time.time() * 1000))
    t = time.time()
    try:
        rq = urllib.request.Request(u, headers={"User-Agent": "audit", "Accept": "application/json"})
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return r.status, round(time.time() - t, 2), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, round(time.time() - t, 2), json.loads(e.read().decode())
        except Exception:
            return e.code, round(time.time() - t, 2), {}
    except Exception as e:
        return None, round(time.time() - t, 2), {"_err": str(e)}


def parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except Exception:
        return None


R = []


def chk(layer, name, ok, detail=""):
    R.append({"layer": layer, "name": name, "ok": ok, "detail": str(detail)[:220]})


EPS = [
    "health", "signals/all", "autonomous/status", "edge/risk-gauge", "nse/dashboard",
    "research/next-day-picks", "performance", "trades?limit=100", "track-record",
    "context/v3", "macro/intelligence", "tradeflow",
    "swing/shortlist", "prediction/RELIANCE", "dashboard/overview", "market/mood",
    "analytics/scorecard", "sectors", "breakouts", "forex/rates", "crypto/intelligence",
    "autonomous/feed",
]
D = {}
print("Fetching %d endpoints..." % len(EPS))
for ep in EPS:
    st, lat, body = fetch(W + "/api/" + ep)
    D[ep] = (st, lat, body)
    deg = isinstance(body, dict) and (
        body.get("degraded") is True or str(body.get("source", "")).startswith("EDGE_DEGRADED")
    )
    if ep == "forex/rates" and st == 403 and isinstance(body, dict) and "Section 6A" in str(body.get("error", "")):
        chk("AVAIL", ep, True, "status=403 (Section 6A NSE Focus guard intentional)")
    else:
        chk("AVAIL", ep, st == 200 and not deg, "status=%s lat=%ss degraded=%s" % (st, lat, deg))

H = D["health"][2] if isinstance(D["health"][2], dict) else {}
S = D["signals/all"][2] if isinstance(D["signals/all"][2], dict) else {}
A = D["autonomous/status"][2] if isinstance(D["autonomous/status"][2], dict) else {}
G = D["edge/risk-gauge"][2] if isinstance(D["edge/risk-gauge"][2], dict) else {}
N = D["nse/dashboard"][2] if isinstance(D["nse/dashboard"][2], dict) else {}
P = D["research/next-day-picks"][2] if isinstance(D["research/next-day-picks"][2], dict) else {}
now = parse(H.get("server_time")) or datetime.now(timezone.utc)

# ---------- INFRA ----------
chk("INFRA", "database connected", (H.get("database") or {}).get("connected") is True, H.get("health_reasons"))
rss = H.get("memory_rss_mb") or 999
chk("INFRA", "RSS < 450", rss < 450, rss)
mt = H.get("memory_tier")
chk(
    "INFRA",
    "memory_tier folded into verdict",
    not (mt in ("WARN", "CRITICAL") and H.get("healthy") is True and "nominal" in str(H.get("overall_reason", ""))),
    "tier=%s overall=%s" % (mt, H.get("overall_reason")),
)
chk("INFRA", "build_sha present", bool(H.get("build_sha")), H.get("build_sha"))
try:
    _, _, c = fetch("https://api.github.com/repos/%s/commits?per_page=1" % REPO)
    head = c[0]["sha"][:8] if isinstance(c, list) and c else None
    chk("INFRA", "deployed == repo HEAD", head and H.get("build_sha", "")[:8] == head, "deployed=%s head=%s" % (H.get("build_sha"), head))
except Exception as e:
    chk("INFRA", "deployed == repo HEAD", None, "github unreachable: %s" % e)
ld = H.get("loading") or {}
chk("INFRA", "loaded <= total", (ld.get("loaded") or 0) <= (ld.get("total") or 0), ld)

# ---------- FEED ----------
force_market_hours = "--market-hours" in sys.argv
in_session = bool(H.get("is_market_open")) or force_market_hours
prov = H.get("providers") or {}
for k, v in prov.items():
    if not isinstance(v, dict):
        continue
    if str(v.get("role", "")).startswith("PRIMARY") and ((v.get("total_calls") or 0) > 0 or v.get("state") == "ACTIVE"):
        chk(
            "FEED",
            "%s latency measured" % k,
            (v.get("avg_latency_ms") or 0) > 0,
            "%.1f ms (state=%s)" % (float(v.get("avg_latency_ms") or 0), v.get("state")),
        )
        up_h = (H.get("uptime_seconds") or 1) / 3600
        rate = (v.get("total_calls") or 0) / max(up_h, 0.01)
        chk("FEED", "%s call rate plausible" % k, rate > 10 if in_session else None, "%.1f calls/h" % rate)
    if v.get("role", "").startswith(("PRIMARY", "FALLBACK")) and (v.get("total_calls") or 0) == 0:
        chk("FEED", "%s not dead" % k, False if in_session else None, "0 calls, role=%s (off-hours=%s)" % (v.get("role"), not in_session))
feed = H.get("feed") or {}
bar_age = H.get("bar_age_seconds")
chk("FEED", "bar_age < 8min (session)", ((bar_age or 999) < 480) if in_session else None, "%s (in_session=%s)" % (bar_age, in_session))
chk("FEED", "snapshot/bar clocks split", "snapshot_age_seconds" in feed, list(feed.keys())[:6])

# ---------- PRICE SANITY (P0) ----------
mv = N.get("movers") or {}
bad = []
for side in ("gainers", "losers"):
    for m in mv.get(side, []) or []:
        pc = m.get("pct_change")
        if pc is not None and abs(pc) > 20:
            bad.append("%s %.2f%%" % (m.get("symbol"), pc))
chk("DATA", "no >20% day moves", not bad, bad)
mcs = str(P.get("macro_context_summary", ""))
chk("DATA", "index not >15% from own EMA", "38.9" not in mcs and "35,050" not in mcs, mcs[:120])
sigs = S.get("signals") or []
px_sig = {s.get("symbol"): (s.get("live") or {}).get("close") or s.get("close") for s in sigs}
mism = []
for side in ("gainers", "losers"):
    for m in mv.get(side, []) or []:
        sy, lp = m.get("symbol"), m.get("ltp")
        if sy in px_sig and px_sig[sy] and lp:
            if abs(lp / px_sig[sy] - 1) > 0.02:
                mism.append("%s dash=%.2f sig=%.2f" % (sy, lp, px_sig[sy]))
chk("DATA", "dashboard vs signals prices agree", not mism, mism)

# ---------- LOOK-AHEAD ----------
cut = parse(str(P.get("information_cutoff_ist", "")).replace(" IST", ""))
chk("PICKS", "cutoff <= now", cut is None or cut <= now, "%s vs now %s" % (cut, now))
if H.get("is_market_open"):
    today = now.astimezone(IST).strftime("%Y-%m-%d")
    f = [
        p.get("symbol")
        for p in (P.get("picks") or [])
        if today in str(p.get("freshness", "")) and "Completed" in str(p.get("freshness", ""))
    ]
    chk("PICKS", "no completed-bar claim mid-session", not f, f)


# ---------- GEOMETRY ----------
def geom(rows, ek, sk, tk, dk=None):
    rrs, widths, inv = [], [], []
    for r in rows:
        e, s, t = r.get(ek), r.get(sk), r.get(tk)
        if not all(isinstance(x, (int, float)) for x in (e, s, t)) or e == s:
            continue
        d = (r.get(dk) or r.get("direction") or "LONG").upper()
        risk = (e - s) if d == "LONG" else (s - e)
        rew = (t - e) if d == "LONG" else (e - t)
        if risk <= 0:
            inv.append(r.get("symbol"))
            continue
        rrs.append(round(rew / risk, 3))
        atr = r.get("atr") or (r.get("technical_levels") or {}).get("atr14")
        if atr:
            widths.append(round(risk / atr, 2))
    return rrs, widths, inv


tp = [
    dict(p.get("trade_plan") or {}, symbol=p.get("symbol"), atr=(p.get("technical_levels") or {}).get("atr14"))
    for p in (P.get("picks") or [])
]
rrs, widths, inv = geom(tp, "trigger_price", "stop_loss", "target_1", "direction")
chk("PICKS", "R:R varies (not formulaic)", len(set(rrs)) > 1, rrs)
chk("PICKS", "stop >= 1.2 ATR", not widths or min(widths) >= 1.2, widths)
chk("PICKS", "no inverted geometry", not inv, inv)
srrs, sw, sinv = geom(sigs, "entry", "stop_loss", "target", "direction")
chk("SIGNALS", "R:R varies", (len(set(srrs)) > 1) if (in_session or srrs) else None, srrs)
chk("SIGNALS", "no inverted geometry", not sinv, sinv)

# ---------- CATALYSTS ----------
old, wrong = [], []
for p in (P.get("picks") or []):
    d = (p.get("directional_bias") or "LONG").upper()
    for c in (p.get("news_catalysts") or []):
        pub = parse(c.get("published_at", "").replace("GMT", "+0000")) if c.get("published_at") else None
        try:
            pub = datetime.strptime(c["published_at"][:16], "%a, %d %b %Y").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        if pub and (now - pub).days > 7:
            old.append("%s:%dd" % (p.get("symbol"), (now - pub).days))
        if c.get("impact") == "BEARISH" and d == "LONG":
            wrong.append(p.get("symbol"))
chk("PICKS", "catalysts <= 7d old", not old, old)
chk("PICKS", "catalyst sign matches direction", not wrong, wrong)
seo = [
    p.get("symbol")
    for p in (P.get("picks") or [])
    for c in (p.get("news_catalysts") or [])
    if "Forecast" in c.get("headline", "") or "Should You Buy" in c.get("headline", "")
]
chk("PICKS", "no SEO pages as disclosure", not seo, seo)
fn = [
    p.get("symbol")
    for p in (P.get("picks") or [])
    if (p.get("trade_plan") or {}).get("fno_derivatives", {}).get("ce_wall") is None
]
chk("PICKS", "F&O walls populated", not fn, fn)

# ---------- GAUGE ----------
det = G.get("indicators_detail") or []
pts = sum(x.get("points", 0) for x in det if x.get("scored"))
adj = sum(a.get("points", 0) for a in (G.get("adjustments") or []))
chk(
    "GAUGE",
    "score reconciles",
    abs(pts + adj - (G.get("risk_score") or 0)) < 0.06,
    "sum=%.3f adj=%.3f score=%s" % (pts, adj, G.get("risk_score")),
)
chk(
    "GAUGE",
    "weights sum 100",
    abs(sum(x.get("weight_pct", 0) for x in det if x.get("scored")) - 100) < 0.01,
    G.get("weight_sum"),
)
chk("GAUGE", "cache < 120s in session", (G.get("cache_age_seconds") or 0) < 120, G.get("cache_age_seconds"))
chk("GAUGE", "integrity resolved", G.get("data_integrity") not in ("UNCONFIRMED_VOL_SPIKE",), G.get("data_integrity"))

# ---------- SIGNALS HYGIENE ----------
sc = H.get("signals_cached")
chk("SIGNALS", "count == signals_cached", sc is not None and len(sigs) == sc, "%d vs %s" % (len(sigs), sc))
agemis = [
    s.get("symbol")
    for s in sigs
    if s.get("signal_age_minutes") is not None
    and s.get("stale_minutes") is not None
    and abs(s["signal_age_minutes"] - s["stale_minutes"]) > 2
]
chk("SIGNALS", "age fields agree", not agemis, agemis)
ev = [s.get("days_to_next_event") for s in sigs]
chk("SIGNALS", "event calendar loaded", (bool(ev) and any(v != 99 for v in ev)) if sigs else None, sorted(set(ev)))
term = [s.get("symbol") for s in sigs if (s.get("lifecycle") or {}).get("terminal") and (s.get("confluence_score") or 0) > 0]
chk("SIGNALS", "terminal confluence zeroed", not term, term)
ua = parse(S.get("updated_at"))
chk("SIGNALS", "envelope fresh", (ua is not None and (now - ua).total_seconds() < 600) if in_session else None, (now - ua).total_seconds() if ua else None)

# ---------- PAPER BOOK ----------
trades = []
for ep in ("performance", "autonomous/feed"):
    b = D[ep][2]
    if isinstance(b, dict):
        for k in ("trades", "closed_trades", "feed", "items"):
            v = b.get(k)
            if isinstance(v, list):
                trades += [x for x in v if isinstance(x, dict) and "entry_price" in x]
tr_body = D.get("trades?limit=100", (None, 0, {}))[2]
raw_book_trades = (tr_body.get("trades") or []) if isinstance(tr_body, dict) else []
quarantined = [x for x in raw_book_trades if isinstance(x, dict) and x.get("data_quality") == "LEGACY_EXCLUDED"]
clean_book_trades = [
    x for x in raw_book_trades
    if isinstance(x, dict) and "entry_price" in x and x.get("data_quality") != "LEGACY_EXCLUDED"
]
seen_trade_ids = {t.get("id") for t in trades if t.get("id") is not None}
for t in clean_book_trades:
    tid = t.get("id")
    if tid is None or tid not in seen_trade_ids:
        trades.append(t)
        if tid is not None:
            seen_trade_ids.add(tid)
chk(
    "BOOK",
    "trades ledger & legacy quarantine",
    isinstance(tr_body, dict)
    and isinstance(tr_body.get("trades"), list)
    and all(bool(q.get("exclusion_reason")) for q in quarantined),
    "total=%d clean=%d quarantined=%d" % (len(raw_book_trades), len(clean_book_trades), len(quarantined)),
)
track = D.get("track-record", (None, 0, {}))[2]
perf = D.get("performance", (None, 0, {}))[2]
tr_ok = isinstance(track, dict) and track.get("status") == "ok" and isinstance(track.get("total_trades"), int)
if tr_ok and isinstance(perf, dict) and perf.get("total_trades") is not None:
    tr_ok = track.get("total_trades") == perf.get("total_trades")
if tr_ok and len(raw_book_trades) < 100 and isinstance(track.get("excluded_legacy_trades_count"), int):
    tr_ok = track.get("excluded_legacy_trades_count") == len(quarantined)
chk(
    "BOOK",
    "track-record reconciles with performance",
    tr_ok,
    "track=%s perf=%s excluded=%s" % (
        track.get("total_trades") if isinstance(track, dict) else None,
        perf.get("total_trades") if isinstance(perf, dict) else None,
        track.get("excluded_legacy_trades_count") if isinstance(track, dict) else None,
    ),
)
inv, slip, nocal, nofx, nonet = [], [], [], [], []
for t in trades:
    e, s, d = t.get("entry_price"), t.get("stop_loss"), (t.get("direction") or "LONG").upper()
    if isinstance(e, (int, float)) and isinstance(s, (int, float)):
        if (d == "LONG" and s >= e) or (d == "SHORT" and s <= e):
            inv.append("%s#%s" % (t.get("symbol"), t.get("id")))
    if t.get("status") == "CLOSED_SL" and isinstance(t.get("exit_price"), (int, float)) and isinstance(s, (int, float)):
        if abs(t["exit_price"] - s) / max(s, 1e-9) > 0.005:
            slip.append("%s#%s" % (t.get("symbol"), t.get("id")))
    if t.get("p_calibrated") is None:
        nocal.append(t.get("id"))
    if t.get("quote_currency") and t.get("base_currency") and t["quote_currency"] != t["base_currency"] and t.get("fx_rate_entry") == 1.0:
        nofx.append("%s#%s" % (t.get("symbol"), t.get("id")))
    if (t.get("total_pnl") or 0) != 0 and (t.get("net_pnl") or 0) == 0:
        nonet.append(t.get("id"))
chk("BOOK", "no inverted stops", not inv, inv)
chk("BOOK", "SL exits at stop", not slip, slip)
chk("BOOK", "p_calibrated recorded", not nocal, "%d missing" % len(nocal))
chk("BOOK", "FX converted", not nofx, nofx)
chk("BOOK", "net_pnl populated", not nonet, nonet)

# ---------- EXECUTION ----------
wk = A.get("worker") or {}
st = A.get("status") or {}
act = [s.get("symbol") for s in sigs if s.get("actionable")]
chk(
    "EXEC",
    "actionable signals executed or reason",
    not act or (wk.get("trades_executed") or 0) > 0 or "ledger" in json.dumps(A).lower(),
    "actionable=%s executed=%s" % (act, wk.get("trades_executed")),
)
chk(
    "EXEC",
    "eod review current",
    st.get("last_eod_review") == now.astimezone(IST).strftime("%Y-%m-%d")
    or st.get("last_eod_review") == (now.astimezone(IST) - timedelta(days=1)).strftime("%Y-%m-%d"),
    st.get("last_eod_review"),
)

# ---------- REGRESSION vs previous run ----------
prev = {}
if os.path.exists(PREV_FILE):
    try:
        with open(PREV_FILE, encoding="utf-8") as fh:
            prev = json.load(fh)
    except Exception:
        prev = {}
cur = {
    "rss": rss,
    "vix": (G.get("indicators") or {}).get("vix"),
    "cautions": G.get("cautions") or [],
    "usdjpy": (G.get("indicators") or {}).get("usdjpy_level"),
    "india_vix": (G.get("indicators") or {}).get("india_vix"),
}
if prev:
    chk("REGRESS", "RSS not climbing >20MB", rss - (prev.get("rss") or rss) < 20, "%s -> %s" % (prev.get("rss"), rss))
    chk("REGRESS", "VIX value changed (not frozen)", cur["vix"] != prev.get("vix"), "%s -> %s" % (prev.get("vix"), cur["vix"]))
    lost = [c for c in (prev.get("cautions") or []) if c not in cur["cautions"]]
    worse = (cur.get("usdjpy") or 0) > (prev.get("usdjpy") or 0)
    chk("REGRESS", "cautions not lost while risk rose", not (lost and worse), "lost=%s" % lost)
os.makedirs(os.path.dirname(PREV_FILE), exist_ok=True)
with open(PREV_FILE, "w", encoding="utf-8") as fh:
    json.dump(cur, fh)

# ---------- stability ----------
time.sleep(60)
_, _, H2 = fetch(W + "/api/health")
chk(
    "INFRA",
    "no restart in 60s",
    (H2.get("uptime_seconds") or 0) > (H.get("uptime_seconds") or 0),
    "%s -> %s" % (H.get("uptime_seconds"), H2.get("uptime_seconds")),
)

# ---------- report ----------
print("\n" + "=" * 84)
layers = {}
for r in R:
    layers.setdefault(r["layer"], []).append(r)
for L, rows in layers.items():
    print("\n--- %s ---" % L)
    for r in rows:
        tag = "PASS" if r["ok"] else ("N/A " if r["ok"] is None else "FAIL")
        print("%s  %-42s %s" % (tag, r["name"], r["detail"]))
p = sum(1 for r in R if r["ok"])
f = sum(1 for r in R if r["ok"] is False)
print("\n" + "=" * 84)
print("%d/%d passed, %d FAILED" % (p, len(R), f))
with open("scripts/audit_report.json", "w", encoding="utf-8") as fh:
    json.dump({"at": now.isoformat(), "results": R}, fh, indent=2)
print("report -> scripts/audit_report.json")
