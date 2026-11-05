/**
 * TradeAI PRO — Cloudflare Edge Worker with Reverse Proxy & Edge Caching.
 * 
 * Routes:
 * - /api/*  -> Reverse proxy to Render backend (https://trading-api-n1wt.onrender.com)
 *              with Cloudflare Edge Caching (caches.default) for GET requests.
 * - /ws/*   -> Transparent WebSocket passthrough to Render.
 * - /*      -> High-speed static asset delivery via Cloudflare ASSETS binding.
 */

const BACKEND_HOST = "trading-api-n1wt.onrender.com";
const BACKEND_ORIGIN = `https://${BACKEND_HOST}`;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // 1. Handle API & WebSocket requests
    if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) {
      const targetUrl = new URL(url.pathname + url.search, BACKEND_ORIGIN);

      // Handle WebSockets
      if (request.headers.get("Upgrade") === "websocket") {
        const wsRequest = new Request(targetUrl, request);
        wsRequest.headers.set("Host", BACKEND_HOST);
        return fetch(wsRequest);
      }

      // Check Edge Cache for GET requests
      const isGet = request.method === "GET";
      const cache = caches.default;
      // Normalizing cacheKey to request.url without volatile Vary headers
      const cacheKey = new Request(request.url, { method: "GET" });

      if (isGet) {
        const cachedResponse = await cache.match(cacheKey);
        if (cachedResponse) {
          const res = new Response(cachedResponse.body, cachedResponse);
          res.headers.set("CF-Cache-Status", "HIT");
          res.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
          res.headers.set("Access-Control-Allow-Origin", "*");
          return res;
        }
      }

      // Forward request to Render Origin
      const originHeaders = new Headers(request.headers);
      originHeaders.set("Host", BACKEND_HOST);
      // Remove any client caching bypass headers so origin isn't forced to recompute unnecessarily
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

      // Cache eligible GET responses with 200 OK status
      const cacheControl = originResponse.headers.get("cache-control") || "";
      const isCacheable = isGet && originResponse.status === 200 && (
        cacheControl.includes("public") ||
        url.pathname.startsWith("/api/overview/") ||
        url.pathname.startsWith("/api/signals/")
      );

      if (isCacheable) {
        const responseToCache = originResponse.clone();
        const cacheHeaders = new Headers(responseToCache.headers);
        // Strip vary: Origin to prevent cache fragmentation across different browsers/clients
        cacheHeaders.delete("vary");
        cacheHeaders.set("Cache-Control", "public, max-age=15, s-maxage=30, stale-while-revalidate=60");
        cacheHeaders.set("Access-Control-Allow-Origin", "*");

        const cachedRes = new Response(responseToCache.body, {
          status: responseToCache.status,
          statusText: responseToCache.statusText,
          headers: cacheHeaders,
        });
        ctx.waitUntil(cache.put(cacheKey, cachedRes));
      }

      const clientResponse = new Response(originResponse.body, originResponse);
      clientResponse.headers.set("CF-Cache-Status", isCacheable ? "MISS" : "BYPASS");
      clientResponse.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
      clientResponse.headers.set("Access-Control-Allow-Origin", "*");
      return clientResponse;
    }

    // 2. Fallback to Static Asset delivery
    if (env.ASSETS) {
      return env.ASSETS.fetch(request);
    }

    return new Response("Not Found", { status: 404 });
  },
};
