/**
 * TradeAI PRO — Cloudflare Edge Worker with Reverse Proxy, Explicit SWR Caching & Secure Routing.
 * 
 * Architecture:
 * - Public GET Endpoints: Allowed shared edge caching with explicit RFC 5861 Stale-While-Revalidate (SWR).
 * - Private / User State Endpoints: Strict cache BYPASS, Cache-Control: private, no-store, preserve all headers.
 * - WebSockets (/ws/*): Upgraded and proxied to Render origin backend.
 * - Static Assets: Served directly via Cloudflare ASSETS binding.
 */

const BACKEND_HOST = "trading-api-n1wt.onrender.com";
const BACKEND_ORIGIN = `https://${BACKEND_HOST}`;

// Unified Cache Policy (Seconds)
const BROWSER_TTL_SEC = 5;
const EDGE_FRESH_TTL_SEC = 15;
const EDGE_SWR_WINDOW_SEC = 30;
const PHYSICAL_EDGE_TTL_SEC = 600; // 600 seconds (10 minutes) total edge retention

const UNIFIED_CACHE_CONTROL = `public, max-age=${BROWSER_TTL_SEC}, s-maxage=${EDGE_FRESH_TTL_SEC}, stale-while-revalidate=${EDGE_SWR_WINDOW_SEC}`;

// Security headers applied to every static asset response.
// NOTE: the dashboard ships inline <script> blocks and inline event handlers, so
// 'unsafe-inline' is required until those are extracted. Chart.js is self-hosted
// from /static/vendor to avoid a third-party script origin.
const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' https://fonts.gstatic.com data:",
  "img-src 'self' data: blob: https://api.qrserver.com https://*.githubusercontent.com",
  "connect-src 'self' wss://tradeai-pro.tradingpro.workers.dev https://tradeai-pro.tradingpro.workers.dev https://trading-api-n1wt.onrender.com wss://trading-api-n1wt.onrender.com https://api.binance.com https://fapi.binance.com https://data-api.binance.vision https://api.coingecko.com",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const SECURITY_HEADERS = {
  "Content-Security-Policy": CONTENT_SECURITY_POLICY,
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
  "Cross-Origin-Opener-Policy": "same-origin",
};

