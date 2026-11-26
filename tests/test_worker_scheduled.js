/**
 * Unit test suite for Cloudflare Worker:
 * 1. Scheduled cron trigger keep-alive handler (handleScheduled).
 * 2. Edge proxy request routing, SWR edge caching, and cache poisoning prevention (worker.fetch).
 */
import assert from 'node:assert';
import worker, {
  handleScheduled,
  getNormalizedCacheKey,
  shouldDispatchCycle,
  istClock,
  getDegradedFallbackResponse,
  isPathCacheable,
  hasCrossedNseSessionBoundary,
  warmEdgeCache,
  shouldWarmEdgeCache,
  shouldPingSidecars,
  EDGE_WARM_PATHS,
} from '../src/worker.js';

// In-memory mock Cache implementation adhering to Cloudflare Worker Cache API
class MockCache {
  constructor() {
    this.store = new Map();
    this.putCalls = [];
  }

  async match(key) {
    const urlStr = typeof key === 'string' ? key : key.url;
    const entry = this.store.get(urlStr);
    return entry ? entry.clone() : null;
  }

  async put(key, response) {
    const urlStr = typeof key === 'string' ? key : key.url;
    this.putCalls.push({
      key: urlStr,
      status: response.status,
      headers: new Headers(response.headers),
      bodyText: await response.clone().text(),
    });
    this.store.set(urlStr, response.clone());
  }

  reset() {
    this.store.clear();
    this.putCalls = [];
  }
}

