/**
 * Adversarial Empirical Verification Suite for teamwork_challenger_2:
 * 1. Cloudflare Edge Proxy Anti-Poisoning & Non-200 Cache Rejection (worker.fetch)
 * 2. Rapid sequential burst & flapping error stress test
 * 3. Retry-After header propagation across JSON & HTML challenge responses
 * 4. Frontend UI Graceful Degradation under HTTP 403 & 429 (dashboard/app.js)
 * 5. Bounded Exponential Backoff halting (max 5 retries, no infinite loop)
 * 6. Timer cleanup & cancellation via resetForexRetry()
 */

import assert from 'node:assert';
import fs from 'node:fs';
import vm from 'node:vm';
import worker, { handleScheduled } from '../src/worker.js';

console.log('================================================================');
console.log('STARTING EMPIRICAL ADVERSARIAL CHALLENGE SUITE (CHALLENGER 2)');
console.log('================================================================\n');

// -----------------------------------------------------------------------------
// PART 1: EDGE WORKER CACHE POISONING & RETRY-AFTER PROPAGATION
// -----------------------------------------------------------------------------

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

async function testEdgeWorker() {
  console.log('--- [EDGE PROXY] Test 1.1: Non-200 Response Cache Rejection & Anti-Poisoning Headers ---');
  const mockCache = new MockCache();
  globalThis.caches = { default: mockCache };

  const originalGlobalFetch = globalThis.fetch;
  let currentOriginFetch = async () => {
    throw new Error('Not configured');
  };
  globalThis.fetch = async (req, ...args) => currentOriginFetch(req, ...args);

  const waitUntilTasks = [];
  const mockCtx = { waitUntil: (p) => waitUntilTasks.push(p) };

  try {
    // Test matrix of non-200 status codes:
    // 204 (No Content), 206 (Partial Content), 400, 401, 403, 404, 422, 429, 500, 502, 503, 504
    const testStatuses = [204, 206, 400, 401, 403, 404, 422, 429, 500, 502, 503, 504];

    for (const status of testStatuses) {
      mockCache.reset();
      waitUntilTasks.length = 0;

      currentOriginFetch = async () => {
        const body = status === 204 ? null : JSON.stringify({ code: status, msg: `Response for status ${status}` });
        const headers = { 'Content-Type': 'application/json' };
        if (status === 429) headers['Retry-After'] = '45';
        // Add potentially dangerous leaky headers from misconfigured origin
        headers['Cache-Control'] = 'public, max-age=3600';
        return new Response(body, { status, headers });
      };

      const req = new Request('https://tradeai-pro.tradingpro.workers.dev/api/overview/intelligence', {
        method: 'GET',
      });
      const res = await worker.fetch(req, {}, mockCtx);
      await Promise.all(waitUntilTasks);

      // 1. MUST NEVER call cache.put on ANY non-200 response
      assert.strictEqual(
        mockCache.putCalls.length,
        0,
        `Edge cache MUST NEVER call cache.put for non-200 status code ${status}`
      );

      // 2. Error codes (4xx and 5xx) MUST receive anti-poisoning headers
      if (status >= 400) {
        assert.strictEqual(
          res.headers.get('Cache-Control'),
          'no-store, no-cache, must-revalidate, max-age=0',
          `HTTP ${status} must have anti-poisoning Cache-Control`
        );
        assert.strictEqual(
          res.headers.get('Pragma'),
          'no-cache',
          `HTTP ${status} must have Pragma: no-cache`
        );
      }

      // 3. Status must match origin status
      assert.strictEqual(res.status, status, `Response status must match origin status ${status}`);
    }
    console.log('  ✓ Verified 12 non-200 status codes (204, 206, 400, 401, 403, 404, 422, 429, 500, 502, 503, 504)');
    console.log('  ✓ Confirmed cache.put was called exactly 0 times across all non-200 responses.');
    console.log('  ✓ Confirmed anti-poisoning headers (no-store, no-cache, must-revalidate, max-age=0) attached to all 4xx/5xx.');

    // -------------------------------------------------------------------------
    // Test 1.2: Flapping Origin Burst Stress (50 Rapid Sequential Requests)
    // -------------------------------------------------------------------------
    console.log('\n--- [EDGE PROXY] Test 1.2: Flapping Origin Burst Stress (50 Alternating Requests) ---');
    mockCache.reset();
    let originCallCount = 0;

    for (let i = 0; i < 50; i++) {
      waitUntilTasks.length = 0;
      // Alternate: even index = 200 OK, odd index = error (400, 403, 429, 500, 502, etc.)
      const isOk = i % 2 === 0;
      const statusToReturn = isOk ? 200 : [400, 403, 429, 500, 502, 503][(i >> 1) % 6];

      currentOriginFetch = async () => {
        originCallCount++;
        if (isOk) {
          return new Response(JSON.stringify({ iteration: i, status: 'ok' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        } else {
          return new Response(JSON.stringify({ iteration: i, error: 'failure', status: statusToReturn }), {
            status: statusToReturn,
            headers: { 'Content-Type': 'application/json', 'Retry-After': '30' },
          });
        }
      };

      // Use unique query param to bypass cache hit and test origin response handling
      const req = new Request(`https://tradeai-pro.tradingpro.workers.dev/api/overview/intelligence?burst=${i}`, {
        method: 'GET',
      });
      const res = await worker.fetch(req, {}, mockCtx);
      await Promise.all(waitUntilTasks);

      if (isOk) {
        assert.strictEqual(res.status, 200);
        assert.strictEqual(res.headers.get('CF-Cache-Status'), 'MISS');
      } else {
        assert.strictEqual(res.status, statusToReturn);
        assert.strictEqual(
          res.headers.get('Cache-Control'),
          'no-store, no-cache, must-revalidate, max-age=0',
          `Flapping error ${statusToReturn} on iteration ${i} must receive anti-poisoning headers`
        );
      }
    }

    // Out of 50 requests: exactly 25 were 200 (stored in cache), and 25 were errors (NEVER stored)
    assert.strictEqual(mockCache.putCalls.length, 25, 'Only the 25 successful 200 responses must be stored in cache');
    for (const call of mockCache.putCalls) {
      assert.strictEqual(call.status, 200, 'Every cached entry must strictly have status 200');
    }
    console.log(`  ✓ 50 rapid alternating requests executed. Exactly 25 HTTP 200s cached; 0 error responses cached.`);

    // -------------------------------------------------------------------------
    // Test 1.3: Upstream Retry-After Header Preservation & Propagation
    // -------------------------------------------------------------------------
    console.log('\n--- [EDGE PROXY] Test 1.3: Upstream Retry-After Propagation on 429 ---');

    // 1.3a: JSON 429 with integer Retry-After
    currentOriginFetch = async () => {
      return new Response(JSON.stringify({ error: 'Rate limit exceeded' }), {
        status: 429,
        headers: { 'Content-Type': 'application/json', 'Retry-After': '65' },
      });
    };
    const req429a = new Request('https://tradeai-pro.tradingpro.workers.dev/api/forex/rates', { method: 'GET' });
    const res429a = await worker.fetch(req429a, {}, mockCtx);
    assert.strictEqual(res429a.status, 429);
    assert.strictEqual(res429a.headers.get('Retry-After'), '65', 'Must preserve integer Retry-After header');
    assert.strictEqual(res429a.headers.get('Cache-Control'), 'no-store, no-cache, must-revalidate, max-age=0');
    console.log('  ✓ JSON 429 preserved Retry-After: 65');

    // 1.3b: HTML 429 challenge page (Render or Cloudflare HTML error converted to JSON)
    currentOriginFetch = async () => {
      return new Response('<html><head><title>429 Too Many Requests</title></head><body>Rate Limit</body></html>', {
        status: 429,
        headers: { 'Content-Type': 'text/html; charset=utf-8', 'Retry-After': '120' },
      });
    };
    const res429b = await worker.fetch(req429a, {}, mockCtx);
    assert.strictEqual(res429b.status, 429);
    assert.strictEqual(res429b.headers.get('Retry-After'), '120', 'Must preserve Retry-After from HTML 429');
    assert.strictEqual(res429b.headers.get('Content-Type'), 'application/json', 'Must convert HTML 429 to JSON');
    assert.strictEqual(res429b.headers.get('Cache-Control'), 'no-store, no-cache, must-revalidate, max-age=0');
    const body429b = await res429b.json();
    assert.strictEqual(body429b.error, 'Rate limit in effect. Retrying shortly...');
    console.log('  ✓ HTML 429 converted to JSON and preserved Retry-After: 120');

    // 1.3c: Private / non-cacheable route with 429 (e.g. /api/portfolio/orders)
    currentOriginFetch = async () => {
      return new Response(JSON.stringify({ error: 'Too many order requests' }), {
        status: 429,
        headers: { 'Content-Type': 'application/json', 'Retry-After': '30' },
      });
    };
    const req429Private = new Request('https://tradeai-pro.tradingpro.workers.dev/api/portfolio/orders', {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });
    const res429Private = await worker.fetch(req429Private, {}, mockCtx);
    assert.strictEqual(res429Private.status, 429);
    assert.strictEqual(res429Private.headers.get('Retry-After'), '30');
    assert.strictEqual(res429Private.headers.get('Cache-Control'), 'no-store, no-cache, must-revalidate, max-age=0');
    console.log('  ✓ Private endpoint 429 preserved Retry-After: 30 and anti-poisoning headers');

    // -------------------------------------------------------------------------
    // Test 1.4: Section 6A 403 Lock Immediate Honor (Zero Masking by Stale Cache)
    // -------------------------------------------------------------------------
    console.log('\n--- [EDGE PROXY] Test 1.4: Section 6A 403 Honor & Zero Masking ---');
    mockCache.reset();
    // Pre-populate stale cache entry with 200 OK beyond the physical TTL window (> 600s)
    const staleRates = new Response(JSON.stringify({ rates: { 'USD/INR': 83.5 } }), {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'X-SWR-Cached-At': String(Date.now() - 700000), // 700s old (exceeds physical edge window of 600s)
        'Cache-Control': 'public, max-age=600, s-maxage=600',
      },
    });
    await mockCache.put('https://tradeai-pro.tradingpro.workers.dev/api/forex/rates', staleRates);

    currentOriginFetch = async () => {
      return new Response(
        JSON.stringify({ error: 'Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A).' }),
        { status: 403, headers: { 'Content-Type': 'application/json' } }
      );
    };

    const req403 = new Request('https://tradeai-pro.tradingpro.workers.dev/api/forex/rates', { method: 'GET' });
    const res403 = await worker.fetch(req403, {}, mockCtx);
    assert.strictEqual(res403.status, 403, 'Section 6A 403 MUST NOT be masked by stale cache');
    assert.strictEqual(res403.headers.get('CF-Cache-Status'), 'BYPASS');
    assert.strictEqual(res403.headers.get('Cache-Control'), 'no-store, no-cache, must-revalidate, max-age=0');
    console.log('  ✓ Section 6A 403 is honored immediately without serving stale fallback.');

  } finally {
    globalThis.fetch = originalGlobalFetch;
    delete globalThis.caches;
  }
}

// -----------------------------------------------------------------------------
// PART 2: FRONTEND UI DEGRADATION & EXPONENTIAL BACKOFF STRESS HARNESS
// -----------------------------------------------------------------------------

class MockElement {
  constructor(id) {
    this.id = id;
    this.innerHTML = '';
    this.textContent = '';
    this.style = {};
  }
}

function createFrontendEnvironment() {
  const elements = {
    forexScanMeta: new MockElement('forexScanMeta'),
    forexSignalGrid: new MockElement('forexSignalGrid'),
    forexLiquidityBadge: new MockElement('forexLiquidityBadge'),
    forexOverlapText: new MockElement('forexOverlapText'),
    forexTopCountBadge: new MockElement('forexTopCountBadge'),
    forexTopSetupsGrid: new MockElement('forexTopSetupsGrid'),
    forexSessionClock: new MockElement('forexSessionClock'),
    forexCurrencyMatrix: new MockElement('forexCurrencyMatrix'),
    forexRiskSentimentBadge: new MockElement('forexRiskSentimentBadge'),
    forexInrRow: new MockElement('forexInrRow'),
  };

  // Seed with typical loading placeholders
  elements.forexSessionClock.innerHTML = '<span class="kpi-lbl">Loading sessions…</span>';
  elements.forexCurrencyMatrix.innerHTML = '<div style="font-size:0.78rem;color:var(--text-muted);">Loading strengths…</div>';
  elements.forexInrRow.innerHTML = '<span style="color:var(--text-muted);font-size:0.82rem;">Loading…</span>';

  const documentMock = {
    getElementById: (id) => elements[id] || null,
  };

  const scheduledTimeouts = [];
  let nextTimerId = 100;
  let clearedTimerIds = [];

  const sandbox = {
    document: documentMock,
    window: {},
    console: {
      log: () => {},
      warn: () => {},
      error: () => {},
    },
    Date: Date,
    Intl: Intl,
    Math: Math,
    Number: Number,
    String: String,
    Object: Object,
    Array: Array,
    parseInt: parseInt,
    isNaN: isNaN,
    setTimeout: (fn, delay) => {
      const id = ++nextTimerId;
      scheduledTimeouts.push({ id, fn, delay });
      return id;
    },
    clearTimeout: (id) => {
      clearedTimerIds.push(id);
      const idx = scheduledTimeouts.findIndex((t) => t.id === id);
      if (idx !== -1) scheduledTimeouts.splice(idx, 1);
    },
    _scheduledTimeouts: scheduledTimeouts,
    _clearedTimerIds: clearedTimerIds,
    _escHtml: (s) => String(s || ''),
    escapeHtml: (s) => String(s || ''),
    handleCryptoForexToggleClick: () => {},
    API: '',
    fetchWithTimeout: async () => {
      throw new Error('fetchWithTimeout not configured in test');
    },
  };

  const appCode = fs.readFileSync('dashboard/app.js', 'utf8');
  const startMarker = '// ── Forex Handlers ────────────────────────────────────────────────';
  const endMarker = '// ── NSE Prediction Handlers ───────────────────────────────────────';
  const startIndex = appCode.indexOf(startMarker);
  const endIndex = appCode.indexOf(endMarker);
  assert(startIndex !== -1 && endIndex !== -1, 'Could not locate Forex section in dashboard/app.js');
  const forexCode = appCode.substring(startIndex, endIndex);

  vm.createContext(sandbox);
  vm.runInContext(forexCode, sandbox);

  // Wrap loadForexData to allow tests to await background invocations from setTimeout
  let currentInFlightLoad = null;
  const rawLoadForexData = sandbox.loadForexData;
  sandbox.loadForexData = async (isAutoRetry = false) => {
    const p = rawLoadForexData(isAutoRetry);
    currentInFlightLoad = p;
    return await p;
  };
  sandbox.waitForInFlightLoad = async () => {
    if (currentInFlightLoad) {
      await currentInFlightLoad;
      currentInFlightLoad = null;
    }
  };

  return { sandbox, elements, scheduledTimeouts, clearedTimerIds };
}

async function testFrontendDegradation() {
  console.log('\n--- [FRONTEND DEGRADATION] Test 2.1: HTTP 403 Section 6A Full UI Fallback ---');
  const { sandbox, elements } = createFrontendEnvironment();

  sandbox.fetchWithTimeout = async (url) => {
    return {
      ok: false,
      status: 403,
      headers: { get: () => null },
      json: async () => ({ error: 'Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A).' }),
    };
  };

  await sandbox.loadForexData(false);

  // 1. Session clock must NOT be stuck on loading
  assert(!elements.forexSessionClock.innerHTML.includes('Loading sessions'), 'Clock must not show Loading sessions');
  assert(elements.forexSessionClock.innerHTML.includes('ASIAN'), 'Clock must render ASIAN session');
  assert(elements.forexSessionClock.innerHTML.includes('LONDON'), 'Clock must render LONDON session');
  assert(elements.forexSessionClock.innerHTML.includes('NEWYORK'), 'Clock must render NEWYORK session');
  assert(elements.forexSessionClock.innerHTML.includes('NSE_INR'), 'Clock must render NSE_INR session');

  // 2. Currency matrix must NOT be stuck on loading or calculating
  assert(!elements.forexCurrencyMatrix.innerHTML.includes('Loading strengths'), 'Matrix must not show Loading strengths');
  assert(!elements.forexCurrencyMatrix.innerHTML.includes('Calculating strengths'), 'Matrix must not show Calculating strengths');
  assert(elements.forexCurrencyMatrix.innerHTML.includes('USD'), 'Matrix must render USD');
  assert(elements.forexCurrencyMatrix.innerHTML.includes('0.00%'), 'Matrix must render neutral 0.00%');
  assert.strictEqual(elements.forexRiskSentimentBadge.textContent, 'STANDBY', 'Badge must show STANDBY');

  // 3. INR strip must NOT be stuck on loading
  assert(!elements.forexInrRow.innerHTML.includes('Loading…'), 'INR row must not show Loading…');
  assert(elements.forexInrRow.innerHTML.includes('USD/INR'), 'INR row must display USD/INR benchmark');
  assert(elements.forexInrRow.innerHTML.includes('86.85'), 'INR row must display 86.85 benchmark');
  assert(elements.forexInrRow.innerHTML.includes('NSE CURRENCY STANDBY'), 'INR row must display NSE CURRENCY STANDBY pill');

  // 4. Signal grid and banners
  assert(elements.forexSignalGrid.innerHTML.includes('STANDBY: NSE FOCUS'), 'Grid must display STANDBY: NSE FOCUS');
  assert(elements.forexTopSetupsGrid.innerHTML.includes('SECTION 6A ACTIVE'), 'Top setups must show SECTION 6A ACTIVE');
  assert(elements.forexScanMeta.textContent.includes('Standby (NSE Focus Mode Active)'), 'Scan meta must show Standby text');
  console.log('  ✓ Verified 403: No stuck loading states, neutral currency matrix, benchmark INR rates, and Section 6A standby card.');

  // ---------------------------------------------------------------------------
  // Test 2.2: Bounded Exponential Backoff Halts After 5 Retries (Infinite Loop Prevention)
  // ---------------------------------------------------------------------------
  console.log('\n--- [FRONTEND DEGRADATION] Test 2.2: Exponential Backoff Halting After 5 Retries ---');
  const env2 = createFrontendEnvironment();
  const s2 = env2.sandbox;
  const el2 = env2.elements;

  // Origin consistently returns HTTP 429
  s2.fetchWithTimeout = async (url) => {
    return {
      ok: false,
      status: 429,
      headers: { get: () => null }, // no Retry-After, rely on exponential backoff
      json: async () => ({ error: 'Rate limit' }),
    };
  };

  // Initial call (manual invocation)
  await s2.loadForexData(false);
  assert.strictEqual(s2.getForexRetryCount(), 1, 'Retry count should be 1 after first 429');
  assert.strictEqual(env2.scheduledTimeouts.length, 1, 'Exactly one timeout should be scheduled');
  assert.strictEqual(env2.scheduledTimeouts[0].delay, 8000, 'Initial backoff delay should be 8000ms');

  // Simulate execution of Retries 2, 3, 4, 5
  const expectedDelays = [14000, 26000, 47000, 65000]; // 8000 * 1.8^(n-1) rounded to sec * 1000, capped at 65s

  for (let step = 2; step <= 5; step++) {
    const pendingTimer = env2.scheduledTimeouts.shift();
    assert.ok(pendingTimer, `Timer for retry ${step} must exist`);
    // Execute scheduled timeout callback and wait for background loadForexData to complete
    pendingTimer.fn();
    await s2.waitForInFlightLoad();

    assert.strictEqual(s2.getForexRetryCount(), step, `Retry count should be ${step}`);
    if (step < 5) {
      assert.strictEqual(env2.scheduledTimeouts.length, 1, `Timeout should be scheduled for retry ${step + 1}`);
      const delay = env2.scheduledTimeouts[0].delay;
      assert.strictEqual(delay, expectedDelays[step - 2], `Retry ${step} delay mismatch`);
    }
  }

  // At retry 5: one timer was scheduled for retry 6
  assert.strictEqual(s2.getForexRetryCount(), 5);
  assert.strictEqual(env2.scheduledTimeouts.length, 1);

  // Now execute the 5th timer callback (attempting 6th run)
  const finalTimer = env2.scheduledTimeouts.shift();
  finalTimer.fn();
  await s2.waitForInFlightLoad();

  // MUST HALT: Retry count is still 5, NO new timers scheduled, retry button displayed
  assert.strictEqual(s2.getForexRetryCount(), 5, 'Retry count should cap at 5');
  assert.strictEqual(env2.scheduledTimeouts.length, 0, 'CRITICAL: No further timers should be scheduled. Infinite loop halted!');
  assert(el2.forexScanMeta.textContent.includes('Retries exhausted'), 'Meta must state Retries exhausted');
  assert(el2.forexSignalGrid.innerHTML.includes('↺ Retry Scanner Now'), 'Grid must display manual retry button');
  console.log('  ✓ Verified backoff sequence: 8s -> 14s -> 26s -> 47s -> 65s.');
  console.log('  ✓ Confirmed backoff strictly halts after 5 retries. Zero infinite loop risk.');

  // ---------------------------------------------------------------------------
  // Test 2.3: Manual Refresh & Timer Cancellation (resetForexRetry)
  // ---------------------------------------------------------------------------
  console.log('\n--- [FRONTEND DEGRADATION] Test 2.3: Timer Cancellation via resetForexRetry() ---');
  const env3 = createFrontendEnvironment();
  const s3 = env3.sandbox;

  s3.fetchWithTimeout = async () => ({
    ok: false,
    status: 429,
    headers: { get: () => null },
    json: async () => ({ error: 'Rate limit' }),
  });

  // Start backoff
  await s3.loadForexData(false);
  assert.strictEqual(s3.getForexRetryCount(), 1);
  assert.strictEqual(env3.scheduledTimeouts.length, 1);
  const activeTimerId = env3.scheduledTimeouts[0].id;

  // User clicks "↺ Refresh Scanner" (manual invocation)
  s3.resetForexRetry();

  assert(env3.clearedTimerIds.includes(activeTimerId), 'clearTimeout MUST be called with active timer ID');
  assert.strictEqual(env3.scheduledTimeouts.length, 0, 'Active timeouts list must be empty after reset');
  assert.strictEqual(s3.getForexRetryCount(), 0, 'Retry counter must be reset to 0');
  console.log('  ✓ Confirmed resetForexRetry() cancels active timers and resets retry counter to 0.');

  // ---------------------------------------------------------------------------
  // Test 2.4: Retry-After Header Capped at 65s
  // ---------------------------------------------------------------------------
  console.log('\n--- [FRONTEND DEGRADATION] Test 2.4: Retry-After Header Capping (Max 65s) ---');
  const env4 = createFrontendEnvironment();
  const s4 = env4.sandbox;

  // Origin sends excessive Retry-After: 300s
  s4.fetchWithTimeout = async () => ({
    ok: false,
    status: 429,
    headers: { get: (name) => name.toLowerCase() === 'retry-after' ? '300' : null },
    json: async () => ({ error: 'Rate limit' }),
  });

  await s4.loadForexData(false);
  assert.strictEqual(env4.scheduledTimeouts.length, 1);
  // Must cap at 65000ms
  assert.strictEqual(env4.scheduledTimeouts[0].delay, 65000, 'Retry-After > 65s MUST be capped at 65s (65000ms)');
  console.log('  ✓ Confirmed Retry-After of 300s is safely capped at 65s.');

  // ---------------------------------------------------------------------------
  // Test 2.5: Total Network Exception / Gateway Drop
  // ---------------------------------------------------------------------------
  console.log('\n--- [FRONTEND DEGRADATION] Test 2.5: Network Exception Resilience ---');
  const env5 = createFrontendEnvironment();
  const s5 = env5.sandbox;
  const el5 = env5.elements;

  // Origin throws network disconnection
  s5.fetchWithTimeout = async () => {
    throw new Error('ECONNREFUSED / Network Drop');
  };

  await s5.loadForexData(false);

  // 1. Session clock renders client-calculated IST
  assert(!el5.forexSessionClock.innerHTML.includes('Loading'), 'Clock must not be stuck on loading');
  assert(el5.forexSessionClock.innerHTML.includes('ASIAN'), 'Clock must render ASIAN');

  // 2. Currency matrix renders neutral 0.00%
  assert(el5.forexCurrencyMatrix.innerHTML.includes('0.00%'), 'Matrix must render neutral 0.00%');

  // 3. INR strip renders benchmarks
  assert(el5.forexInrRow.innerHTML.includes('86.85'), 'INR row must render benchmarks');

  // 4. Signal grid shows clean service offline badge without raw stack traces
  assert(el5.forexSignalGrid.innerHTML.includes('SERVICE TEMPORARILY OFFLINE'), 'Must show clean badge');
  assert(!el5.forexSignalGrid.innerHTML.includes('ECONNREFUSED'), 'Must NEVER expose raw error message');
  console.log('  ✓ Confirmed zero UI freezing and zero raw exception text leakage on total network drop.');
}

async function runAll() {
  await testEdgeWorker();
  await testFrontendDegradation();
  console.log('\n================================================================');
  console.log('ALL EMPIRICAL ADVERSARIAL STRESS TESTS COMPLETED SUCCESSFULLY!');
  console.log('================================================================');
}

runAll().catch((err) => {
  console.error('\n❌ STRESS TEST FAILURE:', err);
  process.exit(1);
});
