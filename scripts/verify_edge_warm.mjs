/**
 * Verification harness for the cron edge-warm path (src/worker.js).
 *
 * Runs the real warmEdgeCache() against the public Worker origin, then requests
 * the same paths once more and prints the cache verdict. A warm miss costs the
 * user a cold origin fetch, so this is worth re-checking before a market open:
 *
 *   node scripts/verify_edge_warm.mjs                       # 3 sample paths
 *   node scripts/verify_edge_warm.mjs /api/stats /api/signals/all
 *   node scripts/verify_edge_warm.mjs $(node -e "...")      # any path list
 *
 * Expected result right after the warm pass: 200 with cf-cache-status HIT, or
 * STALE when the entry is older than s-maxage (15s) - the Worker then serves the
 * stale entry immediately and revalidates in the background, which is the whole
 * point: the user never waits on a cold origin.
 *
 * Requests are paced (GAP_MS) because the free Render instance has 0.1 CPU and
 * a burst is what this warm path exists to avoid.
 */
import { warmEdgeCache } from '../src/worker.js';

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36';
const DEFAULT_PATHS = ['/api/stats', '/api/signals/all', '/api/macro/intelligence'];
const PATHS = process.argv.slice(2).length ? process.argv.slice(2) : DEFAULT_PATHS;
const GAP_MS = 4000;
const PUBLIC_ORIGIN = 'https://tradeai-pro.tradingpro.workers.dev';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const pacedFetch = async (url, options = {}) => {
  await sleep(GAP_MS);
  return fetch(url, options);
};

console.log('--- warm pass (real worker code, paced) ---');
const warm = await warmEdgeCache(pacedFetch, { paths: PATHS });
console.log(JSON.stringify(warm, null, 2));

await sleep(2000);

console.log('--- user request cache status right after warm ---');
for (const path of PATHS) {
  await sleep(GAP_MS);
  const res = await fetch(`${PUBLIC_ORIGIN}${path}`, {
    headers: { 'User-Agent': UA, Accept: 'application/json' },
  });
  console.log(
    `${path} -> ${res.status} cf=${res.headers.get('cf-cache-status')} age=${res.headers.get('age')} swrAt=${res.headers.get('x-swr-cached-at')}`
  );
  await res.arrayBuffer();
}
