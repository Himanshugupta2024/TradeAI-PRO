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
const PHYSICAL_EDGE_TTL_SEC = EDGE_FRESH_TTL_SEC + EDGE_SWR_WINDOW_SEC; // 45 seconds total edge retention

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
  "connect-src 'self' wss://tradeai-pro.tradingpro.workers.dev https://tradeai-pro.tradingpro.workers.dev https://trading-api-n1wt.onrender.com wss://trading-api-n1wt.onrender.com",
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

// Strict Allowlist of Public Market-Data Endpoints eligible for shared edge caching
const PUBLIC_CACHEABLE_PATHS = new Set([
  "/api/overview/intelligence",
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
  "/api/macro/intelligence",
  "/api/edge/macro-heatmap",
  "/api/edge/risk-gauge",
  "/api/research/next-day-picks",
  "/api/research/next-day-picks/history",
  "/api/health",
  "/api/health/live",
]);

function isPathCacheable(pathname) {
  if (PUBLIC_CACHEABLE_PATHS.has(pathname)) return true;
  if (
    pathname.startsWith("/api/backtest/") ||
    pathname.startsWith("/api/news/") ||
    pathname.startsWith("/api/search/") ||
    pathname.startsWith("/api/swing/symbol/")
  ) {
    return true;
  }
  return false;
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

// Helper to store response in Cloudflare edge cache using exact request.url string
async function storeInEdgeCache(cache, urlStr, originResponse) {
  try {
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
    await cache.put(urlStr, cachedResponse);
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
      const cache = caches.default;

      // Check Edge Cache only for approved public GET endpoints
      if (isPublicCacheable) {
        const cachedResponse = await cache.match(request.url);
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
            ctx.waitUntil((async () => {
              try {
                const revalHeaders = createCleanOriginHeaders(request.headers);

                const revalReq = new Request(targetUrl, {
                  method: "GET",
                  headers: revalHeaders,
                });
                const revalRes = await fetch(revalReq);
                if (revalRes.status === 200) {
                  await storeInEdgeCache(cache, request.url, revalRes);
                }
              } catch (revalErr) {
                // Background fetch failed, stale entry remains until physical expiry
              }
            })());

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
        if (isPublicCacheable) {
          try {
            const stale = await cache.match(request.url);
            if (stale) {
              const res = new Response(stale.body, stale);
              res.headers.set("CF-Cache-Status", "STALE-FALLBACK-ERR");
              res.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
              res.headers.set("Access-Control-Allow-Origin", "*");
              return res;
            }
          } catch (_) {}
        }
        return new Response(JSON.stringify({ error: "Backend origin unavailable", detail: String(err) }), {
          status: 502,
          headers: {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
          },
        });
      }

      // If origin returns 502 or 503, fallback to stale edge cache if available
      if (isPublicCacheable && (originResponse.status === 502 || originResponse.status === 503 || originResponse.status === 429 || originResponse.status === 403 || originResponse.status === 504)) {
        try {
          const stale = await cache.match(request.url);
          if (stale) {
            const res = new Response(stale.body, stale);
            res.headers.set("CF-Cache-Status", "STALE-FALLBACK-503");
            res.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
            res.headers.set("Access-Control-Allow-Origin", "*");
            return res;
          }
        } catch (_) {}
      }

      // Public Cacheable: Store in cache and return MISS with unified headers
      if (isPublicCacheable && originResponse.status === 200) {
        ctx.waitUntil(storeInEdgeCache(cache, request.url, originResponse.clone()));

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
        return new Response(
          JSON.stringify({
            error: originResponse.status === 429 
              ? "Rate limit in effect. Retrying shortly..." 
              : `Origin returned error status ${originResponse.status}`,
            status: originResponse.status
          }),
          {
            status: originResponse.status,
            headers: {
              "Content-Type": "application/json",
              "Access-Control-Allow-Origin": "*",
              "X-Edge-Proxy": "TradeAI-Cloudflare-Worker"
            }
          }
        );
      }

      // Non-public / Private / Mutating endpoints: Explicit BYPASS & Private Caching
      const clientHeaders = new Headers(originResponse.headers);
      clientHeaders.set("CF-Cache-Status", "BYPASS");
      clientHeaders.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
      clientHeaders.set("Access-Control-Allow-Origin", "*");

      // For sensitive user/trading endpoints, enforce strict private no-store headers
      if (
        url.pathname.includes("/portfolio") ||
        url.pathname.includes("/trades") ||
        url.pathname.includes("/paper") ||
        url.pathname.includes("/capital") ||
        url.pathname.includes("/risk") ||
        url.pathname.includes("/journal") ||
        url.pathname.includes("/orders") ||
        url.pathname.includes("/positions") ||
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
      if (assetResponse.status === 404 && isGetOrHead && wantsHtml) {
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
};