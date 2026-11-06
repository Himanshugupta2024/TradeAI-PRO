/**
 * Unit test for Cloudflare Worker scheduled() cron trigger keep-alive handler.
 */
import assert from 'node:assert';
import { handleScheduled } from '../src/worker.js';

async function runTests() {
  console.log('Test 1: handleScheduled sends GET request to backend /api/health...');
  let calledUrl = null;
  let calledOptions = null;

  const mockFetch = async (url, options) => {
    calledUrl = url;
    calledOptions = options;
    return {
      ok: true,
      status: 200,
    };
  };

  const result = await handleScheduled({ cron: '*/5 * * * *' }, {}, {}, mockFetch);
  assert.strictEqual(calledUrl, 'https://trading-api-n1wt.onrender.com/api/health');
  assert.strictEqual(calledOptions.method, 'GET');
  assert.strictEqual(calledOptions.headers['User-Agent'], 'TradeAI-Cloudflare-Cron-KeepAlive/1.0');
  assert.strictEqual(calledOptions.headers['Accept'], 'application/json');
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.status, 200);
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

  console.log('\nAll worker scheduled keep-alive unit tests passed successfully!');
}

runTests().catch((err) => {
  console.error('Test failed:', err);
  process.exit(1);
});
