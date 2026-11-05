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

// Strict Allowlist of Public Market-Data Endpoints eligible for shared edge caching
const PUBLIC_CACHEABLE_PATHS = new Set([
  "/api/overview/intelligence",
  "/api/signals/all",
  "/api/signals/winrate",
  "/api/signals/leaderboard",
  "/api/market/mood",
  "/api/screener/gainers",
  "/api/screener/losers",
  "/api/screener/opportunities",
  "/api/breakouts",
  "/api/swing/shortlist",
  "/api/swing/stats",
  "/api/forex/signals",
  "/api/health/live",
]);

// Helper to store response in Cloudflare edge cache with explicit SWR metadata
async function storeInEdgeCache(cache, cacheKey, originResponse) {
  const now = Date.now();
  const cacheHeaders = new Headers(originResponse.headers);
  cacheHeaders.set("X-SWR-Cached-At", String(now));
  // Instruct Cloudflare's underlying cache engine to retain response for 45s
  cacheHeaders.set("Cache-Control", `public, max-age=${BROWSER_TTL_SEC}, s-maxage=${PHYSICAL_EDGE_TTL_SEC}`);
  // Only vary on Accept-Encoding for public JSON to prevent cache fragmentation across different user-agents
  cacheHeaders.set("Vary", "Accept-Encoding");
  cacheHeaders.set("Access-Control-Allow-Origin", "*");

  const cachedResponse = new Response(originResponse.body, {
    status: originResponse.status,
    statusText: originResponse.statusText,
    headers: cacheHeaders,
  });
  await cache.put(cacheKey, cachedResponse);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // 1. Handle WebSocket Upgrade (/ws/*)
    if (url.pathname.startsWith("/ws/")) {
      const targetWsUrl = new URL(url.pathname + url.search, BACKEND_ORIGIN);
      if (request.headers.get("Upgrade") === "websocket") {
        const wsRequest = new Request(targetWsUrl, request);
        wsRequest.headers.set("Host", BACKEND_HOST);
        return fetch(wsRequest);
      }
      return new Response("Expected WebSocket Upgrade", { status: 426 });
    }

    // 2. Handle API routes (/api/*)
    if (url.pathname.startsWith("/api/")) {
      const targetUrl = new URL(url.pathname + url.search, BACKEND_ORIGIN);
      const isGet = request.method === "GET";
      const isPublicCacheable = isGet && PUBLIC_CACHEABLE_PATHS.has(url.pathname);

      const cache = caches.default;
      const cacheKey = new Request(request.url, { method: "GET" });

      // Check Edge Cache only for approved public GET endpoints
      if (isPublicCacheable) {
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
            ctx.waitUntil((async () => {
              try {
                const revalHeaders = new Headers(request.headers);
                revalHeaders.set("Host", BACKEND_HOST);
                revalHeaders.delete("if-none-match");
                revalHeaders.delete("if-modified-since");

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

      // Forward request to Render Origin
      const originHeaders = new Headers(request.headers);
      originHeaders.set("Host", BACKEND_HOST);
      originHeaders.delete("if-none-match");
      originHeaders.delete("if-modified-since");

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
        return new Response(JSON.stringify({ error: "Backend origin unavailable", detail: String(err) }), {
          status: 502,
          headers: {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
          },
        });
      }

      // Response processing
      if (isPublicCacheable && originResponse.status === 200) {
        // Cache in background for future requests
        ctx.waitUntil(storeInEdgeCache(cache, cacheKey, originResponse.clone()));

        const clientResponse = new Response(originResponse.body, originResponse);
        clientResponse.headers.set("CF-Cache-Status", "MISS");
        clientResponse.headers.set("Cache-Control", UNIFIED_CACHE_CONTROL);
        clientResponse.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
        clientResponse.headers.set("Access-Control-Allow-Origin", "*");
        return clientResponse;
      }

      // Non-public / Private / Mutating endpoints: Explicit BYPASS & Private Caching
      const clientResponse = new Response(originResponse.body, originResponse);
      clientResponse.headers.set("CF-Cache-Status", "BYPASS");
      clientResponse.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
      clientResponse.headers.set("Access-Control-Allow-Origin", "*");

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
        clientResponse.headers.set("Cache-Control", "private, no-cache, no-store, must-revalidate");
        clientResponse.headers.set("Pragma", "no-cache");
      }

      // Preserve origin Vary headers exactly as-is for private/general responses
      return clientResponse;
    }

    // 3. Fallback to Static Asset delivery
    if (env.ASSETS) {
      return env.ASSETS.fetch(request);
    }

    return new Response("Not Found", { status: 404 });
  },
};