async function runTests() {
  console.log('Test 0: symbols query parameters are canonicalized for edge caching...');
  const normalizedSymbolsUrl = getNormalizedCacheKey(
    'https://tradeai-pro.tradingpro.workers.dev/api/watchlist/quotes?symbols=vedl,%20sbin,RELIANCE,vedl&_ts=123&foo=bar'
  );
  assert.strictEqual(
    normalizedSymbolsUrl,
    'https://tradeai-pro.tradingpro.workers.dev/api/watchlist/quotes?foo=bar&symbols=RELIANCE%2CSBIN%2CVEDL'
  );

  const emptySymbolsUrl = getNormalizedCacheKey(
    'https://tradeai-pro.tradingpro.workers.dev/api/watchlist/quotes?symbols=%20%2C%20&_ts=456'
  );
  assert.strictEqual(
    emptySymbolsUrl,
    'https://tradeai-pro.tradingpro.workers.dev/api/watchlist/quotes'
  );
  console.log('✓ Test 0 passed.');
  console.log('Test 1: handleScheduled sends GET request to backend /api/health...');
  const t1Calls = [];

  const mockFetch = async (url, options) => {
    t1Calls.push({ url, options });
    return {
      ok: true,
      status: 200,
    };
  };

  // 12:00 UTC Monday = 17:30 IST -> outside the pre-open edge-warm window, so the
  // cron must issue exactly one request (the liveness ping) and nothing else.
  const t1Now = new Date(Date.UTC(2026, 10, 9, 12, 0));
  const result = await handleScheduled({ cron: '*/5 * * * *' }, {}, {}, mockFetch, t1Now);
  assert.strictEqual(t1Calls.length, 1, 'outside the warm window the cron is a pure keep-alive');
  const calledUrl = t1Calls[0].url;
  const calledOptions = t1Calls[0].options;
  assert.strictEqual(calledUrl, 'https://trading-api-n1wt.onrender.com/api/health/live');
  assert.strictEqual(calledOptions.method, 'GET');
  assert.strictEqual(calledOptions.headers['User-Agent'], 'TradeAI-Cloudflare-Cron-KeepAlive/1.0');
  assert.strictEqual(calledOptions.headers['Accept'], 'application/json');
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.status, 200);
  assert.strictEqual(result.warm, null, 'no edge warm outside the pre-open window');
  console.log('✓ Test 1 passed.');

  console.log('Test 2: handleScheduled propagates network error on fetch failure...');
  const failingFetch = async () => {
    throw new Error('Connection refused');
  };

  await assert.rejects(
    async () => {
      await handleScheduled({ cron: '*/5 * * * *' }, {}, {}, failingFetch);
    },
    /Connection refused/
  );
  console.log('✓ Test 2 passed.');

  // Set up MockCache and global fetch interceptor for worker.fetch tests
  const mockCache = new MockCache();
  globalThis.caches = { default: mockCache };

  const originalGlobalFetch = globalThis.fetch;
  let currentOriginFetch = async () => {
    throw new Error('Origin fetch handler not configured');
  };
  globalThis.fetch = async (req, ...args) => currentOriginFetch(req, ...args);

  try {
    // ------------------------------------------------------------------------
    // Test 3: Public cacheable GET with origin 200 calls cache.put and stores response
    // ------------------------------------------------------------------------
    console.log('Test 3: Public cacheable GET with origin 200 calls cache.put and stores response...');
    mockCache.reset();
    const waitUntil3 = [];
    const ctx3 = { waitUntil: (p) => waitUntil3.push(p) };

    currentOriginFetch = async (req) => {
      return new Response(JSON.stringify({ status: 'ok', market: 'bullish' }), {
        status: 200,
        headers: {
          'Content-Type': 'application/json',
          'X-Custom-Origin-Header': 'TradeAI-Origin',
        },
      });
    };

    const req3 = new Request('https://tradeai-pro.tradingpro.workers.dev/api/overview/intelligence', {
      method: 'GET',
    });
    const res3 = await worker.fetch(req3, {}, ctx3);
    await Promise.all(waitUntil3);

    assert.strictEqual(res3.status, 200);
    assert.strictEqual(res3.headers.get('CF-Cache-Status'), 'MISS');
    assert.strictEqual(
      res3.headers.get('Cache-Control'),
      'public, max-age=5, s-maxage=15, stale-while-revalidate=30'
    );
    assert.strictEqual(res3.headers.get('X-Edge-Proxy'), 'TradeAI-Cloudflare-Worker');
    assert.strictEqual(mockCache.putCalls.length, 1);
    assert.strictEqual(mockCache.putCalls[0].key, req3.url);
    assert.strictEqual(mockCache.putCalls[0].status, 200);
    assert.strictEqual(mockCache.putCalls[0].headers.get('Cache-Control'), 'public, max-age=600, s-maxage=600');
    assert.ok(mockCache.putCalls[0].headers.get('X-SWR-Cached-At'), 'Must include X-SWR-Cached-At');

    // Subsequent request within fresh TTL hits edge cache
    const res3Hit = await worker.fetch(req3, {}, ctx3);
    assert.strictEqual(res3Hit.status, 200);
    assert.strictEqual(res3Hit.headers.get('CF-Cache-Status'), 'HIT');
    assert.strictEqual(
      res3Hit.headers.get('Cache-Control'),
      'public, max-age=5, s-maxage=15, stale-while-revalidate=30'
    );
    console.log('✓ Test 3 passed.');

    // ------------------------------------------------------------------------
    // Test 4: Origin returning 403, 429, 500, or 502 NEVER calls cache.put (cache poisoning prevention)
    // ------------------------------------------------------------------------
    console.log('Test 4: Origin returning 403, 429, 500, or 502 NEVER calls cache.put (cache poisoning prevention)...');
    const poisonCheckStatuses = [403, 429, 500, 502];
    for (const status of poisonCheckStatuses) {
      mockCache.reset();
      const waitUntilPoison = [];
      const ctxPoison = { waitUntil: (p) => waitUntilPoison.push(p) };

      currentOriginFetch = async () => {
        return new Response(JSON.stringify({ error: `Origin failure with code ${status}` }), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const reqPoison = new Request('https://tradeai-pro.tradingpro.workers.dev/api/overview/intelligence', {
        method: 'GET',
      });
      const resPoison = await worker.fetch(reqPoison, {}, ctxPoison);
      await Promise.all(waitUntilPoison);

      assert.strictEqual(
        mockCache.putCalls.length,
        0,
        `cache.put must NEVER be called when origin returns status ${status}`
      );
      assert.strictEqual(resPoison.status, status);
    }

    // Verify Section 6A 403 does not return stale cache (admin locks honored immediately)
    mockCache.reset();
    const expiredStaleAt = String(Date.now() - 700000); // 700s old (exceeds physical fresh/SWR window of 600s)
    const seededStaleForex = new Response(JSON.stringify({ rates: { 'USD/INR': 83.5 } }), {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'X-SWR-Cached-At': expiredStaleAt,
        'Cache-Control': 'public, max-age=600, s-maxage=600',
      },
    });
    await mockCache.put('https://tradeai-pro.tradingpro.workers.dev/api/forex/rates', seededStaleForex);
    mockCache.putCalls = []; // reset count

    currentOriginFetch = async () => {
      return new Response(
        JSON.stringify({ error: 'Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A)' }),
        {
          status: 403,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    };

    const reqForex403 = new Request('https://tradeai-pro.tradingpro.workers.dev/api/forex/rates', {
      method: 'GET',
    });
    const resForex403 = await worker.fetch(reqForex403, {}, ctx3);
    assert.strictEqual(
      resForex403.status,
      403,
      'Section 6A 403 must be served immediately, never masked by stale cache'
    );
    assert.strictEqual(
      resForex403.headers.get('CF-Cache-Status'),
      'BYPASS',
      'Should bypass stale cache fallback on 403'
    );
    assert.strictEqual(mockCache.putCalls.length, 0);
    console.log('✓ Test 4 passed.');

    // ------------------------------------------------------------------------
    // Test 5: Origin error responses (4xx/5xx) and network errors return anti-poisoning headers
    // ------------------------------------------------------------------------
    console.log('Test 5: Origin error responses (4xx/5xx) and network errors return anti-poisoning cache headers...');
    const expectedAntiPoisoningCacheControl = 'no-store, no-cache, must-revalidate, max-age=0';
    const expectedPragma = 'no-cache';

    // 5a. JSON error responses (even if origin returned leaky public cache headers)
    const errorScenarios = [400, 403, 404, 500];
    for (const errCode of errorScenarios) {
      currentOriginFetch = async () => {
        return new Response(JSON.stringify({ error: `Error ${errCode}` }), {
          status: errCode,
          headers: {
            'Content-Type': 'application/json',
            'Cache-Control': 'public, max-age=300', // Origin misconfiguration
          },
        });
      };

      const reqErr = new Request('https://tradeai-pro.tradingpro.workers.dev/api/overview/intelligence', {
        method: 'GET',
      });
      const resErr = await worker.fetch(reqErr, {}, ctx3);
      assert.strictEqual(resErr.status, errCode);
      assert.strictEqual(
        resErr.headers.get('Cache-Control'),
        expectedAntiPoisoningCacheControl,
        `HTTP ${errCode} must have anti-poisoning Cache-Control`
      );
      assert.strictEqual(resErr.headers.get('Pragma'), expectedPragma);
    }

    // 5b. HTML error responses (Render 502 HTML or CDN challenge) converted to JSON
    currentOriginFetch = async () => {
      return new Response('<html><body>502 Bad Gateway</body></html>', {
        status: 502,
        headers: {
          'Content-Type': 'text/html; charset=utf-8',
        },
      });
    };
    const reqHtmlErr = new Request('https://tradeai-pro.tradingpro.workers.dev/api/overview/intelligence', {
      method: 'GET',
    });
    const resHtmlErr = await worker.fetch(reqHtmlErr, {}, ctx3);
    assert.strictEqual(resHtmlErr.status, 502);
    assert.strictEqual(resHtmlErr.headers.get('Content-Type'), 'application/json');
    assert.strictEqual(resHtmlErr.headers.get('Cache-Control'), expectedAntiPoisoningCacheControl);
    assert.strictEqual(resHtmlErr.headers.get('Pragma'), expectedPragma);
    const bodyHtmlErr = await resHtmlErr.json();
    assert.strictEqual(bodyHtmlErr.status, 502);

    // 5c. Origin network failure (fetch throws)
    currentOriginFetch = async () => {
      throw new Error('Connection reset by peer');
    };
    mockCache.reset(); // no stale cache present
    const reqNetErr = new Request('https://tradeai-pro.tradingpro.workers.dev/api/overview/intelligence', {
      method: 'GET',
    });
    const resNetErr = await worker.fetch(reqNetErr, {}, ctx3);
    assert.strictEqual(resNetErr.status, 502);
    assert.strictEqual(resNetErr.headers.get('Content-Type'), 'application/json');
    assert.strictEqual(resNetErr.headers.get('Cache-Control'), expectedAntiPoisoningCacheControl);
    assert.strictEqual(resNetErr.headers.get('Pragma'), expectedPragma);
    const bodyNetErr = await resNetErr.json();
    assert.strictEqual(bodyNetErr.error, 'Backend origin unavailable');

    // 5d. Stale fallback on origin 503 sets short SWR Cache-Control
    mockCache.reset();
    const staleEntry = new Response(JSON.stringify({ status: 'stale_snapshot' }), {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'X-SWR-Cached-At': expiredStaleAt,
        'Cache-Control': 'public, max-age=45, s-maxage=45',
      },
    });
    await mockCache.put('https://tradeai-pro.tradingpro.workers.dev/api/overview/intelligence', staleEntry);

    currentOriginFetch = async () => {
      return new Response(JSON.stringify({ error: 'Service Unavailable' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      });
    };
    const resStaleFallback = await worker.fetch(reqNetErr, {}, ctx3);
    assert.strictEqual(resStaleFallback.status, 200);
    assert.strictEqual(resStaleFallback.headers.get('CF-Cache-Status'), 'STALE-FALLBACK-503');
    assert.strictEqual(
      resStaleFallback.headers.get('Cache-Control'),
      'public, max-age=0, must-revalidate, stale-while-revalidate=10'
    );
    console.log('✓ Test 5 passed.');

    // ------------------------------------------------------------------------
    // Test 6: Origin 429 response preserves Retry-After header
    // ------------------------------------------------------------------------
    console.log('Test 6: Origin 429 response preserves Retry-After header...');
    // 6a. JSON 429 response from origin
    currentOriginFetch = async () => {
      return new Response(JSON.stringify({ error: 'Twelve Data rate limit exceeded' }), {
        status: 429,
        headers: {
          'Content-Type': 'application/json',
          'Retry-After': '65',
        },
      });
    };
    const req429 = new Request('https://tradeai-pro.tradingpro.workers.dev/api/forex/rates', {
      method: 'GET',
    });
    const res429 = await worker.fetch(req429, {}, ctx3);
    assert.strictEqual(res429.status, 429);
    assert.strictEqual(res429.headers.get('Retry-After'), '65');
    assert.strictEqual(res429.headers.get('Cache-Control'), expectedAntiPoisoningCacheControl);
    assert.strictEqual(res429.headers.get('Pragma'), expectedPragma);

    // 6b. HTML 429 challenge response from origin
    currentOriginFetch = async () => {
      return new Response('<html><body>Too Many Requests</body></html>', {
        status: 429,
        headers: {
          'Content-Type': 'text/html; charset=utf-8',
          'Retry-After': '120',
        },
      });
    };
    const res429Html = await worker.fetch(req429, {}, ctx3);
    assert.strictEqual(res429Html.status, 429);
    assert.strictEqual(res429Html.headers.get('Retry-After'), '120');
    assert.strictEqual(res429Html.headers.get('Content-Type'), 'application/json');
    assert.strictEqual(res429Html.headers.get('Cache-Control'), expectedAntiPoisoningCacheControl);
    assert.strictEqual(res429Html.headers.get('Pragma'), expectedPragma);
    const body429 = await res429Html.json();
    assert.strictEqual(body429.error, 'Rate limit in effect. Retrying shortly...');
    console.log('✓ Test 6 passed.');

    // ------------------------------------------------------------------------
    // Test 7: cycle dispatch cadence (IST session window, 20-minute cadence)
    // ------------------------------------------------------------------------
    console.log('Test 7: cycle dispatch cadence follows the NSE session and 20-min cadence...');
    const at = (utcHH, utcMM, day = 9) => new Date(Date.UTC(2026, 10, day, utcHH, utcMM));

    // 2026-11-09 is a Monday. 04:50 UTC = 10:20 IST -> in session, on cadence.
    assert.strictEqual(istClock(at(4, 50)).minutesOfDay, 10 * 60 + 20);
    assert.deepStrictEqual(shouldDispatchCycle(at(4, 50)), { dispatch: true, reason: 'session_cadence' });

    // 04:55 UTC = 10:25 IST -> in session but off the 20-min boundary.
    assert.deepStrictEqual(shouldDispatchCycle(at(4, 55)), { dispatch: false, reason: 'off_cadence' });

    // 03:40 UTC = 09:10 IST -> before the open.
    assert.deepStrictEqual(shouldDispatchCycle(at(3, 40)), { dispatch: false, reason: 'outside_session' });

    // 10:20 UTC = 15:50 IST -> after the close.
    assert.deepStrictEqual(shouldDispatchCycle(at(10, 20)), { dispatch: false, reason: 'outside_session' });

    // Saturday 10:20 IST -> weekend.
    assert.deepStrictEqual(shouldDispatchCycle(at(4, 50, 7)), { dispatch: false, reason: 'weekend' });
    console.log('✓ Test 7 passed.');

    // ------------------------------------------------------------------------
    // Test 8: cron dispatches the workflow when a token is configured
    // ------------------------------------------------------------------------
    console.log('Test 8: cron dispatches the autonomous cycle workflow on cadence...');
    const dispatchCalls = [];
    const dispatchFetch = async (url, options = {}) => {
      dispatchCalls.push({ url, options });
      if (String(url).includes('api.github.com')) {
        return { ok: true, status: 204 };
      }
      return { ok: true, status: 200 };
    };

    const dispatchResult = await handleScheduled(
      { cron: '*/5 * * * *' },
      { GH_PAT: 'test-token' },
      {},
      dispatchFetch,
      at(4, 50)
    );

    assert.strictEqual(dispatchCalls.length, 2, 'keep-warm ping + one workflow dispatch');
    const ghCall = dispatchCalls.find((c) => String(c.url).includes('api.github.com'));
    assert.ok(ghCall, 'must call the GitHub dispatches API');
    assert.strictEqual(ghCall.options.method, 'POST');
    assert.strictEqual(ghCall.options.headers.Authorization, 'Bearer test-token');
    assert.deepStrictEqual(JSON.parse(ghCall.options.body), { ref: 'main' });
    assert.strictEqual(dispatchResult.cycle.dispatched, true);
    console.log('✓ Test 8 passed.');

    // ------------------------------------------------------------------------
    // Test 9: no dispatch outside the session, even with a token
    // ------------------------------------------------------------------------
    console.log('Test 9: no workflow dispatch outside the NSE session...');
    const idleCalls = [];
    const idleResult = await handleScheduled(
      { cron: '*/5 * * * *' },
      { GH_PAT: 'test-token' },
      {},
      async (url, options = {}) => {
        idleCalls.push({ url, options });
        return { ok: true, status: 200 };
      },
      at(12, 0) // 17:30 IST
    );
    assert.strictEqual(idleCalls.length, 1, 'only the keep-warm ping');
    assert.strictEqual(idleResult.cycle.dispatched, false);
    assert.strictEqual(idleResult.cycle.reason, 'outside_session');

    // Without a token the cron stays a pure keep-alive.
    const noTokenCalls = [];
    const noTokenResult = await handleScheduled(
      { cron: '*/5 * * * *' },
      {},
      {},
      async (url) => {
        noTokenCalls.push(url);
        return { ok: true, status: 200 };
      },
      at(4, 50)
    );
    assert.strictEqual(noTokenCalls.length, 1);
    assert.strictEqual(noTokenResult.cycle.reason, 'no_token');
    console.log('✓ Test 9 passed.');

    // ------------------------------------------------------------------------
    // Test 10: market refresh is triggered when CRON_SECRET is configured
    // ------------------------------------------------------------------------
    console.log('Test 10: market refresh is dispatched when CRON_SECRET is present...');
    const refreshCalls = [];
    const refreshFetch = async (url, options = {}) => {
      refreshCalls.push({ url, options });
      return { ok: true, status: 200 };
    };

    const refreshResult = await handleScheduled(
      { cron: '*/2 3-10 * * 1-5' },
      { CRON_SECRET: 'test-secret-123' },
      {},
      refreshFetch,
      at(12, 0)
    );

    assert.strictEqual(refreshCalls.length, 2, 'keep-warm ping + market refresh call');
    const cronCall = refreshCalls.find((c) => String(c.url).includes('/api/cron/refresh'));
    assert.ok(cronCall, 'must trigger /api/cron/refresh');
    assert.strictEqual(cronCall.options.method, 'POST');
    assert.strictEqual(cronCall.options.headers['X-Cron-Secret'], 'test-secret-123');
    assert.strictEqual(cronCall.options.headers['User-Agent'], 'TradeAI-Cloudflare-Cron-Refresh/1.0');
    assert.strictEqual(refreshResult.refresh.ok, true);
    console.log('✓ Test 10 passed.');

    // ------------------------------------------------------------------------
    // Test 11: Degraded fallbacks for /api/performance and /api/tradeflow
    // ------------------------------------------------------------------------
    console.log('Test 11: Degraded fallbacks return status: degraded with null financials...');
    const perfFallback = getDegradedFallbackResponse('/api/performance');
    assert.ok(perfFallback, 'Performance fallback must exist');
    assert.strictEqual(perfFallback.status, 200);
    const perfData = await perfFallback.json();
    assert.strictEqual(perfData.status, 'degraded');
    assert.strictEqual(perfData.degraded, true);
    assert.strictEqual(perfData.capital, null);
    assert.strictEqual(perfData.total_pnl, null);
    assert.strictEqual(perfData.source, 'EDGE_DEGRADED_FALLBACK');

    const tfFallback = getDegradedFallbackResponse('/api/tradeflow');
    assert.ok(tfFallback, 'Tradeflow fallback must exist');
    assert.strictEqual(tfFallback.status, 200);
    const tfData = await tfFallback.json();
    assert.strictEqual(tfData.status, 'degraded');
    assert.strictEqual(tfData.degraded, true);
    assert.strictEqual(tfData.fii_net, null);
    assert.strictEqual(tfData.dii_net, null);
    assert.strictEqual(tfData.source, 'EDGE_DEGRADED_FALLBACK');
    console.log('✓ Test 11 passed.');

    // ------------------------------------------------------------------------
    // Test 12: /api/index-mover/ route is edge cacheable
    // ------------------------------------------------------------------------
    console.log('Test 12: /api/index-mover/ is dynamically edge cacheable with short TTL...');
    assert.strictEqual(isPathCacheable('/api/index-mover/NIFTY%2050'), true);
    assert.strictEqual(isPathCacheable('/api/index-mover/BANKNIFTY'), true);
    assert.strictEqual(isPathCacheable('/api/health'), false);
    console.log('✓ Test 12 passed.');

    // ------------------------------------------------------------------------
    // Test 13: hasCrossedNseSessionBoundary detects session boundaries
    // ------------------------------------------------------------------------
    console.log('Test 13: hasCrossedNseSessionBoundary detects 15:30 close and date changes...');
    // Same day, 15:25 IST (09:55 UTC) -> 15:35 IST (10:05 UTC) => crosses 15:30 close
    const cachedAt1 = new Date('2026-11-13T09:55:00Z').getTime();
    const now1 = new Date('2026-11-13T10:05:00Z').getTime();
    assert.strictEqual(hasCrossedNseSessionBoundary(cachedAt1, now1), true);

    // Same day, 10:00 IST (04:30 UTC) -> 10:05 IST (04:35 UTC) => within session
    const cachedAt2 = new Date('2026-11-13T04:30:00Z').getTime();
    const now2 = new Date('2026-11-13T04:35:00Z').getTime();
    assert.strictEqual(hasCrossedNseSessionBoundary(cachedAt2, now2), false);

    // Crosses calendar date in IST
    const cachedAt3 = new Date('2026-11-13T15:25:00Z').getTime();
    const now3 = new Date('2026-11-14T04:00:00Z').getTime();
    assert.strictEqual(hasCrossedNseSessionBoundary(cachedAt3, now3), true);
    console.log('✓ Test 13 passed.');

    // ------------------------------------------------------------------------
    // Test 14: Macro degraded fallback never invents commodity prices
    // ------------------------------------------------------------------------
    console.log('Test 14: Macro degraded fallback ships null quotes and status degraded...');
    const macroFallback = getDegradedFallbackResponse('/api/macro/intelligence');
    assert.ok(macroFallback, 'Macro fallback must exist');
    const macroData = await macroFallback.json();
    assert.strictEqual(macroData.status, 'degraded', 'Degraded macro must not claim status ok');
    assert.strictEqual(macroData.degraded, true);
    assert.strictEqual(macroData.macro.feed_source, 'DEGRADED_EDGE_FALLBACK');
    const macroQuotes = [
      'brent_crude', 'wti_crude', 'us_10y_yield', 'dxy_dollar_index',
      'sp500', 'nasdaq', 'gold',
    ];
    for (const key of macroQuotes) {
      assert.strictEqual(
        macroData.macro[key].price,
        null,
        `${key}.price must stay null on the degraded edge, never a placeholder price`
      );
    }
    console.log('✓ Test 14 passed.');

    // ------------------------------------------------------------------------
    // Test 15: WebSocket upgrade failures never leak an origin challenge page
    // ------------------------------------------------------------------------
    console.log('Test 15: /ws/* origin refusal is reshaped into a retryable JSON 503...');
    const wsRequest = () => new Request('https://edge.test/ws/prices', {
      headers: { Upgrade: 'websocket', Connection: 'Upgrade' },
    });

    // Origin answers with a Cloudflare challenge / 429 HTML page instead of 101.
    globalThis.fetch = async () => new Response('<html>challenge</html>', {
      status: 429,
      headers: { 'Content-Type': 'text/html' },
    });
    const wsRefused = await worker.fetch(wsRequest(), {}, {});
    assert.strictEqual(wsRefused.status, 503);
    assert.match(wsRefused.headers.get('Content-Type'), /application\/json/);
    assert.strictEqual(wsRefused.headers.get('Retry-After'), '5');
    const wsRefusedBody = await wsRefused.json();
    assert.strictEqual(wsRefusedBody.upstream_status, 429);
    assert.strictEqual(wsRefusedBody.degraded, true);

    // A genuine upgrade must be passed through untouched.
    const upgraded = new Response(null, { status: 200 });
    Object.defineProperty(upgraded, 'webSocket', { value: {} });
    globalThis.fetch = async () => upgraded;
    const wsLive = await worker.fetch(wsRequest(), {}, {});
    assert.strictEqual(wsLive, upgraded, 'Successful upgrade must be returned as-is');

    // A non-upgrade plain request still gets 426.
    globalThis.fetch = async () => new Response('nope');
    const wsPlain = await worker.fetch(new Request('https://edge.test/ws/prices'), {}, {});
    assert.strictEqual(wsPlain.status, 426);
    console.log('✓ Test 15 passed.');

    // ------------------------------------------------------------------------
    // Test 16: pre-open edge warm fills the shared cache through the public origin
    // ------------------------------------------------------------------------
    console.log('Test 16: cron warms the edge cache through the public origin before the open...');

    // Every warm path must be edge-cacheable, otherwise the warm request skips
    // the cache branch it is supposed to populate.
    for (const path of EDGE_WARM_PATHS) {
      assert.ok(isPathCacheable(path), `${path} must be edge-cacheable to be worth warming`);
    }

    // Policy: pre-open window only, env overrides win.
    assert.deepStrictEqual(shouldWarmEdgeCache({}, at(3, 0)), { warm: true, reason: 'pre_open_window' }); // 08:30 IST
    assert.deepStrictEqual(shouldWarmEdgeCache({}, at(4, 14)), { warm: true, reason: 'pre_open_window' }); // 09:44 IST
    assert.deepStrictEqual(shouldWarmEdgeCache({}, at(4, 15)), { warm: false, reason: 'outside_window' }); // 09:45 IST, exclusive end
    assert.deepStrictEqual(shouldWarmEdgeCache({}, at(2, 59)), { warm: false, reason: 'outside_window' }); // 08:29 IST
    assert.deepStrictEqual(shouldWarmEdgeCache({}, at(4, 50)), { warm: false, reason: 'outside_window' }); // 10:20 IST
    assert.deepStrictEqual(shouldWarmEdgeCache({ EDGE_WARM: 'off' }, at(3, 30)), { warm: false, reason: 'disabled' });
    assert.deepStrictEqual(shouldWarmEdgeCache({ EDGE_WARM: 'always' }, at(12, 0)), { warm: true, reason: 'forced' });
    assert.deepStrictEqual(shouldWarmEdgeCache({}, at(3, 30, 7)), { warm: false, reason: 'weekend' }); // Saturday

    // warmEdgeCache must hit the PUBLIC worker origin and walk the list serially.
    const warmCalls = [];
    const warmFetch = async (url, options = {}) => {
      warmCalls.push({ url, options });
      return { ok: true, status: 200 };
    };
    const warmResult = await warmEdgeCache(warmFetch);
    assert.strictEqual(warmCalls.length, EDGE_WARM_PATHS.length);
    assert.strictEqual(warmResult.total, EDGE_WARM_PATHS.length);
    assert.strictEqual(warmResult.filled, EDGE_WARM_PATHS.length);
    assert.strictEqual(warmResult.origin, 'https://tradeai-pro.tradingpro.workers.dev');
    for (const call of warmCalls) {
      assert.ok(
        call.url.startsWith('https://tradeai-pro.tradingpro.workers.dev/api/'),
        `warm call must use the public origin, got ${call.url}`
      );
      assert.strictEqual(call.options.method, 'GET');
      assert.strictEqual(call.options.headers['User-Agent'], 'TradeAI-Cloudflare-Cron-EdgeWarm/1.0');
    }

    // A warm failure is non-fatal: the run reports it and moves on.
    const partialWarm = await warmEdgeCache(async (url) => {
      if (String(url).includes('/api/stats')) throw new Error('origin cold');
      return { ok: true, status: 200 };
    });
    assert.strictEqual(partialWarm.total, EDGE_WARM_PATHS.length);
    assert.strictEqual(partialWarm.filled, EDGE_WARM_PATHS.length - 1);
    assert.strictEqual(partialWarm.warmed[0].ok, false);
    assert.match(partialWarm.warmed[0].error, /origin cold/);

    // Full cron tick inside the window: health ping -> light refresh -> warm.
    const tickCalls = [];
    const tickResult = await handleScheduled(
      { cron: '*/2 3-10 * * 1-5' },
      {},
      {},
      async (url, options = {}) => {
        tickCalls.push({ url, options });
        return { ok: true, status: 200 };
      },
      at(3, 30) // 09:00 IST, 15 minutes before the open
    );
    const healthCalls = tickCalls.filter((c) => String(c.url).includes('/api/health/live'));
    const edgeWarmCalls = tickCalls.filter((c) => String(c.url).includes('tradeai-pro.tradingpro.workers.dev'));
    assert.strictEqual(healthCalls.length, 1, 'liveness ping still comes first and only once');
    assert.strictEqual(edgeWarmCalls.length, EDGE_WARM_PATHS.length, 'every warm path is requested once');
    assert.strictEqual(tickResult.warm.reason, 'pre_open_window');
    assert.strictEqual(tickResult.warm.filled, EDGE_WARM_PATHS.length);
    assert.strictEqual(tickResult.cycle.dispatched, false, 'no dispatch without GH_PAT');
    console.log('✓ Test 16 passed.');

    // ------------------------------------------------------------------------
    // Test 17: handleScheduled pings ML sidecar when ML_BACKEND_ORIGIN is set
    // ------------------------------------------------------------------------
    console.log('Test 17: cron pings ML sidecar /health/live when ML_BACKEND_ORIGIN is set...');
    const mlCalls = [];
    const mlMockFetch = async (url, options = {}) => {
      mlCalls.push({ url, options });
      return { ok: true, status: 200 };
    };
    await handleScheduled(
      { cron: '*/10 * * * *' },
      { ML_BACKEND_ORIGIN: 'https://trading-ml-8umr.onrender.com' },
      {},
      mlMockFetch,
      at(12, 0) // off-hours
    );
    const mlHealthCalls = mlCalls.filter((c) => c.url === 'https://trading-ml-8umr.onrender.com/health/live');
    assert.strictEqual(mlHealthCalls.length, 1, 'ML sidecar /health/live must be pinged exactly once');
    assert.strictEqual(mlHealthCalls[0].options.headers['User-Agent'], 'TradeAI-Cloudflare-Cron-KeepAlive/1.0');
    console.log('✓ Test 17 passed.');

    // ------------------------------------------------------------------------
    // Test 18: handleScheduled pings Feed service when FEED_BACKEND_ORIGIN is set
    // ------------------------------------------------------------------------
    console.log('Test 18: cron pings Feed service /health/live when FEED_BACKEND_ORIGIN is set...');
    const feedCalls = [];
    const feedMockFetch = async (url, options = {}) => {
      feedCalls.push({ url, options });
      return { ok: true, status: 200 };
    };
    await handleScheduled(
      { cron: '*/10 * * * *' },
      { FEED_BACKEND_ORIGIN: 'https://trading-feed.onrender.com' },
      {},
      feedMockFetch,
      at(12, 0)
    );
    const feedHealthCalls = feedCalls.filter((c) => c.url === 'https://trading-feed.onrender.com/health/live');
    assert.strictEqual(feedHealthCalls.length, 1, 'Feed service /health/live must be pinged exactly once');
    assert.strictEqual(feedHealthCalls[0].options.headers['User-Agent'], 'TradeAI-Cloudflare-Cron-KeepAlive/1.0');
    console.log('✓ Test 18 passed.');

    // ------------------------------------------------------------------------
    // Test 19: SIDECAR_MARKET_HOURS_ONLY gates Box B & C pings to 08:30-16:30 IST Mon-Fri
    // ------------------------------------------------------------------------
    console.log('Test 19: SIDECAR_MARKET_HOURS_ONLY saves Render hours outside NSE hours...');
    assert.deepStrictEqual(
      shouldPingSidecars({ SIDECAR_MARKET_HOURS_ONLY: 'true' }, at(3, 0)), // 08:30 IST Thu
      { ping: true, reason: 'market_window' }
    );
    assert.deepStrictEqual(
      shouldPingSidecars({ SIDECAR_MARKET_HOURS_ONLY: 'true' }, at(10, 59)), // 16:29 IST Thu
      { ping: true, reason: 'market_window' }
    );
    assert.deepStrictEqual(
      shouldPingSidecars({ SIDECAR_MARKET_HOURS_ONLY: 'true' }, at(12, 0)), // 17:30 IST Thu
      { ping: false, reason: 'outside_market_window' }
    );
    assert.deepStrictEqual(
      shouldPingSidecars({ SIDECAR_MARKET_HOURS_ONLY: 'true' }, at(4, 0, 7)), // Saturday 09:30 IST
      { ping: false, reason: 'weekend' }
    );

    const offHourUrls = [];
    await handleScheduled(
      { cron: '*/10 * * * *' },
      {
        SIDECAR_MARKET_HOURS_ONLY: 'true',
        ML_BACKEND_ORIGIN: 'https://trading-ml-8umr.onrender.com',
        FEED_BACKEND_ORIGIN: 'https://trading-feed.onrender.com',
      },
      {},
      async (url) => {
        offHourUrls.push(String(url));
        return { ok: true, status: 200 };
      },
      at(12, 0) // 17:30 IST (off-hours)
    );
    assert.ok(offHourUrls.some((u) => u.includes('trading-api-n1wt.onrender.com/api/health/live')));
    assert.strictEqual(offHourUrls.some((u) => u.includes('trading-ml-8umr.onrender.com')), false);
    assert.strictEqual(offHourUrls.some((u) => u.includes('trading-feed.onrender.com')), false);
    console.log('✓ Test 19 passed.');

  } finally {
    // Restore global fetch and caches
    globalThis.fetch = originalGlobalFetch;
    delete globalThis.caches;
  }

  console.log('\nAll worker scheduled keep-alive and edge proxy unit tests passed successfully!');
}

runTests().catch((err) => {
  console.error('Test failed:', err);
  process.exit(1);
});