function harden(response) {
  const headers = new Headers(response.headers);
  for (const [key, value] of Object.entries(SECURITY_HEADERS)) {
    headers.set(key, value);
  }
  if (response.status === 204 || response.status === 304) {
    return new Response(null, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

// Strict Allowlist of Public Market-Data Endpoints eligible for shared edge caching.
// /api/health/live is deliberately absent: caching a liveness probe lets a stale
// "alive" answer mask a dead origin for up to the full SWR window.
const PUBLIC_CACHEABLE_PATHS = new Set([
  "/api/overview/intelligence",
  "/api/dashboard/overview",
  "/api/nse/dashboard",
  "/api/stats",
  "/api/crypto/intelligence",
  "/api/morning-scan",
  "/api/market-control/status",
  "/api/autonomous/feed",
  "/api/autonomous/status",
  "/api/signals/all",
  "/api/signals/winrate",
  "/api/signals/leaderboard",
  "/api/market/mood",
  "/api/context/market-mood",
  "/api/context/events",
  "/api/screener/gainers",
  "/api/screener/losers",
  "/api/screener/opportunities",
  "/api/breakouts",
  "/api/swing/shortlist",
  "/api/swing/stats",
  "/api/forex/signals",
  "/api/forex/rates",
  "/api/forex/intelligence",
  "/api/forex/sessions",
  "/api/macro/intelligence",
  "/api/edge/macro-heatmap",
  "/api/edge/risk-gauge",
  "/api/research/next-day-picks",
  "/api/research/next-day-picks/history",
]);

// Health and readiness routes must NEVER be cached by Cloudflare edge.
// Caching them allows origin downtime or cold starts to be masked by stale responses.
function isHealthOrReadinessPath(pathname) {
  return (
    pathname === "/api/health" ||
    pathname.startsWith("/api/health/") ||
    pathname === "/api/ready" ||
    pathname.startsWith("/api/ready/") ||
    pathname === "/api/system/data-health"
  );
}

function isPathCacheable(pathname) {
  if (isHealthOrReadinessPath(pathname)) return false;
  if (PUBLIC_CACHEABLE_PATHS.has(pathname)) return true;
  if (
    pathname.startsWith("/api/backtest/") ||
    pathname.startsWith("/api/news/") ||
    pathname.startsWith("/api/search/") ||
    pathname.startsWith("/api/swing/symbol/") ||
    pathname.startsWith("/api/forex/ohlcv/") ||
    pathname.startsWith("/api/watchlist/quotes")
  ) {
    return true;
  }
  return false;
}

// User state must never enter the shared edge cache. Portfolio, paper book and
// trade history are per-account data: caching them publicly (or copying them
// into a shared cache keyed only by URL) leaks one user's positions to another.
function isPrivateStatePath(pathname) {
  return (
    pathname.startsWith("/api/paper/") ||
    pathname.startsWith("/api/portfolio") ||
    pathname.startsWith("/api/trades") ||
    pathname.startsWith("/api/positions") ||
    pathname.startsWith("/api/orders") ||
    pathname.startsWith("/api/journal") ||
    pathname.startsWith("/api/capital") ||
    pathname.startsWith("/api/risk")
  );
}

// Helper to sanitize headers forwarded to Render origin.
// Crucial: Strip cf-* and x-forwarded-* headers because Render's Cloudflare edge
// treats incoming CF-Connecting-IP / CF-Ray from an external caller as spoofing and blocks with HTTP 403 / 429.
function createCleanOriginHeaders(incomingHeaders) {
  const clean = new Headers();
  for (const [key, value] of incomingHeaders.entries()) {
    const k = key.toLowerCase();
    if (
      k.startsWith("cf-") ||
      k.startsWith("x-forwarded-") ||
      k.startsWith("x-real-") ||
      k === "host" ||
      k === "if-none-match" ||
      k === "if-modified-since"
    ) {
      continue;
    }
    clean.set(key, value);
  }
  clean.set("Host", BACKEND_HOST);
  clean.set(
    "User-Agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
  );
  return clean;
}

// Helper to attach anti-poisoning cache headers to prevent downstream CDN/browser caching of errors
function applyAntiPoisoningHeaders(headers) {
  headers.set("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0");
  headers.set("Pragma", "no-cache");
}

// Graceful degraded JSON fallback for endpoints during origin stalls / 429 / 503
export function getDegradedFallbackResponse(pathname) {
  const normPath = pathname.replace(/\/+$/, "");
  const headers = new Headers({
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "CF-Cache-Status": "DEGRADED-FALLBACK",
    "X-Edge-Proxy": "TradeAI-Cloudflare-Worker",
    "Cache-Control": "public, max-age=0, must-revalidate, stale-while-revalidate=10",
  });

  if (normPath === "/api/autonomous/feed") {
    const nowIso = new Date().toISOString();
    const payload = {
      ok: true,
      feed: [
        {
          id: `standby-${Date.now()}`,
          timestamp: nowIso,
          action: "SYSTEM_STANDBY",
          symbol: "SYSTEM",
          direction: "NEUTRAL",
          price: 0.0,
          qs_score: 0.0,
          ai_confidence: 0.0,
          reason: "Autonomous worker operational. Edge degraded fallback active while origin settles.",
          asset_class: "SYSTEM",
        },
      ],
      count: 1,
      degraded: true,
    };
    return new Response(JSON.stringify(payload), { status: 200, headers });
  }

  if (normPath === "/api/macro/intelligence") {
    const nowIso = new Date().toISOString();
    const payload = {
      status: "ok",
      macro: {
        timestamp: nowIso,
        brent_crude: { symbol: "BZ=F", name: "Brent Crude Oil", price: 82.5, change_pct: 0.0, unit: "$/bbl", status: "NEUTRAL" },
        wti_crude: { symbol: "CL=F", name: "WTI Crude Oil", price: 78.2, change_pct: 0.0, unit: "$/bbl", status: "NEUTRAL" },
        us_10y_yield: { symbol: "^TNX", name: "US 10-Year Yield", price: 4.25, change_pct: 0.0, unit: "%", status: "NEUTRAL" },
        dxy_dollar_index: { symbol: "DX-Y.NYB", name: "US Dollar Index", price: 104.1, change_pct: 0.0, unit: "pts", status: "NEUTRAL" },
        sp500: { symbol: "^GSPC", name: "S&P 500 (US)", price: 5000.0, change_pct: 0.0, unit: "pts", status: "NEUTRAL" },
        nasdaq: { symbol: "^IXIC", name: "Nasdaq Composite", price: 16000.0, change_pct: 0.0, unit: "pts", status: "NEUTRAL" },
        gold: { symbol: "GC=F", name: "Gold", price: 2350.0, change_pct: 0.0, unit: "$/oz", status: "NEUTRAL" },
        macro_regime: "NEUTRAL",
        macro_cue_summary: "Macro radar on standby. Indicators in neutral posture while origin settles.",
        active_sector_alerts: [],
        breaking_news: [],
        feed_source: "DEGRADED_EDGE_FALLBACK",
      },
      degraded: true,
    };
    return new Response(JSON.stringify(payload), { status: 200, headers });
  }

  return null;
}

// Helper to strip anti-cache query parameters (_ts, _, timestamp, t) and canonicalize parameters so edge cache keys are uniform
export function getNormalizedCacheKey(rawUrl) {
  try {
    const u = new URL(rawUrl);
    u.searchParams.delete("_ts");
    u.searchParams.delete("_");
    u.searchParams.delete("timestamp");
    u.searchParams.delete("t");

    // Guardrail 2: Normalize symbols parameter (split, trim, uppercase, dedup, sort)
    if (u.searchParams.has("symbols")) {
      const rawSyms = u.searchParams.get("symbols") || "";
      const sortedSyms = rawSyms
        .split(",")
        .map(s => s.trim().toUpperCase())
        .filter(Boolean)
        .sort();
      const dedupedSyms = [...new Set(sortedSyms)];
      if (dedupedSyms.length > 0) {
        u.searchParams.set("symbols", dedupedSyms.join(","));
      } else {
        u.searchParams.delete("symbols");
      }
    }

    // Sort query parameters for canonical cache key ordering
    u.searchParams.sort();

    return u.toString();
  } catch (_) {
    return rawUrl;
  }
}

// Helper to store response in Cloudflare edge cache using exact request.url string
async function storeInEdgeCache(cache, urlStr, originResponse) {
  if (!originResponse.ok || originResponse.status !== 200) return; // strictly cache only HTTP 200
  if (!cache || typeof cache.put !== "function") return;
  try {
    const normUrl = getNormalizedCacheKey(urlStr);
    const now = Date.now();
    const cacheHeaders = new Headers(originResponse.headers);
    cacheHeaders.set("X-SWR-Cached-At", String(now));
    // Physical retention for full fresh + SWR window
    cacheHeaders.set("Cache-Control", `public, max-age=${PHYSICAL_EDGE_TTL_SEC}, s-maxage=${PHYSICAL_EDGE_TTL_SEC}`);
    // Delete vary on public JSON so cache matches across all user-agents/headers
    cacheHeaders.delete("vary");
    cacheHeaders.set("Access-Control-Allow-Origin", "*");

    const cachedResponse = new Response(originResponse.body, {
      status: originResponse.status,
      statusText: originResponse.statusText,
      headers: cacheHeaders,
    });
    await cache.put(normUrl, cachedResponse);
  } catch (err) {
    // Non-fatal cache store failure
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // 1. Handle WebSocket Upgrade (/ws/*)
    if (url.pathname.startsWith("/ws/")) {
      const targetWsUrl = new URL(url.pathname + url.search, BACKEND_ORIGIN);
      if (request.headers.get("Upgrade") === "websocket") {
        const cleanWsHeaders = createCleanOriginHeaders(request.headers);
        const wsRequest = new Request(targetWsUrl, {
          method: "GET",
          headers: cleanWsHeaders,
        });
        return fetch(wsRequest);
      }
      return new Response("Expected WebSocket Upgrade", { status: 426 });
    }

    // 2. Handle API routes (/api/*)
    if (url.pathname.startsWith("/api/")) {
      const targetUrl = new URL(url.pathname + url.search, BACKEND_ORIGIN);
      const isGet = request.method === "GET";
      const isPublicCacheable = isGet && isPathCacheable(url.pathname);
      const cache = typeof caches !== "undefined" ? caches.default : null;

      // Check Edge Cache only for approved public GET endpoints
      const cacheKey = isPublicCacheable ? getNormalizedCacheKey(request.url) : request.url;
      if (isPublicCacheable && cache) {
        const cachedResponse = await cache.match(cacheKey);
        if (cachedResponse) {
          const cachedAt = parseInt(cachedResponse.headers.get("X-SWR-Cached-At") || "0", 10);
          const ageSec = Math.max(0, Math.floor((Date.now() - cachedAt) / 1000));

          if (ageSec <= EDGE_FRESH_TTL_SEC) {
            // Case A: FRESH HIT (0 - 15s)
            const res = new Response(cachedResponse.body, cachedResponse);
            res.headers.set("CF-Cache-Status", "HIT");
            res.headers.set("Age", String(ageSec));
            res.headers.set("Cache-Control", UNIFIED_CACHE_CONTROL);
            res.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
            res.headers.set("Access-Control-Allow-Origin", "*");
            return res;
          } else if (ageSec <= PHYSICAL_EDGE_TTL_SEC) {
            // Case B: STALE HIT (16s - 45s) -> True RFC 5861 Stale-While-Revalidate!
            // 1. Spawn asynchronous revalidation in background
            const revalTask = (async () => {
              try {
                const revalHeaders = createCleanOriginHeaders(request.headers);

                const revalReq = new Request(targetUrl, {
                  method: "GET",
                  headers: revalHeaders,
                });
                const revalRes = await fetch(revalReq);
                if (revalRes.status === 200) {
                  await storeInEdgeCache(cache, cacheKey, revalRes);
                }
              } catch (revalErr) {
                // Background fetch failed, stale entry remains until physical expiry
              }
            })();
            if (ctx && typeof ctx.waitUntil === "function") {
              ctx.waitUntil(revalTask);
            }

            // 2. Return stale cached data immediately to client (<20ms!)
            const res = new Response(cachedResponse.body, cachedResponse);
            res.headers.set("CF-Cache-Status", "STALE");
            res.headers.set("Age", String(ageSec));
            res.headers.set("Cache-Control", UNIFIED_CACHE_CONTROL);
            res.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
            res.headers.set("Access-Control-Allow-Origin", "*");
            return res;
          }
          // Case C: ageSec > 45s falls through to synchronous origin fetch
        }
      }

      // Forward request to Render Origin with clean headers
      const originHeaders = createCleanOriginHeaders(request.headers);

      const originRequest = new Request(targetUrl, {
        method: request.method,
        headers: originHeaders,
        body: request.method !== "GET" && request.method !== "HEAD" ? request.body : undefined,
        redirect: "follow",
      });

      let originResponse;
      try {
        originResponse = await fetch(originRequest);
      } catch (err) {
        if (isPublicCacheable && cache) {
          try {
            const stale = await cache.match(cacheKey);
            if (stale) {
              const res = new Response(stale.body, stale);
              res.headers.set("CF-Cache-Status", "STALE-FALLBACK-ERR");
              res.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
              res.headers.set("Access-Control-Allow-Origin", "*");
              res.headers.set("Cache-Control", "public, max-age=0, must-revalidate, stale-while-revalidate=10");
              return res;
            }
          } catch (_) {}
        }
        const fallback = getDegradedFallbackResponse(url.pathname);
        if (fallback) return fallback;

        const errHeaders = new Headers({
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*",
          "X-Edge-Proxy": "TradeAI-Cloudflare-Worker",
        });
        applyAntiPoisoningHeaders(errHeaders);
        return new Response(JSON.stringify({ error: "Backend origin unavailable", detail: String(err) }), {
          status: 502,
          headers: errHeaders,
        });
      }

      // If origin returns 502, 503, 429, or 504, fallback to stale edge cache if available.
      // NOTE: 403 (Section 6A Focus Mode / admin lock) is strictly excluded so administrative locks are honored immediately.
      if (isPublicCacheable && cache && (originResponse.status === 502 || originResponse.status === 503 || originResponse.status === 429 || originResponse.status === 504)) {
        try {
          const stale = await cache.match(cacheKey);
          if (stale) {
            const res = new Response(stale.body, stale);
            res.headers.set("CF-Cache-Status", "STALE-FALLBACK-503");
            res.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
            res.headers.set("Access-Control-Allow-Origin", "*");
            res.headers.set("Cache-Control", "public, max-age=0, must-revalidate, stale-while-revalidate=10");
            return res;
          }
        } catch (_) {}
        const fallback = getDegradedFallbackResponse(url.pathname);
        if (fallback) return fallback;
      } else if (originResponse.status === 429 || originResponse.status === 503) {
        const fallback = getDegradedFallbackResponse(url.pathname);
        if (fallback) return fallback;
      }

      // Public Cacheable: Store in cache and return MISS with unified headers
      if (isPublicCacheable && originResponse.status === 200) {
        if (cache) {
          const storePromise = storeInEdgeCache(cache, cacheKey, originResponse.clone());
          if (ctx && typeof ctx.waitUntil === "function") {
            ctx.waitUntil(storePromise);
          } else {
            await storePromise;
          }
        }

        const clientHeaders = new Headers(originResponse.headers);
        clientHeaders.set("CF-Cache-Status", "MISS");
        clientHeaders.set("Cache-Control", UNIFIED_CACHE_CONTROL);
        clientHeaders.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
        clientHeaders.set("Access-Control-Allow-Origin", "*");

        return new Response(originResponse.body, {
          status: originResponse.status,
          statusText: originResponse.statusText,
          headers: clientHeaders,
        });
      }

      // Ensure API endpoints NEVER return HTML challenge pages to JSON consumers
      const originContentType = (originResponse.headers.get("content-type") || "").toLowerCase();
      if (!originContentType.includes("application/json") && originResponse.status >= 400) {
        const errorHeaders = new Headers({
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*",
          "X-Edge-Proxy": "TradeAI-Cloudflare-Worker",
        });
        applyAntiPoisoningHeaders(errorHeaders);
        if (originResponse.status === 429) {
          const retryAfter = originResponse.headers.get("retry-after");
          if (retryAfter) {
            errorHeaders.set("Retry-After", retryAfter);
          }
        }
        return new Response(
          JSON.stringify({
            error: originResponse.status === 429 
              ? "Rate limit in effect. Retrying shortly..." 
              : `Origin returned error status ${originResponse.status}`,
            status: originResponse.status
          }),
          {
            status: originResponse.status,
            headers: errorHeaders,
          }
        );
      }

      // Non-public / Private / Mutating endpoints: Explicit BYPASS & Private Caching
      const clientHeaders = new Headers(originResponse.headers);
      const privateState = isPrivateStatePath(url.pathname);
      clientHeaders.set("CF-Cache-Status", "BYPASS");
      clientHeaders.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
      if (privateState) {
        // Never advertise another account's positions to arbitrary origins.
        clientHeaders.delete("Access-Control-Allow-Origin");
      } else {
        clientHeaders.set("Access-Control-Allow-Origin", "*");
      }

      // Anti-poisoning headers on any 4xx/5xx responses to prevent downstream caching
      if (originResponse.status >= 400) {
        applyAntiPoisoningHeaders(clientHeaders);
        if (originResponse.status === 429) {
          const retryAfter = originResponse.headers.get("retry-after");
          if (retryAfter) {
            clientHeaders.set("Retry-After", retryAfter);
          }
        }
      } else if (isHealthOrReadinessPath(url.pathname)) {
        clientHeaders.set("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0");
        clientHeaders.set("Pragma", "no-cache");
      } else if (
        privateState ||
        request.method !== "GET"
      ) {
        clientHeaders.set("Cache-Control", "private, no-cache, no-store, must-revalidate");
        clientHeaders.set("Pragma", "no-cache");
      }

      return new Response(originResponse.body, {
        status: originResponse.status,
        statusText: originResponse.statusText,
        headers: clientHeaders,
      });
    }

    // 3. Static asset delivery with SPA fallback + security headers
    if (env.ASSETS) {
      const isHtml = (response) =>
        (response.headers.get("content-type") || "").toLowerCase().includes("text/html");
      const looksLikeAsset = (pathname) =>
        pathname.startsWith("/static/") || /\.[a-z0-9]{2,5}$/i.test(pathname);
      const notFound = () =>
        new Response("Not Found", {
          status: 404,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });

      let assetResponse = await env.ASSETS.fetch(request);

      // `not_found_handling: single-page-application` turns every missing path
      // into a 200 index.html. For asset-looking requests that is a lie: a
      // missing .css/.js must be a 404, not HTML (browsers reject it anyway).
      if (
        isHtml(assetResponse) &&
        looksLikeAsset(url.pathname) &&
        !url.pathname.endsWith(".html")
      ) {
        let resolved = null;
        if (url.pathname.startsWith("/static/")) {
          const strippedPath = url.pathname.replace(/^\/static/, "");
          const strippedReq = new Request(new URL(strippedPath + url.search, url), request);
          const retryRes = await env.ASSETS.fetch(strippedReq);
          if (retryRes.status === 200 && !isHtml(retryRes)) {
            resolved = retryRes;
          }
        }
        if (!resolved) {
          return harden(notFound());
        }
        assetResponse = resolved;
      }

      // If requested under /static/ and not found, retry without /static/ prefix
      if (assetResponse.status === 404 && url.pathname.startsWith("/static/")) {
        const strippedPath = url.pathname.replace(/^\/static/, "");
        const strippedReq = new Request(new URL(strippedPath + url.search, url), request);
        const retryRes = await env.ASSETS.fetch(strippedReq);
        if (retryRes.status === 200) {
          assetResponse = retryRes;
        }
      }

      // Client-side tab routes (/performance, /portfolio, ...) must deep-link to
      // the dashboard shell instead of returning a blank 404.
      const isGetOrHead = request.method === "GET" || request.method === "HEAD";
      const wantsHtml = (request.headers.get("accept") || "").includes("text/html") || !url.pathname.includes(".");
      const isRedirectLoop = (assetResponse.status === 307 || assetResponse.status === 308) &&
        (assetResponse.headers.get("location") === url.pathname || (assetResponse.headers.get("location") || "").startsWith("/admin"));

      if ((assetResponse.status === 404 || isRedirectLoop) && isGetOrHead && wantsHtml) {
        if (url.pathname === "/admin" || url.pathname === "/admin/" || url.pathname === "/admin.html") {
          const adminResponse = await env.ASSETS.fetch(
            new Request(new URL("/admin.html", url), { method: "GET", headers: request.headers })
          );
          if (adminResponse.status === 200) {
            const htmlHeaders = new Headers(adminResponse.headers);
            htmlHeaders.set("Content-Type", "text/html; charset=utf-8");
            htmlHeaders.set("Cache-Control", "private, no-cache, no-store, must-revalidate");
            return harden(
              new Response(request.method === "HEAD" ? null : adminResponse.body, {
                status: 200,
                statusText: "OK",
                headers: htmlHeaders,
              })
            );
          }
        }

        const indexResponse = await env.ASSETS.fetch(
          new Request(new URL("/index.html", url), { method: "GET", headers: request.headers })
        );
        if (indexResponse.status === 200) {
          const htmlHeaders = new Headers(indexResponse.headers);
          htmlHeaders.set("Content-Type", "text/html; charset=utf-8");
          htmlHeaders.set("Cache-Control", "public, max-age=0, must-revalidate");
          return harden(
            new Response(request.method === "HEAD" ? null : indexResponse.body, {
              status: 200,
              statusText: "OK",
              headers: htmlHeaders,
            })
          );
        }
      }

      return harden(assetResponse);
    }

    return harden(
      new Response("Not Found", {
        status: 404,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      })
    );
  },

  /**
   * Cloudflare Cron Trigger scheduled handler.
   * Runs every 5 minutes to ping the Render backend health endpoint,
   * keeping the Render free container warm even when GitHub Actions is disabled.
   *
   * It also dispatches the autonomous cycle workflow every 20 minutes during the
   * NSE session. GitHub's own `schedule` cron silently drops runs on low-traffic
   * repositories (on 2026-11-09 it delivered zero runs for a whole session), so
   * the scheduler lives on Cloudflare's edge instead of relying on GitHub.
   */
  async scheduled(event, env, ctx) {
    return handleScheduled(event, env, ctx);
  },
};

const CYCLE_REPO = "Himanshugupta2024/TradeAI-PRO";
const CYCLE_WORKFLOW_FILE = "autonomous_cycle.yml";
const CYCLE_DISPATCH_INTERVAL_MIN = 20;

/** Wall-clock parts in IST (UTC+5:30) for a given instant. */
export function istClock(date = new Date()) {
  const ist = new Date(date.getTime() + 5.5 * 3600 * 1000);
  return {
    weekday: ist.getUTCDay(), // 0=Sun
    minutesOfDay: ist.getUTCHours() * 60 + ist.getUTCMinutes(),
    minuteOfHour: ist.getUTCMinutes(),
  };
}

/**
 * Decide whether this cron tick should dispatch a cycle.
 * Cron fires every 5 minutes; cadence check keeps it to one dispatch per 20 minutes.
 */
export function shouldDispatchCycle(date = new Date()) {
  const { weekday, minutesOfDay, minuteOfHour } = istClock(date);
  if (weekday < 1 || weekday > 5) return { dispatch: false, reason: "weekend" };
  if (minutesOfDay < 9 * 60 + 15 || minutesOfDay > 15 * 60 + 30) {
    return { dispatch: false, reason: "outside_session" };
  }
  if (minuteOfHour % CYCLE_DISPATCH_INTERVAL_MIN !== 0) {
    return { dispatch: false, reason: "off_cadence" };
  }
  return { dispatch: true, reason: "session_cadence" };
}

async function dispatchAutonomousCycle(env, fetchFn, now) {
  if (!env || !env.GH_PAT) return { dispatched: false, reason: "no_token" };
  const decision = shouldDispatchCycle(now);
  if (!decision.dispatch) return { dispatched: false, reason: decision.reason };
  try {
    const res = await fetchFn(
      `https://api.github.com/repos/${CYCLE_REPO}/actions/workflows/${CYCLE_WORKFLOW_FILE}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_PAT}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": "TradeAI-Cloudflare-Cron-Scheduler/1.0",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );
    const ok = res.status === 201 || res.status === 204;
    if (!ok) console.error(`[cron] cycle dispatch rejected: HTTP ${res.status}`);
    return { dispatched: ok, status: res.status, reason: decision.reason };
  } catch (err) {
    console.error("[cron] cycle dispatch failed:", err);
    return { dispatched: false, reason: "dispatch_error", error: String(err) };
  }
}

export async function handleScheduled(event, env, ctx, fetchFn = fetch, now = new Date()) {
  // Keep-warm ping. Probe the cheap liveness route, not /api/health: the rich
  // health endpoint walks the DB, model registry and feed state, so a 5-minute
  // cron hammering it burns the free instance's CPU budget for no benefit.
  const healthUrl = `${BACKEND_ORIGIN}/api/health/live`;
  try {
    const res = await fetchFn(healthUrl, {
      method: "GET",
      headers: {
        "User-Agent": "TradeAI-Cloudflare-Cron-KeepAlive/1.0",
        "Accept": "application/json",
      },
    });
    const cycle = await dispatchAutonomousCycle(env, fetchFn, now);
    return { ok: res.ok, status: res.status, url: healthUrl, cycle };
  } catch (err) {
    console.error(`[cron] Health ping to ${healthUrl} failed:`, err);
    throw err;
  }
}
