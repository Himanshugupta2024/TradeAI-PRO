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
      const cacheKey = new Request(targetUrl.toString(), request);

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
      const originRequest = new Request(targetUrl, {
        method: request.method,
        headers: request.headers,
        body: request.method !== "GET" && request.method !== "HEAD" ? request.body : undefined,
        redirect: "follow",
      });
      originRequest.headers.set("Host", BACKEND_HOST);

      let response;
      try {
        response = await fetch(originRequest);
      } catch (err) {
        return new Response(JSON.stringify({ error: "Backend origin unavailable", detail: String(err) }), {
          status: 502,
          headers: {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
          },
        });
      }

      // Clone response to inspect and cache
      const responseToReturn = new Response(response.body, response);
      responseToReturn.headers.set("X-Edge-Proxy", "TradeAI-Cloudflare-Worker");
      responseToReturn.headers.set("Access-Control-Allow-Origin", "*");

      // Cache eligible GET responses with 200 OK status
      if (isGet && response.status === 200) {
        responseToReturn.headers.set("CF-Cache-Status", "MISS");
        // Store in Cloudflare Edge Cache if response allows caching
        ctx.waitUntil(cache.put(cacheKey, responseToReturn.clone()));
      }

      return responseToReturn;
    }

    // 2. Fallback to Static Asset delivery
    if (env.ASSETS) {
      return env.ASSETS.fetch(request);
    }

    return new Response("Not Found", { status: 404 });
  },
};
