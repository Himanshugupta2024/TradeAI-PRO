const _tabLastFetched = {};
let _lastOppsFetchTs = 0;

let _lastCryptoFetchTs = 0;

let _lastMorningFetchTs = 0;

/**



 * TradeAI PRO — Dashboard v3.0



 *



 * Tabs: Overview, Market (Gainers/Losers + Sector Heatmap),



 * Trade Flow (FII/DII), Portfolio, Performance.



 */







const API = (() => {
    if (typeof window === 'undefined') return '';
    if (window.__TRADEAI_CONFIG__ && typeof window.__TRADEAI_CONFIG__.API_BASE_URL === 'string') {
        return window.__TRADEAI_CONFIG__.API_BASE_URL;
    }
    const port = window.location.port;
    if (port === '5500' || port === '3000' || port === '5173') {
        return 'http://127.0.0.1:8000';
    }
    const host = window.location.hostname;
    if (host.endsWith('workers.dev')) {
        return '';
    }
    if (
        host.endsWith('pages.dev') ||
        host.endsWith('vercel.app') ||
        host.includes('tradeai')
    ) {
        return 'https://trading-api-n1wt.onrender.com';
    }
    return '';
})();

// ── Resilient JSON Fetcher with Dual-Origin Fallback & Content-Type Safety ──
async function fetchSafeJSON(url, options = {}, timeoutMs = 12000) {
    const method = (options.method || 'GET').toUpperCase();
    if (method === 'GET' && typeof _inFlightRequests !== 'undefined' && _inFlightRequests.has(url)) {
        return _inFlightRequests.get(url).then(r => ({ ok: r.ok, status: r.status, data: r._cachedData || {} }));
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const opts = { ...options, signal: controller.signal };
        let res = await fetch(url, opts);

        const ct = (res.headers.get('content-type') || '').toLowerCase();
        if (!res.ok) {
            let errData = {};
            if (ct.includes('application/json')) {
                try { errData = await res.json(); } catch (_) {}
            }
            return {
                ok: false,
                status: res.status,
                error: errData.error || (res.status === 429 ? 'Rate limit in effect. Retrying shortly...' : `HTTP ${res.status}`),
                detail: errData,
                data: errData
            };
        }
        if (!ct.includes('application/json')) {
            return { ok: false, status: res.status, error: 'Non-JSON response' };
        }
        const data = await res.json();
        return { ok: true, status: res.status, data };
    } catch (err) {
        return { ok: false, status: 0, error: err.name === 'AbortError' ? 'Timeout' : (err.message || 'Network error') };
    } finally {
        clearTimeout(timer);
    }
}

// Pre-loaded top NSE symbols for 0ms instant client-side search autocomplete
const _STATIC_NSE_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "ITC", "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI", "TITAN", "WIPRO",
    "TATAMOTORS", "SUNPHARMA", "ULTRACEMCO", "ADANIENT", "ADANIPORTS", "ADANIPOWER", "ADANIGREEN",
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "COALINDIA", "NTPC", "POWERGRID", "ONGC", "BPCL",
    "IOC", "GAIL", "TECHM", "HCLTECH", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP",
    "ASIANPAINT", "NESTLEIND", "BRITANNIA", "DABUR", "GODREJCP", "PIDILITIND", "BERGEPAINT",
    "HAVELLS", "VOLTAS", "SIEMENS", "ABB", "BHEL", "M&M", "EICHERMOT", "HEROMOTOCO",
    "BAJAJ-AUTO", "TVSMOTOR", "INDUSINDBK", "BANDHANBNK", "PNB", "BANKBARODA", "IDFCFIRSTB",
    "SBILIFE", "HDFCLIFE", "ICICIGI", "BAJAJFINSV", "CHOLAFIN", "SHRIRAMFIN", "MUTHOOTFIN",
    "HAL", "BEL", "IRCTC", "NYKAA", "OLAELEC", "POLICYBZR", "DELHIVERY", "TATAPOWER",
    "TATAELXSI", "PERSISTENT", "LTIM", "MPHASIS", "COFORGE", "VEDL", "JINDALSTEL", "SAIL",
    "NMDC", "DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "INDIGO", "TRENT", "DMART",
    "PAGEIND", "DIXON", "POLYCAB", "KALYANKJIL", "CUPID", "PRECWIRE", "PREMIERENE",
    "WELSPUNIND", "WELSPUNLIV", "RAILVIKAS", "IRFC", "MAZDOCK", "COCHINSHIP", "NATIONALUM",
    "JINDALSAW", "MOLDTKPAC", "TIINDIA", "GRINDWELL", "ELECON", "DYNAMATECH", "GMDCLTD",
    "RITES", "HGINFRA", "CAPLIPOINT"
];

// Pre-loaded top crypto pairs for 0ms instant client-side search autocomplete
const _STATIC_CRYPTO_SYMBOLS = [
    { symbol: "BTC/USDT", ticker: "BTC/USDT", base: "BTC", type: "CRYPTO" },
    { symbol: "ETH/USDT", ticker: "ETH/USDT", base: "ETH", type: "CRYPTO" },
    { symbol: "SOL/USDT", ticker: "SOL/USDT", base: "SOL", type: "CRYPTO" },
    { symbol: "BNB/USDT", ticker: "BNB/USDT", base: "BNB", type: "CRYPTO" },
    { symbol: "XRP/USDT", ticker: "XRP/USDT", base: "XRP", type: "CRYPTO" },
    { symbol: "DOGE/USDT", ticker: "DOGE/USDT", base: "DOGE", type: "CRYPTO" },
    { symbol: "ADA/USDT", ticker: "ADA/USDT", base: "ADA", type: "CRYPTO" },
    { symbol: "AVAX/USDT", ticker: "AVAX/USDT", base: "AVAX", type: "CRYPTO" },
    { symbol: "SUI/USDT", ticker: "SUI/USDT", base: "SUI", type: "CRYPTO" },
    { symbol: "NEAR/USDT", ticker: "NEAR/USDT", base: "NEAR", type: "CRYPTO" },
    { symbol: "LINK/USDT", ticker: "LINK/USDT", base: "LINK", type: "CRYPTO" },
    { symbol: "DOT/USDT", ticker: "DOT/USDT", base: "DOT", type: "CRYPTO" },
    { symbol: "LTC/USDT", ticker: "LTC/USDT", base: "LTC", type: "CRYPTO" },
    { symbol: "SHIB/USDT", ticker: "SHIB/USDT", base: "SHIB", type: "CRYPTO" },
    { symbol: "PEPE/USDT", ticker: "PEPE/USDT", base: "PEPE", type: "CRYPTO" },
    { symbol: "WIF/USDT", ticker: "WIF/USDT", base: "WIF", type: "CRYPTO" },
    { symbol: "BONK/USDT", ticker: "BONK/USDT", base: "BONK", type: "CRYPTO" },
    { symbol: "FLOKI/USDT", ticker: "FLOKI/USDT", base: "FLOKI", type: "CRYPTO" },
    { symbol: "RENDER/USDT", ticker: "RENDER/USDT", base: "RENDER", type: "CRYPTO" },
    { symbol: "INJ/USDT", ticker: "INJ/USDT", base: "INJ", type: "CRYPTO" },
    { symbol: "FET/USDT", ticker: "FET/USDT", base: "FET", type: "CRYPTO" },
    { symbol: "TON/USDT", ticker: "TON/USDT", base: "TON", type: "CRYPTO" },
    { symbol: "APT/USDT", ticker: "APT/USDT", base: "APT", type: "CRYPTO" },
    { symbol: "UNI/USDT", ticker: "UNI/USDT", base: "UNI", type: "CRYPTO" },
    { symbol: "AAVE/USDT", ticker: "AAVE/USDT", base: "AAVE", type: "CRYPTO" }
];

// ── TradeAI-PRO Currency & Market Display Helpers ──────────────────────────
function _symCurr(sym) {
    if (!sym) return '₹';
    const s = String(sym).toUpperCase();
    if (s.includes('/USDT') || s.includes('/USD') || s.endsWith('USDT') || s.endsWith('USD')) return '$';
    return '₹';
}

function _fmtMoney(v, sym = '₹') {
    if (v == null || isNaN(v)) return '—';
    const num = Number(v);
    const sign = num < 0 ? '-' : '';
    return `${sign}${sym}${Math.abs(num).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

function _fmtSignedMoney(v, sym = '₹') {
    if (v == null || isNaN(v)) return '—';
    const num = Number(v);
    const sign = num < 0 ? '-' : (num > 0 ? '+' : '');
    return `${sign}${sym}${Math.abs(num).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

function _fmtDuration(entryTime, exitTime) {
    if (!entryTime || !exitTime) return '—';
    try {
        const start = new Date(entryTime).getTime();
        const end = new Date(exitTime).getTime();
        if (isNaN(start) || isNaN(end) || end < start) return '—';
        const diffSecs = Math.floor((end - start) / 1000);
        if (diffSecs < 60) return `${diffSecs}s`;
        const diffMins = Math.floor(diffSecs / 60);
        if (diffMins < 60) return `${diffMins}m`;
        const diffHours = Math.floor(diffMins / 60);
        const remMins = diffMins % 60;
        if (diffHours < 24) return remMins > 0 ? `${diffHours}h ${remMins}m` : `${diffHours}h`;
        const diffDays = Math.floor(diffHours / 24);
        const remHours = diffHours % 24;
        return remHours > 0 ? `${diffDays}d ${remHours}h` : `${diffDays}d`;
    } catch {
        return '—';
    }
}









// ── State ───────────────────────────────────────────────







let currentTab = 'overview';



let allSignals = [];



let marketDataCache = {};
let _lastOpenTrades = [];
let _lastMacroData = null;
let _lastMacroFetchTs = 0;



let priceWs = null;



let wsReconnectTimer = null;



let _wsReconnectBackoffMs = 4000;



let currentModalSymbol = null;



let currentModalInterval = '5m';



let _modalRefreshTimer = null;



let _refreshTimer = null;



let _isRefreshing = false;



let _lastSignalsSignature = '';



let _showSkipSignals = false;



let _signalTypeFilter = 'all';



let _signalSortMode = 'qs_desc';



const REFRESH_INTERVAL_MS = 60000;







// ── Cockpit & section-level state ───────────────────────



let _cockpitFilter = 'all';       // active risk-aware filter



let _lastCockpitData = null;      // latest /api/overview/intelligence payload



let _lastStatsData = null;        // last good stats snapshot



let _lastDashboardData = null;    // last good dashboard snapshot



let _lastOppsData = null;         // last good opportunities snapshot

let _unpricedPositions = 0;       // open paper trades valued at entry price

function _applyUnpricedMtm() {
    const uEl = document.getElementById('statUnrealizedPnl');
    if (!uEl) return;
    if (_unpricedPositions > 0) {
        uEl.textContent = 'MTM: —';
        uEl.title = _unpricedPositions + ' open position(s) valued at entry price — live quotes unavailable';
        uEl.style.color = 'var(--text-muted, #8b949e)';
    }
}







// ── Phase 6: Crypto mini-panel state ──────────────────────



let _lastCryptoIntelData = null;  // latest /api/crypto/intelligence payload







// ── Phase 4: Watchlist + Alerts + Smart Refresh ──────────



let _watchlist = (() => {
    try {
        const stored = localStorage.getItem('tradeai_watchlist');
        if (stored) {
            const arr = JSON.parse(stored);
            if (Array.isArray(arr) && arr.length > 0) return new Set(arr);
        }
    } catch (_) {}
    return new Set(['RELIANCE', 'HDFCBANK', 'TCS', 'INFY', 'VEDL']);
})();



let _alertThresholds = JSON.parse(localStorage.getItem('tradeai_alerts') || '{}'); // {symbol: {qs, conf}}



let _lastAlertFired = {};         // symbol → last fired timestamp (prevent spam)



let _morningScanData = null;      // cached morning scan



let _smartRefreshInterval = REFRESH_INTERVAL_MS;  // adapts based on market session







const escapeHtml = (s) => _escHtml(s);

function _escHtml(s) {



    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');



}







function _getAuthHeaders(customHeaders = {}) {



    const headers = { 'Content-Type': 'application/json', ...customHeaders };



    const key = localStorage.getItem('tradeai_admin_key') || localStorage.getItem('admin_api_key');



    if (key && key.trim()) {



        headers['X-API-Key'] = key.trim();



    }



    return headers;



}







function _handleAuthChallenge(status) {



    if (status === 401 || status === 403) {



        const key = prompt('Admin API authentication required. Please enter your ADMIN_API_KEY:');



        if (key && key.trim()) {



            localStorage.setItem('tradeai_admin_key', key.trim());



            if (typeof showToast === 'function') {



                showToast('Admin API key saved. Please retry your action.', 'info');



            }



            return true;



        }



    }



    return false;



}







const _activeTradeSubmissions = new Set();











// ── Init ────────────────────────────────────────────────







function hydrateFromLocalStorageCache() {



    try {



        const rawAi = localStorage.getItem('tradeai_ai_engine_v2');
        if (rawAi) {
            try {
                const ai = JSON.parse(rawAi);
                if (typeof applyAiBadgeState === 'function') applyAiBadgeState(ai);
            } catch (_) {}
        }
        const raw = localStorage.getItem('tradeai_cached_signals_v1');
        if (!raw) return;
        const cached = JSON.parse(raw);
        const cacheAgeMs = cached.updated_at ? (Date.now() - new Date(cached.updated_at).getTime()) : Infinity;
        if (isNaN(cacheAgeMs) || cacheAgeMs > 24 * 60 * 60 * 1000) {
            console.debug('[Hydrate] Local cache is stale (>24h). Discarding.');
            localStorage.removeItem('tradeai_cached_signals_v1');
            return;
        }



        if (Array.isArray(cached.signals) && cached.signals.length > 0) {



            allSignals = cached.signals;



            _lastSignalsSignature = getSignalsSignature(allSignals);



            if (_cockpitFilter !== 'all') {



                applyCockpitFilter();



            } else {



                renderSignals(allSignals);



            }



            updateSummary(allSignals);



            const loader = document.getElementById('loadingState');



            if (loader) loader.classList.add('hidden');



            if (cached.stats) {



                _lastStatsData = cached.stats;



                updateStatsStrip(_lastStatsData);



            }



            if (cached.updated_at) {



                const updated = new Date(cached.updated_at);



                if (!isNaN(updated.getTime())) {



                    const el = document.getElementById('lastUpdated');



                    if (el) el.textContent = updated.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });



                }



            }



            console.debug(`[Hydrate] Loaded ${allSignals.length} signals instantly from local cache.`);



        }



    
        const rawCockpit = localStorage.getItem('tradeai_cached_cockpit_v1');
        if (rawCockpit) {
            try {
                const cachedCockpit = JSON.parse(rawCockpit);
                if (cachedCockpit && !cachedCockpit.error) {
                    _lastCockpitData = cachedCockpit;
                    renderCockpit(cachedCockpit);
                }
            } catch (_) {}
        }
    } catch (e) {



        console.debug('[Hydrate] Local cache hydration skipped:', e);



    }



}







document.addEventListener('DOMContentLoaded', () => {
    updateWatchlistBadge();



    initTheme();



    initTabs();



    initSidebarCollapse();



    initSignalControls();



    initCockpitFilters();



    initPhase5Cockpit();



    updateMarketSessionBanner();
    fetchMarketControlStatus();



    initModal();



    initBmcModal();



    startClock();



    hydrateFromLocalStorageCache();
    hydrateAutonomousFromStorage();

    loadOverviewFast();
    // Stagger peripheral requests across 300-2500ms to stay within Render free-tier concurrent connection caps
    setTimeout(() => loadAutonomousFeed(), 300);
    setTimeout(() => loadAllData({ background: false }), 800);
    if (typeof currentTab !== 'undefined' && currentTab === 'portfolio') {
        setTimeout(() => loadPortfolio(), 1400);
    }
    setTimeout(() => updateAiModelHealthBadge(), 1800);
    setTimeout(() => connectPriceWebSocket(), 2400);

    startBackgroundRefresh();



    const refreshBtn = document.getElementById('manualRefreshBtn');



    if (refreshBtn) {



        refreshBtn.addEventListener('click', triggerManualRefresh);



    }



});







async function triggerManualRefresh() {



    const btn = document.getElementById('manualRefreshBtn');



    if (btn) {



        btn.disabled = true;



        btn.textContent = 'Refreshing...';



    }



    try {



        await fetchWithTimeout(`${API}/api/refresh`, { method: 'POST', headers: _getAuthHeaders() }, 30000);



    } catch (err) {



        console.warn('Manual backend refresh failed:', err);



    } finally {



        await loadAllData({ background: false });



        if (btn) {



            btn.disabled = false;



            btn.textContent = '↻ Refresh';



        }



    }



}







function getSignalsSignature(signals) {



    if (!Array.isArray(signals) || signals.length === 0) return '';



    return signals



        .map(s => [



            s.symbol,



            s.signal,



            s.close,



            s.timestamp,



            s.quality_score,



            s.confidence,



            s.regime,



        ].join('|'))



        .join('||');



}







function startBackgroundRefresh() {



    if (_refreshTimer) clearTimeout(_refreshTimer);







    const tick = async () => {



        if (!document.hidden && !_isRefreshing) {



            await loadAllData({ background: true });



        }



        // Smart refresh schedules next tick via _scheduleSmartRefresh inside loadAllData



        // Fallback: if loadAllData didn't schedule (e.g. was skipped), schedule here



        if (!_refreshTimer) {



            _refreshTimer = setTimeout(tick, _smartRefreshInterval);



        }



    };







    _refreshTimer = setTimeout(tick, _smartRefreshInterval);



}







/** Phase 4: Adaptive polling - fast 6s during initial warmup if 0 signals, 30s during active market hours, 90s outside. */



function _scheduleSmartRefresh() {



    if (_refreshTimer) clearTimeout(_refreshTimer);



    const isWarmingUp = !Array.isArray(allSignals) || allSignals.length === 0;



    const now = new Date();

    const utcMinutes = now.getUTCHours() * 60 + now.getUTCMinutes();

    const istMinutes = (utcMinutes + 330) % (24 * 60);

    const istDayOffset = Math.floor((utcMinutes + 330) / (24 * 60));

    const istDay = (now.getUTCDay() + istDayOffset) % 7;



    // NSE: 09:15-15:30 IST

    const inMarketHours = istMinutes >= (9 * 60 + 15) && istMinutes <= (15 * 60 + 35);

    const isWeekday = istDay >= 1 && istDay <= 5;

    const isActive = inMarketHours && isWeekday;



    _smartRefreshInterval = isWarmingUp ? 6000 : (isActive ? 30000 : 120000);



    // Update the UI label

    const label = document.querySelector('.auto-refresh-label');

    if (label) {

        label.textContent = isWarmingUp

            ? 'Warming up cache (6s retry)...'

            : `Auto-refresh ${isActive ? '30s' : '120s'}${isActive ? ' (market open)' : ' (market closed)'}`;

    }







    _refreshTimer = setTimeout(async () => {



        _refreshTimer = null;



        if (!document.hidden && !_isRefreshing) {



            await loadAllData({ background: true });



        } else {



            _scheduleSmartRefresh();



        }



    }, _smartRefreshInterval);



}







document.addEventListener('visibilitychange', () => {



    // When user comes back to tab, do one immediate silent refresh.



    if (!document.hidden && !_isRefreshing) {



        loadAllData({ background: true });



    }



});











// ── WebSocket for Real-Time Prices ──────────────────────







function connectPriceWebSocket() {



    // Only skip when the socket is actually open — treating CONNECTING as "busy"



    // prevented new attempts and left tabs stuck when the handshake never completed



    // (common on cold Render free-tier instances behind a proxy).



    if (priceWs && priceWs.readyState === WebSocket.OPEN) return;







    if (wsReconnectTimer) {



        clearTimeout(wsReconnectTimer);



        wsReconnectTimer = null;



    }







    if (priceWs) {



        try {



            priceWs.onopen = null;



            priceWs.onmessage = null;



            priceWs.onerror = null;



            priceWs.onclose = null;



            if (priceWs.readyState === WebSocket.CONNECTING || priceWs.readyState === WebSocket.OPEN) {



                priceWs.close();



            }



        } catch (e) {



            /* ignore */



        }



        priceWs = null;



    }







    const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';



    let wsUrl = `${wsProto}//${location.host}/ws/prices`;
    if (window.__TRADEAI_CONFIG__ && window.__TRADEAI_CONFIG__.WS_URL) {
        wsUrl = window.__TRADEAI_CONFIG__.WS_URL;
    } else if (API && API.startsWith('http')) {
        try {
            const parsed = new URL(API);
            const resolvedHost = parsed.host;
            const resolvedProto = parsed.protocol === 'https:' ? 'wss:' : 'ws:';
            wsUrl = `${resolvedProto}//${resolvedHost}/ws/prices`;
        } catch (e) {
            console.warn('[WS] Could not parse API for WebSocket URL:', e);
        }
    }







    try {



        priceWs = new WebSocket(wsUrl);







        priceWs.onopen = () => {



            console.log('[WS] Connected to price stream');



            _wsReconnectBackoffMs = 4000;



            updateWsIndicator(true);



            if (wsReconnectTimer) {



                clearTimeout(wsReconnectTimer);



                wsReconnectTimer = null;



            }



        };







        priceWs.onmessage = (event) => {



            try {



                const data = JSON.parse(event.data);



                if (data.prices) {



                    updateLivePrices(data.prices);



                }



                if (data.market_mood) {
                    updateMarketMood(data.market_mood);
                    if (typeof updateMoodStripUI === 'function') {
                        updateMoodStripUI(data.market_mood);
                    }
                }



                if (data.timestamp) {



                    document.getElementById('lastUpdated').textContent =



                        new Date(data.timestamp).toLocaleTimeString('en-IN', {



                            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true,



                        });



                }



                // Live stats strip refresh from WebSocket (avoids extra /api/stats poll)



                if (data.stats) {



                    updateStatsStrip(data.stats);



                }



                // Live feed health badge update from WebSocket



                if (data.feed_status) {



                    _updateFeedStatusFromWs(data.feed_status);



                }



            } catch (e) {



                console.warn('[WS] Failed to parse message:', e);



            }



        };







        priceWs.onclose = () => {



            const delay = Math.min(



                30000,



                Math.round(_wsReconnectBackoffMs + Math.random() * 1500),



            );



            _wsReconnectBackoffMs = Math.min(30000, _wsReconnectBackoffMs * 1.6);



            console.log(`[WS] Disconnected — reconnecting in ${Math.round(delay / 1000)}s...`);



            updateWsIndicator(false);



            priceWs = null;



            wsReconnectTimer = setTimeout(connectPriceWebSocket, delay);



        };







        priceWs.onerror = (err) => {



            console.error('[WS] Error:', err);



            updateWsIndicator(false);



        };



    } catch (e) {



        console.error('[WS] Failed to connect:', e);



        const delay = Math.min(30000, Math.round(_wsReconnectBackoffMs + Math.random() * 2000));



        wsReconnectTimer = setTimeout(connectPriceWebSocket, delay);



    }



}







let _wsConnected = false;







function updateWsIndicator(connected) {



    _wsConnected = Boolean(connected);



    const dot = document.getElementById('refreshDot');



    if (dot) {



        dot.title = connected ? 'Live WebSocket Connected' : 'Polling / Reconnecting';



        dot.style.background = connected ? 'var(--green)' : '#f59e0b';



    }



    if (typeof updateDynamicMarketStatus === 'function') {



        updateDynamicMarketStatus();



    }



}







function _updateFeedStatusFromWs(feedStatus) {
    if (!feedStatus) return;
    if (typeof feedStatus === 'string') {
        feedStatus = { state: feedStatus };
    }



    if (_lastCockpitData) {



        if (!_lastCockpitData.market_guardrails) _lastCockpitData.market_guardrails = {};



        const prev = _lastCockpitData.market_guardrails.feed_status || {};



        _lastCockpitData.market_guardrails.feed_status = { ...prev, ...feedStatus };



    }



    _renderFeedStalenessBanner(_lastCockpitData && _lastCockpitData.market_guardrails);







    // Update the guardrails tile feed row if cockpit data is loaded



    if (!_lastCockpitData) return;



    const inner = document.getElementById('cockpitGuardrailsInner');



    if (!inner) return;



    // Patch only the feed row to avoid full re-render



    const state = feedStatus.state || 'UNKNOWN';



    const age = feedStatus.age_seconds != null ? ` (${feedStatus.age_seconds}s ago)` : '';



    const color = (state === 'LIVE' || state === 'CURRENT')
        ? 'var(--green)'
        : (state === 'EOD' || state === 'CLOSED' || state === 'STANDBY')
            ? '#22d3ee'
            : (state === 'DELAYED' ? '#f59e0b' : 'var(--red)');



    const rows = inner.querySelectorAll('.guardrail-row');



    rows.forEach(row => {



        const key = row.querySelector('.guardrail-key');



        if (key && key.textContent === 'Feed') {



            const val = row.querySelector('.guardrail-val');



            if (val) {



                val.textContent = `${state}${age}`;



                val.style.color = color;



            }



        }



    });



}







function updateLivePrices(prices) {



    // prices[symbol] is an object: {close, high, low, rsi, signal, regime}



    // (WebSocket sends full tick object, not a bare number)



    document.querySelectorAll('.signal-card').forEach(card => {



        const symEl = card.querySelector('.signal-symbol');



        if (!symEl) return;



        const symbol = symEl.textContent.trim();



        const tick = prices[symbol];



        if (tick == null) return;







        // Extract numeric price from tick object (or use directly if already a number)



        const newPrice = typeof tick === 'object' ? (tick.close ?? tick.price ?? tick.ltp) : tick;



        if (newPrice == null || isNaN(newPrice)) return;







        const priceEl = card.querySelector('.signal-price');



        if (priceEl) {



            const oldPrice = parseFloat(priceEl.dataset.price || 0);



            const isCrypto = symbol.includes('/');



            const curr = isCrypto ? '$' : '₹';



            priceEl.textContent = `${curr}${Number(newPrice).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;



            priceEl.dataset.price = newPrice;







            // Flash green/red on change



            if (oldPrice && newPrice !== oldPrice) {



                priceEl.classList.remove('flash-green', 'flash-red');



                void priceEl.offsetWidth;



                priceEl.classList.add(newPrice > oldPrice ? 'flash-green' : 'flash-red');



            }



        }







        // Also update regime badge if the tick includes a new regime



        if (typeof tick === 'object' && tick.regime) {



            const regimeBadge = card.querySelector('.regime-badge');



            if (regimeBadge) {



                const r = String(tick.regime).toLowerCase().replace(' ', '_');



                regimeBadge.className = `regime-badge regime-${r}`;



                regimeBadge.textContent = String(tick.regime).replace('_', ' ');



            }



        }



    });







    // Update portfolio open positions with live prices



    updateOverviewActiveTradesPrices(prices);
    if (currentTab === 'portfolio') {



        updatePortfolioPrices(prices);



    }







    // Cache numeric close prices for chart/modal use



    for (const [sym, tick] of Object.entries(prices)) {



        marketDataCache[sym] = typeof tick === 'object' ? tick : { close: tick };



    }
    updateMoodStripFromCache();



}







function updatePortfolioPrices(prices) {



    document.querySelectorAll('#openPositions tr').forEach(row => {



        const symCell = row.querySelector('td:first-child');



        if (!symCell) return;



        const symbol = symCell.textContent.trim();



        const tick = prices[symbol];



        if (tick == null) return;



        const price = typeof tick === 'object' ? (tick.close ?? tick.price) : tick;



        if (price == null || isNaN(price)) return;



        const currentPriceCell = row.querySelector('td:nth-child(4)');



        if (currentPriceCell) {



            currentPriceCell.textContent = Number(price).toFixed(2);



        }



    });



}











// ── Theme Toggle ────────────────────────────────────────







function initTheme() {



    const saved = localStorage.getItem('theme');



    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;



    const initial = saved || 'dark';



    document.documentElement.setAttribute('data-theme', initial);



    updateThemeIcon(initial);







    const toggle = document.getElementById('themeToggle');



    if (!toggle) return;







    toggle.addEventListener('click', () => {



        const current = document.documentElement.getAttribute('data-theme');



        const next = current === 'dark' ? 'light' : 'dark';



        document.documentElement.setAttribute('data-theme', next);



        localStorage.setItem('theme', next);



        updateThemeIcon(next);



    });



}







function updateThemeIcon(theme) {



    const icon = document.getElementById('themeIcon');



    if (icon) {



        icon.textContent = theme === 'dark' ? '🌙' : '☀️';



    }



}











// ── Tab Navigation & Sidebar ────────────────────────────







function initSidebarCollapse() {



    const btn = document.getElementById('sidebarCollapseBtn');



    const sidebar = document.getElementById('sidebar');



    const appMain = document.getElementById('appMain');



    const icon = document.getElementById('sidebarCollapseIcon');



    if (!btn || !sidebar || !appMain) return;







    const isCollapsed = localStorage.getItem('tradeai_sidebar_collapsed') === 'true';



    if (isCollapsed) {



        sidebar.classList.add('collapsed');



        appMain.classList.add('sidebar-collapsed');



        if (icon) icon.innerHTML = '<polyline points="9 18 15 12 9 6"></polyline>';



    }







    btn.addEventListener('click', () => {



        const collapsed = sidebar.classList.toggle('collapsed');



        appMain.classList.toggle('sidebar-collapsed', collapsed);



        localStorage.setItem('tradeai_sidebar_collapsed', collapsed ? 'true' : 'false');



        if (icon) {



            icon.innerHTML = collapsed



                ? '<polyline points="9 18 15 12 9 6"></polyline>'



                : '<polyline points="15 18 9 12 15 6"></polyline>';



        }



    });



}







function initTabs() {



    document.querySelectorAll('.tab-btn').forEach(btn => {



        btn.addEventListener('click', () => {



            const tab = btn.dataset.tab;



            switchTab(tab);



        });



    });



}







function switchTab(tab) {



    currentTab = tab;







    // Update buttons



    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));



    const tabBtn = document.querySelector(`[data-tab="${tab}"]`);



    if (tabBtn) tabBtn.classList.add('active');







    // Update content



    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));



    const panel = document.getElementById(`${tab}Tab`);



    if (!panel) {



        console.warn(`[switchTab] Missing tab panel #${tab}Tab — check dashboard HTML`);



        return;



    }



    panel.classList.add('active');







    // Load tab-specific data



        const _now = Date.now();
    if (!_tabLastFetched[tab] || (_now - _tabLastFetched[tab] > 10000)) {
        _tabLastFetched[tab] = _now;
        if (tab === 'watchlist') loadDedicatedWatchlist();
        if (tab === 'portfolio') loadPortfolio();
        if (tab === 'performance') loadPerformance();
        if (tab === 'market') loadMarket();
        if (tab === 'tradeflow') loadTradeFlow();
        if (tab === 'scorecard') loadScorecard();
        if (tab === 'swing') loadSwingScanner();
        if (tab === 'intraday') loadIntradayLatest();
        if (tab === 'indexmover') loadIndexMover();
        if (tab === 'learning') loadLearningStats();
        if (tab === 'breakouts') loadBreakouts();
        if (tab === 'nseprediction') loadNsePredictionTab();
        if (tab === 'forex') loadForexData();
        if (tab === 'edge-top-setups') loadEdgeTopSetups();
        if (tab === 'edge-macro-heatmap') loadEdgeMacroHeatmap();
        if (tab === 'edge-risk-gauge') loadEdgeRiskGauge();
        if (tab === 'next-day-picks') loadNextDayPicks();
    }



}







// ── Fetch with Timeout + 503/Network Retry Helper ──────



// Handles Render free-tier cold-start: retries up to 3 times on:



//   • HTTP 503 responses (LB queue full, server starting)



//   • Thrown network errors e.g. ERR_ABORTED when LB drops the connection



// Uses exponential backoff (4s → 8s → 16s). Genuine timeouts still throw.



const _inFlightRequests = new Map();







async function fetchWithTimeout(url, options = {}, timeoutMs = 12000, _retries = 3, _backoffMs = 4000) {



    const isGet = !options.method || options.method.toUpperCase() === 'GET';
    const isSafeMethod = !options.method || ['GET', 'HEAD', 'OPTIONS'].includes(options.method.toUpperCase());



    if (isGet && _inFlightRequests.has(url)) {



        return _inFlightRequests.get(url).then(r => r.clone());



    }







    const ac = new AbortController();



    const tid = setTimeout(() => ac.abort(), timeoutMs);







    const fetchPromise = (async () => {



        let resp;



        try {



            resp = await fetch(url, { ...options, signal: ac.signal });



            clearTimeout(tid);



        } catch (err) {



            clearTimeout(tid);



            // Our own timeout fired → do not retry, propagate immediately



            if (ac.signal.aborted) {
                const timeoutErr = new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
                timeoutErr.name = 'TimeoutError';
                throw timeoutErr;
            }



            // Network-level drop (ERR_ABORTED, ERR_CONNECTION_RESET, etc.) → retry



            if (isSafeMethod && _retries > 0) {



                const delay = Math.min(_backoffMs, 20000);



                console.warn(`[fetch] Network error on ${url} — retrying in ${delay / 1000}s (${_retries} left)...`);



                await new Promise(r => setTimeout(r, delay));



                return fetchWithTimeout(url, options, timeoutMs, _retries - 1, _backoffMs * 2);



            }



            throw err;



        } finally {



            if (isGet) _inFlightRequests.delete(url);



        }



        // HTTP-level 503 or 429 → server waking up or burst rate-limited, retry with backoff
        if (isSafeMethod && (resp.status === 503 || resp.status === 429) && _retries > 0) {
            let delay = Math.min(_backoffMs + Math.floor(Math.random() * 1000), 20000);
            try {
                const retryAfterHeader = resp.headers && resp.headers.get ? resp.headers.get('retry-after') : null;
                if (retryAfterHeader) {
                    const parsedSeconds = parseInt(retryAfterHeader, 10);
                    if (!isNaN(parsedSeconds) && parsedSeconds > 0) {
                        delay = Math.min(parsedSeconds * 1000, 65000);
                    }
                }
            } catch (_) {}
            console.warn(`[fetch] ${resp.status} from ${url} — retrying in ${(delay / 1000).toFixed(1)}s (${_retries} left)...`);
            await new Promise(r => setTimeout(r, delay));
            return fetchWithTimeout(url, options, timeoutMs, _retries - 1, _backoffMs * 1.5);
        }



        return resp;



    })();







    if (isGet) _inFlightRequests.set(url, fetchPromise);



    return fetchPromise.then(r => r.clone());



}







/** Parse JSON after fetch; avoids SyntaxError on empty 502/503 bodies from cold starts. */



async function parseJsonResponse(res) {



    if (!res.ok) {



        const err = new Error(`HTTP ${res.status}`);



        err.status = res.status;



        throw err;



    }



    const text = await res.text();



    if (!text || !text.trim()) {



        const err = new Error('Empty response body');



        throw err;



    }



    try {



        return JSON.parse(text);



    } catch (e) {



        const err = new Error('Invalid JSON from API');



        err.cause = e;



        throw err;



    }



}













// ── Autonomous Cockpit: Decoupled & Resilient Architecture ─────────────
let _autonomousStatusInFlight = false;
let _autonomousFeedInFlight = false;
let _autonomousStatusGen = 0;
let _autonomousFeedGen = 0;
let _autonomousPollTimer = null;
let _autonomousStatusController = null;
let _autonomousFeedController = null;
let _lastAutonomousStatusData = null;
let _lastAutonomousFeedData = null;
let _autonomousConsecutiveErrors = 0;

const AUTO_FEED_STORAGE_KEY = 'tradeai_cached_autofeed_v2';
const AUTO_FEED_MAX_CACHE_AGE_MS = 24 * 60 * 60 * 1000; // 24 hours

function hydrateAutonomousFromStorage() {
    try {
        const raw = localStorage.getItem(AUTO_FEED_STORAGE_KEY);
        if (!raw) return;
        const parsed = JSON.parse(raw);
        if (parsed && Array.isArray(parsed.feed) && parsed.feed.length > 0) {
            const age = Date.now() - (parsed.cached_at || 0);
            if (age < AUTO_FEED_MAX_CACHE_AGE_MS) {
                renderAutonomousFeedUI(parsed.feed, { isCached: true });
            }
        }
    } catch (e) {
        console.warn('Corrupt autonomous feed cache cleared:', e);
        try { localStorage.removeItem(AUTO_FEED_STORAGE_KEY); } catch (_) {}
    }
}

function clearAutonomousCache() {
    try { localStorage.removeItem(AUTO_FEED_STORAGE_KEY); } catch (_) {}
}

async function fetchAutonomousStatus({ isManual = false } = {}) {
    if (_autonomousStatusInFlight && !isManual) return;
    _autonomousStatusInFlight = true;
    const currentGen = ++_autonomousStatusGen;

    if (_autonomousStatusController) {
        try { _autonomousStatusController.abort(); } catch (_) {}
    }
    _autonomousStatusController = new AbortController();

    const workerBadge = document.getElementById('autonomousWorkerBadge');
    if (isManual && workerBadge) {
        workerBadge.textContent = 'Worker: Checking...';
        workerBadge.style.color = '#94a3b8';
    }

    try {
        const reqHeaders = {};
        const adminKey = (typeof localStorage !== 'undefined') ? (localStorage.getItem('tradeai_admin_key') || localStorage.getItem('admin_api_key')) : null;
        if (adminKey && adminKey.trim()) reqHeaders['X-API-Key'] = adminKey.trim();

        let targetUrl = `${API}/api/autonomous/status`;
        let res;
        try {
            res = await fetchWithTimeout(targetUrl, {
                signal: _autonomousStatusController.signal,
                headers: reqHeaders,
            }, 10000, 0);
        } catch (netErr) {
            // Same-origin/Worker deployments: retain relative URL to leverage Worker edge proxy and avoid direct 429 challenges.
            // Failure mode covered by absolute fallback: static Pages/Vercel hosting where same-origin /api does not exist.
            const fallbackUrl = (targetUrl.startsWith('/') || !targetUrl.startsWith('http'))
                ? `/api/autonomous/status`
                : `${API}/api/autonomous/status`;
            try {
                res = await fetchWithTimeout(fallbackUrl, {
                    signal: _autonomousStatusController.signal,
                    headers: reqHeaders,
                }, 10000, 0);
            } catch (_) {
                throw netErr;
            }
        }

        if (res && (!res.ok || res.status === 429 || res.status === 403 || res.status === 502 || res.status === 503)) {
            // Same-origin/Worker deployments: retain relative URL to leverage Worker edge proxy and avoid direct 429 challenges.
            // Failure mode covered by absolute fallback: static Pages/Vercel hosting where same-origin /api does not exist.
            const altUrl = (targetUrl.startsWith('/') || !targetUrl.startsWith('http'))
                ? `/api/autonomous/status`
                : `${API}/api/autonomous/status`;
            try {
                const altRes = await fetchWithTimeout(altUrl, {
                    signal: _autonomousStatusController.signal,
                    headers: reqHeaders,
                }, 10000, 0);
                if (altRes && altRes.ok) res = altRes;
            } catch (_) {}
        }

        if (currentGen !== _autonomousStatusGen) return; // Stale response discarded

        const data = await parseJsonResponse(res);
        if (data && data.ok) {
            _lastAutonomousStatusData = data;
            _autonomousConsecutiveErrors = 0;
            renderAutonomousStatusUI(data);
        } else {
            renderAutonomousStatusError(new Error(data?.message || 'Status payload invalid'));
        }
    } catch (err) {
        if (currentGen !== _autonomousStatusGen) return;
        _autonomousConsecutiveErrors++;
        renderAutonomousStatusError(err);
    } finally {
        _autonomousStatusInFlight = false;
        _autonomousStatusController = null;
    }
}

function renderAutonomousStatusUI(statusData) {
    const st = statusData.status || {};
    const wd = statusData.watchdog || {};
    const badge = document.getElementById('autonomousModeBadge');
    const workerBadge = document.getElementById('autonomousWorkerBadge');
    const toggleBtn = document.getElementById('toggleAutonomousBtn');
    const slotsBadge = document.getElementById('autonomousSlotsBadge');

    if (badge) {
        if (st.enabled) {
            badge.textContent = 'Autonomous Active';
            badge.style.background = 'rgba(16,185,129,0.15)';
            badge.style.color = '#10b981';
            badge.style.borderColor = 'rgba(16,185,129,0.3)';
        } else {
            badge.textContent = 'Paused';
            badge.style.background = 'rgba(239,68,68,0.15)';
            badge.style.color = '#ef4444';
            badge.style.borderColor = 'rgba(239,68,68,0.3)';
        }
    }

    if (workerBadge) {
        if (wd.status === 'RUNNING') {
            workerBadge.textContent = 'Worker: Running ⚡';
            workerBadge.style.background = 'rgba(56,189,248,0.2)';
            workerBadge.style.color = '#38bdf8';
            workerBadge.style.borderColor = 'rgba(56,189,248,0.4)';
        } else if (wd.status === 'HEALTHY') {
            const ageText = wd.age_minutes != null ? `${wd.age_minutes}m ago` : 'active';
            workerBadge.textContent = `Worker: Healthy (${ageText})`;
            workerBadge.style.background = 'rgba(16,185,129,0.15)';
            workerBadge.style.color = '#10b981';
            workerBadge.style.borderColor = 'rgba(16,185,129,0.3)';
        } else if (wd.status === 'WORKER_STALE') {
            workerBadge.textContent = `Worker: Stale (${wd.age_minutes}m)`;
            workerBadge.style.background = 'rgba(245,158,11,0.15)';
            workerBadge.style.color = '#f59e0b';
            workerBadge.style.borderColor = 'rgba(245,158,11,0.3)';
        } else if (wd.status === 'MARKET_CLOSED') {
            workerBadge.textContent = 'Worker: Standby (Closed)';
            workerBadge.style.background = 'rgba(148,163,184,0.15)';
            workerBadge.style.color = '#94a3b8';
            workerBadge.style.borderColor = 'rgba(148,163,184,0.3)';
        } else {
            workerBadge.textContent = 'Worker: Idle';
            workerBadge.style.background = 'rgba(148,163,184,0.15)';
            workerBadge.style.color = '#94a3b8';
            workerBadge.style.borderColor = 'rgba(148,163,184,0.3)';
        }
    }

    if (toggleBtn) {
        toggleBtn.textContent = st.enabled ? 'Pause Auto-Trade' : 'Resume Auto-Trade';
    }
    if (slotsBadge) {
        slotsBadge.textContent = `Slots: ${st.open_positions || 0}/${st.max_positions || 8} Active`;
    }
}

function renderAutonomousStatusError(err) {
    const workerBadge = document.getElementById('autonomousWorkerBadge');
    if (workerBadge) {
        workerBadge.textContent = 'Worker: Unavailable (Retry)';
        workerBadge.style.background = 'rgba(244,63,94,0.15)';
        workerBadge.style.color = '#f43f5e';
        workerBadge.style.borderColor = 'rgba(244,63,94,0.3)';
        workerBadge.style.cursor = 'pointer';
        workerBadge.title = 'Click to retry status check: ' + (err?.message || 'Network error');
        workerBadge.onclick = () => fetchAutonomousStatus({ isManual: true });
    }
}

async function fetchAutonomousFeed({ isManual = false } = {}) {
    if (_autonomousFeedInFlight && !isManual) return;
    _autonomousFeedInFlight = true;
    const currentGen = ++_autonomousFeedGen;

    if (_autonomousFeedController) {
        try { _autonomousFeedController.abort(); } catch (_) {}
    }
    _autonomousFeedController = new AbortController();

    try {
        const reqHeaders = {};
        const adminKey = (typeof localStorage !== 'undefined') ? (localStorage.getItem('tradeai_admin_key') || localStorage.getItem('admin_api_key')) : null;
        if (adminKey && adminKey.trim()) reqHeaders['X-API-Key'] = adminKey.trim();

        let targetUrl = `${API}/api/autonomous/feed?limit=25`;
        let res;
        try {
            res = await fetchWithTimeout(targetUrl, {
                signal: _autonomousFeedController.signal,
                headers: reqHeaders,
            }, 10000, 0);
        } catch (netErr) {
            // Same-origin/Worker deployments: retain relative URL to leverage Worker edge proxy and avoid direct 429 challenges.
            // Failure mode covered by absolute fallback: static Pages/Vercel hosting where same-origin /api does not exist.
            const fallbackUrl = (targetUrl.startsWith('/') || !targetUrl.startsWith('http'))
                ? `/api/autonomous/feed?limit=25`
                : `${API}/api/autonomous/feed?limit=25`;
            try {
                res = await fetchWithTimeout(fallbackUrl, {
                    signal: _autonomousFeedController.signal,
                    headers: reqHeaders,
                }, 10000, 0);
            } catch (_) {
                throw netErr;
            }
        }

        if (res && (!res.ok || res.status === 429 || res.status === 403 || res.status === 502 || res.status === 503)) {
            // Same-origin/Worker deployments: retain relative URL to leverage Worker edge proxy and avoid direct 429 challenges.
            // Failure mode covered by absolute fallback: static Pages/Vercel hosting where same-origin /api does not exist.
            const altUrl = (targetUrl.startsWith('/') || !targetUrl.startsWith('http'))
                ? `/api/autonomous/feed?limit=25`
                : `${API}/api/autonomous/feed?limit=25`;
            try {
                const altRes = await fetchWithTimeout(altUrl, {
                    signal: _autonomousFeedController.signal,
                    headers: reqHeaders,
                }, 10000, 0);
                if (altRes && altRes.ok) res = altRes;
            } catch (_) {}
        }

        if (currentGen !== _autonomousFeedGen) return;

        const data = await parseJsonResponse(res);
        if (data && data.ok && Array.isArray(data.feed)) {
            _lastAutonomousFeedData = data;
            renderAutonomousFeedUI(data.feed, { isCached: false });
            try {
                localStorage.setItem(AUTO_FEED_STORAGE_KEY, JSON.stringify({
                    cached_at: Date.now(),
                    feed: data.feed.slice(0, 25),
                }));
            } catch (_) {}
        } else {
            renderAutonomousFeedError(new Error(data?.message || 'Feed payload invalid'));
        }
    } catch (err) {
        if (currentGen !== _autonomousFeedGen) return;
        renderAutonomousFeedError(err);
    } finally {
        _autonomousFeedInFlight = false;
        _autonomousFeedController = null;
    }
}

function renderAutonomousFeedUI(rawFeed, { isCached = false } = {}) {
    const listEl = document.getElementById('autonomousDecisionFeedList');
    if (!listEl) return;

    if (!Array.isArray(rawFeed) || rawFeed.length === 0) {
        listEl.innerHTML = '<div style="color:var(--text-muted,#94a3b8);font-size:12px;padding:0.5rem;">Autonomous engine active. Monitoring candles for qualified entries...</div>';
        return;
    }

    let lastAction = null;
    const displayFeed = rawFeed.filter(item => {
        const act = (item.action || '').toUpperCase();
        const isStandby = (act === 'MARKET_CLOSED' || act === 'SYSTEM_STANDBY');
        if (isStandby && (lastAction === 'MARKET_CLOSED' || lastAction === 'SYSTEM_STANDBY')) {
            return false;
        }
        lastAction = act;
        return true;
    });

    if (displayFeed.length === 0) {
        listEl.innerHTML = '<div style="color:var(--text-muted,#94a3b8);font-size:12px;padding:0.5rem;">Autonomous engine active. Monitoring candles for qualified entries...</div>';
        return;
    }

    listEl.innerHTML = displayFeed.map(item => {
        let badgeColor = '#38bdf8';
        let bgBadge = 'rgba(56,189,248,0.15)';
        const act = (item.action || '').toUpperCase();

        if (act === 'AUTO_ENTER') {
            badgeColor = '#10b981';
            bgBadge = 'rgba(16,185,129,0.15)';
        } else if (act.includes('TP') || act.includes('TARGET') || act.includes('TRAILING')) {
            badgeColor = '#10b981';
            bgBadge = 'rgba(16,185,129,0.15)';
        } else if (act.includes('SL') || act.includes('STOP')) {
            badgeColor = '#f43f5e';
            bgBadge = 'rgba(244,63,94,0.15)';
        } else if (act.includes('EOD') || act.includes('SQUAREOFF')) {
            badgeColor = '#f59e0b';
            bgBadge = 'rgba(245,158,11,0.15)';
        } else if (act.includes('SKIP') || act.includes('DUPLICATE')) {
            badgeColor = '#a855f7';
            bgBadge = 'rgba(168,85,247,0.15)';
        } else if (act === 'NO_TRADE' || act.includes('CLOSED')) {
            badgeColor = '#94a3b8';
            bgBadge = 'rgba(148,163,184,0.15)';
        }

        const tsStr = item.timestamp ? (item.timestamp.endsWith('Z') || item.timestamp.includes('+') ? item.timestamp : item.timestamp + 'Z') : null;
        const timeStr = tsStr ? new Date(tsStr).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Asia/Kolkata' }) : '';
        const pnlText = (item.pnl_pct !== null && item.pnl_pct !== undefined) ? `<span style="color:${item.pnl_pct >= 0 ? '#10b981' : '#f43f5e'};font-weight:600;">(${item.pnl_pct >= 0 ? '+' : ''}${item.pnl_pct}%)</span>` : '';
        const priceText = item.price ? `₹${Number(item.price).toLocaleString('en-IN')}` : '';
        const scorePill = item.qs_score > 0 ? `<span style="font-size:11px;color:#38bdf8;background:rgba(56,189,248,0.1);padding:1px 4px;border-radius:3px;">QS ${item.qs_score}</span>` : '';

        return `
            <div class="decision-feed-row" style="display:flex;align-items:center;justify-content:space-between;border-radius:6px;padding:6px 10px;font-size:12px;">
                <div style="display:flex;align-items:center;gap:8px;">
                    <span style="font-family:monospace;font-size:11px;color:var(--text-muted,#94a3b8);">${timeStr}</span>
                    <span style="background:${bgBadge};color:${badgeColor};border:1px solid ${badgeColor}33;padding:2px 6px;border-radius:4px;font-size:11px;font-weight:600;">${escapeHtml(item.action)}</span>
                    <strong style="color:var(--text-primary,#f8fafc);">${escapeHtml(item.symbol)}</strong>
                    ${item.direction && item.direction !== 'HOLD' && item.direction !== 'NEUTRAL' ? `<span style="color:${item.direction === 'LONG' ? '#10b981' : '#f43f5e'};font-weight:600;font-size:11px;">${escapeHtml(item.direction)}</span>` : ''}
                    ${priceText ? `<span style="font-family:monospace;font-size:11px;color:var(--text-muted,#94a3b8);">${priceText}</span>` : ''}
                    ${scorePill}
                    ${pnlText}
                </div>
                <div style="color:var(--text-muted,#94a3b8);font-size:11px;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(item.reason || '')}">
                    ${escapeHtml(item.reason || '')}
                </div>
            </div>
        `;
    }).join('');
}

function renderAutonomousFeedError(err) {
    const listEl = document.getElementById('autonomousDecisionFeedList');
    if (!listEl) return;
    if (_lastAutonomousFeedData && Array.isArray(_lastAutonomousFeedData.feed) && _lastAutonomousFeedData.feed.length > 0) {
        return;
    }
    listEl.innerHTML = `
        <div style="color:#f43f5e;font-size:12px;padding:0.6rem;display:flex;align-items:center;justify-content:space-between;background:rgba(244,63,94,0.08);border-radius:6px;border:1px solid rgba(244,63,94,0.2);">
            <span>Could not load decision feed (${escapeHtml(err?.message || 'Network timeout')}).</span>
            <button type="button" onclick="fetchAutonomousFeed({ isManual: true })" style="padding:2px 8px;font-size:11px;background:#1e293b;border:1px solid #475569;border-radius:4px;color:#94a3b8;cursor:pointer;">Retry</button>
        </div>
    `;
}

function _scheduleAutonomousPoll() {
    if (_autonomousPollTimer) {
        clearTimeout(_autonomousPollTimer);
        _autonomousPollTimer = null;
    }

    if (typeof document !== 'undefined' && document.hidden) return; // Tab paused

    let intervalMs = 20000;
    const isNseOpen = _lastAutonomousStatusData?.watchdog?.is_nse_market_open;
    if (!isNseOpen) {
        intervalMs = 60000;
    }
    if (_autonomousConsecutiveErrors > 0) {
        intervalMs = Math.min(intervalMs * Math.pow(1.5, _autonomousConsecutiveErrors), 120000);
    }

    _autonomousPollTimer = setTimeout(() => {
        loadAutonomousFeed({ isManual: false });
    }, intervalMs);
}

async function loadAutonomousFeed({ isManual = false } = {}) {
    await Promise.allSettled([
        fetchAutonomousStatus({ isManual }),
        fetchAutonomousFeed({ isManual }),
    ]);
    _scheduleAutonomousPoll();
}

if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            if (_autonomousPollTimer) {
                clearTimeout(_autonomousPollTimer);
                _autonomousPollTimer = null;
            }
            if (_autonomousStatusController) {
                try { _autonomousStatusController.abort(); } catch (_) {}
            }
            if (_autonomousFeedController) {
                try { _autonomousFeedController.abort(); } catch (_) {}
            }
        } else {
            loadAutonomousFeed({ isManual: false });
        }
    });
}


async function runAutonomousCycleNow() {
    const btn = document.getElementById('runAutonomousCycleBtn');
    const origText = btn ? btn.textContent : '⚡ Run Cycle';
    try {
        if (btn) {
            btn.disabled = true;
            btn.textContent = '⏳ Starting...';
        }
        const targetApi = (typeof API === 'string') ? API : (window.__TRADEAI_CONFIG__?.API_BASE_URL ?? '');
        // /api/autonomous/run-cycle is admin-gated. Without the stored operator
        // key the request always 403s, which used to surface as a generic
        // "Error triggering autonomous cycle" toast.
        const reqHeaders = { 'Content-Type': 'application/json' };
        const adminKey = (typeof localStorage !== 'undefined')
            ? (localStorage.getItem('tradeai_admin_key') || localStorage.getItem('admin_api_key'))
            : null;
        if (adminKey && adminKey.trim()) reqHeaders['X-API-Key'] = adminKey.trim();
        const res = await fetchWithTimeout(targetApi + '/api/autonomous/run-cycle', {
            method: 'POST',
            headers: reqHeaders,
            body: JSON.stringify({ universe: 'tier1' }),
        }, 15000);
        let data = null;
        try {
            data = await parseJsonResponse(res);
        } catch (parseErr) {
            data = null;
        }
        if (res && (res.status === 401 || res.status === 403)) {
            const enteredKey = prompt('Admin API Key required to trigger autonomous worker cycle.\nPlease enter your ADMIN_API_KEY:');
            if (enteredKey && enteredKey.trim()) {
                localStorage.setItem('tradeai_admin_key', enteredKey.trim());
                localStorage.setItem('admin_api_key', enteredKey.trim());
                if (typeof showToast === 'function') showToast('Admin key saved! Triggering cycle...', 'info');
                reqHeaders['X-API-Key'] = enteredKey.trim();
                const retryRes = await fetchWithTimeout(targetApi + '/api/autonomous/run-cycle', {
                    method: 'POST',
                    headers: reqHeaders,
                    body: JSON.stringify({ universe: 'tier1' }),
                }, 15000);
                let retryData = null;
                try { retryData = await parseJsonResponse(retryRes); } catch (_) {}
                if (retryData && retryData.ok) {
                    const okMsg = 'Cycle triggered (' + (retryData.method || 'dispatched') + ')';
                    if (typeof showToast === 'function') showToast(okMsg, 'success'); else alert(okMsg);
                    setTimeout(() => { loadAutonomousFeed(); }, 3000);
                    return;
                }
            }
            const authMsg = 'Admin key required for manual cycle. Open Market Control / admin console and unlock first.';
            if (typeof showToast === 'function') showToast(authMsg, 'warning'); else alert(authMsg);
            return;
        }
        if (data && data.ok) {
            const okMsg = 'Cycle triggered (' + (data.method || 'dispatched') + ')';
            if (typeof showToast === 'function') {
                showToast(okMsg, 'success');
            } else {
                alert(okMsg);
            }
            setTimeout(() => { loadAutonomousFeed(); }, 3000);
        } else {
            const msg = data?.message || data?.detail || data?.error || 'Cycle rate limited. Please wait a minute.';
            if (typeof showToast === 'function') {
                showToast(msg, 'warning');
            } else {
                alert(msg);
            }
        }
    } catch (e) {
        console.error('Failed to trigger autonomous cycle:', e);
        const errMsg = 'Error triggering autonomous cycle: ' + (e.message || e);
        if (typeof showToast === 'function') {
            showToast(errMsg, 'error');
        } else {
            alert(errMsg);
        }
    } finally {
        if (btn) {
            setTimeout(() => {
                btn.disabled = false;
                btn.textContent = origText;
            }, 3000);
        }
    }
}

async function toggleAutonomousMode() {
    try {
        const res = await fetchWithTimeout(`${API}/api/autonomous/toggle`, { method: 'POST' }, 5000);
        const data = await parseJsonResponse(res);
        if (data && data.ok) {
            loadAutonomousFeed();
        }
    } catch (e) {
        console.error('Failed to toggle autonomous mode:', e);
    }
}



async function loadOverviewFast() {

    try {

        const res = await fetchWithTimeout(`${API}/api/dashboard/overview`, {}, 6000);

        const data = await parseJsonResponse(res);

        if (data && data.status === 'ok') {

            if (data.capital !== undefined) {
                const capEl = document.getElementById('statCapital');
                if (capEl) capEl.textContent = _fmtMoney(data.capital);
            }
            if (data.initial_capital !== undefined) {
                const seedEl = document.getElementById('statCapitalSeed');
                if (seedEl) seedEl.textContent = 'Seed: ' + _fmtMoney(data.initial_capital);
            }
            if (_unpricedPositions > 0) {
                _applyUnpricedMtm();
            } else if (data.unrealized_pnl !== undefined) {
                const uEl = document.getElementById('statUnrealizedPnl');
                if (uEl) {
                    const u = Number(data.unrealized_pnl);
                    uEl.textContent = 'MTM: ' + _fmtSignedMoney(u);
                    uEl.style.color = u > 0 ? '#10b981' : (u < 0 ? '#ef4444' : '#8b949e');
                }
            }
            if (Array.isArray(data.positions) && data.positions.length > 0) {
                _lastOpenTrades = data.positions;
            }
            if (data.initial_capital !== undefined) {
                const seedEl = document.getElementById('statCapitalSeed');
                if (seedEl) seedEl.textContent = 'Seed: ' + _fmtMoney(data.initial_capital);
            }
            if (data.total_pnl !== undefined) {
                const pnlEl = document.getElementById('statTotalPnl') || document.getElementById('statPnl');
                if (pnlEl) {
                    const p = Number(data.total_pnl);
                    pnlEl.textContent = _fmtSignedMoney(p);
                    pnlEl.className = 'stat-value ' + (p >= 0 ? 'positive' : 'negative');
                }
            }
            const pSummary = data.portfolio_summary || data;
            if (pSummary) {
                const statSummary = typeof formatTradeStatsSummary === 'function'
                    ? formatTradeStatsSummary(pSummary)
                    : { winRateDisplay: '—', subText: '0 closed trades', titleText: '', closedSubDetail: '0 closed' };
                const openCount = Number(pSummary.open_positions_count ?? (Array.isArray(data.positions) ? data.positions.length : 0));
                const wrEl = document.getElementById('statWinRate');
                if (wrEl) {
                    wrEl.textContent = statSummary.winRateDisplay;
                }
                const wrSub = document.getElementById('statWinRateSub');
                if (wrSub) {
                    wrSub.textContent = statSummary.subText;
                    wrSub.title = statSummary.titleText;
                }
                const subTradesEl = document.getElementById('statTradesSubDetail');
                if (subTradesEl) {
                    subTradesEl.textContent = statSummary.closedSubDetail;
                }
                const tradesEl = document.getElementById('statTrades');
                if (tradesEl && openCount != null) {
                    tradesEl.textContent = `${openCount}`;
                }
            }
            if (data.dominant_regime) {
                const regEl = document.getElementById('statRegime') || document.getElementById('dominantRegimeBadge');
                if (regEl) regEl.textContent = data.dominant_regime;
            }

        }

    } catch (_) {}

}



async function loadAllData({ background = false } = {}) {



    if (_isRefreshing) return;



    _isRefreshing = true;







    const refreshDot = document.getElementById('refreshDot');



    if (refreshDot) refreshDot.classList.add('refreshing');







    // ── Section-resilient parallel fetch ──────────────────────────────────────



    // Each section fetches independently; failure in one does NOT blank others.



    // Adaptive Lazy-Loading: Heavy peripheral modules are fetched on-demand or with 3-minute interval

    const nowTs = Date.now();
    const isColdBoot = (_lastOppsFetchTs === 0 && _lastCryptoFetchTs === 0);

    // Stagger non-essential deep intelligence queries during cold boot
    const shouldFetchOpps = (currentTab === 'opportunities') || (!isColdBoot && (nowTs - (_lastOppsFetchTs || 0) > 180000));
    const shouldFetchCrypto = (currentTab === 'crypto') || (!isColdBoot && (nowTs - (_lastCryptoFetchTs || 0) > 180000));
    const shouldFetchMorning = (currentTab === 'morning-scan') || (!isColdBoot && (nowTs - (_lastMorningFetchTs || 0) > 300000));



    const promises = [

        fetchWithTimeout(`${API}/api/signals/all`, {}, 60000).then((r) => parseJsonResponse(r)),

        fetchWithTimeout(`${API}/api/stats`, {}, 25000).then((r) => parseJsonResponse(r)),

        fetchWithTimeout(`${API}/api/nse/dashboard`, {}, 25000).then((r) => parseJsonResponse(r)),

        shouldFetchOpps ? fetchWithTimeout(`${API}/api/screener/opportunities`, {}, 25000).then((r) => { _lastOppsFetchTs = nowTs; return parseJsonResponse(r); }).catch(() => null) : Promise.resolve(null),

        fetchWithTimeout(`${API}/api/overview/intelligence`, {}, 60000).then((r) => parseJsonResponse(r)).catch(() => null),

        shouldFetchMorning ? fetchWithTimeout(`${API}/api/morning-scan`, {}, 20000).then((r) => { _lastMorningFetchTs = nowTs; return parseJsonResponse(r); }).catch(() => null) : Promise.resolve(null),

        shouldFetchCrypto ? fetchWithTimeout(`${API}/api/crypto/intelligence`, {}, 25000).then((r) => { _lastCryptoFetchTs = nowTs; return parseJsonResponse(r); }).catch(() => null) : Promise.resolve(null),

        fetchWithTimeout(`${API}/api/paper/open`, {}, 15000).then((r) => parseJsonResponse(r)).catch(() => null),

    ];



    const [signalsResult, statsResult, dashboardResult, oppsResult, cockpitResult, morningScanResult, cryptoIntelResult, openTradesResult] = await Promise.allSettled(promises);







    // ── Signals ──────────────────────────────────────────────────────────────



    if (signalsResult.status === 'fulfilled' && signalsResult.value) {



        const signalsData = signalsResult.value;



        const nextSignals = Array.isArray(signalsData.signals) ? signalsData.signals : [];



        const nextSignature = getSignalsSignature(nextSignals);



        allSignals = nextSignals;
        if (Array.isArray(nextSignals) && nextSignals.length > 0) {
            nextSignals.forEach(s => {
                if (s && s.symbol && s.close != null) {
                    marketDataCache[s.symbol] = {
                        close: s.close,
                        change_pct: s.change_pct ?? null,
                    };
                }
            });
            updateMoodStripFromCache();
        }



        if (nextSignature !== _lastSignalsSignature) {



            if (_cockpitFilter !== 'all') {



                // Re-apply the active risk-aware filter when signals update



                applyCockpitFilter();



            } else {



                renderSignals(allSignals);



            }



            _lastSignalsSignature = nextSignature;



        }



        updateSummary(allSignals);



        updateMarketSessionBanner();



        const updated = new Date(signalsData.updated_at);



        if (!isNaN(updated.getTime())) {



            document.getElementById('lastUpdated').textContent =



                updated.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });



        }



        if (nextSignals.length > 0) {



            try {



                localStorage.setItem('tradeai_cached_signals_v1', JSON.stringify({



                    signals: nextSignals,



                    stats: _lastStatsData,



                    updated_at: signalsData.updated_at || new Date().toISOString()



                }));



            } catch (e) {}



        }



        const loader = document.getElementById('loadingState');



        if (loader) {



            if (nextSignals.length === 0 && (!allSignals || allSignals.length === 0)) {



                loader.classList.remove('hidden');



                loader.innerHTML = '<div class="loader"></div><p>Warming up market data... this can take 1-2 minutes on first load.</p>';



            } else {



                loader.classList.add('hidden');



            }



        }



    } else {



        if (!background) handleLoadError();



        console.warn('[loadAllData] signals fetch failed:', signalsResult.reason || 'unknown');



    }







    // ── Stats ────────────────────────────────────────────────────────────────



    if (statsResult.status === 'fulfilled' && statsResult.value) {



        _lastStatsData = statsResult.value;



        updateStatsStrip(_lastStatsData);



    } else if (_lastStatsData) {



        updateStatsStrip(_lastStatsData); // use cached snapshot



    }







    // ── Open Paper Positions on Overview ─────────────────────────────────────



    await loadMacroIntelligence();
    if (openTradesResult && openTradesResult.status === 'fulfilled' && openTradesResult.value) {



        _unpricedPositions = Number((openTradesResult.value.summary || {}).unpriced_positions || 0);
        _applyUnpricedMtm();
        renderOverviewActiveTrades(openTradesResult.value.open_trades || [], openTradesResult.value.summary || {});



    }







    // ── Dashboard (mood, AI summary, movers) ─────────────────────────────────



    if (dashboardResult.status === 'fulfilled' && dashboardResult.value) {



        _lastDashboardData = dashboardResult.value;



        updateMarketIntel(_lastDashboardData);



    } else if (_lastDashboardData) {



        updateMarketIntel(_lastDashboardData);



    }







    // ── Opportunities ────────────────────────────────────────────────────────



    const oppsData = oppsResult.status === 'fulfilled' && oppsResult.value ? oppsResult.value : null;



    if (oppsData && oppsData.opportunities) {



        _lastOppsData = oppsData;



        updateOpportunitiesGrid(oppsData.opportunities, { status: oppsData.status, reason: oppsData.reason });



    } else if (_lastOppsData && _lastOppsData.opportunities) {



        updateOpportunitiesGrid(_lastOppsData.opportunities, { status: _lastOppsData.status, reason: _lastOppsData.reason });



    }







    // ── Decision cockpit (Phase 1: section-level — failure does not blank Overview) ──



    const cockpitData = cockpitResult.status === 'fulfilled' && cockpitResult.value ? cockpitResult.value : null;



    if (cockpitData && !cockpitData.error) {



        _lastCockpitData = cockpitData;



        try {



            localStorage.setItem('tradeai_cached_cockpit_v1', JSON.stringify(cockpitData));



        } catch (_) {}



        renderCockpit(cockpitData);



    } else if (_lastCockpitData) {



        renderCockpit(_lastCockpitData);



    } else {



        const errMsg = (cockpitData && cockpitData.error)



            ? String(cockpitData.error)



            : 'Connecting to market decision engine... Please wait a moment.';



        renderCockpitError(errMsg);



        setTimeout(() => { if (!_isRefreshing) loadAllData({ background: true }); }, 6000);



    }







    // ── Morning Scan ─────────────────────────────────────────────────────────



    const morningScanData = morningScanResult.status === 'fulfilled' && morningScanResult.value ? morningScanResult.value : null;



    if (morningScanData && morningScanData.results) {



        _morningScanData = morningScanData;



        _renderMorningScan(morningScanData);



    } else if (_morningScanData) {



        _renderMorningScan(_morningScanData);



    }







    // ── Phase 6: Crypto mini-panel in Overview ────────────────────────────────



    const cryptoIntelData = cryptoIntelResult?.status === 'fulfilled' ? cryptoIntelResult.value : null;



    if (cryptoIntelData && !cryptoIntelData.error) {



        _lastCryptoIntelData = cryptoIntelData;



        _renderCryptoMiniPanel(cryptoIntelData);



    } else if (_lastCryptoIntelData) {



        _renderCryptoMiniPanel(_lastCryptoIntelData);



    }







    // ── Phase 4: Alert checks ─────────────────────────────────────────────────



    if (allSignals.length) _checkAlertThresholds(allSignals);







    // Background tab refreshes



    if (currentTab === 'learning') loadLearningStats();



    if (currentTab === 'portfolio') loadPortfolio();



    if (currentTab === 'performance') loadPerformance();







    if (refreshDot) refreshDot.classList.remove('refreshing');



    _isRefreshing = false;







    // ── Phase 4: Smart Refresh — reschedule with adaptive interval ────────────



    _scheduleSmartRefresh();



}











// ── Overview Intelligence / Decision Cockpit ─────────────────────────────────







function renderCockpit(data) {



    if (!data) return;







    _renderSystemState(data.system_state || {}, data.updated_at);



    _renderGuardrails(data.market_guardrails || {});



    _renderLearningOverlay(data.learning_overlay);



    _renderTopSetups(data.top_setups || [], data.ranking_meta || {});



    _renderAvoidList(data.avoid_list || []);



    // Phase 3



    _renderPortfolioCaution(data.portfolio_caution || {});



    _renderDeltaPanel(data.delta || null);



    _renderSectorRotation(data.sector_rotation || []);



    _renderIndexLeadership(data.index_leadership || {});



    // Phase 4



    _renderWatchlistPanel();



    _renderOpportunityTimeline(data.top_setups || []);



    // Phase 5



    _renderFeedStalenessBanner(data.market_guardrails || {});



}







/** Phase 1: show explicit error in cockpit tiles when /api/overview/intelligence fails and no cache exists */



function renderCockpitError(message) {



    const msg = message || 'Cockpit unavailable';



    const html = `<div class="cockpit-error" role="alert">${msg}</div>`;



    ['cockpitSysInner', 'cockpitGuardrailsInner', 'cockpitLearningInner'].forEach(id => {



        const el = document.getElementById(id);



        if (el) el.innerHTML = html;



    });



    const grid = document.getElementById('cockpitSetupsGrid');



    if (grid) grid.innerHTML = html;



    const avoid = document.getElementById('cockpitAvoidList');



    if (avoid) avoid.innerHTML = html;



}







function _renderSystemState(sys, intelligenceUpdatedAt) {



    const el = document.getElementById('cockpitSysInner');



    if (!el) return;







    const fmt = v => (v != null && !isNaN(v)) ? `₹${Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}` : '—';



    const pnl = Number(sys.total_pnl || 0);



    const pnlColor = pnl >= 0 ? 'var(--green)' : 'var(--red)';



    const heatPct = Number(sys.portfolio_heat_pct || 0);



    const heatColor = heatPct >= 60 ? 'var(--red)' : heatPct >= 40 ? '#f59e0b' : 'var(--green)';



    const ddPct = Number(sys.daily_drawdown_pct || 0);







    const circuitHtml = sys.circuit_breaker_active



        ? `<div class="circuit-alert">⛔ Circuit Breaker: ${sys.circuit_breaker_reason || 'active'}</div>`



        : '';







    let refreshLine = '';



    if (sys.last_market_refresh) {



        try {



            const t = new Date(sys.last_market_refresh);



            refreshLine = `



        <div class="cockpit-sys-row">



            <span class="cockpit-sys-key">Last data refresh</span>



            <span class="cockpit-sys-val" style="font-size:0.72rem;">${t.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true })}</span>



        </div>`;



        } catch (_) { /* ignore */ }



    }



    let intelLine = '';



    if (intelligenceUpdatedAt) {



        try {



            const ti = new Date(intelligenceUpdatedAt);



            intelLine = `



        <div class="cockpit-sys-row">



            <span class="cockpit-sys-key">Cockpit snapshot</span>



            <span class="cockpit-sys-val" style="font-size:0.72rem;">${ti.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true })}</span>



        </div>`;



        } catch (_) { /* ignore */ }



    }







    el.innerHTML = `



        ${refreshLine}



        ${intelLine}



        <div class="cockpit-sys-row">



            <span class="cockpit-sys-key">Capital</span>



            <span class="cockpit-sys-val">${fmt(sys.capital)}</span>



        </div>



        <div class="cockpit-sys-row">



            <span class="cockpit-sys-key">P&L</span>



            <span class="cockpit-sys-val" style="color:${pnlColor}">${pnl >= 0 ? '+' : ''}${fmt(pnl)}</span>



        </div>



        <div class="cockpit-sys-row">



            <span class="cockpit-sys-key">Open Trades</span>



            <span class="cockpit-sys-val">${sys.open_trades ?? 0}</span>



        </div>



        <div class="cockpit-sys-row">



            <span class="cockpit-sys-key">Portfolio Heat</span>



            <span class="cockpit-sys-val" style="color:${heatColor}">${heatPct.toFixed(1)}%</span>



        </div>



        <div class="cockpit-sys-row">



            <span class="cockpit-sys-key">Daily Drawdown</span>



            <span class="cockpit-sys-val" style="color:${ddPct > 3 ? 'var(--red)' : 'var(--text-primary)'}">${ddPct.toFixed(2)}%</span>



        </div>



        ${circuitHtml}



    `;



}







function _renderGuardrails(grd) {



    const el = document.getElementById('cockpitGuardrailsInner');



    if (!el) return;







    const mood = grd.mood || 'NEUTRAL';



    const score = Number(grd.mood_score || 50).toFixed(1);



    const moodColor = mood === 'BULLISH' ? 'var(--green)' : mood === 'BEARISH' ? 'var(--red)' : mood === 'CAUTIOUS' ? '#f59e0b' : '#22d3ee';



    const feed = grd.feed_status || {};



    if (typeof feed === 'string') {
        feed = { state: feed };
    }
    const feedState = feed.state || 'UNKNOWN';



    const feedColor = (feedState === 'LIVE' || feedState === 'CURRENT')
        ? 'var(--green)'
        : (feedState === 'EOD' || feedState === 'CLOSED' || feedState === 'STANDBY')
            ? '#22d3ee'
            : (feedState === 'DELAYED' ? '#f59e0b' : 'var(--red)');



    const regime = grd.regime_summary || {};



    const bsplit = grd.breadth_split || {};



    const breadth = bsplit.breadth || {};



    const sigBias = bsplit.signal_bias || {};







    const sectorAvg = grd.sector_avg_pct;



    const whyHint = grd.why_market_hint || '';







    el.innerHTML = `



        <div class="guardrail-row">



            <span class="guardrail-key">Mood</span>



            <span class="guardrail-val" style="color:${moodColor}">${mood} ${score}/100</span>



        </div>



        <div class="guardrail-row">



            <span class="guardrail-key">Feed</span>



            <span class="guardrail-val" style="color:${feedColor}">${feedState}${feed.age_seconds != null ? ` (${feed.age_seconds}s ago)` : ''}</span>



        </div>



        <div class="guardrail-row">



            <span class="guardrail-key">Dominant Regime</span>



            <span class="guardrail-val">${regime.dominant || '—'} (${(regime.pct_trending || 0).toFixed(0)}% trending)</span>



        </div>



        <div class="guardrail-row">



            <span class="guardrail-key">Market Breadth</span>



            <span class="guardrail-val" style="color:${(breadth.label || '') === 'BULLISH' ? 'var(--green)' : (breadth.label || '') === 'BEARISH' ? 'var(--red)' : '#22d3ee'}">${breadth.label || '—'} ${breadth.score != null ? `(${Number(breadth.score).toFixed(0)}%)` : ''}</span>



        </div>



        <div class="guardrail-row">



            <span class="guardrail-key">Signal Bias</span>



            <span class="guardrail-val" style="color:${(sigBias.label || '') === 'BULLISH' ? 'var(--green)' : (sigBias.label || '') === 'BEARISH' ? 'var(--red)' : '#22d3ee'}">${sigBias.label || '—'} ${sigBias.score != null ? `(${Number(sigBias.score).toFixed(0)}%)` : ''}</span>



        </div>



        ${sectorAvg != null ? `



        <div class="guardrail-row">



            <span class="guardrail-key">Sector avg (cache)</span>



            <span class="guardrail-val">${Number(sectorAvg) >= 0 ? '+' : ''}${Number(sectorAvg).toFixed(2)}%</span>



        </div>` : ''}



        ${whyHint ? `<div class="why-market-hint" title="Why market looks this way">${whyHint}</div>` : ''}



    `;



}







function _renderLearningOverlay(overlay) {



    const el = document.getElementById('cockpitLearningInner');



    if (!el) return;







    if (!overlay) {



        el.innerHTML = '<span class="cockpit-skeleton">Not enough trades yet (need 10+)</span>';



        return;



    }







    const wrColor = overlay.overall_win_rate >= 55 ? 'var(--green)' : overlay.overall_win_rate >= 45 ? '#f59e0b' : 'var(--red)';







    const p2 = overlay.phase2_note



        ? `<div class="phase2-calib-note">${overlay.phase2_note}</div>`



        : '';







    el.innerHTML = `



        <div class="learning-advisory-text" style="margin-bottom:0.4rem;">${overlay.advisory}</div>



        ${p2}



        <div class="learning-stat-row">



            <span class="learning-stat-key">Trades Learnt</span>



            <span class="learning-stat-val">${overlay.total_learnt_trades}</span>



        </div>



        <div class="learning-stat-row">



            <span class="learning-stat-key">Win Rate</span>



            <span class="learning-stat-val" style="color:${wrColor}">${overlay.overall_win_rate.toFixed(1)}%</span>



        </div>



        <div class="learning-stat-row">



            <span class="learning-stat-key">Model Accuracy</span>



            <span class="learning-stat-val">${overlay.model_accuracy.toFixed(1)}%</span>



        </div>



        <div class="learning-stat-row">



            <span class="learning-stat-key">Best Regime</span>



            <span class="learning-stat-val">${overlay.best_regime}</span>



        </div>



    `;



}







function _renderTopSetups(setups, meta) {



    const grid = document.getElementById('cockpitSetupsGrid');



    const countBadge = document.getElementById('cockpitTopCount');



    const metaLabel = document.getElementById('cockpitRankMeta');







    if (countBadge) countBadge.textContent = setups.length;



    if (metaLabel) {



        const total = meta.total_tracked || 0;



        const actionable = meta.actionable || 0;



        const blocked = meta.blocked_high_quality || 0;



        const ver = meta.scoring_version || '';



        const sessionTag = meta.session_state === 'EOD' ? ' · Last Session' : '';
        const fallbackNote = meta.fallback_applied ? ' · high-QS candidates' : '';
        metaLabel.textContent = `${actionable} actionable / ${total} tracked (${blocked} blocked)${sessionTag}${fallbackNote}${ver ? ` · ${ver}` : ''}`;



    }



    if (!grid) return;







    if (setups.length === 0) {



        grid.innerHTML = '<span class="cockpit-empty-msg">No high-quality actionable setups right now. Market may be in SKIP zone or data is warming up.</span>';



        return;



    }







    const isCrypto = sym => sym.includes('/');



    const curr = sym => isCrypto(sym) ? '$' : '₹';



    const fmtP = (sym, v) => v != null ? `${curr(sym)}${Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—';







    grid.innerHTML = setups.map(s => {



        const isShort = s.signal === 'SHORT';



        const signalClass = isShort ? 'short' : 'long';



        let cardClass = isShort ? 'short-card' : '';
        if (s.stale) cardClass += ' stale-card';



        const blockersHtml = s.blockers && s.blockers.length > 0



            ? `<div class="cs-blocker">⚠ ${s.blockers.map(b => `<span class="cs-blocker-chip">${b.replace(/_/g, ' ')}</span>`).join('')}</div>`



            : '';







        const qColor = s.quality_grade === 'A' ? '#10b981' : s.quality_grade === 'B' ? '#818cf8' : s.quality_grade === 'C' ? '#f59e0b' : '#6b7094';







        const cal = s.calibration || {};



        const band = cal.confidence_band || 'Balanced';



        const bandStyle = band === 'Aggressive' ? 'background:rgba(16,185,129,0.15);color:#10b981;border-color:rgba(16,185,129,0.4);'



            : band === 'Conservative' ? 'background:rgba(245,158,11,0.12);color:#f59e0b;border-color:rgba(245,158,11,0.35);'



            : 'background:rgba(148,163,184,0.12);color:#94a3b8;border-color:rgba(148,163,184,0.3);';



        const histNote = cal.historical_note



            ? `<div class="cs-calib-hint" title="Advisory — from paper trades">${cal.historical_note}</div>`



            : '';







        const rx = s.rank_explain || {};



        const tops = (rx.top_contributors || []).slice(0, 2).map(t => `${t.factor} +${t.points}`).join(' · ');



        const ptot = rx.penalty_total != null && Number(rx.penalty_total) > 0



            ? ` · penalties −${Number(rx.penalty_total).toFixed(0)}`



            : '';



        const explainLine = (tops || rx.base_blend != null)



            ? `<div class="cs-explain-line" title="Phase 5 rank breakdown">Blend ${rx.base_blend != null ? rx.base_blend : '—'}${ptot}${tops ? ` · ${tops}` : ''}</div>`



            : '';







        const cfactors = (s.confluence_factors || []).join(' · ');



        const confLine = s.confluence_score != null



            ? `<div class="cs-conf-line" title="Advisory confluence: ${cfactors || 'factors'}">Confluence ${Number(s.confluence_score)}${cfactors ? ` · ${(s.confluence_factors || []).slice(0, 2).join(', ')}` : ''}</div>`



            : '';







        const pct = s.setup_percentile != null



            ? `<span class="cs-pct-badge">P${Number(s.setup_percentile).toFixed(0)}</span>`



            : '';







        // Phase 3: freshness badge



        let freshBadge = '';

        const chaseBadge = s.chase_risk
            ? `<span class="cs-fresh-badge" style="background:rgba(245,158,11,0.15);color:#fbbf24;border:1px solid rgba(245,158,11,0.45);" title="Price has moved ${Math.abs(Number(s.entry_distance_pct) || 0).toFixed(2)}% from the planned entry">CHASE ${Math.abs(Number(s.entry_distance_pct) || 0).toFixed(1)}%</span>`
            : '';



        if (s.stale) {



            freshBadge = `<span class="cs-fresh-badge" style="background:rgba(239,68,68,0.15);color:#f87171;border:1px solid rgba(239,68,68,0.45);" title="Signal data is stale — last session">STALE</span>`;



        } else if (s.data_age_min != null) {



            const age = Number(s.data_age_min);



            const freshColor = age <= 5 ? '#10b981' : age <= 15 ? '#f59e0b' : '#ef4444';



            const freshLabel = age <= 1 ? 'Live' : age < 60 ? `${age.toFixed(0)}m ago` : `${(age / 60).toFixed(1)}h ago`;



            freshBadge = `<span class="cs-fresh-badge" style="background:${freshColor}22;color:${freshColor};border:1px solid ${freshColor}55;" title="Data age">${freshLabel}</span>`;



        }







        return `



            <div class="cockpit-setup-card ${cardClass}" onclick="openSymbolModal('${s.symbol}')" title="${s.thesis}">



                <div class="cs-header">



                    <span class="cs-symbol">${s.symbol}</span>



                    <span class="cs-signal-badge ${signalClass}">${s.signal}</span>



                    ${freshBadge}${chaseBadge}${s.session_badge ? `<span class="cs-pct-badge" style="background:rgba(99,102,241,0.22);color:#a5b4fc;border:1px solid rgba(99,102,241,0.4);font-size:11px;padding:1px 5px;border-radius:4px;margin-left:4px;">${s.session_badge}</span>` : ''}



                </div>



                <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.25rem;margin-bottom:0.15rem;">



                    <span class="cs-rank" title="Phase 5 composite (freshness + bias alignment − penalties)">Rank ${s.composite_rank} ${pct}</span>



                    <span class="cs-band-pill" style="${bandStyle}">${band}</span>



                    <span style="font-size: 0.72rem;font-weight:800;color:${qColor}">QS ${s.quality_score} · ${s.quality_grade}</span>



                </div>



                ${histNote}



                ${explainLine}



                ${confLine}



                <div class="cs-thesis">${s.thesis || 'Multi-factor swing setup'}</div>



                <div class="cs-levels">



                    <div class="cs-level"><span class="cs-level-key">Entry </span><span class="cs-level-val">${fmtP(s.symbol, s.entry)}</span></div>



                    <div class="cs-level"><span class="cs-level-key">SL </span><span class="cs-level-val">${fmtP(s.symbol, s.stop_loss)}</span></div>



                    <div class="cs-level"><span class="cs-level-key">TP </span><span class="cs-level-val">${fmtP(s.symbol, s.target)}</span></div>



                    <div class="cs-level"><span class="cs-level-key">R:R </span><span class="cs-level-val">${s.rr || (s.risk_reward ? s.risk_reward + 'x' : '1.8x')}</span></div>



                    <div class="cs-level"><span class="cs-level-key">Conf </span><span class="cs-level-val">${s.confidence}%</span></div>



                </div>



                ${blockersHtml}



            </div>



        `;



    }).join('');



}







function _renderAvoidList(list) {
    const el = document.getElementById('cockpitAvoidList');
    const countBadge = document.getElementById('cockpitAvoidCount');

    // Defensive: Avoid For Now is strictly for real risk blockers (regime, conflict, drift, event).
    // Filter out any internal missing level artifacts from rendering.
    const displayList = (list || []).filter(s => {
        const blockers = s.blockers || [];
        return !blockers.some(b => b === 'missing_or_invalid_levels' || b === 'missing or invalid levels');
    });

    if (countBadge) countBadge.textContent = displayList.length || '';
    if (!el) return;

    if (displayList.length === 0) {
        el.innerHTML = '<span class="cockpit-empty-msg">No high-quality blocked setups right now.</span>';
        return;
    }

    el.innerHTML = displayList.map(s => {



        const blockerChips = (s.blockers || ['gate']).map(b =>



            `<span class="avoid-blocker-chip">${b.replace(/_/g, ' ')}</span>`



        ).join('');



        return `



            <div class="avoid-row" onclick="openSymbolModal('${s.symbol}')" title="QS ${s.quality_score} — blocked: ${(s.blockers||[]).join(', ')}">



                <span class="avoid-sym">${s.symbol}</span>



                <div class="avoid-blockers">${blockerChips}</div>



                <span class="avoid-qs">QS ${s.quality_score}</span>



            </div>



        `;



    }).join('');



}







// ── Phase 3 Render Functions ──────────────────────────────────────────────────







function _renderPortfolioCaution(caution) {



    const el = document.getElementById('cockpitPortfolioCaution');



    if (!el) return;



    const level = caution.caution_level || 'LOW';



    const msg = caution.caution_message || '';



    if (level === 'LOW' && !msg) {



        el.style.display = 'none';



        return;



    }



    el.style.display = '';



    const bgColor = level === 'HIGH' ? 'rgba(239,68,68,0.12)' : 'rgba(245,158,11,0.10)';



    const borderColor = level === 'HIGH' ? '#ef4444' : '#f59e0b';



    const icon = level === 'HIGH' ? '⛔' : '⚠';



    el.style.background = bgColor;



    el.style.borderTop = `2px solid ${borderColor}`;



    el.innerHTML = `



        <span class="p3-caution-icon">${icon}</span>



        <span class="p3-caution-msg">${msg}</span>



        <span class="p3-caution-heat" style="color:${level === 'HIGH' ? '#ef4444' : '#f59e0b'}">Heat ${caution.heat_pct || 0}%</span>



    `;



}







function _renderDeltaPanel(delta) {



    const el = document.getElementById('cockpitDeltaPanel');



    if (!el) return;



    if (!delta) {



        el.innerHTML = '<span class="cockpit-skeleton">Waiting for first refresh cycle…</span>';



        return;



    }







    const hasAny = delta.new_longs.length || delta.new_shorts.length || delta.dropped.length



        || delta.improved.length || delta.degraded.length || delta.mood_shift;







    if (!hasAny) {



        el.innerHTML = '<span class="p3-delta-quiet">No significant changes since last refresh.</span>';



        return;



    }







    const prevTime = delta.prev_captured_at



        ? (() => { try { return new Date(delta.prev_captured_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true }); } catch (_) { return ''; } })()



        : '';







    const rows = [];



    if (delta.new_longs.length)



        rows.push(`<div class="p3-delta-row"><span class="p3-delta-label p3-new-long">New LONG</span><span class="p3-delta-syms">${delta.new_longs.join(', ')}</span></div>`);



    if (delta.new_shorts.length)



        rows.push(`<div class="p3-delta-row"><span class="p3-delta-label p3-new-short">New SHORT</span><span class="p3-delta-syms">${delta.new_shorts.join(', ')}</span></div>`);



    if (delta.dropped.length)



        rows.push(`<div class="p3-delta-row"><span class="p3-delta-label p3-dropped">Dropped</span><span class="p3-delta-syms">${delta.dropped.join(', ')}</span></div>`);



    if (delta.improved.length)



        rows.push(`<div class="p3-delta-row"><span class="p3-delta-label p3-improved">Improved QS</span><span class="p3-delta-syms">${delta.improved.map(x => `${x.symbol} +${x.qs_delta}`).join(', ')}</span></div>`);



    if (delta.degraded.length)



        rows.push(`<div class="p3-delta-row"><span class="p3-delta-label p3-degraded">Degraded QS</span><span class="p3-delta-syms">${delta.degraded.map(x => `${x.symbol} ${x.qs_delta}`).join(', ')}</span></div>`);



    if (delta.mood_shift)



        rows.push(`<div class="p3-delta-row"><span class="p3-delta-label p3-mood-shift">Mood Shift</span><span class="p3-delta-syms">${delta.mood_shift}</span></div>`);







    el.innerHTML = `



        ${prevTime ? `<div class="p3-delta-since">Changes since ${prevTime}</div>` : ''}



        ${rows.join('')}



    `;



}







function _renderSectorRotation(sectors) {



    const el = document.getElementById('cockpitSectorRotation');



    if (!el) return;



    if (!sectors || sectors.length === 0) {



        el.innerHTML = '<span class="cockpit-skeleton">No sector data yet.</span>';



        return;



    }



    el.innerHTML = sectors.map(s => {



        const isUp = s.direction === 'up';



        const color = isUp ? 'var(--green)' : 'var(--red)';



        const arrow = isUp ? '▲' : '▼';



        const pct = Number(s.pct_change);



        return `



            <div class="p3-sector-row">



                <span class="p3-sector-icon">${s.icon || '📊'}</span>



                <span class="p3-sector-name">${s.name}</span>



                <span class="p3-sector-pct" style="color:${color}">${arrow} ${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</span>



            </div>



        `;



    }).join('');



}







function _renderIndexLeadership(leadership) {



    const el = document.getElementById('cockpitIndexLeadership');



    if (!el) return;



    const entries = Object.entries(leadership);



    if (entries.length === 0) {



        el.innerHTML = '<span class="cockpit-skeleton">Index data loading…</span>';



        return;



    }



    el.innerHTML = entries.map(([name, d]) => {



        const pct = Number(d.pct_change || 0);



        const pctColor = pct >= 0 ? 'var(--green)' : 'var(--red)';



        const trendColor = d.trend === 'BULLISH' ? 'var(--green)' : d.trend === 'BEARISH' ? 'var(--red)' : '#94a3b8';



        const shortName = name.replace('NIFTY 50', 'NIFTY').replace('BANKNIFTY', 'BNKN');



        return `



            <div class="p3-index-row">



                <span class="p3-index-name">${shortName}</span>



                <span class="p3-index-close">₹${Number(d.close || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span>



                <span class="p3-index-pct" style="color:${pctColor}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</span>



                <span class="p3-index-regime" style="color:${trendColor}">${d.trend || '—'}</span>



            </div>



        `;



    }).join('');



}







// ── Phase 4: Watchlist ────────────────────────────────────────────────────────







function _saveWatchlist() {



    localStorage.setItem('tradeai_watchlist', JSON.stringify([..._watchlist]));



}







function toggleWatchlist(symbol) {



    if (_watchlist.has(symbol)) {



        _watchlist.delete(symbol);



    } else {



        _watchlist.add(symbol);



    }



    _saveWatchlist();



    _renderWatchlistPanel();



    // Re-render signals so pin icons update



    renderSignals(allSignals);



}







function _renderWatchlistPanel() {



    const el = document.getElementById('cockpitWatchlist');



    if (!el) return;



    const countBadge = document.getElementById('cockpitWatchlistCount');



    const pins = [..._watchlist];



    if (countBadge) countBadge.textContent = pins.length || '';



    if (pins.length === 0) {



        el.innerHTML = '<span class="p4-watchlist-empty">No pinned symbols. Click ★ on any signal card to pin.</span>';



        return;



    }



    // Match pinned symbols against current signals



    const sigMap = {};



    allSignals.forEach(s => { sigMap[s.symbol] = s; });



    el.innerHTML = pins.map(sym => {



        const s = sigMap[sym];



        const sig = s ? String(s.signal || 'HOLD').toUpperCase() : '—';



        const qs = s ? Number(s.quality_score || 0) : null;



        const conf = s ? Number(s.confidence || 0) : null;



        const sigColor = sig === 'LONG' ? 'var(--green)' : sig === 'SHORT' ? 'var(--red)' : '#94a3b8';



        const alertSet = _alertThresholds[sym];



        const alertIcon = alertSet ? '🔔' : '🔕';



        return `



            <div class="p4-pin-row">



                <span class="p4-pin-sym" onclick="openSymbolModal('${sym}')">${sym}</span>



                <span class="p4-pin-sig" style="color:${sigColor}">${sig}</span>



                ${qs !== null ? `<span class="p4-pin-qs">QS ${qs}</span>` : ''}



                ${conf !== null ? `<span class="p4-pin-conf">${conf.toFixed(0)}%</span>` : ''}



                <button class="p4-pin-alert-btn" onclick="openAlertModal('${sym}')" title="Set alert">${alertIcon}</button>



                <button class="p4-pin-remove" onclick="toggleWatchlist('${sym}')" title="Unpin">✕</button>



            </div>



        `;



    }).join('');



}







// ── Phase 4: Alert Thresholds ─────────────────────────────────────────────────







function openAlertModal(symbol) {



    const existing = _alertThresholds[symbol] || {};



    const modal = document.getElementById('p4AlertModal');



    if (!modal) return;



    document.getElementById('p4AlertSym').textContent = symbol;



    document.getElementById('p4AlertQsInput').value = existing.qs || 70;



    document.getElementById('p4AlertConfInput').value = existing.conf || 65;



    modal.dataset.sym = symbol;



    modal.style.display = 'flex';



}







function closeAlertModal() {



    const modal = document.getElementById('p4AlertModal');



    if (modal) modal.style.display = 'none';



}







function saveAlertThreshold() {



    const modal = document.getElementById('p4AlertModal');



    if (!modal) return;



    const sym = modal.dataset.sym;



    const qs = parseInt(document.getElementById('p4AlertQsInput').value) || 70;



    const conf = parseInt(document.getElementById('p4AlertConfInput').value) || 65;



    _alertThresholds[sym] = { qs, conf };



    localStorage.setItem('tradeai_alerts', JSON.stringify(_alertThresholds));



    closeAlertModal();



    _renderWatchlistPanel();



    showToast(`Alert set for ${sym}: QS ≥ ${qs}, Conf ≥ ${conf}%`, 'success');



}







function clearAlertThreshold(symbol) {



    delete _alertThresholds[symbol];



    localStorage.setItem('tradeai_alerts', JSON.stringify(_alertThresholds));



    _renderWatchlistPanel();



}







function _checkAlertThresholds(signals) {



    if (!Object.keys(_alertThresholds).length) return;



    const now = Date.now();



    signals.forEach(s => {



        const sym = s.symbol;



        const threshold = _alertThresholds[sym];



        if (!threshold) return;



        const qs = Number(s.quality_score || 0);



        const conf = Number(s.confidence || 0);



        const sig = String(s.signal || '').toUpperCase();



        if (qs >= threshold.qs && conf >= threshold.conf && (sig === 'LONG' || sig === 'SHORT')) {



            const lastFired = _lastAlertFired[sym] || 0;



            if (now - lastFired > 5 * 60 * 1000) {  // max once per 5 min



                _lastAlertFired[sym] = now;



                const msg = `${sym}: ${sig} — QS ${qs}, Conf ${conf.toFixed(0)}% (threshold met)`;



                showToast(`🔔 Alert: ${msg}`, 'alert');



                if ('Notification' in window && Notification.permission === 'granted') {



                    new Notification('TradeAI Alert', { body: msg, icon: '/favicon.ico' });



                }



            }



        }



    });



}







function requestNotificationPermission() {



    if ('Notification' in window && Notification.permission === 'default') {



        Notification.requestPermission();



    }



}







// ── Phase 4: Toast notification ───────────────────────────────────────────────







function showToast(message, type = 'info') {



    let container = document.getElementById('p4ToastContainer');



    if (!container) {



        container = document.createElement('div');



        container.id = 'p4ToastContainer';



        container.style.cssText = 'position:fixed;bottom:80px;right:1rem;z-index:9999;display:flex;flex-direction:column;gap:0.4rem;pointer-events:none;';



        document.body.appendChild(container);



    }



    const toast = document.createElement('div');



    const bg = type === 'alert' ? 'rgba(239,68,68,0.92)' : type === 'success' ? 'rgba(16,185,129,0.92)' : 'rgba(30,30,46,0.95)';



    toast.style.cssText = `background:${bg};color:#fff;padding:0.55rem 0.9rem;border-radius:8px;font-size:0.78rem;font-weight:600;max-width:280px;box-shadow:0 4px 16px rgba(0,0,0,0.35);pointer-events:auto;animation:fadeInUp 0.2s ease;`;



    toast.textContent = message;



    container.appendChild(toast);



    setTimeout(() => { toast.style.opacity = '0'; toast.style.transition = 'opacity 0.3s'; setTimeout(() => toast.remove(), 350); }, 4000);



}







// ── Phase 4: Morning Scan Panel ───────────────────────────────────────────────







function _renderMorningScan(data) {



    const el = document.getElementById('cockpitMorningScan');



    const timeEl = document.getElementById('cockpitMorningScanTime');



    if (!el) return;







    if (!data || !data.results || data.results.length === 0) {



        const _msStatus = (data && data.status) || 'empty';
        const _msReason = data && data.reason ? String(data.reason).replace(/_/g, ' ') : '';
        const _msLabel = _msStatus === 'disabled'
            ? ('Scan disabled' + (_msReason ? ' — ' + _msReason : '') + '.')
            : (_msStatus === 'empty' ? 'Scan ran but found no setups.' : 'No morning scan data yet. Runs at market open.');
        el.innerHTML = '<span class="cockpit-skeleton">' + _msLabel + '</span>';



        return;



    }







    if (timeEl && data.scan_time) {



        try {



            const t = new Date(data.scan_time);



            timeEl.textContent = t.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true });



        } catch (_) { /* ignore */ }



    }







    el.innerHTML = data.results.slice(0, 6).map(r => {



        const confColor = r.confidence_score >= 75 ? 'var(--green)' : r.confidence_score >= 55 ? '#f59e0b' : '#94a3b8';



        const rsiColor = r.rsi < 30 ? 'var(--green)' : r.rsi > 70 ? 'var(--red)' : '#94a3b8';



        const volBadge = r.volume_ratio >= 1.5



            ? `<span class="p4-ms-vol-badge">Vol ${r.volume_ratio.toFixed(1)}x</span>`



            : '';



        const pinned = _watchlist.has(r.symbol);



        return `



            <div class="p4-ms-row" onclick="openSymbolModal('${r.symbol}')">



                <div class="p4-ms-left">



                    <span class="p4-ms-sym">${r.symbol}</span>



                    ${volBadge}



                    <button class="p4-ms-pin ${pinned ? 'pinned' : ''}" onclick="event.stopPropagation();toggleWatchlist('${r.symbol}')" title="${pinned ? 'Unpin' : 'Pin'}">${pinned ? '★' : '☆'}</button>



                </div>



                <div class="p4-ms-right">



                    <span class="p4-ms-conf" style="color:${confColor}">${r.confidence_score.toFixed(0)}%</span>



                    <span class="p4-ms-rsi" style="color:${rsiColor}">RSI ${r.rsi.toFixed(0)}</span>



                </div>



                <div class="p4-ms-reason">${r.reason || '—'}</div>



            </div>



        `;



    }).join('');



}







// ── Phase 4: Opportunity Timeline ────────────────────────────────────────────







function _renderOpportunityTimeline(topSetups) {



    const el = document.getElementById('cockpitOppTimeline');



    if (!el) return;



    const setups = topSetups || [];



    if (setups.length === 0) {



        el.innerHTML = '<span class="cockpit-skeleton">No active setups.</span>';



        return;



    }



    const now = Date.now();



    el.innerHTML = setups.map(s => {



        const isShort = s.signal === 'SHORT';



        const sigColor = isShort ? 'var(--red)' : 'var(--green)';



        let ageLabel = '—';



        if (s.first_seen) {



            try {



                const diff = Math.floor((now - new Date(s.first_seen).getTime()) / 60000);



                ageLabel = diff < 1 ? 'Just now' : diff < 60 ? `${diff}m` : `${Math.floor(diff / 60)}h ${diff % 60}m`;



            } catch (_) { /* ignore */ }



        }



        const pinned = _watchlist.has(s.symbol);



        return `



            <div class="p4-tl-row">



                <div class="p4-tl-dot" style="background:${sigColor}"></div>



                <div class="p4-tl-body">



                    <div class="p4-tl-header">



                        <span class="p4-tl-sym" onclick="openSymbolModal('${s.symbol}')">${s.symbol}</span>



                        <span class="p4-tl-sig" style="color:${sigColor}">${s.signal}</span>



                        <span class="p4-tl-age">${ageLabel}</span>



                        <button class="p4-ms-pin ${pinned ? 'pinned' : ''}" onclick="toggleWatchlist('${s.symbol}')" title="${pinned ? 'Unpin' : 'Pin'}">${pinned ? '★' : '☆'}</button>



                    </div>



                    <div class="p4-tl-meta">QS ${s.quality_score} · Rank ${s.composite_rank} · ${s.thesis}</div>



                </div>



            </div>



        `;



    }).join('');



}







// ── Phase 5: Feed banner, export, notes, shortcuts ───────────────────────────







function _renderFeedStalenessBanner(grd) {



    const el = document.getElementById('cockpitFeedBanner');



    if (!el) return;



    const feed = grd && grd.feed_status ? grd.feed_status : {};



    const state = feed.state || 'UNKNOWN';



    // Quote freshness is its own dimension: `state` tracks the last autonomous
    // cycle snapshot, while marks are fetched per request. Using the snapshot
    // age here produced a false "live quotes may be unreliable" alarm.
    const quoteState = feed.quote_state || state;



    const age = feed.age_seconds != null ? Number(feed.age_seconds) : null;



    const marketOpen = typeof _isNseMarketOpen === 'function' && _isNseMarketOpen();







    let show = false;



    let level = 'warn';



    let msg = '';







    if (marketOpen) {



        if (quoteState === 'EOD') { show = false; } else if (quoteState !== 'LIVE' && quoteState !== 'DELAYED') {



            show = true;



            level = 'danger';



            msg = `Feed status: ${quoteState}. Live quotes may be unreliable during market hours.`;



        } else if (state === 'DELAYED') {



            show = true;



            level = 'warn';



            msg = 'Feed is delayed — numbers may lag the exchange.';



        } else if (feed.worker_health === 'WORKER_STALE' || state === 'STALE') {

            show = true;

            level = 'warn';

            msg = 'Live quotes are fresh, but the autonomous worker has not run — no new scans, entries or stop updates.';

        } else if (age != null && age > 120) {



            show = true;



            level = 'warn';



            msg = `Last tick ${age}s ago — check connectivity.`;



        }



    } else if (state === 'OFFLINE' || state === 'DOWN' || state === 'ERROR' || state === 'STALE') {



        show = true;



        level = 'warn';



        msg = `Feed health: ${state} (market closed — advisory only).`;



    }







    if (!show) {



        el.style.display = 'none';



        el.innerHTML = '';



        el.className = 'p5-feed-banner';



        return;



    }



    el.style.display = 'flex';



    el.className = `p5-feed-banner ${level === 'danger' ? 'p5-feed-danger' : 'p5-feed-warn'}`;



    const icon = level === 'danger' ? '⛔' : '⚠';



    el.innerHTML = `<span>${icon}</span><span>${msg}</span>`;



}







// ── Phase 6: Crypto Mini-Panel (compact Overview tile) ─────────────────────







function _renderCryptoMiniPanel(data) {



    const el = document.getElementById('cockpitCryptoMini');



    if (!el) return;



    if (!data) { el.style.display = 'none'; return; }







    const grd = data.market_guardrails || {};



    const mood = String(grd.mood || 'NEUTRAL');



    const moodScore = Number(grd.mood_score || 50);



    const btcPct = Number(grd.btc_24h_pct || 0);



    const ethPct = Number(grd.eth_24h_pct || 0);



    const fg = grd.fear_greed;



    const setups = data.top_setups || [];



    const meta = data.ranking_meta || {};







    const moodCls = mood.includes('BULL') ? '#10b981' : mood.includes('BEAR') ? '#ef4444' : '#22d3ee';



    const btcSign = btcPct >= 0 ? '+' : '';



    const ethSign = ethPct >= 0 ? '+' : '';



    const fgLabel = fg != null ? (fg <= 25 ? 'Extreme Fear' : fg <= 45 ? 'Fear' : fg >= 75 ? 'Extreme Greed' : fg >= 55 ? 'Greed' : `Neutral (${fg})`) : '—';







    const topSetup = setups[0];



    let topSetupHtml = '<span style="color:var(--text3);font-size:0.74rem">No actionable setup yet</span>';



    if (topSetup) {



        const sym = String(topSetup.symbol || '').replace('/USDT', '');



        const sig = String(topSetup.signal || 'HOLD').toUpperCase();



        const sigColor = sig === 'LONG' ? '#10b981' : sig === 'SHORT' ? '#ef4444' : '#94a3b8';



        const cr = Number(topSetup.composite_rank || 0).toFixed(0);



        const thesis = String(topSetup.thesis || '').slice(0, 55);



        topSetupHtml = `<div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.25rem">



            <span style="font-weight:700;font-size:0.85rem">${sym}</span>



            <span style="font-size: 0.70rem;font-weight:800;color:${sigColor};border:1px solid ${sigColor}33;padding:1px 6px;border-radius:4px">${sig}</span>



            <span style="font-family:monospace;font-size: 0.72rem;color:var(--text3)">rank ${cr}</span>



        </div>



        <div style="font-size:0.72rem;color:var(--text2)">${thesis}${thesis.length >= 55 ? '…' : ''}</div>`;



    }







    const secondSetup = setups[1];



    let secondHtml = '';



    if (secondSetup) {



        const sym2 = String(secondSetup.symbol || '').replace('/USDT', '');



        const sig2 = String(secondSetup.signal || 'HOLD').toUpperCase();



        const sigColor2 = sig2 === 'LONG' ? '#10b981' : sig2 === 'SHORT' ? '#ef4444' : '#94a3b8';



        secondHtml = `<div style="display:flex;align-items:center;gap:0.5rem;margin-top:0.4rem">



            <span style="font-weight:700;font-size:0.82rem;color:var(--text2)">${sym2}</span>



            <span style="font-size:0.6rem;font-weight:700;color:${sigColor2}">${sig2}</span>



        </div>`;



    }







    el.style.display = '';



    el.innerHTML = `



        <div class="cockpit-tile-header" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.6rem">



            <span style="font-size: 0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:var(--text2)">₿ Crypto Market</span>



            <a href="/crypto" style="font-size: 0.72rem;color:var(--accent);text-decoration:none;border:1px solid rgba(99,102,241,0.3);padding:2px 8px;border-radius:4px;transition:all 0.15s" onmouseover="this.style.background='rgba(99,102,241,0.1)'" onmouseout="this.style.background=''">Full cockpit →</a>



        </div>



        <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:0.55rem">



            <div>



                <div style="font-size: 0.70rem;color:var(--text3);text-transform:uppercase;margin-bottom:2px">Mood</div>



                <div style="font-size:0.82rem;font-weight:700;color:${moodCls}">${mood}</div>



            </div>



            <div>



                <div style="font-size: 0.70rem;color:var(--text3);text-transform:uppercase;margin-bottom:2px">BTC 24h</div>



                <div style="font-size:0.82rem;font-weight:700;color:${btcPct>=0?'var(--green)':'var(--red)'}">${btcSign}${btcPct.toFixed(2)}%</div>



            </div>



            <div>



                <div style="font-size: 0.70rem;color:var(--text3);text-transform:uppercase;margin-bottom:2px">ETH 24h</div>



                <div style="font-size:0.82rem;font-weight:700;color:${ethPct>=0?'var(--green)':'var(--red)'}">${ethSign}${ethPct.toFixed(2)}%</div>



            </div>



            <div>



                <div style="font-size: 0.70rem;color:var(--text3);text-transform:uppercase;margin-bottom:2px">F&G</div>



                <div style="font-size:0.8rem;font-weight:600;color:var(--text2)">${fgLabel}</div>



            </div>



            <div>



                <div style="font-size: 0.70rem;color:var(--text3);text-transform:uppercase;margin-bottom:2px">Setups</div>



                <div style="font-size:0.82rem;font-weight:700;color:var(--text)">${meta.actionable || 0}</div>



            </div>



        </div>



        <div style="border-top:1px solid rgba(255,255,255,0.06);padding-top:0.5rem">



            ${topSetupHtml}${secondHtml}



        </div>



    `;



}







function initPhase5Cockpit() {



    const jsonBtn = document.getElementById('p5ExportJsonBtn');



    const csvBtn = document.getElementById('p5ExportCsvBtn');



    const notesBtn = document.getElementById('p5ToggleNotesBtn');



    const notesWrap = document.getElementById('p5TraderNotesWrap');



    const notesTa = document.getElementById('p5TraderNotes');



    if (jsonBtn) jsonBtn.addEventListener('click', exportDecisionPackJson);



    if (csvBtn) csvBtn.addEventListener('click', exportDecisionPackCsv);



    if (notesTa) {



        notesTa.value = localStorage.getItem('tradeai_trader_notes') || '';



        let debounceT;



        notesTa.addEventListener('input', () => {



            clearTimeout(debounceT);



            debounceT = setTimeout(() => localStorage.setItem('tradeai_trader_notes', notesTa.value), 400);



        });



    }



    if (notesBtn && notesWrap) {



        notesBtn.addEventListener('click', () => {



            const open = notesWrap.style.display === 'none' || notesWrap.style.display === '';



            notesWrap.style.display = open ? 'block' : 'none';



            notesBtn.setAttribute('aria-expanded', String(open));



            notesBtn.textContent = open ? 'Trader notes ▴' : 'Trader notes ▾';



        });



    }



    document.addEventListener('keydown', phase5Keydown);



    const p5m = document.getElementById('p5ShortcutsModal');



    if (p5m) p5m.addEventListener('click', e => { if (e.target === p5m) closeP5Shortcuts(); });



}







function phase5Keydown(e) {



    const tag = e.target && e.target.tagName;



    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;



    if (e.target && e.target.isContentEditable) return;







    if (e.key === 'Escape') {



        const m = document.getElementById('p5ShortcutsModal');



        if (m && m.style.display === 'flex') {



            closeP5Shortcuts();



            e.preventDefault();



        }



        return;



    }



    if (e.key === '?' || (e.shiftKey && e.key === '/')) {



        e.preventDefault();



        openP5Shortcuts();



        return;



    }



    if (e.key === 'r' || e.key === 'R') {



        if (e.ctrlKey || e.metaKey || e.altKey) return;



        e.preventDefault();



        loadAllData({ background: false });



    }



}







function openP5Shortcuts() {



    const m = document.getElementById('p5ShortcutsModal');



    if (m) m.style.display = 'flex';



}







function closeP5Shortcuts() {



    const m = document.getElementById('p5ShortcutsModal');



    if (m) m.style.display = 'none';



}







function exportDecisionPackJson() {
    const hasFilter = Array.isArray(window._lastFilteredSignals);
    const activeList = hasFilter
        ? window._lastFilteredSignals
        : ((_lastCockpitData && _lastCockpitData.top_setups) ? _lastCockpitData.top_setups : []);

    if (!activeList.length) {
        showToast(hasFilter ? 'No signals match the active filter criteria to export.' : 'No signals or cockpit data to export yet.', 'info');
        return;
    }
    const payload = {
        exported_at: new Date().toISOString(),
        source: 'TradeAI PRO Active Signal Table',
        count: activeList.length,
        signals: activeList,
        overview: (_lastCockpitData && _lastCockpitData.overview) ? _lastCockpitData.overview : null,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `tradeai-signals-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    showToast(`Exported ${activeList.length} signals to JSON`, 'success');
}

function sanitizeCsvCell(v) {
    if (v == null) return '';
    if (typeof v === 'number' && Number.isFinite(v)) {
        return String(v);
    }
    let s = String(v).trim();
    if (!isNaN(Number(s)) && !isNaN(parseFloat(s))) {
        return s;
    }
    if (/^[=+\-@\t\r]/.test(s)) {
        s = "'" + s;
    }
    if (/[",\n\r]/.test(s)) {
        return `"${s.replace(/"/g, '""')}"`;
    }
    return s;
}

function exportDecisionPackCsv() {
    const hasFilter = Array.isArray(window._lastFilteredSignals);
    const activeList = hasFilter
        ? window._lastFilteredSignals
        : ((_lastCockpitData && _lastCockpitData.top_setups) ? _lastCockpitData.top_setups : []);

    if (!activeList.length) {
        showToast(hasFilter ? 'No signals match the active filter criteria to export.' : 'No signals or cockpit data to export yet.', 'info');
        return;
    }

    const headers = ['symbol', 'signal', 'quality_score', 'confidence', 'composite_rank', 'confluence_score', 'thesis'];
    const rows = [headers.join(',')];

    activeList.forEach(s => {
        rows.push([
            sanitizeCsvCell(s.symbol),
            sanitizeCsvCell(s.signal || s.direction),
            sanitizeCsvCell(s.quality_score),
            sanitizeCsvCell(s.confidence),
            sanitizeCsvCell(s.composite_rank || s.rank),
            sanitizeCsvCell(s.confluence_score != null ? s.confluence_score : (s.score != null ? s.score : '')),
            sanitizeCsvCell(s.thesis || s.why_ranked || s.rationale || ''),
        ].join(','));
    });

    const blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');



    a.href = URL.createObjectURL(blob);



    a.download = `tradeai-top-setups-${new Date().toISOString().slice(0, 10)}.csv`;



    a.click();



    URL.revokeObjectURL(a.href);



    showToast('Exported top setups (CSV)', 'success');



}







// ── Risk-aware cockpit filter strip ─────────────────────────────────────────







function initCockpitFilters() {



    document.querySelectorAll('.cockpit-filter-btn').forEach(btn => {



        btn.addEventListener('click', () => {



            _cockpitFilter = btn.dataset.cf || 'all';



            document.querySelectorAll('.cockpit-filter-btn').forEach(b => b.classList.remove('active'));



            btn.classList.add('active');



            applyCockpitFilter();



        });



    });







    // Phase 4: alert modal close on backdrop click



    const alertModal = document.getElementById('p4AlertModal');



    if (alertModal) {



        alertModal.addEventListener('click', e => { if (e.target === alertModal) closeAlertModal(); });



    }







    // Phase 4: request notification permission on first interaction



    requestNotificationPermission();



}







function applyCockpitFilter() {



    if (!allSignals.length) return;







    let filtered = allSignals.slice();



    const market_trend = _lastCockpitData?.market_guardrails?.breadth_split?.signal_bias?.label || 'NEUTRAL';







    switch (_cockpitFilter) {



        case 'actionable':



            filtered = filtered.filter(s => {



                const sig = String(s.signal || '').toUpperCase();



                return (sig === 'LONG' || sig === 'SHORT') && !s.stale;



            });



            break;



        case 'high_conv':



            filtered = filtered.filter(s => Number(s.quality_score || 0) >= 70 && Number(s.confidence || 0) >= 65);



            break;



        case 'trend_aligned':



            filtered = filtered.filter(s => {



                const sig = String(s.signal || '').toUpperCase();



                const trend = String(s.trend || '').toUpperCase();



                if (market_trend === 'BULLISH') return sig === 'LONG' && trend.includes('BULL');



                if (market_trend === 'BEARISH') return sig === 'SHORT' && trend.includes('BEAR');



                return sig === 'LONG' || sig === 'SHORT';



            });



            break;



        case 'no_drift':



            filtered = filtered.filter(s => !s.drift_detected);



            break;



        case 'event_safe':



            filtered = filtered.filter(s => !s.event_risk_active && Number(s.days_to_next_event || 99) > 3);



            break;



        case 'low_risk':



            filtered = filtered.filter(s =>



                !s.drift_detected



                && !s.event_risk_active



                && Number(s.quality_score || 0) >= 60



                && Number(s.confidence || 0) >= 50



            );



            break;



        default:



            filtered = _showSkipSignals ? filtered : filtered.filter(s => String(s.signal || '').toUpperCase() !== 'SKIP');



            break;



    }







    // Apply existing type filter and sort on top of cockpit filter



    filtered = filtered.filter(s => {



        const sig = String(s.signal || '').toUpperCase();



        const conf = Number(s.confidence || 0);



        if (_signalTypeFilter === 'buy') return sig === 'LONG' || sig === 'BUY';



        if (_signalTypeFilter === 'sell') return sig === 'SHORT' || sig === 'SELL';



        if (_signalTypeFilter === 'high_conf') return conf > 75;



        return true;



    });







    const sorters = {



        qs_desc: (a, b) => Number(b.quality_score || 0) - Number(a.quality_score || 0),



        conf_desc: (a, b) => Number(b.confidence || 0) - Number(a.confidence || 0),



        signal_type: (a, b) => {



            const rank = { LONG: 0, BUY: 0, SHORT: 1, SELL: 1, SKIP: 2, HOLD: 3 };



            return (rank[String(a.signal || '').toUpperCase()] ?? 9) - (rank[String(b.signal || '').toUpperCase()] ?? 9);



        },



    };



    filtered.sort(sorters[_signalSortMode] || sorters.qs_desc);







    _renderFilteredCards(filtered);







    const noteEl = document.getElementById('signalsFilterNote');



    if (noteEl) {



        const filterLabel = { all: 'All', actionable: 'Actionable only', high_conv: 'High Conviction', trend_aligned: 'Trend Aligned', no_drift: 'No Drift', event_safe: 'Event Safe', low_risk: 'Low Risk' };



        noteEl.textContent = `Showing ${filtered.length}/${allSignals.length} · Filter: ${filterLabel[_cockpitFilter] || _cockpitFilter}`;



    }



}







// Thin wrapper — renders a pre-filtered array using the existing renderSignals card builder



// without the full signal fetch cycle. We temporarily swap allSignals, render, restore.



function _renderFilteredCards(signals) {



    const grid = document.getElementById('signalsGrid');



    if (!grid) return;



    grid.querySelectorAll('.signal-card, .error-state').forEach(el => el.remove());



    const saved = allSignals;



    allSignals = signals;



    // Call renderSignals with a no-filter mode



    const savedTypeFilter = _signalTypeFilter;



    const savedSkip = _showSkipSignals;



    _signalTypeFilter = 'all';



    _showSkipSignals = true;



    renderSignals(signals);



    _signalTypeFilter = savedTypeFilter;



    _showSkipSignals = savedSkip;



    allSignals = saved;



}











function handleLoadError() {



    const grid = document.getElementById('signalsGrid');



    const loader = document.getElementById('loadingState');



    if (loader) loader.classList.add('hidden');







    if (!grid.querySelector('.signal-card')) {



        grid.innerHTML = `



            <div class="error-state">



                <p>⚠️ Could not connect to server. Make sure the backend is running.</p>



                <p style="margin-top: 8px; color: var(--text-muted); font-size: 0.8rem;">



                    Run: <code>python -m uvicorn server:app --reload --port 8000</code>



                </p>



            </div>



        `;



    }



}











// ── Manual Paper Trade Execution ─────────────────────────



async function takePaperTrade(symbol, direction, entry, stopLoss, target, atr, strategy, btnEl, extraMeta = {}) {



    const subKey = `${symbol}_${direction}`;



    if (_activeTradeSubmissions.has(subKey)) {



        console.warn(`Trade submission already in progress for ${subKey}`);



        return;



    }







    const numEntry = Number(entry || 0);



    const numSl = Number(stopLoss || 0);



    const numTp = Number(target || 0);



    const cleanDir = String(direction || 'LONG').trim().toUpperCase();







    // Client-side geometry validation before network dispatch



    const _formatPriceDebug = (p) => {
        if (!p && p !== 0) return '0.00';
        const abs = Math.abs(p);
        if (abs < 0.001) return Number(p).toFixed(8);
        if (abs < 0.1) return Number(p).toFixed(6);
        if (abs < 1.0) return Number(p).toFixed(4);
        if (abs < 50.0) return Number(p).toFixed(3);
        return Number(p).toFixed(2);
    };

    if (cleanDir === 'LONG' || cleanDir === 'BUY') {
        if (!(numSl < numEntry && numEntry < numTp)) {
            showToast(`⚠️ Invalid geometry for LONG: Stop Loss (${_formatPriceDebug(numSl)}) < Entry (${_formatPriceDebug(numEntry)}) < Target (${_formatPriceDebug(numTp)}) required`, 'warn');
            return;
        }
    } else if (cleanDir === 'SHORT' || cleanDir === 'SELL') {
        if (!(numTp < numEntry && numEntry < numSl)) {
            showToast(`⚠️ Invalid geometry for SHORT: Target (${_formatPriceDebug(numTp)}) < Entry (${_formatPriceDebug(numEntry)}) < Stop Loss (${_formatPriceDebug(numSl)}) required`, 'warn');
            return;
        }
    }

    _activeTradeSubmissions.add(subKey);



    if (btnEl) {



        btnEl.disabled = true;



        btnEl.innerHTML = `<span class="spin">⏳</span> Executing...`;



    }



    try {



        const res = await fetchWithTimeout(`${API}/api/paper/trade`, {



            method: 'POST',



            headers: _getAuthHeaders(),



            body: JSON.stringify({



                symbol: String(symbol || '').trim(),



                direction: cleanDir,



                entry: numEntry,



                stop_loss: numSl,



                target: numTp,



                atr: Number(atr || 0),



                strategy: strategy || 'MANUAL',



                confidence: Number(extraMeta.confidence || 0),



                regime: String(extraMeta.regime || 'TRENDING'),



                rsi: Number(extraMeta.rsi || 0),



                volume_above_avg: Boolean(extraMeta.volume_above_avg),



            }),



        }, 10000);







        if (res.status === 401 || res.status === 403) {



            _handleAuthChallenge(res.status);



            if (btnEl) {



                btnEl.disabled = false;



                btnEl.innerHTML = `⚡ Paper Trade`;



            }



            return;



        }



        let data = {};



        try {



            data = await res.json();



        } catch (_) {



            data = {};



        }



        if (!res.ok || data.error) {



            const reason = data.error || (data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : `HTTP ${res.status}`);



            showToast(`⚠️ Trade blocked: ${reason}`, 'warn');



            if (btnEl) {



                btnEl.disabled = false;



                btnEl.innerHTML = `⚠️ Blocked`;



                setTimeout(() => { btnEl.innerHTML = `⚡ Paper Trade`; }, 3000);



            }



            return;



        }



        showToast(`✅ Paper Trade Placed: ${direction} ${symbol} @ ₹${Number(entry).toFixed(1)}`, 'success');



        if (btnEl) {



            btnEl.style.background = 'rgba(16,185,129,0.25)';



            btnEl.style.borderColor = 'var(--green)';



            btnEl.innerHTML = `✓ Active Trade #${data.trade ? data.trade.id : ''}`;



        }



        if (data.stats) {



            updateStatsStrip(data.stats);



        }



        refreshPaperPositions();
        if (typeof loadPaperScorecard === 'function') {



            loadPaperScorecard();



        }



    } catch (err) {



        console.error('takePaperTrade error:', err);



        showToast(`❌ Failed to place trade: ${err.message}`, 'error');



        if (btnEl) {



            btnEl.disabled = false;



            btnEl.innerHTML = `⚡ Paper Trade`;



        }



    } finally {



        _activeTradeSubmissions.delete(subKey);



    }



}







// ── Stats Strip ─────────────────────────────────────────







function renderOverviewActiveTrades(openTrades, summary) {
    _lastOpenTrades = Array.isArray(openTrades) ? openTrades : [];
    const card = document.getElementById('overviewActiveTradesCard');
    const badge = document.getElementById('overviewOpenCountBadge');
    const list = document.getElementById('overviewOpenTradesList');
    if (!card || !list) return;

    if (!Array.isArray(openTrades) || openTrades.length === 0) {
        card.style.display = 'none';
        return;
    }

    card.style.display = 'block';
    const missingCount = (summary && summary.missing_quotes_count) || openTrades.filter(t => t.is_entry_fallback).length;
    if (badge) {
        if (missingCount > 0) {
            badge.innerHTML = `${openTrades.length} Open <span style="color:#f59e0b;font-weight:700;">(⚠️ ${missingCount} Unquoted)</span>`;
            badge.title = `${missingCount} trade(s) using entry reference price due to unquoted live feed`;
        } else {
            badge.textContent = `${openTrades.length} Open`;
            badge.title = 'All open positions valued with live market quotes';
        }
    }

    list.innerHTML = openTrades.map(t => {
        const isLong = t.direction === 'LONG' || t.direction === 'BUY';
        const sideClass = isLong ? 'pos-ticket-long' : 'pos-ticket-short';
        const entry = Number(t.entry_price || 0);
        const sl = Number(t.stop_loss || 0);
        const tp = Number(t.target || 0);
        const qty = Number(t.quantity || 1);
        const isFallback = Boolean(t.is_entry_fallback);

        // Resolve current price from ticket or cached marketData
        const cachedTick = marketDataCache[t.symbol];
        const cachedPrice = typeof cachedTick === 'object' ? (cachedTick.close ?? cachedTick.price ?? cachedTick.ltp) : cachedTick;
        const ltp = Number(t.current_price ?? cachedPrice ?? entry);

        // Calculate unrealized MTM
        const mult = isLong ? 1 : -1;
        const pnl = (ltp - entry) * qty * mult;
        const pnlPct = entry > 0 ? ((ltp - entry) / entry) * 100 * mult : 0;
        const isProfit = pnl > 0.01;
        const isLoss = pnl < -0.01;
        const pnlClass = isProfit ? 'pnl-profit' : (isLoss ? 'pnl-loss' : 'pnl-neutral');
        const pnlSign = pnl < -0.005 ? '-' : (pnl > 0.005 ? '+' : '');
        const pnlPctSign = pnlPct >= 0 ? '+' : '';

        // Distance to target / SL
        let distText = '';
        if (tp > 0 && ltp > 0) {
            const distTp = isLong ? ((tp - ltp) / ltp) * 100 : ((ltp - tp) / ltp) * 100;
            distText = distTp > 0 ? `Target ${distTp.toFixed(1)}% away` : 'Target reached';
        }

        const currSym = _symCurr(t.symbol);
        const isEodQuote = t.quote_status === 'LAST_SESSION_EOD';
        const isStale = Boolean(t.is_stale_quote || t.zombie);
        const fallbackBadge = isFallback
            ? `<span class="pos-fallback-badge" style="font-size: 0.72rem;font-weight:700;color:#f59e0b;background:rgba(245,158,11,0.15);padding:1px 5px;border-radius:4px;border:1px solid rgba(245,158,11,0.3);margin-left:6px;" title="Real-time quote unavailable. Entry price used as reference.">⚠️ Unquoted Ref</span>`
            : (isEodQuote
                ? `<span class="pos-fallback-badge" style="font-size: 0.72rem;font-weight:700;color:#38bdf8;background:rgba(56,189,248,0.15);padding:1px 5px;border-radius:4px;border:1px solid rgba(56,189,248,0.3);margin-left:6px;" title="Previous session EOD settlement quote">🌙 EOD</span>`
                : '');
        const zombieBadge = t.zombie
            ? `<span class="pos-fallback-badge" style="font-size: 0.72rem;font-weight:700;color:#f87171;background:rgba(239,68,68,0.12);padding:1px 5px;border-radius:4px;border:1px solid rgba(239,68,68,0.35);margin-left:6px;" title="Missed scheduled exit: ${(t.zombie_reason || '').replace(/_/g, ' ')}">STALE</span>`
            : (t.is_stale_quote && !isFallback && !isEodQuote
                ? `<span class="pos-fallback-badge" style="font-size: 0.72rem;font-weight:700;color:#f87171;background:rgba(239,68,68,0.12);padding:1px 5px;border-radius:4px;border:1px solid rgba(239,68,68,0.35);margin-left:6px;" title="Stale quote: feed delayed or unresponsive">STALE</span>`
                : '');
        const statusPulse = isFallback
            ? `<span class="pos-status-pulse" style="color:#f59e0b;">⚠️ UNQUOTED REF</span>`
            : (isStale
                ? `<span class="pos-status-pulse" style="color:#f87171;">⚠️ STALE</span>`
                : (isEodQuote
                    ? `<span class="pos-status-pulse" style="color:#38bdf8;">🌙 LAST SESSION EOD</span>`
                    : `<span class="pos-status-pulse">● RUNNING</span>`));

        return `
            <div class="pos-ticket ${sideClass}" data-symbol="${t.symbol}" data-id="${t.id}" data-entry="${entry}" data-qty="${qty}" data-dir="${t.direction}" data-tp="${tp}" data-sl="${sl}" onclick="openPositionDetail(${t.id})" style="cursor:pointer;" title="Click to view full position details">
                <div class="pos-ticket-top">
                    <div class="pos-symbol-group">
                        <span class="pos-dir-pill ${isLong ? 'dir-long' : 'dir-short'}">${t.direction}</span>
                        <span class="pos-symbol-name">${t.symbol}</span>
                        ${fallbackBadge}${zombieBadge}
                    </div>
                    <button class="pos-exit-btn" onclick="event.stopPropagation(); closePaperTrade(${t.id}, ${ltp})" type="button">
                        Exit
                    </button>
                </div>
                <div class="pos-metrics-grid">
                    <div class="pos-metric-cell">
                        <span class="pos-metric-label">ENTRY</span>
                        <span class="pos-metric-number">${currSym}${entry.toFixed(2)}</span>
                    </div>
                    <div class="pos-metric-cell">
                        <span class="pos-metric-label">SL</span>
                        <span class="pos-metric-number text-loss">${currSym}${sl.toFixed(2)}</span>
                    </div>
                    <div class="pos-metric-cell">
                        <span class="pos-metric-label">TARGET</span>
                        <span class="pos-metric-number text-profit">${currSym}${tp.toFixed(2)}</span>
                    </div>
                    <div class="pos-metric-cell">
                        <span class="pos-metric-label">QTY</span>
                        <span class="pos-metric-number">${qty.toFixed(1)}</span>
                    </div>
                </div>
                <div class="pos-live-bar">
                    <div class="pos-live-left">
                        <span class="pos-live-lbl">${isEodQuote ? 'EOD' : (isFallback ? 'REF' : 'LTP')}</span>
                        <span class="pos-live-price" id="posLtp-${t.id}">${currSym}${ltp.toFixed(2)}${isFallback ? ' <small style="color:#f59e0b;">(Ref)</small>' : ''}</span>
                    </div>
                    <div class="pos-pnl-pill ${pnlClass}" id="posPnlPill-${t.id}">
                        <span id="posPnlVal-${t.id}">${pnlSign}${currSym}${Math.abs(pnl).toFixed(2)}</span>
                        <span class="pos-pnl-pct" id="posPnlPct-${t.id}">(${pnlPctSign}${pnlPct.toFixed(2)}%)</span>
                    </div>
                </div>
                <div class="pos-ticket-footer">
                    <span class="pos-dist-hint" id="posDist-${t.id}">${distText}</span>
                    ${statusPulse}
                </div>
            </div>
        `;
    }).join('');
}

function updateOverviewActiveTradesPrices(prices) {
    if (!prices || typeof prices !== 'object') return;
    let totalUnrealized = 0;
    let hasOpen = false;

    document.querySelectorAll('.pos-ticket').forEach(card => {
        hasOpen = true;
        const sym = card.getAttribute('data-symbol');
        const id = card.getAttribute('data-id');
        const entry = parseFloat(card.getAttribute('data-entry') || 0);
        const qty = parseFloat(card.getAttribute('data-qty') || 1);
        const isLong = card.getAttribute('data-dir') === 'LONG' || card.getAttribute('data-dir') === 'BUY';
        const tp = parseFloat(card.getAttribute('data-tp') || 0);

        const tick = prices[sym];
        if (!tick) return;
        const price = typeof tick === 'object' ? (tick.close ?? tick.price ?? tick.ltp) : tick;
        if (price == null || isNaN(price) || price <= 0) return;

        const mult = isLong ? 1 : -1;
        const pnl = (price - entry) * qty * mult;
        const pnlPct = entry > 0 ? ((price - entry) / entry) * 100 * mult : 0;
        totalUnrealized += pnl;

        const currSym = _symCurr(sym);
        const ltpEl = document.getElementById(`posLtp-${id}`);
        if (ltpEl) ltpEl.textContent = `${currSym}${price.toFixed(2)}`;

        const pillEl = document.getElementById(`posPnlPill-${id}`);
        const valEl = document.getElementById(`posPnlVal-${id}`);
        const pctEl = document.getElementById(`posPnlPct-${id}`);

        if (pillEl && valEl && pctEl) {
            const isProfit = pnl > 0.01;
            const isLoss = pnl < -0.01;
            const pnlSign = pnl < 0 ? '-' : (pnl > 0 ? '+' : '');
            pillEl.className = `pos-pnl-pill ${isProfit ? 'pnl-profit' : (isLoss ? 'pnl-loss' : 'pnl-neutral')}`;
            valEl.textContent = `${pnlSign}${currSym}${Math.abs(pnl).toFixed(2)}`;
            pctEl.textContent = `(${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)`;
        }

        const distEl = document.getElementById(`posDist-${id}`);
        if (distEl && tp > 0) {
            const distTp = isLong ? ((tp - price) / price) * 100 : ((price - tp) / price) * 100;
            distEl.textContent = distTp > 0 ? `Target ${distTp.toFixed(1)}% away` : 'Target reached';
        }
    });

    if (hasOpen) {
        const uEl = document.getElementById('statUnrealizedPnl');
        if (uEl && _unpricedPositions > 0) {
            _applyUnpricedMtm();
        } else if (uEl) {
            uEl.textContent = `MTM: ${totalUnrealized >= 0 ? '+' : ''}₹${Number(totalUnrealized).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
            uEl.style.color = totalUnrealized > 0 ? 'var(--green, #10b981)' : (totalUnrealized < 0 ? 'var(--red, #ef4444)' : 'var(--text-muted, #8b949e)');
        }
    }
}







// ── Open Position Detail Modal ───────────────────────────────
async function openPositionDetail(tradeId) {
    if (!tradeId) return;

    let trade = null;
    // 1. Try local caches first for instant render
    if (Array.isArray(window._lastOpenTrades)) {
        trade = window._lastOpenTrades.find(t => Number(t.id) === Number(tradeId));
    }
    if (!trade && Array.isArray(window._cachedOpenPositions)) {
        trade = window._cachedOpenPositions.find(t => Number(t.id) === Number(tradeId));
    }
    if (!trade && Array.isArray(window._cachedTradeHistory)) {
        trade = window._cachedTradeHistory.find(t => Number(t.id) === Number(tradeId));
    }

    // 2. Fetch fresh open positions if missing
    if (!trade) {
        try {
            const res = await fetchWithTimeout(`${API}/api/paper/open`, {}, 10000);
            const data = await parseJsonResponse(res);
            const openTrades = data.open_trades || [];
            trade = openTrades.find(t => Number(t.id) === Number(tradeId));
            if (openTrades.length) window._lastOpenTrades = openTrades;
        } catch (e) {
            console.warn('Failed to fetch open trades for detail:', e);
        }
    }

    // 3. Fallback to trade history if not open
    if (!trade) {
        try {
            const res = await fetchWithTimeout(`${API}/api/trades?limit=50`, {}, 10000);
            const data = await parseJsonResponse(res);
            const histTrades = data.trades || [];
            trade = histTrades.find(t => Number(t.id) === Number(tradeId));
            if (histTrades.length) window._cachedTradeHistory = histTrades;
        } catch (e) {
            console.warn('Failed to fetch trade history for detail:', e);
        }
    }

    if (!trade) {
        if (typeof showToast === 'function') {
            showToast('Trade #' + tradeId + ' details not found.', 'warning');
        } else {
            alert('Trade #' + tradeId + ' details not found.');
        }
        return;
    }

    const modalBackdrop = document.getElementById('positionDetailModalBackdrop');
    if (!modalBackdrop) return;

    const sym = trade.symbol || '';
    const currSym = _symCurr(sym);
    const isLong = trade.direction === 'LONG' || trade.direction === 'BUY';
    const sideClass = isLong ? 'dir-long' : 'dir-short';
    const sideText = isLong ? 'LONG' : 'SHORT';
    const entry = Number(trade.entry_price || 0);
    const sl = Number(trade.stop_loss || 0);
    const tp = Number(trade.target || 0);
    const qty = Number(trade.quantity || 1);
    const initQty = Number(trade.initial_quantity || qty);
    const isFallback = Boolean(trade.is_entry_fallback || trade.quote_source === 'ENTRY_FALLBACK' || trade.pnl_status === 'UNPRICED');

    // Current price / LTP
    const cachedTick = (typeof marketDataCache === 'object' && marketDataCache) ? marketDataCache[sym] : null;
    const cachedPrice = typeof cachedTick === 'object' && cachedTick ? (cachedTick.close ?? cachedTick.price ?? cachedTick.ltp) : cachedTick;
    const ltp = Number(trade.current_price ?? cachedPrice ?? entry);

    const isClosed = trade.status && trade.status !== 'OPEN';
    const exitPx = Number(trade.exit_price || ltp);
    const mult = isLong ? 1 : -1;
    const pnl = isClosed
        ? Number(trade.total_pnl != null ? trade.total_pnl : ((trade.pnl || 0) * qty))
        : (ltp - entry) * qty * mult;
    const pnlPct = isClosed
        ? Number(trade.pnl_pct || (entry > 0 ? ((exitPx - entry) / entry * 100 * mult) : 0))
        : (entry > 0 ? ((ltp - entry) / entry) * 100 * mult : 0);
    const pnlSign = pnl < -0.005 ? '-' : (pnl > 0.005 ? '+' : '');
    const pnlBg = pnl >= 0 ? 'rgba(16, 185, 129, 0.08)' : 'rgba(239, 68, 68, 0.08)';
    const pnlBorder = pnl >= 0 ? 'rgba(16, 185, 129, 0.25)' : 'rgba(239, 68, 68, 0.25)';
    const pnlColor = pnl >= 0 ? 'var(--green, #10b981)' : 'var(--red, #ef4444)';

    // Distances
    const comparePx = isClosed ? exitPx : ltp;
    const slDistPct = entry > 0 ? Math.abs((comparePx - sl) / comparePx * 100) : 0;
    const tpDistPct = comparePx > 0 && tp > 0 ? (isLong ? ((tp - comparePx) / comparePx * 100) : ((comparePx - tp) / comparePx * 100)) : 0;

    // Duration
    const duration = trade.duration_human || trade.time_in_trade_human || _fmtDuration(trade.entry_time, trade.exit_time || new Date().toISOString());

    // Update Header
    const titleEl = document.getElementById('posModalTitle');
    if (titleEl) titleEl.textContent = sym;
    const dirBadge = document.getElementById('posModalDirBadge');
    if (dirBadge) {
        dirBadge.className = `pos-dir-pill ${sideClass}`;
        dirBadge.textContent = sideText;
    }
    const idBadge = document.getElementById('posModalIdBadge');
    if (idBadge) idBadge.textContent = `#${trade.id}`;

    const subMeta = document.getElementById('posModalSubMeta');
    if (subMeta) {
        const exitNote = isClosed ? ` · Status: ${trade.exit_reason || trade.status}` : '';
        subMeta.textContent = `${trade.signal_type || 'AUTONOMOUS'} · ${trade.regime || 'MARKET'} Regime · Duration: ${duration}${exitNote}`;
    }

    // Status rendering
    const statusText = isClosed ? `● ${trade.status || 'CLOSED'}` : '● ACTIVE RUNNING';
    const statusColor = isClosed ? pnlColor : 'var(--green)';
    const pnlTitle = isClosed ? 'Final Realized P&amp;L' : 'Floating Unrealized P&amp;L';

    // Render Rich Body
    const bodyEl = document.getElementById('posModalBody');
    if (bodyEl) {
        bodyEl.innerHTML = `
            ${trade.is_entry_fallback ? `
            <div style="background:rgba(245,158,11,0.1);border:1px solid rgba(245,158,11,0.3);border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:0.75rem;color:#f59e0b;display:flex;align-items:center;gap:10px;">
                <span style="font-size:1.1rem;">⚠️</span>
                <span><strong>Unquoted Entry Fallback:</strong> Real-time quote unavailable. Valuation computed from entry reference price (${currSym}${entry.toFixed(2)}) and will update automatically once market quotes resume.</span>
            </div>
            ` : ''}
            <!-- Live P&L Card -->
            <div style="background:${pnlBg};border:1px solid ${pnlBorder};border-radius:10px;padding:14px 16px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;">
                <div>
                    <div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">${pnlTitle}</div>
                    <div style="font-size:1.5rem;font-weight:800;font-family:var(--font-mono);line-height:1.2;margin-top:2px;color:${pnlColor};">
                        ${pnlSign}${currSym}${Math.abs(pnl).toFixed(2)}
                        <span style="font-size:0.95rem;font-weight:600;margin-left:6px;">(${pnlSign}${pnlPct.toFixed(2)}%)</span>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">Status</div>
                    <div style="font-size:0.9rem;font-weight:700;color:${statusColor};margin-top:4px;">${statusText}</div>
                </div>
            </div>

            <!-- Key Price Levels -->
            <div style="font-size:0.75rem;font-weight:700;color:#c9d1d9;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:8px;">
                Price Targets &amp; Risk Protection
            </div>
            <div style="display:grid;grid-template-columns:repeat(3, 1fr);gap:10px;margin-bottom:16px;">
                <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);padding:10px 12px;border-radius:8px;">
                    <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">Entry Price</div>
                    <div style="font-size:1.05rem;font-weight:700;font-family:var(--font-mono);color:#f0f6fc;margin-top:2px;">${currSym}${entry.toFixed(2)}</div>
                    <div style="font-size: 0.72rem;color:var(--text-muted);margin-top:2px;">Filled</div>
                </div>
                <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);padding:10px 12px;border-radius:8px;">
                    <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">${isClosed ? 'Exit Price' : (trade.quote_status === 'LAST_SESSION_EOD' ? 'Last Session Mark' : (isFallback || trade.is_entry_fallback ? 'Reference Mark' : 'Live Market LTP'))}</div>
                    <div style="font-size:1.05rem;font-weight:700;font-family:var(--font-mono);color:#38bdf8;margin-top:2px;">${currSym}${isClosed ? exitPx.toFixed(2) : ltp.toFixed(2)}</div>
                    <div style="font-size: 0.72rem;color:${isClosed ? 'var(--text-muted)' : '#38bdf8'};margin-top:2px;">${isClosed ? (trade.exit_time ? 'Closed ' + new Date(trade.exit_time).toLocaleTimeString('en-IN', {hour:'2-digit', minute:'2-digit'}) : 'Closed') : (trade.quote_status === 'LAST_SESSION_EOD' ? 'Last Session EOD' : (isFallback || trade.is_entry_fallback || trade.is_stale_quote ? 'Reference Mark' : 'Real-Time'))}</div>
                </div>
                <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);padding:10px 12px;border-radius:8px;">
                    <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">Stop Loss (SL)</div>
                    <div style="font-size:1.05rem;font-weight:700;font-family:var(--font-mono);color:var(--red);margin-top:2px;">${currSym}${sl.toFixed(2)}</div>
                    <div style="font-size: 0.72rem;color:var(--text-muted);margin-top:2px;">${slDistPct.toFixed(1)}% away</div>
                </div>
            </div>

            <div style="display:grid;grid-template-columns:repeat(3, 1fr);gap:10px;margin-bottom:16px;">
                <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);padding:10px 12px;border-radius:8px;">
                    <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">Full Target (TP)</div>
                    <div style="font-size:1.05rem;font-weight:700;font-family:var(--font-mono);color:var(--green);margin-top:2px;">${currSym}${tp.toFixed(2)}</div>
                    <div style="font-size: 0.72rem;color:var(--text-muted);margin-top:2px;">${tpDistPct > 0 ? tpDistPct.toFixed(1) + '% away' : 'Target Reached'}</div>
                </div>
                <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);padding:10px 12px;border-radius:8px;">
                    <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">Partial Target</div>
                    <div style="font-size:1.05rem;font-weight:700;font-family:var(--font-mono);color:#f59e0b;margin-top:2px;">${trade.partial_target > 0 ? currSym + Number(trade.partial_target).toFixed(2) : '—'}</div>
                    <div style="font-size: 0.72rem;color:var(--text-muted);margin-top:2px;">${trade.partial_taken ? '✅ Booked 50%' : 'Pending Scale-out'}</div>
                </div>
                <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);padding:10px 12px;border-radius:8px;">
                    <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">Trailing SL</div>
                    <div style="font-size:1.05rem;font-weight:700;font-family:var(--font-mono);color:#c084fc;margin-top:2px;">📐 Active</div>
                    <div style="font-size: 0.72rem;color:var(--text-muted);margin-top:2px;">Dynamic Lock</div>
                </div>
            </div>

            <!-- Execution & Quant Metadata -->
            <div style="font-size:0.75rem;font-weight:700;color:#c9d1d9;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:8px;">
                Execution &amp; Sizing Intelligence
            </div>
            <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:8px;padding:12px;display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:0.8rem;">
                <div>
                    <span style="color:var(--text-muted);">Position Quantity:</span>
                    <b style="color:#f0f6fc;font-family:var(--font-mono);margin-left:6px;">${qty}</b>
                    ${initQty !== qty ? `<span style="font-size:0.7rem;color:var(--text-muted);"> (init: ${initQty})</span>` : ''}
                </div>
                <div>
                    <span style="color:var(--text-muted);">Entry Time:</span>
                    <b style="color:#f0f6fc;margin-left:6px;">${trade.entry_time ? new Date(trade.entry_time).toLocaleString('en-IN', {day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'}) : '—'}</b>
                </div>
                <div>
                    <span style="color:var(--text-muted);">Strategy / Engine:</span>
                    <b style="color:#38bdf8;margin-left:6px;">${trade.signal_type || 'AUTONOMOUS'}</b>
                </div>
                <div>
                    <span style="color:var(--text-muted);">Market Regime:</span>
                    <b style="color:#f0f6fc;margin-left:6px;">${trade.regime || 'UNKNOWN'}</b>
                </div>
                <div>
                    <span style="color:var(--text-muted);">AI Confidence:</span>
                    <b style="color:#10b981;margin-left:6px;">${trade.confidence ? Number(trade.confidence).toFixed(0) + '%' : '—'}</b>
                </div>
                <div>
                    <span style="color:var(--text-muted);">RSI at Entry:</span>
                    <b style="color:#f0f6fc;margin-left:6px;">${trade.rsi_at_entry ? Number(trade.rsi_at_entry).toFixed(1) : '—'}</b>
                </div>
            </div>
        `;
    }

    // Wire action buttons
    const viewChartBtn = document.getElementById('posModalViewChartBtn');
    if (viewChartBtn) {
        viewChartBtn.onclick = () => {
            closePositionDetailModal();
            if (typeof openSymbolModal === 'function') {
                openSymbolModal(sym);
            }
        };
    }

    const exitBtn = document.getElementById('posModalExitBtn');
    if (exitBtn) {
        if (isClosed) {
            exitBtn.style.display = 'none';
        } else {
            exitBtn.style.display = 'inline-block';
            exitBtn.onclick = () => {
                closePositionDetailModal();
                if (typeof closePaperTrade === 'function') {
                    closePaperTrade(trade.id, ltp);
                }
            };
        }
    }

    modalBackdrop.style.display = 'flex';
}

function closePositionDetailModal() {
    const modalBackdrop = document.getElementById('positionDetailModalBackdrop');
    if (modalBackdrop) modalBackdrop.style.display = 'none';
}

window.openPositionDetail = openPositionDetail;
window.closePositionDetailModal = closePositionDetailModal;


function updateStatsStrip(stats) {
    const fmt = (v) => _fmtMoney(v);

    let u = stats && stats.unrealized_pnl != null ? Number(stats.unrealized_pnl) : 0;
    if (u === 0 && Array.isArray(_lastOpenTrades) && _lastOpenTrades.length > 0) {
        const openMtm = _lastOpenTrades.reduce((sum, t) => {
            const val = Number(t.unrealized_pnl ?? t.unrealized_inr ?? t.pnl ?? 0);
            return sum + (isNaN(val) ? 0 : val);
        }, 0);
        if (openMtm !== 0) {
            u = openMtm;
        }
    }

    let cap = stats && (stats.total_equity != null ? Number(stats.total_equity) : (stats.capital != null ? Number(stats.capital) : Number(stats.initial_capital)));
    if (u !== 0) {
        const cashCap = stats && (stats.cash_capital != null ? Number(stats.cash_capital) : (stats.capital != null ? Number(stats.capital) : null));
        if (cashCap != null && Math.abs(cap - cashCap) < 1.0) {
            cap = cashCap + u;
        }
    }

    const capEl = document.getElementById('statCapital');
    if (capEl) capEl.textContent = cap != null && !isNaN(cap) ? fmt(cap) : '—';

    const seedEl = document.getElementById('statCapitalSeed');
    if (seedEl && stats && stats.initial_capital != null) {
        seedEl.textContent = `Seed: ${fmt(stats.initial_capital)}`;
    }

    const uEl = document.getElementById('statUnrealizedPnl');
    if (uEl && _unpricedPositions > 0) {
        _applyUnpricedMtm();
    } else if (uEl) {
        uEl.textContent = `MTM: ${u >= 0 ? '+' : ''}${fmt(u)}`;
        uEl.style.color = u > 0 ? 'var(--green, #10b981)' : (u < 0 ? 'var(--red, #ef4444)' : 'var(--text-muted, #8b949e)');
    }

    const pnlEl = document.getElementById('statPnl') || document.getElementById('statTotalPnl');



    const pnl = stats ? stats.total_pnl : null;



    if (pnl != null) {



        pnlEl.textContent = _fmtSignedMoney(pnl);



        pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';



    } else {



        pnlEl.textContent = '—';



    }



    const closedTrades = Number(stats?.total_trades || 0);
    const liveOpenCount = Array.isArray(_lastOpenTrades) ? _lastOpenTrades.length : 0;
    const openTrades = stats?.open_trades != null ? Number(stats.open_trades) : liveOpenCount;
    const effectiveOpen = Math.max(openTrades, liveOpenCount);

    const statSummary = typeof formatTradeStatsSummary === 'function'
        ? formatTradeStatsSummary(stats, closedTrades)
        : { winRateDisplay: '—', subText: '0 closed trades', titleText: '', closedSubDetail: `${closedTrades} closed` };

    document.getElementById('statWinRate').textContent = statSummary.winRateDisplay;
    const wrSub = document.getElementById('statWinRateSub');
    if (wrSub) {
        wrSub.textContent = statSummary.subText;
        wrSub.title = statSummary.titleText;
    }

    // Hero metric tile is "Open Positions", so the primary number is ALWAYS the count of active open positions!
    const tradesEl = document.getElementById('statTrades');
    if (tradesEl) {
        tradesEl.textContent = `${effectiveOpen}`;
    }
    const subTradesEl = document.getElementById('statTradesSubDetail');
    if (subTradesEl) {
        subTradesEl.textContent = statSummary.closedSubDetail;
    }







    const hint = document.getElementById('statsHint');



    if (hint) {



        if (closedTrades === 0 && openTrades === 0) {



            hint.style.display = 'block';



            hint.textContent = 'No trades yet — click "⚡ Paper Trade" on any signal card below to start simulated execution.';



        } else if (effectiveOpen > 0 && closedTrades === 0) {



            hint.style.display = 'block';



            hint.textContent = `${effectiveOpen} paper trade(s) actively running in real-time. P&L and win rate will finalize on exit.`;



        } else {



            hint.style.display = 'none';



        }



    }



}







// ── Paper Trading Scorecard Banner & EOD Resolve ─────────



async function loadPaperScorecard() {



    const banner = document.getElementById('paperScorecardBanner');



    if (!banner) return;



    try {



        const res = await fetchWithTimeout(`${API}/api/paper/scorecard`, {}, 8000);



        if (!res.ok) return;



        const data = await res.json();



        banner.style.display = 'block';



        const badge = document.getElementById('paperUnlockBadge');



        if (badge) {



            const level = (data.unlock_level || 'grey').toLowerCase();



            badge.textContent = level === 'green' ? 'VALIDATED' : level === 'orange' ? 'IN PROGRESS' : level === 'red' ? 'LOW WR' : 'BUILDING';



            const colorMap = { green: 'var(--green, #10b981)', orange: '#f59e0b', red: 'var(--red, #ef4444)', grey: '#9ca3af' };



            badge.style.color = colorMap[level] || '#9ca3af';



            badge.style.borderColor = badge.style.color;



        }



        const totalEl = document.getElementById('paperTotalTrades');



        if (totalEl) totalEl.textContent = data.total_trades ?? 0;



        const openEl = document.getElementById('paperOpenTrades');



        if (openEl) openEl.textContent = data.open_trades ?? 0;



        const wrEl = document.getElementById('paperWinRate');



        if (wrEl) wrEl.textContent = data.win_rate_pct != null ? `${Number(data.win_rate_pct).toFixed(1)}%` : '—';



        const msgEl = document.getElementById('paperUnlockMsg');



        if (msgEl) msgEl.textContent = data.unlock_message || '';



    } catch (e) {



        console.debug('loadPaperScorecard failed:', e);



    }



}







async function paperResolveEOD() {



    showToast('⏳ Resolving paper trades against EOD prices...', 'info');



    try {



        const res = await fetchWithTimeout(`${API}/api/paper/resolve`, { method: 'POST', headers: _getAuthHeaders() }, 15000);



        if (res.status === 401 || res.status === 403) {



            _handleAuthChallenge(res.status);



            return;



        }



        let data = {};



        try { data = await res.json(); } catch (_) { data = {}; }



        if (!res.ok) {



            showToast(`❌ Resolve failed: ${data.detail || data.error || 'Server error'}`, 'error');



            return;



        }



        showToast(`✅ EOD Resolve: ${data.resolved ?? 0} resolved, ${data.open_remaining ?? 0} open`, 'success');



        loadPaperScorecard();



    } catch (e) {



        showToast(`❌ Resolve failed: ${e.message}`, 'error');



    }



}







window.loadPaperScorecard = loadPaperScorecard;



window.paperResolveEOD = paperResolveEOD;















function _getIstDate() {



    const fmt = new Intl.DateTimeFormat('en-US', {



        timeZone: 'Asia/Kolkata',



        year: 'numeric',



        month: '2-digit',



        day: '2-digit',



        hour: '2-digit',



        minute: '2-digit',



        second: '2-digit',



        hour12: false,



    });



    const parts = Object.fromEntries(fmt.formatToParts(new Date()).map(p => [p.type, p.value]));



    return {



        year: Number(parts.year),



        month: Number(parts.month),



        day: Number(parts.day),



        hour: Number(parts.hour),



        minute: Number(parts.minute),



        second: Number(parts.second),



    };



}











function _isNseMarketOpen() {



    const t = _getIstDate();



    const jsDay = new Date(Date.UTC(t.year, t.month - 1, t.day)).getUTCDay();



    if (jsDay === 0 || jsDay === 6) return false;



    const mins = t.hour * 60 + t.minute;



    return mins >= (9 * 60 + 15) && mins <= (15 * 60 + 30);



}











function _nextNseOpenLabel() {
    const t = _getIstDate();
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const curDate = new Date(Date.UTC(t.year, t.month - 1, t.day));
    const currentMins = t.hour * 60 + t.minute;

    for (let i = 0; i < 8; i++) {
        const candidate = new Date(curDate.getTime() + i * 86400000);
        const dayOfWeek = candidate.getUTCDay();
        if (dayOfWeek === 0 || dayOfWeek === 6) continue;
        if (i === 0 && currentMins >= (9 * 60 + 15)) continue;

        const dayNum = String(candidate.getUTCDate()).padStart(2, '0');
        const monthName = months[candidate.getUTCMonth()];
        return `${dayNum} ${monthName}, 09:15 am`;
    }
    return '09:15 AM IST';



    return '09:15 AM IST';



}











function updateMarketSessionBanner() {



    const banner = document.getElementById('marketSessionBanner');



    if (!banner) return;



    if (_isNseMarketOpen()) {



        banner.style.display = 'none';



        banner.textContent = '';



        return;



    }



    banner.style.display = 'flex';



    banner.textContent = `Market Closed · Opens at 9:15 AM IST · Showing last session signals (next open: ${_nextNseOpenLabel()})`;



}











function initSignalControls() {



    const toggle = document.getElementById('showSkipToggle');



    if (toggle) {



        toggle.checked = _showSkipSignals;



        toggle.addEventListener('change', () => {



            _showSkipSignals = Boolean(toggle.checked);



            renderSignals(allSignals);



        });



    }







    document.querySelectorAll('.signal-filter-btn').forEach(btn => {



        btn.addEventListener('click', () => {



            _signalTypeFilter = btn.dataset.signalFilter || 'all';



            document.querySelectorAll('.signal-filter-btn').forEach(b => b.classList.remove('active'));



            btn.classList.add('active');



            renderSignals(allSignals);



        });



    });







    const sortSelect = document.getElementById('signalSortSelect');



    if (sortSelect) {



        sortSelect.value = _signalSortMode;



        sortSelect.addEventListener('change', () => {



            _signalSortMode = sortSelect.value;



            renderSignals(allSignals);



        });



    }



}











// ── Render Signal Cards ─────────────────────────────────







function renderConflictBadge(conflict) {



    if (!conflict || typeof conflict !== 'object') return '';



    const severity = String(conflict.severity || 'none').toLowerCase();



    const reasons = Array.isArray(conflict.reasons) ? conflict.reasons : [];



    if (severity === 'none' || reasons.length === 0) return '';







    const title = reasons.join(' | ').replace(/"/g, '&quot;');



    if (severity === 'block') {



        return '<span class="skip-badge" style="background:rgba(239,68,68,0.14);color:#ef4444;border:1px solid rgba(239,68,68,0.4);" title="' + title + '">🚫 BLOCKED</span>';



    }



    return '<span class="skip-badge" style="background:rgba(245,158,11,0.15);color:#f59e0b;border:1px solid rgba(245,158,11,0.35);" title="' + title + '">⚠️ CONFLICT</span>';



}







function renderMoodSplit(split) {



    const safeSplit = split && typeof split === 'object' ? split : {};



    const breadth = safeSplit.breadth || {};



    const signalBias = safeSplit.signal_bias || {};



    const composite = safeSplit.composite || {};







    const panel = (title, data) => {



        const label = String(data.label || 'NEUTRAL').toUpperCase();



        const score = Number(data.score || 50).toFixed(1);



        const detail = String(data.detail || 'No details available');



        let tone = 'var(--text-muted)';



        if (label === 'BULLISH') tone = 'var(--green)';



        if (label === 'BEARISH') tone = 'var(--red)';







        return `



            <div style="border:1px solid var(--border);background:var(--surface);border-radius:8px;padding:0.5rem 0.55rem;">



                <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">${title}</div>



                <div style="font-size:0.78rem;font-weight:700;color:${tone};margin-top:0.2rem;">${label} ${score}%</div>



                <div style="font-size: 0.72rem;color:var(--text-muted);margin-top:0.12rem;">${detail}</div>



            </div>



        `;



    };







    return `



        <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem;margin-top:0.55rem;">



            ${panel('MARKET BREADTH', breadth)}



            ${panel('SIGNAL BIAS', signalBias)}



        </div>



        <div style="margin-top:0.45rem;font-size:0.74rem;color:var(--text-muted);">



            Composite: <span style="color:var(--text-primary);font-weight:700;">${Number(composite.score || 50).toFixed(1)} / 100</span>



            <span style="margin-left:0.35rem;">${String(composite.note || '')}</span>



        </div>



    `;



}







function renderSignals(signals) {



    const grid = document.getElementById('signalsGrid');



    const filterNote = document.getElementById('signalsFilterNote');



    const skipLabel = document.getElementById('showSkipLabel');



    grid.querySelectorAll('.signal-card, .error-state').forEach(el => el.remove());







    let visibleSignals = _showSkipSignals



        ? signals



        : signals.filter(sig => String(sig.signal || '').toUpperCase() !== 'SKIP');







    visibleSignals = visibleSignals.filter(sig => {



        const s = String(sig.signal || '').toUpperCase();



        const conf = Number(sig.confidence || 0);



        if (_signalTypeFilter === 'buy') return s === 'LONG' || s === 'BUY';



        if (_signalTypeFilter === 'sell') return s === 'SHORT' || s === 'SELL';



        if (_signalTypeFilter === 'high_conf') return conf > 75;



        return true;



    });







    const sorters = {



        qs_desc: (a, b) => Number(b.quality_score || 0) - Number(a.quality_score || 0),



        conf_desc: (a, b) => Number(b.confidence || 0) - Number(a.confidence || 0),



        signal_type: (a, b) => {



            const rank = { LONG: 0, BUY: 0, SHORT: 1, SELL: 1, SKIP: 2, HOLD: 3 };



            return (rank[String(a.signal || '').toUpperCase()] ?? 9) - (rank[String(b.signal || '').toUpperCase()] ?? 9);



        },



    };



    visibleSignals.sort(sorters[_signalSortMode] || sorters.qs_desc);







    if (skipLabel) {



        const filteredCount = signals.filter(sig => String(sig.signal || '').toUpperCase() === 'SKIP').length;



        skipLabel.textContent = `Show filtered signals (${filteredCount} SKIP)`;



    }







    if (filterNote) {



        const filterMap = {



            all: 'All',



            buy: 'BUY',



            sell: 'SELL',



            high_conf: 'High Confidence (>75%)',



        };



        if (_showSkipSignals) {



            filterNote.textContent = `Showing ${visibleSignals.length} signals · Filter: ${filterMap[_signalTypeFilter] || 'All'} · Sort: ${_signalSortMode}`;



        } else {



            filterNote.textContent = `Showing ${visibleSignals.length}/${signals.length} · SKIP hidden by default · Filter: ${filterMap[_signalTypeFilter] || 'All'} · Sort: ${_signalSortMode}`;



        }



    }







    window._lastFilteredSignals = visibleSignals;
    visibleSignals.forEach((sig, i) => {



        const card = document.createElement('div');



        const isSkip = sig.signal === 'SKIP';



        // Map LONG→buy, SHORT→sell for CSS classes (existing styles)



        const displaySignal = sig.signal === 'LONG' ? 'BUY' : sig.signal === 'SHORT' ? 'SELL' : sig.signal;



        const signalLower = displaySignal.toLowerCase();



        card.className = `signal-card card-${signalLower}`;



        if (sig.stale) card.classList.add('card-stale');



        if (isSkip)   card.classList.add('card-skip');



        card.style.animationDelay = `${i * 0.06}s`;



        card.onclick = () => openSymbolModal(sig.symbol);







        const isCrypto = sig.symbol.includes('/');



        const curr = isCrypto ? '$' : '₹';



        const fmtPrice = (v) => { if (v == null) return '—'; const num = Number(v); if (isNaN(num)) return String(v); const digits = (num !== 0 && Math.abs(num) < 0.001) ? 8 : ((num !== 0 && Math.abs(num) < 0.1) ? 6 : (Math.abs(num) < 10 ? 4 : 2)); return `${curr}${num.toLocaleString('en-US', { minimumFractionDigits: digits < 2 ? digits : 2, maximumFractionDigits: digits })}`; };







        let rsiClass = 'rsi-normal';



        if (sig.rsi && sig.rsi < 30) rsiClass = 'rsi-oversold';



        else if (sig.rsi && sig.rsi > 70) rsiClass = 'rsi-overbought';







        const trend = (sig.trend || 'NEUTRAL').replace('TrendDirection.', '');



        let trendClass = 'neutral';



        if (trend.toUpperCase().includes('BULL')) trendClass = 'bullish';



        else if (trend.toUpperCase().includes('BEAR')) trendClass = 'bearish';







        const regime = (sig.regime || 'RANGING').toLowerCase().replace(' ', '_');



        const regimeLabel = String(sig.regime || 'RANGING').replace('_', ' ');







        const conf = Math.min(sig.confidence || 0, 85);



        const circumference = 2 * Math.PI * 36;



        const offset = circumference - (conf / 100) * circumference;



        let ringColor = '#ef4444';



        let ringGlow = 'rgba(239,68,68,0.25)';



        if (conf >= 65) { ringColor = '#10b981'; ringGlow = 'rgba(16,185,129,0.3)'; }



        else if (conf >= 40) { ringColor = '#f59e0b'; ringGlow = 'rgba(245,158,11,0.25)'; }







        const staleMins = Number(sig.stale_minutes || 0);



        const staleLimit = String(sig.symbol || '').includes('/') ? 60 : 30;



        const isMarketClosed = !_isNseMarketOpen();



        const staleBadge = sig.stale



            ? (isMarketClosed



                ? ''



                : `<span class="stale-badge" title="Signal age: ${staleMins.toFixed(1)} min">STALE > ${staleLimit}m</span>`)



            : '';



        const skipReason = String(sig.skip_reason || '').trim();



        const conflictReason = String(sig.signal_conflict || '').trim();



        const conflictPayload = (sig.conflict && typeof sig.conflict === 'object')



            ? sig.conflict



            : (conflictReason



                ? { severity: 'warn', reasons: [conflictReason] }



                : null);



        const skipBadge = isSkip



            ? `<span class="skip-badge" title="${skipReason ? `Skipped: ${skipReason}` : 'Setup detected but quality or safety gates blocked this trade.'}">SKIP</span>`



            : '';



        const conflictBadge = renderConflictBadge(conflictPayload);







        // Quality score badge



        const qs = sig.quality_score ?? 0;



        const qg = sig.quality_grade || 'D';



        const qualityHint = (isSkip && qs >= 70 && skipReason)



            ? `Quality Score: ${qs}/100. Blocked by safety gate: ${skipReason}`



            : `Quality Score: ${qs}/100. Tradeable setups are usually QS >= 70.`;



        const qualityBadge = `<span class="quality-badge quality-${qg}" title="${qualityHint}">QS ${qs}</span>`;







        const entryVal = Number(sig.entry);



        const slVal = Number(sig.stop_loss);



        const tpVal = Number(sig.target);



        let rrLabel = '—';



        if (Number.isFinite(entryVal) && Number.isFinite(slVal) && Number.isFinite(tpVal)) {



            const risk = Math.abs(entryVal - slVal);



            const reward = Math.abs(tpVal - entryVal);



            if (risk > 0 && reward > 0) {



                rrLabel = `1:${(reward / risk).toFixed(2)}`;



            }



        }







        // Data quality dot



        const dq = sig.data_quality ?? 100;



        const dqClass = dq >= 95 ? 'dq-good' : dq >= 80 ? 'dq-warn' : 'dq-bad';



        const dqDot = `<span class="dq-dot ${dqClass}" title="Data quality: ${dq}%"></span>`;







        // MACD histogram value



        const macdVal = sig.macd_hist != null ? Number(sig.macd_hist).toFixed(3) : null;







        // Display signal label: LONG→LONG, SHORT→SHORT, SKIP→SKIP, HOLD→HOLD



        const signalLabel = sig.signal;







        const signalDate = new Date(sig.timestamp);



        const timeStr = signalDate.toLocaleTimeString('en-IN', {hour: '2-digit', minute:'2-digit'});







        // RSI visual bar



        const rsiVal = sig.rsi ?? null;



        let rsiBarColor = 'var(--text-muted)';



        if (rsiVal !== null) {



            if (rsiVal < 30) rsiBarColor = 'var(--green)';



            else if (rsiVal > 70) rsiBarColor = 'var(--red)';



            else rsiBarColor = 'var(--accent-light)';



        }



        const rsiBarWidth = rsiVal !== null ? Math.min(rsiVal, 100) : 0;







        // Trend arrow



        let trendArrow = '→';



        if (trendClass === 'bullish') trendArrow = '↗';



        else if (trendClass === 'bearish') trendArrow = '↘';







        let sparklineSvg = '';



        if (sig.sparkline && sig.sparkline.length > 1) {



            const minP = Math.min(...sig.sparkline);



            const maxP = Math.max(...sig.sparkline);



            const range = maxP - minP || 1;



            const pts = sig.sparkline.map((p, idx) => {



                const x = (idx / (sig.sparkline.length - 1)) * 64;



                const y = 22 - ((p - minP) / range) * 22;



                return `${x},${y}`;



            }).join(' ');



            



            const isUp = sig.sparkline[sig.sparkline.length-1] >= sig.sparkline[0];



            const spLineColor = isUp ? 'var(--green)' : 'var(--red)';



            const spFillColor = isUp ? 'rgba(16,185,129,0.08)' : 'rgba(239,68,68,0.08)';



            const fillPts = `0,22 ${pts} 64,22`;



            



            sparklineSvg = `



                <div class="sparkline-wrap" title="Last 20 candles">



                    <svg viewBox="0 0 64 22" width="64" height="22" style="overflow:visible;">



                        <polygon points="${fillPts}" fill="${spFillColor}"/>



                        <polyline points="${pts}" fill="none" stroke="${spLineColor}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>



                    </svg>



                </div>



            `;



        }







        const ctxScore = Number(sig.context_score ?? 0);



        const ctxBarWidth = Math.min(100, Math.max(0, ((ctxScore + 1) / 2) * 100));



        const ctxTone = ctxScore > 0.15 ? 'var(--green)' : ctxScore < -0.15 ? 'var(--red)' : 'var(--text-muted)';



        const newsSent = Number(sig.news_sentiment_24hr ?? 0);



        const newsLabel = newsSent > 0.1 ? 'Bullish' : newsSent < -0.1 ? 'Bearish' : 'Neutral';



        const eventDays = Number(sig.days_to_next_event ?? 99);



        const eventWarn = eventDays <= 3 ? `⚠ Event in ${eventDays}d` : '';



        const contextWarning = sig.context_warning ? String(sig.context_warning) : '';



        const driftDetected = Boolean(sig.drift_detected);



        const driftZ = Number(sig.drift_z_score ?? 0);



        const driftFeature = String(sig.drift_feature || '');



        const driftWarn = driftDetected ? `⚠ Drift ${driftZ.toFixed(1)}σ${driftFeature ? ` (${driftFeature})` : ''}` : '';







        const isPinned = _watchlist.has(sig.symbol);



        const isStale = Boolean(sig.stale || sig.is_stale || sig.freshness === 'stale');
        const isBlocked = Boolean(sig.is_blocked || sig.in_avoid_list || (sig.blockers && sig.blockers.length > 0));
        const numEntry = Number(sig.entry || sig.close || 0);
        const numSl = Number(sig.stop_loss || 0);
        const numTp = Number(sig.target || 0);
        const isLongSignal = ['LONG', 'BUY'].includes(String(sig.signal || '').toUpperCase());
        const isShortSignal = ['SHORT', 'SELL'].includes(String(sig.signal || '').toUpperCase());
        const hasValidGeometry = numEntry > 0 && numSl > 0 && numTp > 0 && (
            isLongSignal ? (numSl < numEntry && numEntry < numTp) :
            isShortSignal ? (numTp < numEntry && numEntry < numSl) :
            false
        );

        const isActionable = (isLongSignal || isShortSignal) && !isSkip && !isStale && !isBlocked && hasValidGeometry;

        const tradeDir = isLongSignal ? 'LONG' : 'SHORT';



        let tradeActionHtml = '';
        if (isActionable) {
            tradeActionHtml = `<div style="margin-top:0.6rem;padding-top:0.45rem;border-top:1px solid rgba(255,255,255,0.06);display:flex;justify-content:flex-end;">
                <button type="button" class="btn btn-primary" onclick="event.stopPropagation();takePaperTrade('${_escHtml(sig.symbol)}', '${tradeDir}', ${Number(sig.entry || sig.close || 0)}, ${Number(sig.stop_loss || 0)}, ${Number(sig.target || 0)}, ${Number(sig.atr || 0)}, '${_escHtml(sig.strategy || 'SIGNAL')}', this, { confidence: ${Number(sig.confidence || 0)}, regime: '${_escHtml(sig.regime || 'TRENDING')}', rsi: ${Number(sig.rsi || 0)} })" style="font-size:0.75rem;padding:0.35rem 0.75rem;cursor:pointer;border-radius:5px;background:linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%);color:#fff;border:none;font-weight:600;display:flex;align-items:center;gap:4px;">⚡ Paper Trade</button>
            </div>`;
        } else if (['LONG', 'BUY', 'SHORT', 'SELL'].includes(String(sig.signal || '').toUpperCase())) {
            const blockReason = isStale ? 'Stale Data' : (isBlocked ? 'Setup Blocked' : (!hasValidGeometry ? 'Degenerate Geometry' : 'Not Actionable'));
            tradeActionHtml = `<div style="margin-top:0.6rem;padding-top:0.45rem;border-top:1px solid rgba(255,255,255,0.06);display:flex;justify-content:flex-end;">
                <button type="button" class="btn btn-secondary" disabled style="font-size:0.72rem;padding:0.3rem 0.65rem;border-radius:5px;background:rgba(255,255,255,0.05);color:var(--text-muted);border:1px solid rgba(255,255,255,0.1);cursor:not-allowed;" title="${blockReason}">🔒 ${blockReason}</button>
            </div>`;
        }



        card.innerHTML = `



            <div class="card-top">



                <div style="display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap;flex:1">



                    <span class="card-symbol signal-symbol">${sig.symbol}</span>${dqDot}



                    <span class="regime-badge regime-${regime}">${regimeLabel}</span>



                    ${staleBadge}${skipBadge}${conflictBadge}



                    <button class="p4-card-pin ${isPinned ? 'pinned' : ''}" onclick="event.stopPropagation();toggleWatchlist('${sig.symbol}')" title="${isPinned ? 'Unpin from watchlist' : 'Pin to watchlist'}">${isPinned ? '★' : '☆'}</button>



                </div>



                <div style="text-align:right;display:flex;flex-direction:column;align-items:flex-end;">



                    <span class="card-price signal-price" data-price="${sig.close ?? 0}">${fmtPrice(sig.close)}</span>



                    <span class="signal-time-chip">${timeStr}</span>



                </div>



            </div>



            <div class="card-signal-row">



                <div style="display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap">



                    <div class="signal-badge ${signalLower}">${signalLabel}</div>



                    ${qualityBadge}



                </div>



                <div class="confidence-wrap">



                    <div class="confidence-ring" style="--ring-glow-color:${ringGlow}">



                        <svg viewBox="0 0 80 80">



                            <circle class="ring-bg" cx="40" cy="40" r="36"/>



                            <circle class="ring-fill" cx="40" cy="40" r="36"



                                style="stroke-dashoffset: ${offset}; stroke: ${ringColor};"/>



                        </svg>



                        <span class="confidence-value" style="color:${ringColor}">${conf}%</span>



                    </div>



                    <span class="confidence-label">Confidence</span>



                </div>



            </div>



            <div class="card-levels">



                <div class="level entry">



                    <span class="level-label">Entry</span>



                    <span class="level-value">${fmtPrice(sig.entry)}</span>



                </div>



                <div class="level sl">



                    <span class="level-label">Stop Loss</span>



                    <span class="level-value">${fmtPrice(sig.stop_loss)}</span>



                </div>



                <div class="level tp">



                    <span class="level-label">Target</span>



                    <span class="level-value">${fmtPrice(sig.target)}</span>



                </div>



                <div class="level rr">



                    <span class="level-label">R:R</span>



                    <span class="level-value">${rrLabel}</span>



                </div>



            </div>



            <div class="card-indicators">



                <div class="indicator rsi-bar-wrap">



                    <span class="indicator-label">RSI</span>



                    <span class="indicator-value ${rsiClass}">${rsiVal ?? '—'}</span>



                    ${rsiVal !== null ? `<div class="rsi-mini-bar"><div class="rsi-mini-fill" style="width:${rsiBarWidth}%;background:${rsiBarColor}"></div></div>` : ''}



                </div>



                <div class="indicator">



                    <span class="indicator-label">Trend</span>



                    <span class="trend-arrow ${trendClass}">${trendArrow}</span>



                    <span class="indicator-value ${trendClass}">${trend}</span>



                </div>



                ${sparklineSvg}



                ${macdVal != null ? `



                <div class="indicator">



                    <span class="indicator-label">MACD</span>



                    <span class="indicator-value ${parseFloat(macdVal) >= 0 ? 'bullish' : 'bearish'}">${macdVal}</span>



                </div>` : ''}



            </div>



            <div style="margin-top:0.5rem;padding-top:0.5rem;border-top:1px solid var(--border-color);">



                <div style="display:flex;justify-content:space-between;align-items:center;font-size: 0.72rem;color:var(--text-muted);">



                    <span>Context Score</span>



                    <span style="color:${ctxTone};font-weight:700;">${ctxScore.toFixed(2)}</span>



                </div>



                <div style="margin-top:0.2rem;height:6px;background:rgba(255,255,255,0.08);border-radius:99px;overflow:hidden;">



                    <div style="height:100%;width:${ctxBarWidth}%;background:${ctxTone};border-radius:99px;"></div>



                </div>



                <div style="margin-top:0.35rem;display:flex;justify-content:space-between;gap:0.5rem;font-size: 0.72rem;">



                    <span style="color:var(--text-muted)">News: <b style="color:${newsSent >= 0 ? 'var(--green)' : 'var(--red)'}">${newsLabel}</b></span>



                    ${eventWarn ? `<span style="color:var(--yellow);font-weight:700;">${eventWarn}</span>` : '<span></span>'}



                </div>



                ${driftWarn ? `<div style="margin-top:0.28rem;font-size: 0.72rem;color:var(--orange)">${driftWarn}</div>` : ''}



                ${contextWarning ? `<div style="margin-top:0.28rem;font-size: 0.72rem;color:var(--yellow)">${contextWarning}</div>` : ''}



            </div>



            ${tradeActionHtml}



        `;







        grid.appendChild(card);



    });



}











// ── Summary ─────────────────────────────────────────────







function updateSummary(signals) {



    let buy = 0, sell = 0, hold = 0;



    signals.forEach(s => {



        // LONG=BUY, SHORT=SELL, SKIP/HOLD=neutral



        if (s.signal === 'BUY' || s.signal === 'LONG')   buy++;



        else if (s.signal === 'SELL' || s.signal === 'SHORT') sell++;



        else hold++;



    });



    document.getElementById('sumBuy').textContent = buy;



    document.getElementById('sumSell').textContent = sell;



    document.getElementById('sumHold').textContent = hold;



}











function updateMarketIntel(dashboardData) {



    if (!dashboardData || typeof dashboardData !== 'object') return;







    if (dashboardData.mood) {



        updateMarketMood(dashboardData.mood);



    }







    const summaryEl = document.getElementById('aiSummaryText');



    const summary = dashboardData.ai_summary;



    if (summaryEl) {



        if (summary && Array.isArray(summary.lines) && summary.lines.length > 0) {



            summaryEl.textContent = summary.lines.join(' ');



        } else if (summary && summary.summary) {



            summaryEl.textContent = summary.summary;



        }



    }







    const chipsEl = document.getElementById('topMoverChips');



    if (chipsEl) {



        const gainers = (dashboardData.movers && dashboardData.movers.gainers) || [];



        const losers = (dashboardData.movers && dashboardData.movers.losers) || [];



        const top = [



            ...gainers.slice(0, 3).map(s => ({ ...s, _dir: 'up' })),



            ...losers.slice(0, 3).map(s => ({ ...s, _dir: 'down' })),



        ];







        if (top.length === 0) {



            chipsEl.innerHTML = '<span class="mover-chip">No mover data</span>';



        } else {



            chipsEl.innerHTML = top.map(s => {



                const pct = Number(s.pct_change || 0);



                const sign = pct >= 0 ? '+' : '';



                return `<span class="mover-chip ${s._dir}">${s.symbol} ${sign}${pct.toFixed(2)}%</span>`;



            }).join('');



        }



    }



}







function updateOpportunitiesGrid(opportunities, meta) {



    const grid = document.getElementById('opportunityGrid');



    if (!grid) return;







    if (!opportunities || opportunities.length === 0) {
        const _scanStatus = meta && meta.status;
        if (_scanStatus === 'disabled' || _scanStatus === 'empty') {
            const _scanReason = meta.reason ? String(meta.reason).replace(/_/g, ' ') : 'no candidates';
            const _scanLabel = (_scanStatus === 'disabled' ? 'Scan disabled' : 'No opportunities') + ' — ' + _scanReason + '.';
            grid.innerHTML = '<div style="font-size:0.8rem;color:var(--text-muted);grid-column:1/-1;">' + _scanLabel + '</div>';
            return;
        }



        if (_isNseMarketOpen()) {



            grid.innerHTML = '<div style="font-size:0.8rem;color:var(--text-muted);grid-column:1/-1;">No deep opportunities right now. Market is in SKIP zone — refresh in a few minutes.</div>';



        } else {



            grid.innerHTML = '<div style="font-size:0.8rem;color:var(--text-muted);grid-column:1/-1;">Market closed. Next scan starts at 9:15 AM IST — showing new opportunities after open.</div>';



        }



        return;



    }







    grid.innerHTML = opportunities.map(opp => {



        let sigColor = 'var(--cyan)';



        let sigText = opp.signal;



        if (opp.signal.includes('BUY')) sigColor = 'var(--green)';



        if (opp.signal.includes('SELL')) sigColor = 'var(--red)';



        if (opp.signal.includes('BREAKOUT') || opp.signal.includes('SPIKE')) sigColor = 'var(--yellow)';







        // Strip .NS for display



        const symbol = opp.symbol.replace('.NS', '');







        return `



            <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);padding:0.6rem;border-radius:6px;display:flex;flex-direction:column;gap:0.25rem;">



                <div style="display:flex;justify-content:space-between;align-items:center;">



                    <span style="font-weight:bold;font-size:0.85rem">${symbol}</span>



                    <span style="font-size: 0.72rem;color:${sigColor};border:1px solid ${sigColor};padding:1px 4px;border-radius:3px;">${sigText}</span>



                </div>



                <div style="font-size:0.8rem;">



                    <span style="color:var(--text-muted);">₹${opp.price.toFixed(2)}</span>



                </div>



                <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:var(--text-muted);">



                    <span>RSI: ${opp.rsi ? opp.rsi : '-'}</span>



                    <span>Vol: ${(opp.vol / 1000).toFixed(0)}k <span style="font-size:0.6rem;color:${opp.vol_spike ? 'var(--green)' : 'inherit'}">${opp.vol_spike ? '▲' : ''}</span></span>



                </div>



            </div>



        `;



    }).join('');



}











function updateMarketMood(mood) {



    const badge = document.getElementById('marketMoodBadge');



    const score = document.getElementById('marketMoodScore');



    const explain = document.getElementById('marketMoodExplain');



    const splitContainer = document.getElementById('marketMoodSplitContainer');



    if (!badge || !score || !mood) return;







    const moodLabel = String(mood.mood || 'NEUTRAL').toUpperCase();



    const scoreValue = Number(mood.score || 50);







    badge.textContent = moodLabel;



    badge.className = 'mood-badge mood-neutral';



    if (moodLabel === 'BULLISH') {



        badge.classList.add('mood-bullish');



    } else if (moodLabel === 'BEARISH') {



        badge.classList.add('mood-bearish');



    } else if (moodLabel === 'CAUTIOUS') {



        badge.classList.add('mood-cautious');



    } else {



        badge.classList.add('mood-neutral');



    }







    score.textContent = `${scoreValue.toFixed(1)} / 100`;







    const split = mood.breadth_signal_split || {};



    if (splitContainer) {



        splitContainer.innerHTML = renderMoodSplit(split);



    }







    if (explain) {



        const sigBias = Number((mood.signals && mood.signals.bias_pct) || 0);



        const breadth = Number((mood.breadth && mood.breadth.breadth_pct) || 0);



        const biasSign = sigBias >= 0 ? '+' : '';



        const breadthSign = breadth >= 0 ? '+' : '';



        explain.textContent = `Signal bias ${biasSign}${sigBias.toFixed(1)}%, breadth ${breadthSign}${breadth.toFixed(1)}%. Composite score weights breadth and signal bias separately.`;



    }



}







// Ensure banner stays accurate when users keep the tab open.



setInterval(updateMarketSessionBanner, 60000);











// ── Portfolio Tab ───────────────────────────────────────







async function loadPortfolio() {



    try {



        const [portRes, tradesRes] = await Promise.all([



            fetchWithTimeout(`${API}/api/portfolio`, {}, 25000),



            fetchWithTimeout(`${API}/api/trades?limit=50`, {}, 25000),



        ]);



        const portfolio = await parseJsonResponse(portRes);



        const trades = await parseJsonResponse(tradesRes);







        // Summary



        const openCountEl = document.getElementById('portOpenCount');



        if (openCountEl) {
            const missingQ = portfolio.missing_quotes_count || 0;
            if (missingQ > 0) {
                openCountEl.innerHTML = `${portfolio.total_open || 0} open <span style="color:#f59e0b;font-weight:700;">(⚠️ ${missingQ} Unquoted)</span>`;
            } else {
                openCountEl.textContent = `${portfolio.total_open || 0} open`;
            }
        }



        const ur = portfolio.total_unrealized_pnl || 0;



        const urEl = document.getElementById('portUnrealized');



        if (urEl) {



            urEl.textContent = `₹${ur.toFixed(2)} unrealized`;



            urEl.style.color = ur >= 0 ? 'var(--green)' : 'var(--red)';



        }







        // Risk KPIs



        const rm = portfolio.risk_metrics || {};



        const dd = portfolio.drawdown || {};



        const sharpeEl = document.getElementById('portSharpeVal');



        if (sharpeEl) sharpeEl.textContent = Number(rm.sharpe_ratio || 0).toFixed(2);



        const sortinoEl = document.getElementById('portSortinoVal');



        if (sortinoEl) sortinoEl.textContent = rm.sortino_ratio != null ? Number(rm.sortino_ratio).toFixed(2) : '—';



        const pfEl = document.getElementById('portProfitFactorVal');



        if (pfEl) pfEl.textContent = rm.profit_factor != null ? Number(rm.profit_factor).toFixed(2) : '—';







        // Drawdown



        const curDd = Number(dd.current_drawdown_pct || 0);



        const maxDd = Number(dd.max_drawdown_pct || 0);



        const curDdEl = document.getElementById('portCurrentDrawdownVal');



        if (curDdEl) curDdEl.textContent = `${curDd.toFixed(1)}%`;



        const maxDdEl = document.getElementById('portMaxDrawdownVal');



        if (maxDdEl) maxDdEl.textContent = `${maxDd.toFixed(1)}%`;



        const ddDurEl = document.getElementById('portDrawdownDurationVal');



        if (ddDurEl) ddDurEl.textContent = dd.duration_label || '0d';



        const ddBar = document.getElementById('portDrawdownBar');



        if (ddBar) {



            ddBar.style.width = `${Math.min(100, curDd)}%`;



            ddBar.style.background = curDd > 10 ? 'var(--red)' : curDd > 5 ? '#f59e0b' : 'var(--green)';



        }







        // Concentration warning



        const warnEl = document.getElementById('portConcentrationWarn');



        if (warnEl) {



            const warns = portfolio.concentration_warnings || [];



            if (warns.length > 0) {



                warnEl.style.display = 'block';



                warnEl.innerHTML = `⚠️ <b>Sector Concentration:</b> ${warns.join(' | ')}`;



            } else {



                warnEl.style.display = 'none';



            }



        }







        // Open positions table (All 10 columns accurately aligned)



        const tbody = document.getElementById('positionsBody');



        const openPos = portfolio.open_positions || [];
        window._cachedOpenPositions = openPos;



        if (tbody) {



            if (openPos.length === 0) {



                tbody.innerHTML = '<tr><td colspan="10" class="empty-row">No open positions</td></tr>';



            } else {



                tbody.innerHTML = openPos.map(p => {



                    const unpriced = p.pnl_status === 'UNPRICED' || p.priced === false;
                    const pnl = Number(p.unrealized_pnl || 0);



                    const pnlClass = unpriced ? '' : (pnl >= 0 ? 'pnl-positive' : 'pnl-negative');



                    const isLong = p.direction === 'BUY' || p.direction === 'LONG';



                    const sideClass = isLong ? 'badge-buy' : 'badge-sell';



                    const sideText = isLong ? 'LONG' : 'SHORT';



                    const currentPx = Number(p.current_price || p.entry_price || 0);



                    const rMult = Number(p.r_multiple || 0);



                    const rClass = rMult >= 0 ? 'pnl-positive' : 'pnl-negative';



                    const heat = Number(p.heat_contribution_pct || 0);



                    const timeInTrade = p.time_in_trade_human || '—';



                    const slDist = Number(p.sl_distance_pct || 0);







                    const currSym = _symCurr(p.symbol);
                    const pnlSign = pnl > 0 ? '+' : (pnl < 0 ? '-' : '');
                    return `
                        <tr onclick="openPositionDetail(${p.id})" style="cursor:pointer;" title="Click to view detailed position analytics">
                            <td>
                                <b>${p.symbol}</b>
                                ${p.is_entry_fallback ? `<span style="font-size: 0.72rem;color:#f59e0b;background:rgba(245,158,11,0.15);padding:1px 4px;border-radius:3px;margin-left:4px;" title="Unquoted entry reference price">⚠️ Ref</span>` : ''}
                            </td>
                            <td><span class="${sideClass}">${sideText}</span></td>
                            <td>${currSym}${Number(p.entry_price || 0).toFixed(2)}</td>
                            <td>${currSym}${currentPx.toFixed(2)}${p.is_entry_fallback ? ' <small style="color:#f59e0b;">(Ref)</small>' : ''}</td>
                            <td class="${pnlClass}">${unpriced
                                ? '<span style="color:var(--text-muted);" title="Entry-price reference — live quote unavailable">—</span>'
                                : `<b>${pnlSign}${currSym}${Math.abs(pnl).toFixed(2)}</b>`}</td>
                            <td class="${rClass}">${unpriced ? '—' : `${rMult.toFixed(2)}R`}</td>
                            <td>${heat.toFixed(1)}%</td>
                            <td>${timeInTrade}</td>
                            <td>${slDist.toFixed(1)}%</td>
                            <td>
                                <button class="btn-exit-trade" onclick="event.stopPropagation(); closePaperTrade(${p.id}, ${currentPx})" style="padding:2px 8px;font-size:0.7rem;background:rgba(239,68,68,0.15);border:1px solid var(--red);color:var(--red);border-radius:4px;cursor:pointer;">Exit</button>
                            </td>
                        </tr>
                    `;



                }).join('');



            }



        }







        // Trade history table (Newest First, Asset-Aware Currency, Complete 11 Columns)
        const hBody = document.getElementById('historyBody');
        const hCount = document.getElementById('historyTradeCount');
        const rawTrades = trades.trades || [];
        window._cachedTradeHistory = rawTrades;
        const closedTrades = rawTrades.filter(t => t.status !== 'OPEN');

        if (hCount) {
            hCount.textContent = `${closedTrades.length} trades`;
        }

        if (hBody) {
            if (closedTrades.length === 0) {
                hBody.innerHTML = '<tr><td colspan="11" class="empty-row">No trade history</td></tr>';
            } else {
                // Ensure newest trades come first (by exit_time / timestamp DESC, then ID DESC)
                const sortedTrades = [...closedTrades].sort((a, b) => {
                    const timeA = new Date(a.exit_time || a.timestamp || 0).getTime();
                    const timeB = new Date(b.exit_time || b.timestamp || 0).getTime();
                    if (timeA !== timeB) return timeB - timeA;
                    return (Number(b.id) || 0) - (Number(a.id) || 0);
                });

                hBody.innerHTML = sortedTrades.slice(0, 50).map(t => {
                    const qty = Number(t.initial_quantity || t.quantity || 1);
                    const pnlVal = Number(t.cost_profile && t.net_pnl != null ? t.net_pnl : (t.total_pnl != null ? t.total_pnl : ((t.pnl || 0) * qty)));
                    const pnlClass = pnlVal >= 0 ? 'pnl-positive' : 'pnl-negative';
                    const isLong = t.direction === 'BUY' || t.direction === 'LONG';
                    const sideClass = isLong ? 'badge-buy' : 'badge-sell';
                    const sideText = isLong ? 'LONG' : 'SHORT';
                    const currSym = _symCurr(t.symbol);
                    const qtyFormatted = Number.isInteger(qty) ? qty.toString() : qty.toFixed(qty < 1 ? 4 : 2);

                    const timeRaw = t.exit_time || t.timestamp;
                    const time = timeRaw ? new Date(timeRaw).toLocaleString('en-IN', {
                        day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
                    }) : '—';

                    const duration = t.duration_human || _fmtDuration(t.entry_time, t.exit_time);

                    const rawReason = t.exit_reason || t.status || '';
                    const reason = {
                        'CLOSED_TP': 'Target Hit',
                        'TARGET': 'Target Hit',
                        'CLOSED_SL': 'Stop Loss',
                        'STOP_LOSS': 'Stop Loss',
                        'CLOSED_TRAIL': 'Trailing SL',
                        'TRAILING_STOP': 'Trailing SL',
                        'CLOSED_MANUAL': 'Manual Exit',
                    }[rawReason] || rawReason || '—';

                    const pnlPct = Number(t.pnl_pct || 0);
                    const sign = pnlVal > 0 ? '+' : (pnlVal < 0 ? '-' : '');
                    const pctSign = pnlPct >= 0 ? '+' : '';
                    const signalBadge = t.signal_type ? `<div style="font-size: 0.72rem;color:var(--text-muted);letter-spacing:0.04em;">${t.signal_type}</div>` : '';

                    return `
                        <tr onclick="openPositionDetail(${t.id})" style="cursor:pointer;" title="Click to view complete trade analytics and execution details">
                            <td class="perf-td-id" style="font-family: var(--font-mono); color: var(--text-muted);">#${t.id || '—'}</td>
                            <td style="font-family: var(--font-body); white-space: nowrap;">${time}</td>
                            <td>
                                <b>${t.symbol}</b>
                                ${signalBadge}
                            </td>
                            <td><span class="${sideClass}">${sideText}</span></td>
                            <td style="font-family: var(--font-mono);">${qtyFormatted}</td>
                            <td style="font-family: var(--font-mono);">${currSym}${Number(t.entry_price || 0).toFixed(2)}</td>
                            <td style="font-family: var(--font-mono);">${currSym}${Number(t.exit_price || 0).toFixed(2)}</td>
                            <td class="${pnlClass}" style="font-family: var(--font-mono); font-weight: 600;">${sign}${currSym}${Math.abs(pnlVal).toFixed(2)}</td>
                            <td class="${pnlClass}" style="font-family: var(--font-mono);">${pctSign}${pnlPct.toFixed(2)}%</td>
                            <td style="font-family: var(--font-mono); color: var(--text-muted); font-size: 0.8rem;">${duration}</td>
                            <td style="font-family: var(--font-body);">${reason}</td>
                        </tr>
                    `;
                }).join('');
            }
        }
        } catch (err) {



        console.error('Portfolio load error:', err);



    }



}







// ── Manual Paper Trade Exit ───────────────────────────────



async function bulkClosePaperTrades(mode) {
    const labels = {
        intraday: 'all intraday equity positions',
        zombie: 'stale / zombie positions',
        all: 'ALL open positions',
    };
    const label = labels[mode] || mode;
    if (!confirm(`Close ${label} at the last known price? This cannot be undone.`)) return;

    try {
        const res = await fetchWithTimeout(`${API}/api/paper/close-bulk`, {
            method: 'POST',
            headers: _getAuthHeaders(),
            body: JSON.stringify({ mode, confirm: true }),
        }, 15000);

        if (res.status === 401 || res.status === 403) {
            _handleAuthChallenge(res.status);
            return;
        }

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Bulk close failed');

        alert(`Closed ${data.closed} of ${data.matched} position(s).\nP&L: ₹${Number(data.total_pnl || 0).toFixed(2)}`);
        await loadAllData();
    } catch (err) {
        alert('Bulk close failed: ' + err.message);
    }
}
window.bulkClosePaperTrades = bulkClosePaperTrades;

async function closePaperTrade(tradeId, currentPrice) {



    if (!confirm(`Are you sure you want to close Trade #${tradeId}?`)) return;



    try {



        const res = await fetchWithTimeout(`${API}/api/paper/close/${tradeId}`, {



            method: 'POST',



            headers: _getAuthHeaders(),



            body: JSON.stringify({ exit_price: Number(currentPrice || 0) }),



        }, 10000);



        if (res.status === 401 || res.status === 403) {



            _handleAuthChallenge(res.status);



            return;



        }



        const data = await parseJsonResponse(res);



        if (data.status === 'closed') {



            const pnl = data.trade?.total_pnl ?? data.trade?.pnl ?? 0;



            showToast(`✅ Closed trade #${tradeId} @ ₹${Number(data.trade?.exit_price || currentPrice).toFixed(1)} (PnL: ₹${Number(pnl).toFixed(2)})`, 'success');



            if (typeof loadPortfolio === 'function') loadPortfolio();



            if (typeof loadPerformance === 'function') loadPerformance();



            if (data.stats && typeof updateStatsStrip === 'function') updateStatsStrip(data.stats);
            refreshPaperPositions();



        } else {



            showToast(`⚠️ Could not close trade: ${data.error || 'Unknown error'}`, 'warn');



        }



    } catch (err) {



        console.error('closePaperTrade error:', err);



        showToast(`❌ Failed to close trade: ${err.message}`, 'error');



    }



}



window.closePaperTrade = closePaperTrade;







// ── Learning Tab ────────────────────────────────────────







async function loadLearningStats() {



    try {



        const [statsRes, outcomesRes] = await Promise.all([



            fetchWithTimeout(`${API}/api/learning/stats`, {}, 20000),



            fetchWithTimeout(`${API}/api/learning/outcomes?limit=50`, {}, 20000),



        ]);



        const data = await parseJsonResponse(statsRes);



        const outcomesData = await parseJsonResponse(outcomesRes);







        // Populate KPIs



        const o = data.insights?.overall || {};



        document.getElementById('lsTotalTradesVal').textContent = o.total_trades || 0;



        document.getElementById('lsAccuracyVal').textContent = ((o.model_accuracy || 0).toFixed(1)) + '%';



        document.getElementById('lsBestSignalVal').textContent = data.insights?.best_signal_type || '—';



        document.getElementById('lsWorstSignalVal').textContent = data.insights?.worst_signal_type || '—';



        document.getElementById('lsBestRegimeVal').textContent = data.insights?.best_regime || '—';

        // Populate Learning Health & Thresholds
        const h = data.health || {};
        const reqTrades = h.canonical_sample_requirement || 30;
        const totalClosed = o.total_trades || 0;
        const badgeEl = document.getElementById('lsHealthStatusBadge');
        if (badgeEl) {
            const stateStr = (h.state || 'COLD_START').replace(/_/g, ' ');
            badgeEl.textContent = `${stateStr} (${totalClosed}/${reqTrades} trades)`;
            badgeEl.style.color = totalClosed >= reqTrades ? 'var(--green)' : 'var(--yellow)';
            badgeEl.style.borderColor = totalClosed >= reqTrades ? 'var(--green)' : 'var(--border-color)';
        }
        const dataStatusEl = document.getElementById('lsHealthDataStatus');
        if (dataStatusEl) {
            dataStatusEl.textContent = totalClosed >= reqTrades ? 'Sample Ready (Adaptive Active)' : `Building sample (${totalClosed}/${reqTrades} trades)`;
        }
        const nextUpEl = document.getElementById('lsHealthNextUpdate');
        if (nextUpEl) {
            const rem = h.trades_until_recalibration != null ? h.trades_until_recalibration : Math.max(0, reqTrades - totalClosed);
            nextUpEl.textContent = `${rem} trades remaining`;
        }
        const learnUpEl = document.getElementById('learningUpdated');
        if (learnUpEl) {
            learnUpEl.textContent = `Updating every ${h.analyse_every_n_trades || reqTrades} trades`;
        }
        const lastInEl = document.getElementById('lsHealthLastInsight');
        if (lastInEl) {
            lastInEl.textContent = data.insights?.insights_at ? new Date(data.insights.insights_at).toLocaleTimeString() : 'Live';
        }
        const lastWtEl = document.getElementById('lsHealthLastWeightUpdate');
        if (lastWtEl) {
            lastWtEl.textContent = data.weights?._updated_at ? new Date(data.weights._updated_at).toLocaleTimeString() : 'Default Priors';
        }







        // Signal Win Rate Bars



        const sb = document.getElementById('lsSignalBars');



        const sstats = data.insights?.signal_type_stats || {};



        if (Object.keys(sstats).length === 0) {



            sb.innerHTML = '<div class="loading-state" style="padding:.5rem 0"><p style="font-size:.75rem">No learning data</p></div>';



        } else {



            sb.innerHTML = Object.entries(sstats).sort((a,b)=> b[1].win_rate - a[1].win_rate).map(([sym, st]) => {



                const w = st.win_rate;



                const c = w >= 50 ? 'var(--green)' : 'var(--red)';



                return `



                    <div class="perf-sym-bar-wrap">



                        <div class="perf-sym-label">



                            <span class="psym">${sym}</span>



                            <span class="pnl" style="color:${c}">${w.toFixed(1)}% WR</span>



                        </div>



                        <div class="perf-sym-track">



                            <div class="perf-sym-fill" style="width:${w}%;background:${c}"></div>



                        </div>



                        <div class="perf-sym-count" style="font-size:0.75rem; color:var(--text-muted); display:flex; justify-content:space-between">



                            <span>${st.trades} trades</span>



                            <span>Avg ${st.avg_pnl.toFixed(2)}%</span>



                        </div>



                    </div>



                `;



            }).join('');



        }







        // Auto-Adjusted Weights



        // Confidence Calibration (F10)
        const calibBadge = document.getElementById('lsCalibStatusBadge');
        const calibBandsEl = document.getElementById('lsConfidenceBands');
        const calibHint = document.getElementById('lsCalibHint');

        const calib = data.insights?.confidence_calibration || {};
        const bandStats = data.insights?.confidence_band_stats || {};

        if (calibBadge) {
            const status = calib.status || 'NO_DATA';
            calibBadge.textContent = status.replace(/_/g, ' ');
            if (status === 'CALIBRATED') {
                calibBadge.style.color = 'var(--green, #10b981)';
                calibBadge.style.borderColor = 'rgba(16,185,129,0.4)';
            } else if (status === 'MISALIGNED') {
                calibBadge.style.color = 'var(--red, #ef4444)';
                calibBadge.style.borderColor = 'rgba(239,68,68,0.4)';
            } else if (status === 'WEAK_CALIBRATION') {
                calibBadge.style.color = 'var(--yellow, #f59e0b)';
                calibBadge.style.borderColor = 'rgba(245,158,11,0.4)';
            } else {
                calibBadge.style.color = 'var(--text-muted)';
                calibBadge.style.borderColor = 'var(--border-color)';
            }
        }

        if (calibBandsEl) {
            const entries = Object.entries(bandStats);
            if (entries.length === 0) {
                calibBandsEl.innerHTML = '<div class="loading-state" style="padding:.5rem 0"><p style="font-size:.75rem">No calibration data</p></div>';
            } else {
                calibBandsEl.innerHTML = entries.map(([band, bst]) => {
                    const wr = Number(bst.win_rate || 0);
                    const trades = bst.trades || 0;
                    const c = wr >= 50 ? 'var(--green, #10b981)' : (trades === 0 ? 'var(--text-muted)' : 'var(--red, #ef4444)');
                    return `
                        <div class="perf-sym-bar-wrap" style="margin-bottom:0.6rem;">
                            <div class="perf-sym-label" style="display:flex;justify-content:space-between;font-size:0.75rem;">
                                <span class="psym" style="font-weight:600;">Conf ${band}%</span>
                                <span class="pnl" style="color:${c};font-weight:700;">${trades > 0 ? `${wr.toFixed(1)}% WR` : 'No Trades'}</span>
                            </div>
                            <div class="perf-sym-track" style="height:6px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;margin:3px 0;">
                                <div class="perf-sym-fill" style="width:${trades > 0 ? wr : 0}%;background:${c};height:100%;border-radius:3px;"></div>
                            </div>
                            <div style="font-size: 0.72rem;color:var(--text-muted);display:flex;justify-content:space-between;">
                                <span>${trades} sample trade(s)</span>
                                <span>Avg ${Number(bst.avg_pnl || 0).toFixed(2)}%</span>
                            </div>
                        </div>
                    `;
                }).join('');
            }
        }

        if (calibHint) {
            const status = calib.status;
            const spread = calib.high_minus_low != null ? `${calib.high_minus_low > 0 ? '+' : ''}${calib.high_minus_low}%` : '';
            if (status === 'MISALIGNED') {
                calibHint.innerHTML = `<span style="color:#ef4444;font-weight:600;">⚠️ Misaligned Calibration (spread ${spread}):</span> Lower confidence trades currently outperform higher confidence bands. Sample size caveat applies (${totalClosed}/${reqTrades} trades); model weights adjust adaptively.`;
            } else if (status === 'CALIBRATED') {
                calibHint.innerHTML = `<span style="color:#10b981;font-weight:600;">✅ Well-Calibrated (spread ${spread}):</span> Higher confidence bands correspond to higher realized win rate.`;
            } else if (status === 'WEAK_CALIBRATION') {
                calibHint.innerHTML = `<span style="color:#f59e0b;font-weight:600;">⚠️ Weak Calibration (spread ${spread}):</span> Win rates are positive across bands but spread is under 5%. Continuing sample accumulation.`;
            } else {
                calibHint.textContent = 'Higher confidence bands should show higher realized win rate. Minimum sample required for statistical validation.';
            }
        }

        const wl = document.getElementById('lsWeightsList');



        const weights = data.weights || {};



        if (Object.keys(weights).length === 0) {



            wl.innerHTML = '<div class="loading-state" style="padding:.5rem 0"><p style="font-size:.75rem">No weight data</p></div>';



        } else {



            wl.innerHTML = Object.entries(weights)



                .filter(([k]) => !k.startsWith('_'))



                .sort((a,b)=> b[1] - a[1])



                .map(([sw, w]) => {



                    const diff = w - 1.0;



                    const c = diff > 0 ? 'color:var(--green)' : (diff < 0 ? 'color:var(--red)' : 'color:var(--text-muted)');



                    return `



                    <div class="perf-exit-item">



                        <span class="pe-reason" style="width: 130px">${sw}</span>



                        <div class="pe-track"><div class="pe-fill" style="width:${Math.min(w * 40, 100)}%;"></div></div>



                        <span class="pe-count" style="${c}">${w.toFixed(2)}x</span>



                    </div>



                    `;



            }).join('');



        }







        // Outcomes Table



        const tb = document.getElementById('learningOutcomesBody');



        const outcomes = outcomesData.outcomes || [];



        if (!outcomes.length) {



            tb.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:1.5rem;color:var(--text-muted)">No learning trades yet</td></tr>';



        } else {



            tb.innerHTML = outcomes.map(t => {



                const pnlClass = t.pnl_pct >= 0 ? 'positive' : 'negative';



                return `



                    <tr>



                        <td style="font-family: var(--font-body)">#${t.id}</td>



                        <td style="font-weight:600">${t.symbol}</td>



                        <td>${t.signal_type}</td>



                        <td>${t.regime}</td>



                        <td>${t.rsi_at_entry ? t.rsi_at_entry.toFixed(1) : '—'}</td>



                        <td>${t.volume_above_avg ? 'YES' : 'NO'}</td>



                        <td>${(t.confidence || 0).toFixed(0)}%</td>



                        <td class="${pnlClass}">${(t.pnl_pct || 0).toFixed(2)}%</td>



                        <td class="${pnlClass}">${t.result}</td>



                    </tr>



                `;



            }).join('');



        }



    } catch (err) {



        console.error('Learning stats load error:', err);



    }



}











// ── Performance Tab ─────────────────────────────────────







async function loadPerformance() {



    try {



        const res = await fetchWithTimeout(`${API}/api/performance`, {}, 25000);



        const data = await parseJsonResponse(res);



        const m = data.metrics || {};







        // ── KPI cards ──────────────────────────────────



        const totalPnl = m.total_pnl || 0;



        const ret = m.total_return_pct || 0;



        const sharpe = m.sharpe_ratio || 0;



        const dd = m.max_drawdown_pct || 0;



        const pf = m.profit_factor || 0;







        const hasTrades = Number(m.total_trades || 0) > 0;



        const cap = m.capital != null ? m.capital : m.initial_capital;



        _setKpi('pkCapitalVal', cap != null ? `₹${Math.round(cap).toLocaleString('en-IN')}` : '—');



        _setKpi('pkPnlVal',     hasTrades ? `${totalPnl < 0 ? '-' : (totalPnl > 0 ? '+' : '')}₹${Math.abs(Math.round(totalPnl)).toLocaleString('en-IN')}` : '—', hasTrades ? (totalPnl >= 0 ? 'positive' : 'negative') : 'neutral');



        _setKpi('pkReturnVal',  hasTrades ? `${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%` : '—', hasTrades ? (ret >= 0 ? 'positive' : 'negative') : 'neutral');



        _setKpi('pkWinRateVal', (hasTrades && m.win_rate != null) ? `${Number(m.win_rate).toFixed(1)}%` : '—', hasTrades ? ((m.win_rate||0) >= 50 ? 'positive' : 'negative') : 'neutral');



        _setKpi('pkTradesVal',  String(m.total_trades || 0));



        _setKpi('pkSharpeVal',  hasTrades ? sharpe.toFixed(2) : '—', hasTrades ? (sharpe >= 1 ? 'positive' : sharpe >= 0 ? 'neutral' : 'negative') : 'neutral');



        _setKpi('pkDrawdownVal',hasTrades ? `${dd.toFixed(1)}%` : '—', hasTrades ? (dd > 20 ? 'negative' : dd > 10 ? 'neutral' : 'positive') : 'neutral');



        _setKpi('pkPFVal',      hasTrades ? pf.toFixed(2) : '—', hasTrades ? (pf >= 1.5 ? 'positive' : pf >= 1 ? 'neutral' : 'negative') : 'neutral');



        _setKpi('pkBestVal',    (hasTrades && m.best_trade != null) ? `+₹${Number(m.best_trade).toFixed(0)}` : '—', 'positive');



        _setKpi('pkWorstVal',   (hasTrades && m.worst_trade != null) ? `${Number(m.worst_trade) < 0 ? '-' : ''}₹${Math.abs(Number(m.worst_trade)).toFixed(0)}` : '—', 'negative');







        // ── Return pill ────────────────────────────────



        const pill = document.getElementById('perfReturnPill');



        if (pill) {



            pill.textContent = `${ret >= 0 ? '+' : ''}${ret.toFixed(2)}% Total Return`;



            pill.className = 'perf-return-pill ' + (ret >= 0 ? 'positive' : 'negative');



        }







        // ── Equity curve ───────────────────────────────



        drawEquityCurve(data.equity_curve || []);







        // ── Win/Loss bar ───────────────────────────────



        const dist = data.trade_distribution || {};
        const wins = dist.wins || 0;
        const losses = dist.losses || 0;
        const breakevens = dist.breakevens || 0;
        const total = wins + losses + breakevens || 1;
        const wPct = (wins / total * 100).toFixed(1);
        const lPct = (losses / total * 100).toFixed(1);
        const bePct = (breakevens / total * 100).toFixed(1);

        document.getElementById('winBar').style.width   = `${wPct}%`;
        const beBarEl = document.getElementById('beBar');
        if (beBarEl) beBarEl.style.width = `${bePct}%`;
        document.getElementById('lossBar').style.width  = `${lPct}%`;

        document.getElementById('winLabel').textContent  = `${wins} wins`;
        const beLabelEl = document.getElementById('beLabel');
        if (beLabelEl) beLabelEl.textContent = `${breakevens} BE`;
        document.getElementById('lossLabel').textContent = `${losses} losses`;

        const wpEl = document.getElementById('perfWinPct');
        if (wpEl) wpEl.textContent = `${wPct}%`;



        document.getElementById('metAvgWin').textContent  = `₹${(m.avg_win  || 0).toFixed(0)}`;



        document.getElementById('metAvgLoss').textContent = `${Number(m.avg_loss || 0) < 0 ? '-' : ''}₹${Math.abs(Number(m.avg_loss || 0)).toFixed(0)}`;







        // ── Exit reasons ───────────────────────────────



        _renderExitReasons(data.exit_reasons || []);







        // ── Monthly P&L ────────────────────────────────



        _renderMonthlyPnl(data.monthly_pnl || []);







        // ── Symbol breakdown ───────────────────────────



        _renderSymbolBreakdown(data.symbol_breakdown || []);







        // ── Trade history ──────────────────────────────



        _renderTradeHistory(data.recent_trades || []);







    } catch (err) {



        console.error('Performance load error:', err);



    }



}







function _setKpi(id, text, cls) {



    const el = document.getElementById(id);



    if (!el) return;



    el.textContent = text;



    el.className = 'pkv' + (cls ? ' ' + cls : '');



}







function _renderExitReasons(reasons) {



    const el = document.getElementById('perfExitList');



    if (!el) return;



    if (!reasons.length) { el.innerHTML = '<p style="font-size:.75rem;color:var(--text-muted)">No exits yet</p>'; return; }







    const total = reasons.reduce((s, r) => s + r.count, 0) || 1;



    const iconMap = { CLOSED_TP: 'TP', CLOSED_SL: 'SL', CLOSED_TRAIL: 'Trail', CLOSED_MANUAL: 'Manual', UNKNOWN: 'Other' };



    const labelMap = { CLOSED_TP: 'Take Profit', CLOSED_SL: 'Stop Loss', CLOSED_TRAIL: 'Trailing SL', CLOSED_MANUAL: 'Manual', UNKNOWN: 'Other' };







    el.innerHTML = reasons.map(r => {



        const pct = (r.count / total * 100).toFixed(0);



        const cls = r.reason === 'CLOSED_TP' ? 'positive' : r.reason === 'CLOSED_SL' ? 'negative' : 'neutral';



        return `<div class="perf-exit-row">



            <span class="perf-exit-icon">${iconMap[r.reason] || '❓'}</span>



            <span class="perf-exit-label">${labelMap[r.reason] || r.reason}</span>



            <div class="perf-exit-bar-wrap"><div class="perf-exit-bar ${cls}" style="width:${pct}%"></div></div>



            <span class="perf-exit-count">${r.count}</span>



        </div>`;



    }).join('');



}







// ── Morning Shortlist & View Toggle ──────────────────────



function setSwingViewMode(mode) {



    const btnShortlist = document.getElementById('viewToggleShortlist');



    const btnAll = document.getElementById('viewToggleAll');



    const panel = document.getElementById('morningShortlistPanel');







    if (mode === 'shortlist') {



        if (btnShortlist) { btnShortlist.classList.add('active'); btnShortlist.style.background = ''; btnShortlist.style.color = ''; }



        if (btnAll) { btnAll.classList.remove('active'); btnAll.style.background = 'transparent'; btnAll.style.color = 'var(--text-muted)'; }



        if (panel) panel.style.display = 'block';



    } else {



        if (btnAll) { btnAll.classList.add('active'); btnAll.style.background = ''; btnAll.style.color = ''; }



        if (btnShortlist) { btnShortlist.classList.remove('active'); btnShortlist.style.background = 'transparent'; btnShortlist.style.color = 'var(--text-muted)'; }



        if (panel) panel.style.display = 'none';



    }



}







async function loadMorningShortlist() {



    const grid = document.getElementById('shortlistGrid');



    const tsEl = document.getElementById('shortlistTimestamp');



    if (!grid) return;



    grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Loading morning shortlist...</p></div>';







    try {



        const res = await fetchWithTimeout(`${API}/api/swing/shortlist`, {}, 15000);



        let data = {};



        try { data = await res.json(); } catch (_) { data = {}; }



        const setups = data.setups || [];



        if (tsEl) {



            tsEl.textContent = data.generated_at ? `Scan: ${new Date(data.generated_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}` : (data.scan_date || '');



        }



        if (!setups.length) {



            grid.innerHTML = `<div class="loading-state" style="padding:1.5rem;"><p style="font-size:0.85rem;color:var(--text-muted);">${data.message || 'No shortlist setups available yet.'}</p></div>`;



            return;



        }



        renderMorningShortlist(setups);



    } catch (err) {



        console.error('loadMorningShortlist error:', err);



        grid.innerHTML = '<div class="loading-state"><p style="color:var(--red);">Failed to load morning shortlist</p></div>';



    }



}







function renderMorningShortlist(setups) {



    const grid = document.getElementById('shortlistGrid');



    if (!grid) return;



    grid.innerHTML = setups.map(s => {



        const dirClass = s.direction === 'LONG' ? 'buy' : 'sell';



        const dirColor = s.direction === 'LONG' ? 'var(--green, #10b981)' : 'var(--red, #ef4444)';



        const gradeColor = s.grade === 'A' ? '#10b981' : s.grade === 'B' ? '#6366f1' : '#f59e0b';



        const t1 = s.target_1 || s.target || '—';



        const t2 = s.target_2 || '—';



        return `



        <div class="card" style="padding:1rem;border-top:2px solid ${dirColor};background:rgba(255,255,255,0.02);border-radius:8px;">



            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.6rem;">



                <div>



                    <div style="display:flex;align-items:center;gap:0.5rem;">



                        <span style="font-weight:800;font-size:1.05rem;color:var(--text-primary);">${s.symbol}</span>



                        <span class="signal-badge ${dirClass}" style="font-size: 0.72rem;padding:2px 6px;">${s.direction}</span>



                        <span style="font-size: 0.70rem;font-weight:700;padding:1px 6px;border-radius:4px;background:${gradeColor}20;color:${gradeColor};border:1px solid ${gradeColor}40;">Grade ${s.grade || 'A'}</span>



                    </div>



                    <div style="font-size:0.7rem;color:var(--text-muted);margin-top:2px;">${s.pattern || 'Swing Setup'}</div>



                </div>



                <div style="text-align:right;">



                    <div style="font-size: 0.70rem;color:var(--text-muted);" title="Confluence Score (0-100) — not a win guarantee">Setup Strength</div>



                    <div style="font-weight:800;font-size:1.1rem;color:${gradeColor};font-family:var(--font-mono);">${s.strength || s.score || 0}</div>



                </div>



            </div>



            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:0.4rem;text-align:center;margin:0.5rem 0;background:rgba(0,0,0,0.15);padding:0.4rem;border-radius:6px;">



                <div><div style="font-size:0.6rem;color:var(--text-muted);">Entry</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;">₹${s.entry_zone || s.entry || 0}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">SL</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--red);">₹${s.stop_loss || 0}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">T1</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--green);">₹${t1}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">T2</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--green);">₹${t2}</div></div>



            </div>



            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:0.6rem;">



                <span style="font-size: 0.72rem;color:var(--text-muted);">R:R <b style="color:var(--text-primary);">${s.risk_reward || 0}x</b></span>



                <button type="button" class="btn btn-primary" onclick="event.stopPropagation();takePaperTrade('${_escHtml(s.symbol)}', '${s.direction}', ${Number(s.entry_zone || s.entry || 0)}, ${Number(s.stop_loss || 0)}, ${Number(s.target_1 || s.target || 0)}, ${Number(s.atr || 0)}, 'SWING', this)" style="font-size:0.7rem;padding:0.25rem 0.6rem;cursor:pointer;border-radius:4px;background:linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%);color:#fff;border:none;font-weight:600;">⚡ Paper Trade</button>



            </div>



        </div>`;



    }).join('');



}







window.setSwingViewMode = setSwingViewMode;



window.loadMorningShortlist = loadMorningShortlist;











function _renderMonthlyPnl(months) {



    const el = document.getElementById('perfMonthlyGrid');



    if (!el) return;



    if (!months.length) { el.innerHTML = '<p style="font-size:.75rem;color:var(--text-muted)">No monthly data yet</p>'; return; }







    const maxAbs = Math.max(...months.map(m => Math.abs(m.pnl)), 1);



    const mNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];







    el.innerHTML = months.map(m => {



        const [yr, mo] = m.month.split('-');



        const label = `${mNames[parseInt(mo, 10) - 1]} '${yr.slice(2)}`;



        const intPct = Math.round(Math.abs(m.pnl) / maxAbs * 100);



        const cls = m.pnl >= 0 ? 'positive' : 'negative';



        const sign = m.pnl > 0 ? '+' : (m.pnl < 0 ? '-' : '');



        return `<div class="perf-month-cell ${cls}" title="${label}: ${sign}₹${Math.abs(m.pnl).toLocaleString('en-IN')}" style="--intensity:${intPct}%">



            <span class="month-label">${label}</span>



            <span class="month-pnl">${sign}₹${Math.abs(m.pnl) >= 1000 ? (Math.abs(m.pnl)/1000).toFixed(1)+'K' : Math.abs(m.pnl).toFixed(0)}</span>



        </div>`;



    }).join('');



}







// ── Morning Shortlist & View Toggle ──────────────────────



function setSwingViewMode(mode) {



    const btnShortlist = document.getElementById('viewToggleShortlist');



    const btnAll = document.getElementById('viewToggleAll');



    const panel = document.getElementById('morningShortlistPanel');







    if (mode === 'shortlist') {



        if (btnShortlist) { btnShortlist.classList.add('active'); btnShortlist.style.background = ''; btnShortlist.style.color = ''; }



        if (btnAll) { btnAll.classList.remove('active'); btnAll.style.background = 'transparent'; btnAll.style.color = 'var(--text-muted)'; }



        if (panel) panel.style.display = 'block';



    } else {



        if (btnAll) { btnAll.classList.add('active'); btnAll.style.background = ''; btnAll.style.color = ''; }



        if (btnShortlist) { btnShortlist.classList.remove('active'); btnShortlist.style.background = 'transparent'; btnShortlist.style.color = 'var(--text-muted)'; }



        if (panel) panel.style.display = 'none';



    }



}







async function loadMorningShortlist() {



    const grid = document.getElementById('shortlistGrid');



    const tsEl = document.getElementById('shortlistTimestamp');



    if (!grid) return;



    grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Loading morning shortlist...</p></div>';







    try {



        const res = await fetchWithTimeout(`${API}/api/swing/shortlist`, {}, 15000);



        let data = {};



        try { data = await res.json(); } catch (_) { data = {}; }



        const setups = data.setups || [];



        if (tsEl) {



            tsEl.textContent = data.generated_at ? `Scan: ${new Date(data.generated_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}` : (data.scan_date || '');



        }



        if (!setups.length) {



            grid.innerHTML = `<div class="loading-state" style="padding:1.5rem;"><p style="font-size:0.85rem;color:var(--text-muted);">${data.message || 'No shortlist setups available yet.'}</p></div>`;



            return;



        }



        renderMorningShortlist(setups);



    } catch (err) {



        console.error('loadMorningShortlist error:', err);



        grid.innerHTML = '<div class="loading-state"><p style="color:var(--red);">Failed to load morning shortlist</p></div>';



    }



}







function renderMorningShortlist(setups) {



    const grid = document.getElementById('shortlistGrid');



    if (!grid) return;



    grid.innerHTML = setups.map(s => {



        const dirClass = s.direction === 'LONG' ? 'buy' : 'sell';



        const dirColor = s.direction === 'LONG' ? 'var(--green, #10b981)' : 'var(--red, #ef4444)';



        const gradeColor = s.grade === 'A' ? '#10b981' : s.grade === 'B' ? '#6366f1' : '#f59e0b';



        const t1 = s.target_1 || s.target || '—';



        const t2 = s.target_2 || '—';



        return `



        <div class="card" style="padding:1rem;border-top:2px solid ${dirColor};background:rgba(255,255,255,0.02);border-radius:8px;">



            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.6rem;">



                <div>



                    <div style="display:flex;align-items:center;gap:0.5rem;">



                        <span style="font-weight:800;font-size:1.05rem;color:var(--text-primary);">${s.symbol}</span>



                        <span class="signal-badge ${dirClass}" style="font-size: 0.72rem;padding:2px 6px;">${s.direction}</span>



                        <span style="font-size: 0.70rem;font-weight:700;padding:1px 6px;border-radius:4px;background:${gradeColor}20;color:${gradeColor};border:1px solid ${gradeColor}40;">Grade ${s.grade || 'A'}</span>



                    </div>



                    <div style="font-size:0.7rem;color:var(--text-muted);margin-top:2px;">${s.pattern || 'Swing Setup'}</div>



                </div>



                <div style="text-align:right;">



                    <div style="font-size: 0.70rem;color:var(--text-muted);" title="Confluence Score (0-100) — not a win guarantee">Setup Strength</div>



                    <div style="font-weight:800;font-size:1.1rem;color:${gradeColor};font-family:var(--font-mono);">${s.strength || s.score || 0}</div>



                </div>



            </div>



            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:0.4rem;text-align:center;margin:0.5rem 0;background:rgba(0,0,0,0.15);padding:0.4rem;border-radius:6px;">



                <div><div style="font-size:0.6rem;color:var(--text-muted);">Entry</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;">₹${s.entry_zone || s.entry || 0}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">SL</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--red);">₹${s.stop_loss || 0}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">T1</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--green);">₹${t1}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">T2</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--green);">₹${t2}</div></div>



            </div>



            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:0.6rem;">



                <span style="font-size: 0.72rem;color:var(--text-muted);">R:R <b style="color:var(--text-primary);">${s.risk_reward || 0}x</b></span>



                <button type="button" class="btn btn-primary" onclick="event.stopPropagation();takePaperTrade('${_escHtml(s.symbol)}', '${s.direction}', ${Number(s.entry_zone || s.entry || 0)}, ${Number(s.stop_loss || 0)}, ${Number(s.target_1 || s.target || 0)}, ${Number(s.atr || 0)}, 'SWING', this)" style="font-size:0.7rem;padding:0.25rem 0.6rem;cursor:pointer;border-radius:4px;background:linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%);color:#fff;border:none;font-weight:600;">⚡ Paper Trade</button>



            </div>



        </div>`;



    }).join('');



}







window.setSwingViewMode = setSwingViewMode;



window.loadMorningShortlist = loadMorningShortlist;











function _renderSymbolBreakdown(symbols) {



    const el = document.getElementById('perfSymbolBars');



    if (!el) return;



    if (!symbols.length) { el.innerHTML = '<p style="font-size:.75rem;color:var(--text-muted)">No symbol data yet</p>'; return; }







    const maxAbs = Math.max(...symbols.map(s => Math.abs(s.pnl)), 1);







    el.innerHTML = symbols.map(s => {



        const pct = Math.round(Math.abs(s.pnl) / maxAbs * 100);



        const cls = s.pnl >= 0 ? 'positive' : 'negative';



        const sign = s.pnl > 0 ? '+' : (s.pnl < 0 ? '-' : '');



        return `<div class="perf-sym-row">



            <span class="perf-sym-name">${s.symbol}</span>



            <div class="perf-sym-bar-wrap"><div class="perf-sym-bar ${cls}" style="width:${pct}%"></div></div>



            <span class="perf-sym-pnl ${cls}">${sign}₹${Math.abs(s.pnl).toFixed(0)}</span>



            <span class="perf-sym-trades">${s.trades}T</span>



        </div>`;



    }).join('');



}







// ── Morning Shortlist & View Toggle ──────────────────────



function setSwingViewMode(mode) {



    const btnShortlist = document.getElementById('viewToggleShortlist');



    const btnAll = document.getElementById('viewToggleAll');



    const panel = document.getElementById('morningShortlistPanel');







    if (mode === 'shortlist') {



        if (btnShortlist) { btnShortlist.classList.add('active'); btnShortlist.style.background = ''; btnShortlist.style.color = ''; }



        if (btnAll) { btnAll.classList.remove('active'); btnAll.style.background = 'transparent'; btnAll.style.color = 'var(--text-muted)'; }



        if (panel) panel.style.display = 'block';



    } else {



        if (btnAll) { btnAll.classList.add('active'); btnAll.style.background = ''; btnAll.style.color = ''; }



        if (btnShortlist) { btnShortlist.classList.remove('active'); btnShortlist.style.background = 'transparent'; btnShortlist.style.color = 'var(--text-muted)'; }



        if (panel) panel.style.display = 'none';



    }



}







async function loadMorningShortlist() {



    const grid = document.getElementById('shortlistGrid');



    const tsEl = document.getElementById('shortlistTimestamp');



    if (!grid) return;



    grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Loading morning shortlist...</p></div>';







    try {



        const res = await fetchWithTimeout(`${API}/api/swing/shortlist`, {}, 15000);



        let data = {};



        try { data = await res.json(); } catch (_) { data = {}; }



        const setups = data.setups || [];



        if (tsEl) {



            tsEl.textContent = data.generated_at ? `Scan: ${new Date(data.generated_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}` : (data.scan_date || '');



        }



        if (!setups.length) {



            grid.innerHTML = `<div class="loading-state" style="padding:1.5rem;"><p style="font-size:0.85rem;color:var(--text-muted);">${data.message || 'No shortlist setups available yet.'}</p></div>`;



            return;



        }



        renderMorningShortlist(setups);



    } catch (err) {



        console.error('loadMorningShortlist error:', err);



        grid.innerHTML = '<div class="loading-state"><p style="color:var(--red);">Failed to load morning shortlist</p></div>';



    }



}







function renderMorningShortlist(setups) {



    const grid = document.getElementById('shortlistGrid');



    if (!grid) return;



    grid.innerHTML = setups.map(s => {



        const dirClass = s.direction === 'LONG' ? 'buy' : 'sell';



        const dirColor = s.direction === 'LONG' ? 'var(--green, #10b981)' : 'var(--red, #ef4444)';



        const gradeColor = s.grade === 'A' ? '#10b981' : s.grade === 'B' ? '#6366f1' : '#f59e0b';



        const t1 = s.target_1 || s.target || '—';



        const t2 = s.target_2 || '—';



        return `



        <div class="card" style="padding:1rem;border-top:2px solid ${dirColor};background:rgba(255,255,255,0.02);border-radius:8px;">



            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.6rem;">



                <div>



                    <div style="display:flex;align-items:center;gap:0.5rem;">



                        <span style="font-weight:800;font-size:1.05rem;color:var(--text-primary);">${s.symbol}</span>



                        <span class="signal-badge ${dirClass}" style="font-size: 0.72rem;padding:2px 6px;">${s.direction}</span>



                        <span style="font-size: 0.70rem;font-weight:700;padding:1px 6px;border-radius:4px;background:${gradeColor}20;color:${gradeColor};border:1px solid ${gradeColor}40;">Grade ${s.grade || 'A'}</span>



                    </div>



                    <div style="font-size:0.7rem;color:var(--text-muted);margin-top:2px;">${s.pattern || 'Swing Setup'}</div>



                </div>



                <div style="text-align:right;">



                    <div style="font-size: 0.70rem;color:var(--text-muted);" title="Confluence Score (0-100) — not a win guarantee">Setup Strength</div>



                    <div style="font-weight:800;font-size:1.1rem;color:${gradeColor};font-family:var(--font-mono);">${s.strength || s.score || 0}</div>



                </div>



            </div>



            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:0.4rem;text-align:center;margin:0.5rem 0;background:rgba(0,0,0,0.15);padding:0.4rem;border-radius:6px;">



                <div><div style="font-size:0.6rem;color:var(--text-muted);">Entry</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;">₹${s.entry_zone || s.entry || 0}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">SL</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--red);">₹${s.stop_loss || 0}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">T1</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--green);">₹${t1}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">T2</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--green);">₹${t2}</div></div>



            </div>



            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:0.6rem;">



                <span style="font-size: 0.72rem;color:var(--text-muted);">R:R <b style="color:var(--text-primary);">${s.risk_reward || 0}x</b></span>



                <button type="button" class="btn btn-primary" onclick="event.stopPropagation();takePaperTrade('${_escHtml(s.symbol)}', '${s.direction}', ${Number(s.entry_zone || s.entry || 0)}, ${Number(s.stop_loss || 0)}, ${Number(s.target_1 || s.target || 0)}, ${Number(s.atr || 0)}, 'SWING', this)" style="font-size:0.7rem;padding:0.25rem 0.6rem;cursor:pointer;border-radius:4px;background:linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%);color:#fff;border:none;font-weight:600;">⚡ Paper Trade</button>



            </div>



        </div>`;



    }).join('');



}







window.setSwingViewMode = setSwingViewMode;



window.loadMorningShortlist = loadMorningShortlist;











function _renderTradeHistory(trades) {



    const tbody = document.getElementById('perfTradesBody');



    const countEl = document.getElementById('perfTradeCount');



    if (!tbody) return;







    if (countEl) countEl.textContent = `${trades.length} trades`;







    if (!trades.length) {



        tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:1.5rem;color:var(--text-muted)">No closed trades yet</td></tr>';



        return;



    }







    const exitIcon = { CLOSED_TP: 'TP', CLOSED_SL: 'SL', CLOSED_TRAIL: 'Trail', CLOSED_MANUAL: 'Manual' };



    const exitLabel = { CLOSED_TP: 'TP', CLOSED_SL: 'SL', CLOSED_TRAIL: 'Trail', CLOSED_MANUAL: 'Manual' };







    tbody.innerHTML = trades.map(t => {
        const qty = Number(t.initial_quantity || t.quantity || 1);
        const pnl = Number(t.total_pnl != null ? t.total_pnl : ((t.pnl || 0) * qty));
        const pnlCls = pnl >= 0 ? 'positive' : 'negative';
        const isLong = t.direction === 'BUY' || t.direction === 'LONG';
        const dirCls = isLong ? 'buy' : 'sell';
        const currSym = _symCurr(t.symbol);
        const dt = t.exit_time ? new Date(t.exit_time).toLocaleString('en-IN', {
            day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'
        }) : '—';
        const duration = t.duration_human || _fmtDuration(t.entry_time, t.exit_time);
        const durHtml = duration && duration !== '—' ? `<br><small style="color:var(--text-muted);font-size: 0.72rem;">⏱ ${duration}</small>` : '';
        const sign = pnl > 0 ? '+' : (pnl < 0 ? '-' : '');
        const pnlPct = Number(t.pnl_pct || 0);
        const pctSign = pnlPct > 0 ? '+' : '';
        const qtyDisplay = Number.isInteger(qty) ? qty.toString() : qty.toFixed(qty < 1 ? 4 : 2);

        return `<tr onclick="openPositionDetail(${t.id})" style="cursor:pointer;" title="Click to view complete trade analytics and execution details">
            <td class="perf-td-id">#${t.id || '—'}</td>
            <td class="perf-td-sym"><b>${t.symbol}</b></td>
            <td><span class="perf-dir-badge ${dirCls}">${t.direction}</span></td>
            <td class="mono">${currSym}${(t.entry_price || 0).toFixed(2)}</td>
            <td class="mono">${currSym}${(t.exit_price  || 0).toFixed(2)}</td>
            <td class="mono">${qtyDisplay}</td>
            <td class="mono ${pnlCls}">${sign}${currSym}${Math.abs(pnl).toFixed(2)}</td>
            <td class="mono ${pnlCls}">${pctSign}${pnlPct.toFixed(2)}%</td>
            <td><span class="perf-exit-badge">${exitIcon[t.status] || ''} ${exitLabel[t.status] || t.status || '—'}</span></td>
            <td class="perf-td-time">${dt}${durHtml}</td>
        </tr>`;
    }).join('');



}











// ── Equity Curve Chart (Canvas) ─────────────────────────







function drawEquityCurve(points) {



    const canvas = document.getElementById('equityChart');



    if (!canvas || points.length < 2) return;







    const ctx = canvas.getContext('2d');



    const dpr = window.devicePixelRatio || 1;



    const rect = canvas.parentElement.getBoundingClientRect();



    const W = rect.width || 700;



    const H = 260;



    canvas.width  = W * dpr;



    canvas.height = H * dpr;



    canvas.style.width  = W + 'px';



    canvas.style.height = H + 'px';



    ctx.scale(dpr, dpr);







    const pad = { top: 24, right: 20, bottom: 34, left: 68 };



    const iW = W - pad.left - pad.right;



    const iH = H - pad.top  - pad.bottom;







    const vals   = points.map(p => p.equity);



    const minV   = Math.min(...vals) * 0.997;



    const maxV   = Math.max(...vals) * 1.003;



    const rangeV = maxV - minV || 1;



    const lastV  = vals[vals.length - 1];



    const firstV = vals[0];



    const isUp   = lastV >= firstV;



    const lineClr = isUp ? '#10b981' : '#ef4444';







    const toX = i => pad.left + (i / (points.length - 1)) * iW;



    const toY = v => pad.top  + (1 - (v - minV) / rangeV) * iH;







    ctx.clearRect(0, 0, W, H);







    // Grid lines + Y labels



    const gridClr = 'rgba(99,102,241,0.08)';



    for (let i = 0; i <= 4; i++) {



        const y = pad.top + (i / 4) * iH;



        ctx.strokeStyle = gridClr;



        ctx.lineWidth = 1;



        ctx.setLineDash([4, 4]);



        ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();



        ctx.setLineDash([]);



        const v = maxV - (i / 4) * rangeV;



        ctx.fillStyle = 'rgba(148,163,184,0.7)';



        ctx.font = '10px JetBrains Mono, monospace';



        ctx.textAlign = 'right';



        ctx.fillText('₹' + Math.round(v).toLocaleString('en-IN'), pad.left - 6, y + 3);



    }







    // Drawdown shading



    let peakSoFar = vals[0];



    for (let i = 0; i < vals.length; i++) {



        if (vals[i] > peakSoFar) peakSoFar = vals[i];



        const ddV = peakSoFar - vals[i];



        if (ddV > 0) {



            const x1 = i > 0 ? toX(i - 1) : toX(i);



            const x2 = toX(i);



            const yPk = toY(peakSoFar);



            const yCur = toY(vals[i]);



            ctx.fillStyle = 'rgba(239,68,68,0.10)';



            ctx.fillRect(x1, yPk, x2 - x1 + 1, yCur - yPk);



        }



    }







    // Gradient fill under curve



    const grad = ctx.createLinearGradient(0, pad.top, 0, H - pad.bottom);



    grad.addColorStop(0, isUp ? 'rgba(16,185,129,0.28)' : 'rgba(239,68,68,0.28)');



    grad.addColorStop(1, 'rgba(0,0,0,0)');



    ctx.beginPath();



    ctx.moveTo(toX(0), toY(vals[0]));



    for (let i = 1; i < vals.length; i++) ctx.lineTo(toX(i), toY(vals[i]));



    ctx.lineTo(toX(vals.length - 1), H - pad.bottom);



    ctx.lineTo(toX(0), H - pad.bottom);



    ctx.closePath();



    ctx.fillStyle = grad;



    ctx.fill();







    // Initial capital line (dashed)



    const initY = toY(firstV);



    ctx.strokeStyle = 'rgba(148,163,184,0.35)';



    ctx.lineWidth = 1;



    ctx.setLineDash([6, 4]);



    ctx.beginPath(); ctx.moveTo(pad.left, initY); ctx.lineTo(W - pad.right, initY); ctx.stroke();



    ctx.setLineDash([]);



    ctx.fillStyle = 'rgba(148,163,184,0.5)';



    ctx.font = '9px JetBrains Mono, monospace';



    ctx.textAlign = 'left';



    ctx.fillText('Initial', pad.left + 4, initY - 4);







    // Main equity line



    ctx.beginPath();



    ctx.moveTo(toX(0), toY(vals[0]));



    for (let i = 1; i < vals.length; i++) ctx.lineTo(toX(i), toY(vals[i]));



    ctx.strokeStyle = lineClr;



    ctx.lineWidth = 2.5;



    ctx.lineJoin = 'round';



    ctx.stroke();







    // X-axis date labels (first / mid / last)



    const labelIdxs = [0, Math.floor(points.length / 2), points.length - 1];



    ctx.fillStyle = 'rgba(148,163,184,0.6)';



    ctx.font = '9px Inter, sans-serif';



    labelIdxs.forEach(i => {



        const ts = points[i]?.timestamp;



        if (!ts) return;



        const d = new Date(ts);



        const label = d.toLocaleDateString('en-IN', { day:'2-digit', month:'short' });



        const align = i === 0 ? 'left' : i === points.length - 1 ? 'right' : 'center';



        ctx.textAlign = align;



        const x = toX(i);



        ctx.fillText(label, x, H - pad.bottom + 14);



    });







    // End dot with glow



    const ex = toX(vals.length - 1);



    const ey = toY(lastV);



    ctx.beginPath();



    ctx.arc(ex, ey, 5, 0, Math.PI * 2);



    ctx.fillStyle = lineClr;



    ctx.fill();



    ctx.strokeStyle = '#fff';



    ctx.lineWidth = 1.5;



    ctx.stroke();







    // Current equity label



    ctx.fillStyle = lineClr;



    ctx.font = 'bold 11px JetBrains Mono, monospace';



    ctx.textAlign = 'right';



    ctx.fillText('₹' + Math.round(lastV).toLocaleString('en-IN'), ex - 8, ey - 8);



}











// ── Symbol Detail Modal ─────────────────────────────────







function initBmcModal() {



    const floatBtn = document.getElementById('bmcFloatBtn');



    const card = document.getElementById('bmcWidgetCard');



    const closeBtn = document.getElementById('bmcWidgetClose');



    const coffeeSvg = document.getElementById('bmcCoffeeSvg');



    const closeSvg = document.getElementById('bmcCloseSvg');



    const qrImg = document.getElementById('bmcUpiQrImg');



    const payBtn = document.getElementById('bmcPayActionBtn');



    const payBtnText = document.getElementById('bmcPayBtnText');



    const copyIdBtn = document.getElementById('bmcCopyIdOnlyBtn');



    const donorInput = document.getElementById('bmcDonorName');



    const multBtns = document.querySelectorAll('.bmc-mult-btn');







    if (!floatBtn || !card) return;







    let currentAmount = 50;



    const upiId = 'himanshugupta@ptaxis';



    const payeeName = 'Himanshu Gupta';







    function updatePaymentState() {



        if (payBtnText) payBtnText.textContent = `Support ₹${currentAmount}`;



        const donorName = (donorInput && donorInput.value.trim()) ? donorInput.value.trim() : 'Trader';



        const note = `Coffee from ${donorName}`;



        const upiUrl = `upi://pay?pa=${upiId}&pn=${encodeURIComponent(payeeName)}&am=${currentAmount}&cu=INR&tn=${encodeURIComponent(note)}`;



        if (qrImg) {



            qrImg.src = `https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=${encodeURIComponent(upiUrl)}`;



        }



    }







    function toggleWidget(show) {



        const isVisible = show !== undefined ? show : (card.style.display !== 'none');



        if (isVisible) {



            card.style.display = 'none';



            if (coffeeSvg) coffeeSvg.style.display = 'block';



            if (closeSvg) closeSvg.style.display = 'none';



        } else {



            card.style.display = 'block';



            if (coffeeSvg) coffeeSvg.style.display = 'none';



            if (closeSvg) closeSvg.style.display = 'block';



        }



    }







    floatBtn.addEventListener('click', (e) => {



        e.stopPropagation();



        toggleWidget();



    });







    if (closeBtn) {



        closeBtn.addEventListener('click', (e) => {



            e.stopPropagation();



            toggleWidget(true);



        });



    }







    // Multiplier pills click



    multBtns.forEach((btn) => {



        btn.addEventListener('click', () => {



            multBtns.forEach((b) => b.classList.remove('active'));



            btn.classList.add('active');



            currentAmount = parseInt(btn.dataset.amount, 10) || 50;



            updatePaymentState();



        });



    });







    if (donorInput) {



        donorInput.addEventListener('input', updatePaymentState);



    }







    // Action button



    if (payBtn) {



        payBtn.addEventListener('click', () => {



            const isMobile = /Android|iPhone|iPad|iPod|webOS/i.test(navigator.userAgent);



            const donorName = (donorInput && donorInput.value.trim()) ? donorInput.value.trim() : 'Trader';



            const note = `Coffee from ${donorName}`;



            const upiUrl = `upi://pay?pa=${upiId}&pn=${encodeURIComponent(payeeName)}&am=${currentAmount}&cu=INR&tn=${encodeURIComponent(note)}`;







            if (isMobile) {



                window.location.href = upiUrl;



            } else {



                _copyToClipboard(upiId, () => {



                    const orig = payBtnText.textContent;



                    payBtnText.textContent = 'Copied UPI ID! ✓';



                    payBtn.style.background = '#10b981';



                    setTimeout(() => {



                        payBtnText.textContent = orig;



                        payBtn.style.background = '';



                    }, 2000);



                });



            }



        });



    }







    // Copy ID Only button



    if (copyIdBtn) {



        copyIdBtn.addEventListener('click', (e) => {



            e.stopPropagation();



            _copyToClipboard(upiId, () => {



                copyIdBtn.textContent = 'Copied! ✓';



                copyIdBtn.style.borderColor = '#10b981';



                copyIdBtn.style.color = '#10b981';



                setTimeout(() => {



                    copyIdBtn.textContent = 'Copy ID';



                    copyIdBtn.style.borderColor = '';



                    copyIdBtn.style.color = '';



                }, 2000);



            });



        });



    }







    // Close when clicking outside



    document.addEventListener('click', (e) => {



        if (!card.contains(e.target) && !floatBtn.contains(e.target)) {



            toggleWidget(true);



        }



    });







    // Close on Escape



    document.addEventListener('keydown', (e) => {



        if (e.key === 'Escape' && card.style.display !== 'none') {



            toggleWidget(true);



        }



    });







    function _copyToClipboard(text, onSuccess) {



        if (navigator.clipboard && navigator.clipboard.writeText) {



            navigator.clipboard.writeText(text).then(onSuccess).catch(() => _fallbackCopy(text, onSuccess));



        } else {



            _fallbackCopy(text, onSuccess);



        }



    }







    function _fallbackCopy(text, onSuccess) {



        try {



            const t = document.createElement('textarea');



            t.value = text;



            t.style.position = 'fixed';



            t.style.opacity = '0';



            document.body.appendChild(t);



            t.select();



            document.execCommand('copy');



            document.body.removeChild(t);



            if (onSuccess) onSuccess();



        } catch (_) {}



    }



}







function initModal() {



    document.getElementById('modalBackdrop').addEventListener('click', (e) => {



        if (e.target.id === 'modalBackdrop') closeModal();



    });



    document.getElementById('modalClose').addEventListener('click', closeModal);



    document.addEventListener('keydown', (e) => {



        if (e.key === 'Escape') {
            closeModal();
            if (typeof closeIntradayDrawer === 'function') closeIntradayDrawer();
            if (typeof closePositionModal === 'function') closePositionModal();
            if (typeof closeTradeDetailModal === 'function') closeTradeDetailModal();
            if (typeof closeEdgeDrawer === 'function') closeEdgeDrawer();
        }
        if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) {
            const activeTab = document.querySelector('.tab-content.active')?.id;
            let targetInput = null;
            if (activeTab === 'intradayTab') targetInput = document.getElementById('idSearchInput');
            else if (activeTab === 'swingTab') targetInput = document.getElementById('swingSymbolSearch') || document.getElementById('swingSearchInput');
            else if (activeTab === 'edgeTab') targetInput = document.getElementById('edgeScannerSearch');
            if (targetInput) {
                e.preventDefault();
                targetInput.focus();
                targetInput.select();
            }
        }



    });



}







async function openSymbolModal(symbol) {



    currentModalSymbol = symbol;



    currentModalInterval = '5m';



    document.getElementById('modalTitle').textContent = symbol;



    document.getElementById('modalBackdrop').classList.add('visible');







    // Sync timeframe buttons



    document.querySelectorAll('.tf-btn').forEach(b => {



        b.classList.toggle('active', b.dataset.tf === '5m');



    });







    await _loadModalChart(symbol, '5m');



    _startModalAutoRefresh(symbol);



}







async function _loadModalChart(symbol, interval) {



    const canvas = document.getElementById('modalChart');







    // Show loading placeholder while fetching



    if (canvas) {



        const ctx = canvas.getContext('2d');



        const dpr = window.devicePixelRatio || 1;



        const rect = canvas.parentElement.getBoundingClientRect();



        canvas.width  = rect.width * dpr;



        canvas.height = 300 * dpr;



        canvas.style.width  = rect.width + 'px';



        canvas.style.height = '300px';



        ctx.scale(dpr, dpr);



        ctx.clearRect(0, 0, rect.width, 300);



        ctx.fillStyle = 'rgba(99,102,241,0.15)';



        ctx.fillRect(0, 0, rect.width, 300);



        ctx.fillStyle = '#6b7094';



        ctx.font = '13px JetBrains Mono, monospace';



        ctx.textAlign = 'center';



        ctx.fillText(`Loading ${interval} data…`, rect.width / 2, 150);



    }







    try {



        const encodedSymbol = symbol.replace('/', '-');



        const res = await fetch(`${API}/api/market/${encodedSymbol}?limit=150&interval=${interval}`);



        const data = await res.json();







        if (data.data && data.data.length > 0) {



            drawModalChart(data.data, interval, data);



            renderModalInfo(data.data, symbol, interval, data);



            loadModalBacktest(symbol);
    loadModalNews(symbol);



        } else {



            // Show error on canvas



            if (canvas) {



                const ctx = canvas.getContext('2d');



                ctx.clearRect(0, 0, canvas.width, canvas.height);



                ctx.fillStyle = '#ef4444';



                ctx.font = '12px monospace';



                ctx.textAlign = 'center';



                ctx.fillText(data.error || 'No data available', canvas.width / 2 / (window.devicePixelRatio || 1), 150);



            }



        }



    } catch (err) {



        console.error('Modal data load error:', err);



    }



}







function _startModalAutoRefresh(symbol) {



    if (_modalRefreshTimer) clearInterval(_modalRefreshTimer);



    // Refresh interval: 30s for 5m, 60s for 15m, 3min for 1h, 10min for 1d



    const intervalMs = { '5m': 30_000, '15m': 60_000, '1h': 180_000, '1d': 600_000 };



    _modalRefreshTimer = setInterval(() => {



        if (currentModalSymbol && document.getElementById('modalBackdrop').classList.contains('visible')) {



            _loadModalChart(currentModalSymbol, currentModalInterval);



        }



    }, intervalMs[currentModalInterval] || 30_000);



}







async function switchModalTimeframe(tf) {



    if (!currentModalSymbol) return;



    currentModalInterval = tf;







    document.querySelectorAll('.tf-btn').forEach(b => {



        b.classList.toggle('active', b.dataset.tf === tf);



    });







    await _loadModalChart(currentModalSymbol, tf);



    _startModalAutoRefresh(currentModalSymbol);



}







function closeModal() {



    document.getElementById('modalBackdrop').classList.remove('visible');



    currentModalSymbol = null;



    currentModalInterval = '5m';



    if (_modalRefreshTimer) { clearInterval(_modalRefreshTimer); _modalRefreshTimer = null; }



}







async function loadModalBacktest(symbol) {
    const section = document.getElementById('modalBacktestSection');
    if (!section) return;

    section.innerHTML = '<p style="color:var(--text-muted);font-size:0.8rem">Running backtest engine...</p>';

    try {
        const encodedSymbol = symbol.replace('/', '-');
        const res = await fetchSafeJSON(`${API}/api/backtest/${encodedSymbol}`, {}, 25000);
        if (!res.ok) {
            if (res.status === 429) {
                section.innerHTML = '<p style="color:var(--yellow);font-size:0.8rem">⚠️ Rate limit in effect. Retrying shortly...</p>';
            } else {
                section.innerHTML = `<p style="color:var(--text-muted);font-size:0.8rem">${_escHtml(res.error || 'Backtest unavailable')}</p>`;
            }
            return;
        }
        const data = res.data;

        if (data.error) {
            section.innerHTML = `<p style="color:var(--text-muted);font-size:0.8rem">${_escHtml(data.error)}</p>`;
            return;
        }

        const pfColor = data.profit_factor >= 1.5 ? 'var(--green)' : data.profit_factor >= 1 ? 'var(--yellow)' : 'var(--red)';
        const wrColor = data.win_rate >= 55 ? 'var(--green)' : data.win_rate >= 45 ? 'var(--yellow)' : 'var(--red)';
        const netColor = (data.net_return || 0) >= 0 ? 'var(--green)' : 'var(--red)';
        const ptsColor = (data.points_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)';

        const tradeCount = data.trade_count ?? data.total_trades ?? 0;
        const statsOk = Boolean(data.stats_available ?? (tradeCount >= 20));
        const isExploratory = data.sample_status === 'INSUFFICIENT_SAMPLE' || tradeCount < 20;
        const sampleBadge = isExploratory 
            ? `<span style="font-size: 0.72rem;color:var(--yellow);background:rgba(245,158,11,0.12);padding:2px 8px;border-radius:10px;font-weight:700;">⚠️ ${tradeCount < 20 ? `${tradeCount} trades • Exploratory only` : (data.sample_badge || 'Exploratory only')}</span>`
            : `<span style="font-size: 0.72rem;color:var(--green);background:rgba(16,185,129,0.12);padding:2px 8px;border-radius:10px;font-weight:700;">✓ ${data.sample_badge || 'Statistically valid'}</span>`;

        const wrLabel = statsOk ? 'Win Rate' : 'Win rate (small sample)';
        const sharpeDisplay = statsOk
            ? `${data.sharpe ?? data.sharpe_ratio ?? '—'}`
            : `<span title="Needs ≥20 trades (currently ${tradeCount}) · exploratory only" style="cursor:help;color:var(--text-muted);">—</span>`;
        const pfDisplay = statsOk
            ? `${data.profit_factor ?? '—'}`
            : `<span title="Needs ≥20 trades (currently ${tradeCount}) · exploratory only" style="cursor:help;color:var(--text-muted);">—</span>`;

        const grossRet = Number(data.gross_return ?? 0);
        const estCosts = Number(data.estimated_costs ?? 0);
        const netRet = Number(data.net_return ?? (grossRet - estCosts));
        const perTradeCost = Number(data.per_trade_cost_pct ?? (tradeCount > 0 ? estCosts / tradeCount : 0));
        const costDrag = Number(data.cost_drag_pct ?? (grossRet > 0 ? (estCosts / Math.abs(grossRet)) * 100 : 0));

        const dataWindow = `Data: ${data.bars || data.bars_used || 0} bars (${data.timeframe || '15m'}) · ${data.window_days || 5}d`;

        section.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;flex-wrap:wrap;gap:6px;">
                <div style="display:flex;align-items:center;gap:6px;">
                    <span style="font-size:0.72rem;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">Strategy Backtest</span>
                    ${sampleBadge}
                </div>
                <div style="font-size: 0.72rem;color:#64748b;font-family:var(--font-mono);">${dataWindow}</div>
            </div>
            <div class="backtest-grid">
                <div class="bt-metric">
                    <span class="bt-val" style="color:${wrColor}">${data.win_rate}%</span>
                    <span class="bt-label">${wrLabel}</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val">${tradeCount}</span>
                    <span class="bt-label">Trades</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val" style="color:${ptsColor}">${(data.points_pnl || 0) >= 0 ? '+' : ''}${data.points_pnl || 0} pts</span>
                    <span class="bt-label">Points PnL</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val" style="color:${netColor}">${netRet >= 0 ? '+' : ''}${netRet.toFixed(2)}%</span>
                    <span class="bt-label">Net Return</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val" style="color:${pfColor}">${pfDisplay}</span>
                    <span class="bt-label">Profit Factor</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val" style="color:var(--red)">${data.max_drawdown ?? data.max_drawdown_pct}%</span>
                    <span class="bt-label">Max DD</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val">${sharpeDisplay}</span>
                    <span class="bt-label">Sharpe</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val" style="color:${(data.net_pnl_inr || 0) >= 0 ? 'var(--green)' : 'var(--red)'}">₹${(data.net_pnl_inr || 0) >= 0 ? '+' : ''}${Math.round(data.net_pnl_inr || 0).toLocaleString('en-IN')}</span>
                    <span class="bt-label">Net Cash (₹100k)</span>
                </div>
            </div>
            <div style="margin-top:0.6rem;padding:0.55rem 0.75rem;background:rgba(255,255,255,0.02);border:1px solid var(--border-color);border-radius:6px;font-family:var(--font-mono);font-size:0.7rem;">
                <div style="display:flex;justify-content:space-between;margin-bottom:3px;">
                    <span style="color:var(--text-muted)">Gross return</span>
                    <span style="font-weight:600;color:var(--text-primary)">${grossRet >= 0 ? '+' : ''}${grossRet.toFixed(2)}%</span>
                </div>
                <div style="display:flex;justify-content:space-between;margin-bottom:3px;">
                    <span style="color:var(--text-muted)">Total costs</span>
                    <span style="color:var(--red)">-${estCosts.toFixed(2)}% <span style="color:var(--text-muted);font-size: 0.72rem">(${tradeCount} trades · ${perTradeCost.toFixed(2)}% each)</span></span>
                </div>
                <div style="display:flex;justify-content:space-between;margin-bottom:3px;border-top:1px dashed var(--border-color);padding-top:3px;">
                    <span style="color:var(--text-muted)">Net return</span>
                    <span style="font-weight:700;color:${netColor}">${netRet >= 0 ? '+' : ''}${netRet.toFixed(2)}%</span>
                </div>
                ${costDrag > 0 ? `
                <div style="display:flex;justify-content:space-between;margin-top:3px;">
                    <span style="color:var(--text-muted)">Cost drag</span>
                    <span style="color:var(--yellow);">${Math.round(costDrag)}% <span style="font-size: 0.70rem;color:var(--text-muted)">← costs ate most of the edge</span></span>
                </div>` : ''}
            </div>
            <div style="font-size: 0.72rem;color:#64748b;margin-top:0.4rem;display:flex;justify-content:flex-end;">
                <span>${data.cache_status === 'HIT' ? '⚡ Instant (Cache)' : 'Live Model Computed'}</span>
            </div>
        `;
    } catch (err) {
        section.innerHTML = '<p style="color:var(--text-muted);font-size:0.8rem">Backtest unavailable</p>';
    }
}

async function loadModalNews(symbol) {
    const list = document.getElementById('modalNewsList');
    const freshEl = document.getElementById('modalNewsFreshness');
    if (!list) return;

    list.innerHTML = '<p style="color:var(--text-muted);font-size:0.75rem;">Loading verified company news...</p>';

    try {
        const encodedSymbol = symbol.replace('/', '-');
        const res = await fetchSafeJSON(`${API}/api/news/${encodedSymbol}?limit=6`, {}, 10000);
        if (!res.ok) {
            if (res.status === 429) {
                list.innerHTML = '<p style="color:var(--yellow);font-size:0.75rem;">⚠️ Rate limit in effect. Retrying shortly...</p>';
                if (freshEl) freshEl.textContent = 'Rate limited';
            } else {
                list.innerHTML = '<p style="color:var(--text-muted);font-size:0.75rem;">No recent company-specific announcements.</p>';
                if (freshEl) freshEl.textContent = 'Up to date';
            }
            return;
        }
        const data = res.data;

        if (!data || !data.items || data.items.length === 0) {
            list.innerHTML = '<p style="color:var(--text-muted);font-size:0.75rem;">No recent company-specific announcements.</p>';
            if (freshEl) freshEl.textContent = 'Up to date';
            return;
        }

        if (freshEl) freshEl.textContent = `${data.items.length} verified stories`;

        list.innerHTML = data.items.map(item => {
            const titleSafe = _escHtml(item.headline || item.title || '');
            const srcSafe = _escHtml(item.source || 'News');
            const ageSafe = _escHtml(item.age || 'Recent');
            const rel = item.symbol_relevance || 'COMPANY_SPECIFIC';
            
            const relBadge = rel === 'COMPANY_SPECIFIC'
                ? '<span style="font-size: 0.70rem;background:rgba(16,185,129,0.15);color:var(--green);padding:1px 5px;border-radius:4px;font-weight:700;">COMPANY</span>'
                : rel === 'SECTOR_RELATED'
                ? '<span style="font-size: 0.70rem;background:rgba(56,189,248,0.15);color:var(--cyan);padding:1px 5px;border-radius:4px;font-weight:700;">SECTOR</span>'
                : '<span style="font-size: 0.70rem;background:rgba(148,163,184,0.15);color:#94a3b8;padding:1px 5px;border-radius:4px;font-weight:700;">MACRO</span>';

            return `
            <div style="background:rgba(255,255,255,0.02);padding:0.4rem 0.6rem;border-radius:6px;border:1px solid rgba(255,255,255,0.05);display:flex;justify-content:space-between;align-items:center;gap:8px;">
                <div style="display:flex;align-items:center;gap:6px;flex:1;overflow:hidden;">
                    ${relBadge}
                    <a href="${item.url || '#'}" target="_blank" rel="noopener noreferrer" style="color:#e2e8f0;text-decoration:none;font-size:0.75rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${titleSafe}">
                        ${titleSafe}
                    </a>
                </div>
                <div style="display:flex;align-items:center;gap:6px;font-size: 0.72rem;color:#64748b;white-space:nowrap;">
                    <span>${srcSafe}</span>
                    <span>•</span>
                    <span style="color:#94a3b8;">${ageSafe}</span>
                </div>
            </div>`;
        }).join('');
    } catch (err) {
        list.innerHTML = '<p style="color:var(--text-muted);font-size:0.75rem;">News feed temporarily offline.</p>';
    }
}

function drawModalChart(data, interval, meta) {



    const canvas = document.getElementById('modalChart');



    if (!canvas || data.length < 2) return;







    const ctx = canvas.getContext('2d');



    const dpr = window.devicePixelRatio || 1;



    const rect = canvas.parentElement.getBoundingClientRect();



    const W = rect.width;



    const H = 300;







    canvas.width  = W * dpr;



    canvas.height = H * dpr;



    canvas.style.width  = W + 'px';



    canvas.style.height = H + 'px';



    ctx.scale(dpr, dpr);







    const tf = interval || currentModalInterval || '5m';



    const borderColor = getComputedStyle(document.documentElement).getPropertyValue('--border-color').trim() || 'rgba(99,102,241,0.12)';



    const mutedColor  = getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim() || '#6b7094';







    // ── Layout: price pane (top 72%) + volume pane (bottom 22%) + x-axis (6%)



    const PAD = { top: 26, right: 15, bottom: 28, left: 68 };



    const VOLUME_RATIO = 0.22;



    const totalInner = H - PAD.top - PAD.bottom;



    const volH  = totalInner * VOLUME_RATIO;



    const priceH = totalInner - volH - 4; // 4px gap



    const chartW = W - PAD.left - PAD.right;







    const priceTop = PAD.top;



    const volTop   = PAD.top + priceH + 4;







    // Price range



    const highs = data.map(d => d.high);



    const lows  = data.map(d => d.low);



    const ema20V = data.map(d => d.ema_20).filter(Boolean);



    const ema50V = data.map(d => d.ema_50).filter(Boolean);



    const allV = [...highs, ...lows, ...ema20V, ...ema50V].filter(v => v != null && isFinite(v));



    const minV  = Math.min(...allV) * 0.9994;



    const maxV  = Math.max(...allV) * 1.0006;



    const rangeV = maxV - minV || 1;







    // Volume range



    const vols   = data.map(d => d.volume || 0);



    const maxVol = Math.max(...vols, 1);







    const candleSlotW = chartW / data.length;



    const candleBodyW = Math.max(1, candleSlotW * 0.65);







    const toX    = (i) => PAD.left + i * candleSlotW + candleSlotW / 2;



    const toY    = (v) => priceTop + (1 - (v - minV) / rangeV) * priceH;



    const toVolY = (v) => volTop + (1 - v / maxVol) * volH;







    ctx.clearRect(0, 0, W, H);







    // ── Header bar: timeframe label + date range



    const first = data[0], last = data[data.length - 1];



    const fmtDate = (ts) => {



        const d = new Date(ts);



        if (tf === '1d') return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: '2-digit' });



        return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }) + ' '



             + d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false });



    };



    ctx.font = 'bold 10px JetBrains Mono, monospace';



    ctx.fillStyle = 'rgba(99,102,241,0.9)';



    ctx.textAlign = 'left';



    ctx.fillText(`${tf.toUpperCase()}  •  ${data.length} candles`, PAD.left, 16);



    ctx.fillStyle = mutedColor;



    ctx.font = '9px JetBrains Mono, monospace';



    ctx.textAlign = 'right';



    if (first.timestamp && last.timestamp) {



        ctx.fillText(`${fmtDate(first.timestamp)} → ${fmtDate(last.timestamp)}`, W - PAD.right, 16);



    }







    // ── Price grid lines + Y-axis labels



    ctx.font = '10px JetBrains Mono, monospace';



    ctx.textAlign = 'right';



    for (let i = 0; i <= 4; i++) {



        const y = priceTop + (i / 4) * priceH;



        ctx.strokeStyle = borderColor;



        ctx.lineWidth = 0.5;



        ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(W - PAD.right, y); ctx.stroke();



        const labelVal = maxV - (i / 4) * rangeV;



        ctx.fillStyle = mutedColor;



        ctx.fillText(labelVal.toLocaleString('en-IN', { maximumFractionDigits: 0 }), PAD.left - 6, y + 3);



    }







    // ── Volume bars



    data.forEach((d, i) => {



        const x   = toX(i);



        const isBull = (d.close ?? 0) >= (d.open ?? d.close ?? 0);



        ctx.fillStyle = isBull ? 'rgba(34,197,94,0.35)' : 'rgba(239,68,68,0.35)';



        const vBarH = Math.max(1, (d.volume || 0) / maxVol * volH);



        ctx.fillRect(x - candleBodyW / 2, volTop + volH - vBarH, candleBodyW, vBarH);



    });







    // Volume label



    ctx.fillStyle = mutedColor;



    ctx.font = '8px JetBrains Mono, monospace';



    ctx.textAlign = 'left';



    ctx.fillText('VOL', PAD.left - 62, volTop + 8);







    // ── Candlestick bodies and wicks



    data.forEach((d, i) => {



        const x     = toX(i);



        const open  = d.open  ?? d.close;



        const close = d.close;



        const high  = d.high  ?? Math.max(open, close);



        const low   = d.low   ?? Math.min(open, close);







        const isBull    = close >= open;



        const bodyColor = isBull ? '#22c55e' : '#ef4444';



        const wickColor = isBull ? 'rgba(34,197,94,0.7)' : 'rgba(239,68,68,0.7)';







        const bodyTop    = toY(Math.max(open, close));



        const bodyBottom = toY(Math.min(open, close));



        const bodyHeight = Math.max(1, bodyBottom - bodyTop);







        ctx.strokeStyle = wickColor;



        ctx.lineWidth = Math.max(1, candleSlotW * 0.12);



        ctx.beginPath(); ctx.moveTo(x, toY(high)); ctx.lineTo(x, toY(low)); ctx.stroke();







        ctx.fillStyle = bodyColor;



        ctx.fillRect(x - candleBodyW / 2, bodyTop, candleBodyW, bodyHeight);







        // Signal markers — support both old (BUY/SELL) and new (LONG/SHORT) classes



        const sig = d.signal || '';



        if (sig === 'LONG' || sig === 'BUY') {



            ctx.fillStyle = '#22c55e';



            ctx.font = `bold ${Math.min(11, Math.max(6, candleSlotW))}px sans-serif`;



            ctx.textAlign = 'center';



            ctx.fillText('▲', x, toY(low) + 13);



        } else if (sig === 'SHORT' || sig === 'SELL') {



            ctx.fillStyle = '#ef4444';



            ctx.font = `bold ${Math.min(11, Math.max(6, candleSlotW))}px sans-serif`;



            ctx.textAlign = 'center';



            ctx.fillText('▼', x, toY(high) - 5);



        }



    });







    // Reset



    ctx.font = '10px JetBrains Mono, monospace';



    ctx.textAlign = 'right';







    // ── EMA overlays



    const drawEmaLine = (color, vals) => {



        const mapped = data.map((d, i) => ({ i, v: vals[i] })).filter(p => p.v != null && isFinite(p.v));



        if (mapped.length < 5) return;



        ctx.beginPath();



        ctx.moveTo(toX(mapped[0].i), toY(mapped[0].v));



        mapped.slice(1).forEach(p => ctx.lineTo(toX(p.i), toY(p.v)));



        ctx.strokeStyle = color;



        ctx.lineWidth = 1.2;



        ctx.lineJoin = 'round';



        ctx.setLineDash([]);



        ctx.stroke();



    };



    drawEmaLine('rgba(34,211,238,0.8)',  data.map(d => d.ema_20));



    drawEmaLine('rgba(249,115,22,0.8)',  data.map(d => d.ema_50));







    // ── X-axis time labels (every ~6 labels across chart)



    ctx.fillStyle = mutedColor;



    ctx.font = '9px JetBrains Mono, monospace';



    ctx.textAlign = 'center';



    const labelEvery = Math.max(1, Math.floor(data.length / 6));



    data.forEach((d, i) => {



        if (i % labelEvery === 0 && d.timestamp) {



            const t = new Date(d.timestamp);



            const lbl = tf === '1d'



                ? t.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })



                : t.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false });



            ctx.fillText(lbl, toX(i), H - 6);



        }



    });







    // ── "Live · yfinance" badge bottom-right



    ctx.font = '8px JetBrains Mono, monospace';



    ctx.textAlign = 'right';



    ctx.fillStyle = 'rgba(99,102,241,0.5)';



        if (meta && (meta.is_stale || meta.market_state !== 'OPEN')) {
        ctx.fillStyle = 'rgba(245,158,11,0.7)';
        ctx.fillText('Last session · yfinance', W - PAD.right, H - 6);
    } else {
        ctx.fillStyle = 'rgba(99,102,241,0.5)';
        ctx.fillText('Live · yfinance', W - PAD.right, H - 6);
    }



}







function renderModalInfo(data, symbol, interval, meta) {
    const sessionBadgeEl = document.getElementById('modalSessionBadge');
    if (sessionBadgeEl) {
        const isStale = Boolean(meta?.is_stale);
        const state = meta?.market_state || 'UNKNOWN';
        const asOfStr = meta?.as_of || (data[data.length - 1] && data[data.length - 1].timestamp);
        const asOfDate = asOfStr ? new Date(asOfStr) : new Date();

        const src = (meta?.source || meta?.data_source || '').toUpperCase();
        const isRt = Boolean(meta?.is_realtime);
        const reason = (meta?.status_reason || '').toUpperCase();
        const hasExchangeTs = Boolean(meta?.data_timestamp);

        if (isStale || state !== 'OPEN') {
            const dayStr = asOfDate.toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short' });
            const timeStr = asOfDate.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false });
            sessionBadgeEl.innerHTML = `⏱ Last session · ${dayStr} ${timeStr}`;
            sessionBadgeEl.style.display = 'inline-flex';
            sessionBadgeEl.style.color = 'var(--yellow)';
            sessionBadgeEl.style.background = 'rgba(245,158,11,0.15)';
            sessionBadgeEl.style.border = '1px solid rgba(245,158,11,0.3)';
            sessionBadgeEl.title = `Data timestamp: ${asOfDate.toLocaleString('en-IN')} (Market ${state})`;
        } else if (reason === 'TIMESTAMP_UNKNOWN_FAIL_CLOSED' && src !== 'DELAYED_YFINANCE' && src !== 'YFINANCE') {
            sessionBadgeEl.innerHTML = `⚪ Unknown Timestamp`;
            sessionBadgeEl.style.display = 'inline-flex';
            sessionBadgeEl.style.color = '#94a3b8';
            sessionBadgeEl.style.background = 'rgba(148,163,184,0.15)';
            sessionBadgeEl.style.border = '1px solid rgba(148,163,184,0.3)';
            sessionBadgeEl.title = 'Exchange timestamp unknown · Fail-closed mode (New trades halted)';
        } else if (src === 'DELAYED_YFINANCE' || src === 'YFINANCE') {
            const timeStr = hasExchangeTs ? asOfDate.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false }) : 'Unverified TS';
            sessionBadgeEl.innerHTML = `🟡 15m Delayed · ${timeStr}`;
            sessionBadgeEl.style.display = 'inline-flex';
            sessionBadgeEl.style.color = '#f59e0b';
            sessionBadgeEl.style.background = 'rgba(245,158,11,0.15)';
            sessionBadgeEl.style.border = '1px solid rgba(245,158,11,0.3)';
            sessionBadgeEl.title = hasExchangeTs ? 'Exchange quote delayed ~15m (yFinance fallback) · Intraday trading disabled' : 'Exchange quote delayed ~15m (Unverified exchange timestamp) · Intraday trading disabled';
        } else if (src === 'STALE_CACHE') {
            sessionBadgeEl.innerHTML = `🟠 Stale Cache`;
            sessionBadgeEl.style.display = 'inline-flex';
            sessionBadgeEl.style.color = '#ef4444';
            sessionBadgeEl.style.background = 'rgba(239,68,68,0.15)';
            sessionBadgeEl.style.border = '1px solid rgba(239,68,68,0.3)';
            sessionBadgeEl.title = 'Cache expired · Reconnecting to exchange';
        } else if (!hasExchangeTs || (!isRt && !symbol.includes('/'))) {
            sessionBadgeEl.innerHTML = `⚪ Timestamp Unverified`;
            sessionBadgeEl.style.display = 'inline-flex';
            sessionBadgeEl.style.color = '#94a3b8';
            sessionBadgeEl.style.background = 'rgba(148,163,184,0.15)';
            sessionBadgeEl.style.border = '1px solid rgba(148,163,184,0.3)';
            sessionBadgeEl.title = 'Exchange timestamp missing or unverified · Fail-closed mode (New trades halted)';
        } else {
            const timeStr = asOfDate.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
            sessionBadgeEl.innerHTML = `🟢 Live NSE · ${timeStr}`;
            sessionBadgeEl.style.display = 'inline-flex';
            sessionBadgeEl.style.color = 'var(--green)';
            sessionBadgeEl.style.background = 'rgba(16,185,129,0.15)';
            sessionBadgeEl.style.border = '1px solid rgba(16,185,129,0.3)';
            sessionBadgeEl.title = 'Verified sub-second live exchange feed';
        }
    }
    const last = data[data.length - 1];
    const sig = allSignals.find(s => s.symbol === symbol);
    const isCrypto = symbol.includes('/');
    const curr = isCrypto ? '$' : '₹';
    const tf = interval || currentModalInterval || '5m';

    const sigVal   = sig?.signal || last.signal || 'HOLD';
    const sigDir   = sig?.direction || sigVal;
    const entryVal = sig?.entry ?? last.close;
    const slVal    = sig?.stop_loss;
    const tpVal    = sig?.target;
    const curClose = Number(last.close || 0);

    // Determine trade lifecycle status (active vs target hit vs stopped out)
    const status = sig?.signal_status || (
        sigVal === 'LONG' && tpVal && curClose >= tpVal ? 'TARGET_HIT' :
        sigVal === 'SHORT' && tpVal && curClose <= tpVal ? 'TARGET_HIT' :
        sigVal === 'LONG' && slVal && curClose <= slVal ? 'STOPPED_OUT' :
        sigVal === 'SHORT' && slVal && curClose >= slVal ? 'STOPPED_OUT' :
        sig?.stale ? 'STALE' : 'ACTIVE'
    );

    const isTargetHit = status === 'TARGET_HIT';
    const isStoppedOut = status === 'STOPPED_OUT';
    const displaySig = (isTargetHit || isStoppedOut) ? 'HOLD' : sigVal;

    let statusBadge = '';
    if (isTargetHit) {
        statusBadge = '<span style="font-size: 0.70rem;font-weight:700;color:var(--green);padding:1px 6px;border-radius:8px;background:rgba(16,185,129,0.15);margin-left:4px;">✓ TARGET HIT</span>';
    } else if (isStoppedOut) {
        statusBadge = '<span style="font-size: 0.70rem;font-weight:700;color:var(--red);padding:1px 6px;border-radius:8px;background:rgba(239,68,68,0.15);margin-left:4px;">✕ STOPPED OUT</span>';
    } else if (status === 'STALE') {
        statusBadge = '<span style="font-size: 0.70rem;font-weight:700;color:var(--yellow);padding:1px 6px;border-radius:8px;background:rgba(245,158,11,0.15);margin-left:4px;">⏱ STALE</span>';
    }

    const sigColor = ['LONG','BUY'].includes(displaySig)   ? 'var(--green)'
                   : ['SHORT','SELL'].includes(displaySig) ? 'var(--red)'
                   : displaySig === 'SKIP' ? 'var(--text-muted)'
                   : 'var(--cyan)';

    // MACD color
    const macdHist = last.macd_hist;
    const macdColor = macdHist > 0 ? 'var(--green)' : macdHist < 0 ? 'var(--red)' : 'var(--text-muted)';
    const macdIcon  = macdHist > 0 ? '▲' : macdHist < 0 ? '▼' : '—';

    // RSI color
    const rsiVal = last.rsi;
    const rsiColor = rsiVal < 30 ? 'var(--green)' : rsiVal > 70 ? 'var(--yellow)' : 'var(--text-secondary)';
    const rsiLabel = rsiVal != null ? `${rsiVal.toFixed(1)}${rsiVal > 70 ? ' (OB)' : rsiVal < 30 ? ' (OS)' : ''}` : '—';

    // Multi-timeframe trend separation: Local TF vs Daily Macro
    const localTrend = sig?.local_trend || (last.ema_20 && last.ema_50 ? (last.ema_20 >= last.ema_50 ? 'BULLISH' : 'BEARISH') : 'NEUTRAL');
    const macroTrend = (sig?.trend || '—').replace('TrendDirection.', '');
    const localColor = localTrend === 'BULLISH' ? 'var(--green)' : localTrend === 'BEARISH' ? 'var(--red)' : 'var(--text-muted)';
    const macroColor = macroTrend === 'BULLISH' ? 'var(--green)' : macroTrend === 'BEARISH' ? 'var(--red)' : 'var(--text-muted)';
    const isMacroConflict = (localTrend === 'BULLISH' && macroTrend === 'BEARISH') || (localTrend === 'BEARISH' && macroTrend === 'BULLISH');
    const macroConflictChip = isMacroConflict ? '<span style="font-size: 0.70rem;font-weight:700;color:var(--yellow);margin-left:4px;" title="Macro conflict: local timeframe trend opposes daily macro">⚠️ CONFLICT</span>' : '';

    // Stale check & human age string
    const isSignalStale = Boolean(sig?.stale || sig?.actionable === false || meta?.is_stale);
    const staleMin = sig?.signal_age_minutes ?? sig?.stale_minutes ?? (meta?.bar_age_seconds ? meta.bar_age_seconds / 60 : null);
    let ageHuman = '';
    if (staleMin != null && !isNaN(staleMin)) {
        const m = Math.floor(staleMin);
        if (m < 60) ageHuman = `${m}m`;
        else {
            const h = Math.floor(m / 60);
            const remM = m % 60;
            if (h < 24) ageHuman = remM > 0 ? `${h}h ${remM}m` : `${h}h`;
            else {
                const d = Math.floor(h / 24);
                const remH = h % 24;
                ageHuman = remH > 0 ? `${d}d ${remH}h` : `${d}d`;
            }
        }
    }

    // Limit entry distance calculation and warning chip
    const entryDist = sig?.entry_distance_pct != null
        ? sig.entry_distance_pct
        : (entryVal > 0 ? ((curClose - entryVal) / entryVal * 100) : 0);
    const isEntryCrossed = Boolean(
        sig?.entry_crossed ||
        sig?.chase_risk ||
        (['LONG', 'BUY'].includes(sigDir) && entryDist > 0.25) ||
        (['SHORT', 'SELL'].includes(sigDir) && entryDist < -0.25)
    );
    const entryWarningChip = isEntryCrossed ? `
        <span style="display:inline-block;font-size: 0.70rem;font-weight:600;color:var(--yellow);background:rgba(245,158,11,0.15);border:1px solid rgba(245,158,11,0.3);padding:1px 6px;border-radius:4px;margin-top:3px;" title="Limit entry already crossed; chasing risk.">
            Price moved ${entryDist >= 0 ? '+' : ''}${entryDist.toFixed(2)}% past entry
        </span>` : '';

    // Confluence score and conflict penalty chips
    const confScore = sig?.confluence_score ?? sig?.confidence;
    const confPenalties = sig?.confluence_penalties || [];
    let confChipsHTML = '';
    if (confPenalties.length > 0) {
        confChipsHTML = `<div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:3px;">` +
            confPenalties.map(p => `<span style="font-size: 0.70rem;background:rgba(239,68,68,0.15);color:var(--red);border-radius:3px;padding:1px 4px;font-family:var(--font-mono);" title="Conflict penalty">${p}</span>`).join('') +
            `</div>`;
    }

    // Entry / SL / Target Trade Section
    const hasSignal = ['LONG', 'SHORT', 'BUY', 'SELL'].includes(sigDir);
    let tradeSectionHTML = '';
    if (hasSignal && (slVal || tpVal)) {
        if (!isSignalStale && isTargetHit) {
            const gainPct = entryVal > 0 && tpVal ? Math.abs((tpVal - entryVal) / entryVal * 100).toFixed(2) : '0';
            tradeSectionHTML = `
                <div class="info-item" style="grid-column:1/-1;border-top:1px solid var(--border-color);padding-top:0.55rem;margin-top:0.1rem;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.3rem;">
                        <span style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">Previous Completed Trade</span>
                        <span style="font-size: 0.72rem;color:var(--green);font-weight:700;">✓ Target Achieved (+${gainPct}%)</span>
                    </div>
                    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;text-align:center;">
                        <div>
                            <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">Entry</div>
                            <div style="font-size:0.88rem;font-weight:700;font-family:var(--font-mono);color:var(--text-primary);">${curr}${(entryVal||0).toFixed(2)}</div>
                        </div>
                        <div>
                            <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">Stop Loss</div>
                            <div style="font-size:0.88rem;font-weight:700;font-family:var(--font-mono);color:var(--red);">${slVal ? curr + slVal.toFixed(2) : '—'}</div>
                        </div>
                        <div>
                            <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">Target Hit</div>
                            <div style="font-size:0.88rem;font-weight:700;font-family:var(--font-mono);color:var(--green);">${tpVal ? curr + tpVal.toFixed(2) : '—'}</div>
                        </div>
                    </div>
                </div>`;
        } else {
            const staleBanner = isSignalStale ? `
                <div style="grid-column:1/-1;background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.3);color:var(--yellow);padding:5px 8px;border-radius:6px;font-size: 0.72rem;font-weight:600;margin-bottom:0.4rem;text-align:center;">
                    Last session levels · not actionable ${ageHuman ? `(data ${ageHuman} old)` : ''}
                </div>` : '';
            const dimStyle = isSignalStale ? 'opacity:0.6;' : '';
            tradeSectionHTML = `
                <div class="info-item" style="grid-column:1/-1;border-top:1px solid var(--border-color);padding-top:0.6rem;margin-top:0.1rem;display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;${dimStyle}">
                    ${staleBanner}
                    <div style="text-align:center">
                        <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Entry</div>
                        <div style="font-size:0.92rem;font-weight:700;font-family:var(--font-mono);color:var(--text-primary);">${curr}${(entryVal||0).toFixed(2)}</div>
                    </div>
                    <div style="text-align:center">
                        <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Stop Loss</div>
                        <div style="font-size:0.92rem;font-weight:700;font-family:var(--font-mono);color:var(--red);">${slVal ? curr + slVal.toFixed(2) : '—'}</div>
                    </div>
                    <div style="text-align:center">
                        <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Target</div>
                        <div style="font-size:0.92rem;font-weight:700;font-family:var(--font-mono);color:var(--green);">${tpVal ? curr + tpVal.toFixed(2) : '—'}</div>
                    </div>
                </div>`;
        }
    }

    const sigDimStyle = isSignalStale ? 'opacity:0.6;' : '';
    const signalDisplayHTML = `
        <div style="${sigDimStyle}">
            <span style="color:${sigColor}">${displaySig}</span> ${statusBadge}
            ${entryWarningChip}
        </div>`;

    const confluenceDisplayHTML = `
        <div>
            <span>${confScore != null ? `${confScore}/100` : '—'}</span>
            ${confChipsHTML}
        </div>`;

    const items = [
        { label: 'Close',       value: `${curr}${Number(last.close).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` },
        { label: 'RSI (14)',    value: rsiLabel, color: rsiColor },
        { label: 'MACD Hist',   value: macdHist != null ? `${macdIcon} ${Math.abs(macdHist).toFixed(4)}` : '—', color: macdColor },
        { label: 'Signal',      value: signalDisplayHTML, rawHtml: true },
        { label: 'Confluence',  value: confluenceDisplayHTML, rawHtml: true },
        { label: 'Regime',      value: sig?.regime || '—' },
        { label: `${tf.toUpperCase()} Trend`, value: localTrend, color: localColor },
        { label: 'Daily Macro', value: macroTrend + macroConflictChip, color: macroColor, rawHtml: true },
        { label: 'EMA 20',      value: last.ema_20 ? `${curr}${last.ema_20.toFixed(2)}` : '—' },
        { label: 'EMA 50',      value: last.ema_50 ? `${curr}${last.ema_50.toFixed(2)}` : '—' },
        { label: 'VWAP',        value: last.vwap   ? `${curr}${last.vwap.toFixed(2)}` : '—' },
    ];

    if (sig?.quality_score != null) {
        items.push({ 
            label: 'Quality', 
            value: `${sig.quality_score} ${sig.quality_grade || ''}`,
            color: sig.quality_grade === 'A' ? 'var(--green)' : sig.quality_grade === 'D' ? 'var(--red)' : null 
        });
    }

    const grid = document.getElementById('modalInfoGrid');
    if (grid) {
        grid.innerHTML = items.map(item => `
            <div class="info-item">
                <span class="info-label">${item.label}</span>
                <span class="info-value" ${item.color && !item.rawHtml ? `style="color:${item.color}"` : ''}>${item.value}</span>
            </div>
        `).join('') + tradeSectionHTML;
    }
}


function updateDynamicMarketStatus() {



    const el = document.getElementById('marketStatus');



    if (!el) return;



    const t = _getIstDate();
    const jsDay = new Date(Date.UTC(t.year, t.month - 1, t.day)).getUTCDay();
    const mins = t.hour * 60 + t.minute;

    // Special scheduled sessions (e.g. Diwali Muhurat Trading on Sunday 8 Nov 2026: 14:20 to 15:30 IST)
    const isMuhuratDay = (t.year === 2026 && t.month === 11 && t.day === 8);
    const isMuhuratOpen = isMuhuratDay && (mins >= 14 * 60 + 20 && mins <= 15 * 60 + 30);
    const isMuhuratPre = isMuhuratDay && (mins >= 14 * 60 + 5 && mins < 14 * 60 + 20);

    const isWeekend = !isMuhuratDay && (jsDay === 0 || jsDay === 6);
    const isPreMarket = isMuhuratPre || (!isWeekend && (mins >= 9 * 60 && mins < 9 * 60 + 15));
    const isOpen = isMuhuratOpen || (!isWeekend && (mins >= 9 * 60 + 15 && mins <= 15 * 60 + 30));

    if (isOpen) {
        if (isMuhuratOpen) {
            el.innerHTML = '<span class="status-dot live" style="background:#10b981;box-shadow:0 0 8px #10b981;"></span><span style="color:#10b981;font-weight:700;letter-spacing:0.04em;">NSE OPEN · MUHURAT TRADING</span>';
            el.title = 'Diwali Muhurat Trading Session Active (14:20 - 15:30 IST) · Samvat 2083';
        } else if (_wsConnected) {
            el.innerHTML = '<span class="status-dot live" style="background:#10b981;"></span><span style="color:#10b981;font-weight:700;letter-spacing:0.04em;">NSE OPEN · 15m DELAYED</span>';
            el.title = 'NSE Indian Market Open (09:15 - 15:30 IST) | Quotes delayed ~15m per exchange compliance on zero-cost public feed. WebSocket active.';
        } else {
            el.innerHTML = '<span class="status-dot" style="background:#f59e0b;box-shadow:0 0 6px rgba(245,158,11,0.6);"></span><span style="color:#f59e0b;font-weight:700;letter-spacing:0.04em;">NSE OPEN · SYNCING</span>';
            el.title = 'Indian Market Open (09:15 - 15:30 IST) | Reconnecting to live feed';
        }
    } else if (isPreMarket) {
        el.innerHTML = '<span class="status-dot" style="background:#eab308;"></span><span style="color:#eab308;font-weight:700;letter-spacing:0.04em;">NSE PRE-OPEN</span>';
        el.title = isMuhuratPre ? 'Diwali Muhurat Pre-open Session (14:05 - 14:20 IST)' : 'Pre-market session active (09:00 - 09:15 IST)';
    } else {
        el.innerHTML = '<span class="status-dot" style="background:#ef4444;"></span><span style="color:#94a3b8;font-weight:600;letter-spacing:0.04em;">NSE CLOSED</span>';
        el.title = `Market closed. Next session: ${typeof _nextNseOpenLabel === 'function' ? _nextNseOpenLabel() : '09:15 AM IST'}`;
    }

    // Update Edge Radar market state badge (F13)
    const eeText = document.getElementById('eeMarketStateText');
    const eePill = document.getElementById('eeLiveStatusPill');
    if (eeText && eePill) {
        if (isOpen) {
            eeText.textContent = isMuhuratOpen ? 'NSE OPEN · MUHURAT TRADING' : 'NSE OPEN · 15m Delayed';
            eePill.className = 'ee-telemetry-pill live';
        } else if (isPreMarket) {
            eeText.textContent = 'NSE PRE-OPEN';
            eePill.className = 'ee-telemetry-pill';
        } else if (isWeekend) {
            eeText.textContent = 'NSE CLOSED · WEEKEND';
            eePill.className = 'ee-telemetry-pill';
        } else {
            eeText.textContent = 'NSE CLOSED · OFF-HOURS';
            eePill.className = 'ee-telemetry-pill';
        }
    }



}







function startClock() {



    const update = () => {



        const now = new Date();



        const timeStr = now.toLocaleTimeString('en-IN', {



            timeZone: 'Asia/Kolkata',



            hour: '2-digit',



            minute: '2-digit',



            second: '2-digit',



            hour12: true,



        });



        const clockEl = document.getElementById('clock');



        if (clockEl) {



            clockEl.textContent = `${timeStr} IST`;



        }



        updateDynamicMarketStatus();



    };



    update();



    setInterval(update, 1000);



}











// ── Market Tab (Screener + Sectors) ─────────────────────







let screenerData = { gainers: [], losers: [] };



let currentScreenerMode = 'gainers';







async function loadMarket() {



    const loadSectors = async () => {



        try {



            const sectorRes = await fetchWithTimeout(`${API}/api/sectors`, {}, 20000);



            const sectors = await parseJsonResponse(sectorRes);



            renderSectorHeatmap(sectors.sectors || []);



        } catch (err) {



            console.warn('Sector heatmap load error:', err);



            const grid = document.getElementById('sectorGrid');



            if (grid) grid.innerHTML = '<p style="color:var(--text-muted);padding:2rem;text-align:center;">Sector data unavailable</p>';



        }



    };







        const loadScreenerData = async () => {
        try {
            const [gainersRes, losersRes] = await Promise.allSettled([
                fetchWithTimeout(`${API}/api/screener/gainers`, {}, 40000),
                fetchWithTimeout(`${API}/api/screener/losers`, {}, 40000),
            ]);

            if (gainersRes.status === 'fulfilled' && gainersRes.value.ok) {
                const gainers = await parseJsonResponse(gainersRes.value);
                screenerData.gainers = gainers.data || [];
            }
            if (losersRes.status === 'fulfilled' && losersRes.value.ok) {
                const losers = await parseJsonResponse(losersRes.value);
                screenerData.losers = losers.data || [];
            }

            if (screenerData.gainers.length || screenerData.losers.length) {
                renderScreener(screenerData[currentScreenerMode]);
                const screenerUpd = document.getElementById('screenerUpdatedTime');
                if (screenerUpd) {
                    const now = new Date();
                    screenerUpd.textContent = `Updated ${now.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}`;
                }
            } else {
                const tbody = document.getElementById('screenerBody');
                if (tbody) {
                    tbody.innerHTML = '<tr><td colspan="5" class="empty-row" style="color:var(--text-muted)">⚠️ Screener data unavailable — <a href="#" onclick="loadMarket();return false;" style="color:var(--cyan);text-decoration:underline;cursor:pointer;">Retry</a></td></tr>';
                }
            }
        } catch (err) {
            console.error('Screener load error:', err);
            const tbody = document.getElementById('screenerBody');
            if (tbody) {
                tbody.innerHTML = '<tr><td colspan="5" class="empty-row" style="color:var(--text-muted)">⚠️ Screener data unavailable — <a href="#" onclick="loadMarket();return false;" style="color:var(--cyan);text-decoration:underline;cursor:pointer;">Retry</a></td></tr>';
            }
        }
    };







    const loadContextEvents = async () => {



        try {



            const res = await fetchWithTimeout(`${API}/api/context/events`, {}, 20000);



            if (!res.ok) return;



            const payload = await parseJsonResponse(res);



            renderContextEventsAccordion(payload.events || []);



        } catch (err) {



            console.warn('Context events load error:', err);



            renderContextEventsAccordion([]);



        }



    };







    // Load both datasets independently so one slow API never blocks the other.



    await Promise.allSettled([loadSectors(), loadScreenerData(), loadContextEvents()]);



}







function renderContextEventsAccordion(events) {



    const wrap = document.getElementById('contextEventsAccordion');



    if (!wrap) return;







    if (!events || events.length === 0) {



        wrap.innerHTML = '<div style="color:var(--text-muted);font-size:0.78rem;">No high-impact events in next 7 days.</div>';



        return;



    }







    wrap.innerHTML = events.slice(0, 12).map((evt, idx) => {



        const impact = String(evt.impact || 'LOW').toUpperCase();



        const impactColor = impact === 'HIGH' ? '#ef4444' : impact === 'MEDIUM' ? '#f59e0b' : '#9ca3af';



        const dt = evt.date ? new Date(evt.date).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : 'TBD';



        const name = evt.event_name || 'Event';



        const mkts = evt.relevant_markets || 'GLOBAL';



        const days = Number(evt.days_away ?? 99);



        return `



            <details ${idx === 0 ? 'open' : ''} style="border:1px solid var(--border-color);border-radius:8px;padding:0.5rem 0.6rem;background:rgba(255,255,255,0.02);">



                <summary style="cursor:pointer;display:flex;justify-content:space-between;align-items:center;list-style:none;">



                    <span style="font-size:0.8rem;color:var(--text-primary);font-weight:600;">${name}</span>



                    <span style="font-size: 0.72rem;color:${impactColor};font-weight:700;">${impact}</span>



                </summary>



                <div style="margin-top:0.45rem;font-size:0.74rem;color:var(--text-muted);line-height:1.45;">



                    <div>${dt}</div>



                    <div>Affected: ${mkts}</div>



                    <div>${days >= 0 ? `${days} day(s) away` : ''}</div>



                </div>



            </details>



        `;



    }).join('');



}







function showScreener(mode) {



    currentScreenerMode = mode;



    const gBtn = document.getElementById('gainersBtn');



    const lBtn = document.getElementById('losersBtn');



    const pill = document.getElementById('screenerPill');







    gBtn.classList.toggle('active', mode === 'gainers');



    lBtn.classList.toggle('active', mode === 'losers');



    lBtn.classList.toggle('loser', mode === 'losers');







    const data = screenerData[mode] || [];



    renderScreener(data);







    if (mode === 'gainers') {



        const count = data.length;



        pill.style.color = 'var(--green)';



        pill.textContent = `● ${count} stocks up`;



    } else {



        const count = data.length;



        pill.style.color = 'var(--red)';



        pill.textContent = `● ${count} stocks down`;



    }



}









let screenerSortCol = null;
let screenerSortAsc = false;

function sortScreener(col) {
    if (screenerSortCol === col) {
        screenerSortAsc = !screenerSortAsc;
    } else {
        screenerSortCol = col;
        screenerSortAsc = (col === 'symbol');
    }
    const data = screenerData[currentScreenerMode] || [];
    data.sort((a, b) => {
        let va = a[col] ?? 0;
        let vb = b[col] ?? 0;
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return screenerSortAsc ? -1 : 1;
        if (va > vb) return screenerSortAsc ? 1 : -1;
        return 0;
    });
    renderScreener(data);
}

async function refreshScreener(force = false) {
    const icon = document.getElementById('screenerRefreshIcon');
    const btn = document.getElementById('screenerRefreshBtn');
    if (icon) icon.style.animation = 'spin 1s linear infinite';
    if (btn) btn.disabled = true;

    try {
        const [gainersRes, losersRes] = await Promise.all([
            fetchWithTimeout(`${API}/api/screener/gainers${force ? '?refresh=1' : ''}`, {}, 20000),
            fetchWithTimeout(`${API}/api/screener/losers${force ? '?refresh=1' : ''}`, {}, 20000),
        ]);

        const gainers = await parseJsonResponse(gainersRes);
        const losers = await parseJsonResponse(losersRes);

        screenerData.gainers = gainers.data || [];
        screenerData.losers = losers.data || [];

        renderScreener(screenerData[currentScreenerMode]);

        const upd = document.getElementById('screenerUpdatedTime');
        if (upd) {
            const now = new Date();
            upd.textContent = `Updated ${now.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}`;
        }
    } catch (err) {
        console.error('Error refreshing screener:', err);
    } finally {
        if (icon) icon.style.animation = '';
        if (btn) btn.disabled = false;
    }
}

function filterScreener() {



    const q = document.getElementById('screenerSearch').value.toLowerCase();



    const data = screenerData[currentScreenerMode] || [];



    const filtered = data.filter(s =>



        s.symbol.toLowerCase().includes(q) || (s.company || '').toLowerCase().includes(q)



    );



    renderScreener(filtered);



}







function renderScreener(stocks) {



    const tbody = document.getElementById('screenerBody');



    const pill = document.getElementById('screenerPill');







    if (!stocks || stocks.length === 0) {



        tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No data</td></tr>';



        pill.textContent = '● 0 stocks';



        return;



    }







    const isGainer = currentScreenerMode === 'gainers';



    const upCount = stocks.filter(s => s.pct_change > 0).length;



    const downCount = stocks.filter(s => s.pct_change < 0).length;



    pill.innerHTML = `<span style="color:var(--green)">● ${upCount} stocks (${((upCount/stocks.length)*100).toFixed(0)}% Up)</span><br><span style="color:var(--red)">● ${downCount} stocks (${((downCount/stocks.length)*100).toFixed(0)}% Down)</span>`;







    const colors = ['#6366f1','#10b981','#f59e0b','#ef4444','#22d3ee','#f97316','#8b5cf6','#ec4899','#14b8a6','#a78bfa'];







    tbody.innerHTML = stocks.map((s, i) => {



        const chgClass = s.pct_change >= 0 ? 'chg-up' : 'chg-down';



        const arrow = s.pct_change >= 0 ? '▲' : '▼';



        const xClass = s.x_factor >= 2.5 ? 'x-factor-strong' : s.x_factor >= 1.5 ? 'x-factor-med' : 'x-factor-low';



        const initials = s.symbol.substring(0, 2).toUpperCase();



        const colorIdx = i % colors.length;







        const oiDisplay = (s.oi_change != null)



            ? `<span style="color:${s.oi_change >= 0 ? 'var(--green)' : 'var(--red)'}">${s.oi_change >= 0 ? '+' : ''}${s.oi_change}</span>`



            : `<span style="color:var(--text-muted)">—</span>`;







        return `



            <tr onclick="fetchAndShowQuote('${s.symbol}')" style="cursor:pointer;" title="Click to view detailed analysis & chart for ${s.symbol}">



                <td>



                    <div class="stock-symbol-cell">



                        <div class="stock-avatar" style="background:linear-gradient(135deg,${colors[colorIdx]},${colors[(colorIdx+3)%colors.length]})">${initials}</div>



                        <div>



                            <div style="display:flex;align-items:center;gap:0.35rem;">



                                <span class="stock-sym">${s.symbol}</span>



                                ${s.sector ? `<span style="font-size: 0.70rem;padding:1px 5px;border-radius:4px;background:rgba(255,255,255,0.08);color:var(--text-muted);font-weight:500;">${s.sector}</span>` : ''}



                            </div>



                            <span class="stock-name">${s.company || ''}</span>



                        </div>



                    </div>



                </td>



                <td class="ltp-val">₹${s.ltp.toLocaleString('en-IN', {minimumFractionDigits:2})}</td>



                <td class="${chgClass}">${arrow} ${Math.abs(s.pct_change).toFixed(2)}%</td>



                <td><span class="x-factor-badge ${xClass}">⚡ ${s.x_factor.toFixed(2)}x</span></td>



                <td style="font-family:var(--font-mono);font-size:0.78rem;color:var(--text-secondary)">${(s.volume_cr || 0).toFixed(1)}</td>



            </tr>



        `;



    }).join('');



}







function renderSectorHeatmap(sectors) {



    const grid = document.getElementById('sectorGrid');



    if (!sectors || sectors.length === 0) {



        grid.innerHTML = '<p style="color:var(--text-muted);padding:2rem;text-align:center;">No sector data</p>';



        return;



    }







    grid.innerHTML = sectors.map(s => {



        const cls = s.pct_change > 0.3 ? 'bull' : s.pct_change < -0.3 ? 'bear' : 'flat';



        const pctCls = cls;



        const sign = s.pct_change >= 0 ? '+' : '';



        return `



            <div class="sector-tile ${cls}">



                <span class="sector-icon">${s.icons || '📊'}</span>



                <span class="sector-name">${s.name}</span>



                <span class="sector-pct ${pctCls}">${sign}${s.pct_change.toFixed(2)}%</span>



                <span class="sector-adv">${s.adv || 0}↑ ${s.dec || 0}↓</span>



            </div>



        `;



    }).join('');



}











// ── Scorecard Tab ────────────────────────────────────────







async function loadScorecard() {



    try {



        const res = await fetchWithTimeout(`${API}/api/analytics/scorecard`, {}, 25000);



        const data = await parseJsonResponse(res);



        renderScorecard(data);



    } catch (err) {



        console.error('Scorecard load error:', err);



    }



}







function renderScorecard(data) {
    // ── Top KPI row with strict mathematical separation ──
    const total = data.total_signals || 0;
    const wins = data.wins || 0;
    const losses = data.losses || 0;
    const timeouts = data.timeouts || 0;
    const resolved = data.resolved ?? (wins + losses);
    const pending = data.pending_count ?? 0;
    const resolvedWr = resolved > 0 ? ((wins / resolved) * 100).toFixed(1) + '%' : '—';
    const resRate = total > 0 ? ((resolved / total) * 100).toFixed(1) + '%' : '—';
    const isSmallSample = resolved < 30;

    _scKpi('scTotalSignals', total || '—');
    _scKpi('scWinRate', resolvedWr, resolved > 0 ? (wins / resolved >= 0.5 ? 'var(--green)' : 'var(--yellow)') : null);
    _scKpi('scPending', pending || timeouts);

    const winRateCard = document.getElementById('scWinRate');
    if (winRateCard) {
        const lbl = winRateCard.querySelector('.kpi-lbl');
        if (lbl) lbl.textContent = `Win Rate on Resolved (${wins}W / ${losses}L)`;
    }
    const pendingCard = document.getElementById('scPending');
    if (pendingCard) {
        const lbl = pendingCard.querySelector('.kpi-lbl');
        if (lbl) lbl.textContent = `Pending / Timeouts (${pending + timeouts})`;
    }
    const totalCard = document.getElementById('scTotalSignals');
    if (totalCard) {
        const lbl = totalCard.querySelector('.kpi-lbl');
        if (lbl) lbl.textContent = `Total Logged (${resolved} Resolved · ${resRate} Res.)`;
    }

    let statusBanner = document.getElementById('scSampleStatusBanner');
    if (!statusBanner) {
        statusBanner = document.createElement('div');
        statusBanner.id = 'scSampleStatusBanner';
        statusBanner.style.cssText = 'padding:6px 12px;margin-bottom:12px;border-radius:6px;font-size:0.75rem;font-weight:600;display:flex;align-items:center;gap:8px;';
        const kpiRow = document.getElementById('scKpiRow');
        if (kpiRow && kpiRow.parentNode) {
            kpiRow.parentNode.insertBefore(statusBanner, kpiRow);
        }
    }
    if (statusBanner) {
        if (isSmallSample) {
            statusBanner.style.background = 'rgba(245, 158, 11, 0.12)';
            statusBanner.style.border = '1px solid rgba(245, 158, 11, 0.3)';
            statusBanner.style.color = '#f59e0b';
            statusBanner.innerHTML = `⚠️ <span>INSUFFICIENT RESOLVED SAMPLE (n=${resolved} < 30) — Strategy win rate is evaluated strictly on resolved trades.</span>`;
        } else {
            statusBanner.style.background = 'rgba(16, 185, 129, 0.12)';
            statusBanner.style.border = '1px solid rgba(16, 185, 129, 0.3)';
            statusBanner.style.color = '#10b981';
            statusBanner.innerHTML = `✅ <span>STATISTICALLY VALID SAMPLE (n=${resolved} >= 30) — Resolution rate: ${resRate}.</span>`;
        }
    }







    // Best regime



    const byRegime = data.by_regime || {};



    let bestRegime = '—', bestWR = 0;



    for (const [k, v] of Object.entries(byRegime)) {



        if (v.total >= 3 && v.win_rate > bestWR) { bestWR = v.win_rate; bestRegime = k; }



    }



    _scKpi('scBestRegime', bestRegime === '—' ? '—' : bestRegime.replace('_', ' '), bestRegime !== '—' ? 'var(--green)' : null);







    const emptyMsg = '<p style="color:var(--text-muted);font-size:0.8rem;padding:0.5rem 0">No data yet — builds as signals resolve</p>';







    // ── By Regime ─────────────────────────────────────────



    const regimeEl = document.getElementById('scRegimeGrid');



    regimeEl.innerHTML = Object.keys(byRegime).length



        ? Object.entries(byRegime).map(([k, v]) => _scWrRow(k.replace('_', ' '), v)).join('')



        : emptyMsg;







    // ── By Quality Grade ──────────────────────────────────



    const gradeEl  = document.getElementById('scGradeGrid');



    const gradeColors = { A: '#10b981', B: '#818cf8', C: '#f59e0b', D: '#6b7280' };



    const byGrade  = data.by_quality_grade || {};



    gradeEl.innerHTML = Object.keys(byGrade).length



        ? ['A','B','C','D'].filter(g => byGrade[g]).map(g => _scWrRow(`Grade ${g}`, byGrade[g], gradeColors[g])).join('')



        : emptyMsg;







    // ── By Hour chart (Canvas2D bar chart) ────────────────



    const byHour = data.by_hour || {};



    _drawHourChart(byHour);







    // ── By Confidence ─────────────────────────────────────



    const confEl = document.getElementById('scConfGrid');



    const byConf = data.by_confidence || {};



    confEl.innerHTML = Object.keys(byConf).length



        ? ['0-30','30-50','50-70','70-100'].filter(k => byConf[k]).map(k => _scWrRow(k + '%', byConf[k])).join('')



        : emptyMsg;







    // ── False signal reasons ──────────────────────────────



    const falseEl  = document.getElementById('scFalseReasons');



    const reasons  = data.false_signal_reasons || {};



    const maxCount = Math.max(1, ...Object.values(reasons));



    falseEl.innerHTML = Object.keys(reasons).length



        ? Object.entries(reasons).map(([k, v]) => `



            <div class="sc-false-row">



                <span class="sc-false-name">${k.toLowerCase().replace(/_/g,' ')}</span>



                <div class="sc-false-bar-wrap"><div class="sc-false-bar" style="width:${(v/maxCount*100).toFixed(0)}%"></div></div>



                <span class="sc-false-count">${v}</span>



            </div>`).join('')



        : emptyMsg;







    // ── By Symbol table ───────────────────────────────────



    const tbody = document.getElementById('scSymbolTable');



    const bySymbol = data.by_symbol || {};



    const symEntries = Object.entries(bySymbol).sort((a,b) => b[1].total - a[1].total).slice(0,15);



    tbody.innerHTML = symEntries.length



        ? symEntries.map(([sym, v]) => {



            const wrPct = (v.win_rate * 100).toFixed(0);



            const wrColor = v.win_rate >= 0.55 ? 'var(--green)' : v.win_rate >= 0.4 ? 'var(--yellow)' : 'var(--red)';



            return `<tr>



                <td style="font-weight:600">${sym}</td>



                <td>${v.total}</td>



                <td style="color:var(--green)">${v.win}</td>



                <td style="color:var(--red)">${v.loss}</td>



                <td style="color:var(--text-muted)">${v.timeout}</td>



                <td style="color:${wrColor};font-weight:700;font-family:var(--font-mono)">${wrPct}%</td>



            </tr>`;



        }).join('')



        : '<tr><td colspan="6" class="empty-row">No data yet</td></tr>';



}







function _scKpi(id, val, color) {



    const el = document.getElementById(id);



    if (!el) return;



    const valEl = el.querySelector('.kpi-val');



    if (valEl) { valEl.textContent = val; if (color) valEl.style.color = color; }



}







function _scWrRow(label, v, barColor) {



    const pct = (v.win_rate * 100).toFixed(0);



    const color = barColor || (v.win_rate >= 0.55 ? '#10b981' : v.win_rate >= 0.4 ? '#f59e0b' : '#ef4444');



    return `<div class="sc-wr-row">



        <span style="flex:1;font-size:0.8rem">${label}</span>



        <div class="sc-wr-bar-wrap"><div class="sc-wr-bar" style="width:${pct}%;background:${color}"></div></div>



        <span class="sc-wr-pct" style="color:${color}">${pct}%</span>



        <span class="sc-wr-count">${v.total}T</span>



    </div>`;



}







function _drawHourChart(byHour) {



    const canvas = document.getElementById('scHourChart');



    if (!canvas) return;



    const ctx = canvas.getContext('2d');



    const dpr  = window.devicePixelRatio || 1;



    const W    = canvas.offsetWidth || 600;



    const H    = canvas.offsetHeight || 100;



    canvas.width  = W * dpr;



    canvas.height = H * dpr;



    ctx.scale(dpr, dpr);



    ctx.clearRect(0, 0, W, H);







    const hours = Array.from({length: 24}, (_, i) => i);



    const maxWR = 1.0;



    const barW  = (W - 40) / hours.length;



    const chartH = H - 30;







    hours.forEach((h, i) => {



        const v  = byHour[String(h)];



        const wr = v ? v.win_rate : 0;



        const cnt = v ? v.total : 0;



        const bh = wr * chartH;



        const x  = 20 + i * barW;



        const y  = chartH - bh;







        if (cnt > 0) {



            ctx.fillStyle = wr >= 0.55 ? 'rgba(16,185,129,0.8)' : wr >= 0.4 ? 'rgba(245,158,11,0.8)' : 'rgba(239,68,68,0.8)';



            ctx.beginPath();



            ctx.roundRect(x + 1, y, barW - 2, bh, [2, 2, 0, 0]);



            ctx.fill();



        }







        if (h % 3 === 0) {



            ctx.fillStyle = 'rgba(156,163,175,0.5)';



            ctx.font = `${9 * dpr / dpr}px monospace`;



            ctx.textAlign = 'center';



            ctx.fillText(`${h}:00`, x + barW / 2, H - 4);



        }



    });







    // 50% win-rate reference line



    ctx.strokeStyle = 'rgba(99,102,241,0.4)';



    ctx.setLineDash([4, 4]);



    ctx.beginPath();



    ctx.moveTo(20, chartH * 0.5);



    ctx.lineTo(W - 20, chartH * 0.5);



    ctx.stroke();



    ctx.setLineDash([]);



}











// ── Trade Flow Tab ──────────────────────────────────────







async function loadTradeFlow() {



    const grid = document.getElementById('tradeflowGrid');



    if (!grid) return;



    // Show a bounded loading state — will be replaced by error or real data



    grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Loading FII/DII flows...</p></div>';



    try {



        const res = await fetchWithTimeout(`${API}/api/tradeflow`, {}, 25000);



        const data = await parseJsonResponse(res);



        renderTradeFlow(data);



    } catch (err) {



        if (err.name === 'AbortError') {



            console.warn('Trade flow fetch timed out');



            grid.innerHTML = '<div class="loading-state"><p style="color:var(--text-muted)">⏳ FII/DII data timed out — NSE may be slow. <button onclick="loadTradeFlow()" style="margin-left:0.5rem;padding:4px 12px;border-radius:6px;border:1px solid var(--border-color);background:var(--card-bg);color:var(--text-primary);cursor:pointer">Retry</button></p></div>';



        } else {



            console.error('Trade flow load error:', err);



            grid.innerHTML = '<div class="loading-state"><p style="color:var(--text-muted)">⚠️ FII/DII data unavailable — NSE API may be blocked by Akamai. <button onclick="loadTradeFlow()" style="margin-left:0.5rem;padding:4px 12px;border-radius:6px;border:1px solid var(--border-color);background:var(--card-bg);color:var(--text-primary);cursor:pointer">Retry</button></p></div>';



        }



    }



}







function _fmtCr(val) {



    if (val === null || val === undefined) return '—';



    const abs = Math.abs(val);



    const sign = val >= 0 ? '+' : '-';



    if (abs >= 1000) return `${sign}₹${(abs / 1000).toFixed(2)}K Cr`;



    return `${sign}₹${abs.toFixed(0)} Cr`;



}







function renderTradeFlow(data) {



    const grid = document.getElementById('tradeflowGrid');



    if (!data || !data.flows) {



        grid.innerHTML = '<div class="loading-state"><p>No flow data available</p></div>';



        return;



    }







    const { flows, date, sentiment, combined_net, history, source, real_data, stale, updated_at } = data;







    // Sentiment label & class



    const sentMap = {
        FII_STRONG_BUY:     { label: '🟢 FII Strong Buying',       cls: 'buy' },
        FII_STRONG_SELL:    { label: '🔴 FII Strong Selling',      cls: 'sell' },
        FII_MODERATE_BUY:   { label: '🟢 FII Moderate Buying',     cls: 'buy' },
        FII_MODERATE_SELL:  { label: '🔴 FII Moderate Selling',    cls: 'sell' },
        DII_STRONG_BUY:     { label: '🟢 DII Strong Buying',       cls: 'buy' },
        DII_STRONG_SELL:    { label: '🔴 DII Strong Selling',      cls: 'sell' },
        BOTH_BUYING:        { label: '🟢 Both FII & DII Buying',   cls: 'buy' },
        BOTH_SELLING:       { label: '🔴 Both FII & DII Selling',  cls: 'sell' },
        DII_DOMINANT_BUY:   { label: '🟢 DII Dominant Buying',     cls: 'buy' },
        FII_DOMINANT_BUY:   { label: '🟢 FII Dominant Buying',     cls: 'buy' },
        FII_DOMINANT_SELL:  { label: '🔴 FII Dominant Selling',    cls: 'sell' },
        DII_DOMINANT_SELL:  { label: '🟡 DII Selling Pressure',    cls: 'neutral' },
        NEUTRAL:            { label: '⚪ Balanced Flow',           cls: 'neutral' },
        UNAVAILABLE:        { label: '⚠️ Data Unavailable',        cls: 'neutral' },
    };



    const sent = sentMap[sentiment] || { label: sentiment, cls: 'neutral' };







    const sourceTag = real_data



        ? `<span class="tf-source-badge real">✅ NSE Official Data</span>`



        : `<span class="tf-source-badge fake">⚠️ Data Unavailable</span>`;







    const staleTag = stale



        ? `<span class="tf-stale-badge">⏱ Cached</span>` : '';







    const updatedTime = updated_at



        ? new Date(updated_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })



        : '';







    // Flow cards (FII, DII, Retail)



    const flowCards = flows.map(f => {



        const dirCls = f.direction === 'BUY' ? 'buy' : f.direction === 'SELL' ? 'sell' : 'neutral';



        const hasBuySell = f.buy_cr !== null && f.sell_cr !== null;



        const buySellRows = hasBuySell ? `



            <div class="tf-row"><span class="tf-label">Buy</span><span class="tf-val positive">₹${(f.buy_cr/1000).toFixed(2)}K Cr</span></div>



            <div class="tf-row"><span class="tf-label">Sell</span><span class="tf-val negative">₹${(f.sell_cr/1000).toFixed(2)}K Cr</span></div>` : '';







        return `



        <div class="tf-entity-card">



            <div class="tf-entity-header">



                <span class="tf-entity-icon">${f.icon}</span>



                <div>



                    <div class="tf-entity-name">${f.category}</div>



                    <div class="tf-entity-label">${f.label}</div>



                </div>



                <span class="tf-dir-badge ${dirCls}">${f.direction}</span>



            </div>



            ${buySellRows}



            <div class="tf-net-row">



                <span class="tf-label">Net Flow</span>



                <span class="tf-net-val ${f.net_cr >= 0 ? 'positive' : 'negative'} large">${_fmtCr(f.net_cr)}</span>



            </div>



        </div>`;



    }).join('');







    // Mini history chart (canvas)



    const hasHistory = history && history.length > 1;



    const chartSection = hasHistory ? `



        <div class="tf-chart-section">



            <div class="tf-chart-header">



                <span class="tf-chart-title">30-Day FII/DII Net Flow Trend</span>



                <span class="tf-chart-legend"><span class="legend-dot fii-dot"></span>FII &nbsp; <span class="legend-dot dii-dot"></span>DII</span>



            </div>



            <canvas id="tfTrendCanvas" class="tf-trend-canvas"></canvas>



        </div>` : '';







    grid.innerHTML = `



        <div class="tf-summary-header">



            <div class="tf-sentiment-badge ${sent.cls}">${sent.label}</div>



            <div class="tf-meta">



                ${sourceTag}${staleTag}



                <span class="tf-date">📅 ${date || ''}</span>
                <span class="tf-eod-notice" style="font-size: 0.72rem;color:var(--text-muted);background:rgba(255,255,255,0.06);padding:2px 7px;border-radius:4px;border:1px solid rgba(255,255,255,0.08);" title="Official NSE cash settlement data publishes daily after 17:30 IST">Settled EOD Session (Today's live flow publishes after 17:30 IST)</span>



                ${updatedTime ? `<span class="tf-updated">Updated ${updatedTime}</span>` : ''}



            </div>



        </div>



        <div class="tf-combined-bar">



            <span class="tf-combined-label">Combined Institutional Net</span>



            <span class="tf-combined-val ${(combined_net || 0) >= 0 ? 'positive' : 'negative'} large">${_fmtCr(combined_net)}</span>



        </div>



        <div class="tf-entity-grid">



            ${flowCards}



        </div>



        ${chartSection}



    `;







    // Draw history chart after DOM is ready



    if (hasHistory) {



        requestAnimationFrame(() => _drawTfTrendChart(history));



    }



}







function _drawTfTrendChart(history) {



    const canvas = document.getElementById('tfTrendCanvas');



    if (!canvas) return;



    const ctx = canvas.getContext('2d');



    const dpr = window.devicePixelRatio || 1;



    const W = canvas.offsetWidth || 600;



    const H = canvas.offsetHeight || 120;



    canvas.width  = W * dpr;



    canvas.height = H * dpr;



    ctx.scale(dpr, dpr);







    const pts = history.slice(-30);



    const fiiVals = pts.map(p => p.fii_net);



    const diiVals = pts.map(p => p.dii_net);



    const allVals = [...fiiVals, ...diiVals];



    const minV = Math.min(...allVals);



    const maxV = Math.max(...allVals);



    const range = maxV - minV || 1;







    const padL = 12, padR = 12, padT = 10, padB = 20;



    const iW = W - padL - padR;



    const iH = H - padT - padB;







    const toX = i => padL + (i / (pts.length - 1)) * iW;



    const toY = v => padT + (1 - (v - minV) / range) * iH;







    // Zero line



    const zeroY = toY(0);



    ctx.strokeStyle = 'rgba(255,255,255,0.15)';



    ctx.lineWidth = 1;



    ctx.setLineDash([4, 4]);



    ctx.beginPath(); ctx.moveTo(padL, zeroY); ctx.lineTo(W - padR, zeroY); ctx.stroke();



    ctx.setLineDash([]);







    const drawLine = (vals, color) => {



        ctx.strokeStyle = color;



        ctx.lineWidth = 2;



        ctx.beginPath();



        vals.forEach((v, i) => {



            const x = toX(i), y = toY(v);



            i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);



        });



        ctx.stroke();



        // Dots



        vals.forEach((v, i) => {



            ctx.fillStyle = color;



            ctx.beginPath();



            ctx.arc(toX(i), toY(v), 3, 0, Math.PI * 2);



            ctx.fill();



        });



    };







    drawLine(fiiVals, '#f59e0b');  // amber for FII



    drawLine(diiVals, '#22d3ee');  // cyan for DII







    // X-axis labels (first and last date)



    ctx.fillStyle = 'rgba(255,255,255,0.5)';



    ctx.font = '10px sans-serif';



    ctx.textAlign = 'left';



    ctx.fillText(pts[0].date, padL, H - 4);



    ctx.textAlign = 'right';



    ctx.fillText(pts[pts.length - 1].date, W - padR, H - 4);



}











// ── Global Symbol Search ────────────────────────────────







let searchTimeout = null;



let searchDropdownVisible = false;







function initGlobalSearch() {



    const input = document.getElementById('globalSearchInput');



    const dropdown = document.getElementById('searchDropdown');



    const wrap = document.getElementById('globalSearchWrap');



    if (!input) return;







    // Focus on '/' key press



    document.addEventListener('keydown', (e) => {



        if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {



            e.preventDefault();



            input.focus();



        }



        if (e.key === 'Escape' && searchDropdownVisible) {



            closeSearchDropdown();



            input.blur();



        }



    });







    input.addEventListener('input', () => {



        const q = input.value.trim();



        if (q.length < 1) {



            closeSearchDropdown();



            return;



        }



        clearTimeout(searchTimeout);



        searchTimeout = setTimeout(() => searchSymbols(q), 250);



    });







    input.addEventListener('focus', () => {



        const q = input.value.trim();



        if (q.length >= 1) searchSymbols(q);



    });

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const q = input.value.trim();
            if (!q) return;
            clearTimeout(searchTimeout);
            const firstResult = dropdown?.querySelector('.search-item[onclick]:not(.search-item-fetch)');
            closeSearchDropdown();
            if (firstResult) {
                firstResult.click();
            } else {
                fetchAndShowQuote(q);
            }
        }
    });








    // Close on click outside



    document.addEventListener('click', (e) => {



        if (!wrap.contains(e.target)) closeSearchDropdown();



    });



}







async function searchSymbols(query) {



    const resultsEl = document.getElementById('searchResults');



    const dropdown = document.getElementById('searchDropdown');







    const q = (query || '').toUpperCase().trim();
    if (!q) {
        closeSearchDropdown();
        return;
    }

    // 1. Instant client-side matches for immediate 0ms UI feedback (NSE + Crypto)
    const quickCrypto = (typeof _STATIC_CRYPTO_SYMBOLS !== 'undefined' ? _STATIC_CRYPTO_SYMBOLS : [])
        .filter(c => c.base.includes(q) || c.symbol.includes(q))
        .slice(0, 3)
        .map(c => ({ symbol: c.symbol, ticker: c.ticker, type: 'CRYPTO', tracked: c.symbol in (marketDataCache || {}) }));
    const quickNSE = (typeof _STATIC_NSE_SYMBOLS !== 'undefined' ? _STATIC_NSE_SYMBOLS : [])
        .filter(sym => sym.includes(q))
        .slice(0, 5 - quickCrypto.length)
        .map(sym => ({ symbol: sym, ticker: `${sym}.NS`, type: 'NSE', tracked: sym in (marketDataCache || {}) }));
    const quickMatches = [...quickCrypto, ...quickNSE];

    if (quickMatches.length > 0) {
        resultsEl.innerHTML = quickMatches.map(r => `
            <div class="search-item" onclick="fetchAndShowQuote('${r.symbol.replace(/'/g, '')}')" title="Load ${r.symbol} signal card">
                <span class="search-item-icon">📈</span>
                <div class="search-item-info">
                    <span class="search-item-sym">${r.symbol}</span>
                    <span class="search-item-sub">${r.ticker} · NSE</span>
                </div>
                <span class="search-item-arrow">→</span>
            </div>
        `).join('');
    } else {
        resultsEl.innerHTML = `<div class="search-empty" style="opacity:0.6">🔍 Searching NSE for "${q}"...</div>`;
    }
    dropdown.classList.add('visible');
    searchDropdownVisible = true;

    try {
        const resObj = await fetchSafeJSON(`${API}/api/search/symbols?q=${encodeURIComponent(query)}`, {}, 6000);
        if (!resObj.ok) throw new Error(resObj.error || 'Server error');
        const data = resObj.data || {};
        const results = data.results || [];



        // Build the "Try as NSE" direct-fetch fallback shown at the bottom



        const nseDirectHTML = `



            <div class="search-item search-item-fetch" onclick="fetchAndShowQuote('${q.replace(/'/g, '')}')"



                 title="Fetch ${q}.NS from NSE via yFinance">



                <span class="search-item-icon">🇮🇳</span>



                <div class="search-item-info">



                    <span class="search-item-sym">Try "${q}" as NSE Stock</span>



                    <span class="search-item-sub">Auto-appends .NS · fetches via yFinance</span>



                </div>



                <span class="search-item-arrow">→</span>



            </div>`;







        const yfDirectHTML = `



            <div class="search-item search-item-fetch" onclick="fetchAndShowQuote('${query.replace(/'/g, '')}')"



                 title="Search any global ticker on yFinance">



                <span class="search-item-icon">🌐</span>



                <div class="search-item-info">



                    <span class="search-item-sym">Search "${q}" on yFinance</span>



                    <span class="search-item-sub">Fetch live data for any global ticker</span>



                </div>



                <span class="search-item-arrow">→</span>



            </div>`;







        if (results.length === 0) {



            resultsEl.innerHTML = `



                <div class="search-empty">



                    No pre-loaded match for "<strong>${query}</strong>"



                    <br><span class="search-hint">Use the options below to fetch live data ↓</span>



                </div>



                ${nseDirectHTML}



                ${yfDirectHTML}`;



        } else {



            resultsEl.innerHTML = results.map(r => {



                const trackedBadge = r.tracked



                    ? '<span class="search-tracked">●&nbsp;LIVE</span>'



                    : '';



                // Dynamic results validated in real-time: show live price



                const dynamicBadge = r.dynamic && r.price



                    ? `<span style="font-size: 0.72rem;color:var(--green);font-family:var(--font-mono);font-weight:700;">₹${r.price.toLocaleString('en-IN')}</span>`



                    : trackedBadge;



                const icon = r.type === 'CRYPTO' ? '₿'



                    : (r.symbol.includes('NIFTY') || r.symbol.includes('SENSEX') || r.symbol.includes('BANK') ? '📊' : '📈');



                const subLabel = r.opportunity



                    ? `Screener Opportunity · ${r.signal}`



                    : (r.dynamic ? `${r.ticker} · NSE Live · ₹${(r.price || 0).toLocaleString('en-IN')}` : r.ticker);



                



                let extraTag = '';



                if (r.opportunity) extraTag = ' <span style="font-size:0.6rem;background:rgba(234,179,8,0.15);color:#eab308;border:1px solid rgba(234,179,8,0.3);padding:1px 5px;border-radius:3px;margin-left:0.3rem;">⚡ OPP</span>';



                else if (r.dynamic) extraTag = ' <span style="font-size:0.6rem;background:rgba(16,185,129,0.15);color:#10b981;border:1px solid rgba(16,185,129,0.3);padding:1px 5px;border-radius:3px;">NSE</span>';







                return `



                    <div class="search-item" onclick="fetchAndShowQuote('${r.symbol.replace(/'/g, '')}')"



                         title="Load ${r.symbol} signal card">



                        <span class="search-item-icon">${icon}</span>



                        <div class="search-item-info">



                            <span class="search-item-sym">${r.symbol}${extraTag}</span>



                            <span class="search-item-sub" style="${r.opportunity ? 'color:var(--yellow);font-weight:600;' : ''}">${subLabel}</span>



                        </div>



                        ${dynamicBadge}



                        <span class="search-item-arrow">→</span>



                    </div>`;



            }).join('');



            // Only show direct NSE fetch if query isn't already in results



            const alreadyExact = results.some(r => r.symbol === q || r.ticker === `${q}.NS`);



            if (!alreadyExact) resultsEl.innerHTML += nseDirectHTML;



        }







        dropdown.classList.add('visible');



        searchDropdownVisible = true;







    } catch (err) {
        console.error('Search error:', err);
        const staticMatches = (typeof _STATIC_NSE_SYMBOLS !== 'undefined' ? _STATIC_NSE_SYMBOLS : [])
            .filter(sym => sym.includes(q))
            .slice(0, 5)
            .map(sym => ({ symbol: sym, ticker: `${sym}.NS`, type: 'NSE', tracked: false }));

        if (staticMatches.length > 0) {
            resultsEl.innerHTML = staticMatches.map(r => `
                <div class="search-item" onclick="fetchAndShowQuote('${r.symbol.replace(/'/g, '')}')" title="Load ${r.symbol} signal card">
                    <span class="search-item-icon">📈</span>
                    <div class="search-item-info">
                        <span class="search-item-sym">${r.symbol}</span>
                        <span class="search-item-sub">${r.ticker} · NSE</span>
                    </div>
                    <span class="search-item-arrow">→</span>
                </div>
            `).join('');
            if (!staticMatches.some(r => r.symbol === q)) {
                resultsEl.innerHTML += nseDirectHTML;
            }
        } else {
            resultsEl.innerHTML = `
                <div class="search-empty" style="color:var(--text-muted);font-size:0.75rem;">
                    Offline search for "<strong>${_escHtml(q)}</strong>"
                </div>
                ${nseDirectHTML}
                ${yfDirectHTML}
            `;
        }
        dropdown.classList.add('visible');
        searchDropdownVisible = true;
    }



}







function closeSearchDropdown() {



    const dropdown = document.getElementById('searchDropdown');



    if (dropdown) dropdown.classList.remove('visible');



    searchDropdownVisible = false;



}







async function fetchAndShowQuote(symbol) {



    closeSearchDropdown();



    document.getElementById('globalSearchInput').value = '';







    // Show loading modal immediately



    const titleEl = document.getElementById('modalTitle');



    const infoGrid = document.getElementById('modalInfoGrid');



    titleEl.textContent = `Loading ${symbol.toUpperCase()}...`;



    document.getElementById('modalBackdrop').classList.add('visible');



    infoGrid.innerHTML = `



        <div class="loading-state" style="grid-column:1/-1;padding:2rem;text-align:center">



            <div class="loader"></div>



            <p style="margin-top:0.75rem;font-size:0.8rem;color:var(--text-muted)">Fetching ${symbol.toUpperCase()} from NSE...</p>



        </div>`;







    // Clear the chart



    const canvas = document.getElementById('modalChart');



    if (canvas) {



        const ctx = canvas.getContext('2d');



        ctx.clearRect(0, 0, canvas.width, canvas.height);



    }







    try {



        // Crypto symbols: BTC/USDT → BTC-USDT for URL safety



        const urlSym = symbol.replace(/\//g, '-');
        let resObj = await fetchSafeJSON(`${API}/api/search/quote/${encodeURIComponent(urlSym)}`, {}, 20000);

        if (!resObj.ok && (resObj.status === 0 || resObj.status === 502 || resObj.status === 503 || resObj.status === 504 || resObj.error === 'Failed to fetch' || resObj.error === 'Timeout')) {
            const loaderP = infoGrid.querySelector('p');
            if (loaderP) {
                loaderP.textContent = `Backend waking up... Retrying ${symbol.toUpperCase()} (attempt 2/2)...`;
            }
            await new Promise(r => setTimeout(r, 1200));
            resObj = await fetchSafeJSON(`${API}/api/search/quote/${encodeURIComponent(urlSym)}`, {}, 25000);
        }

        if (!resObj.ok) {
            const err = resObj.data || {};
            const status = resObj.status || 0;
            const detailObj = (typeof err.detail === 'object' && err.detail !== null) ? err.detail : ((typeof resObj.detail === 'object' && resObj.detail !== null) ? resObj.detail : {});
            let titleText = 'Symbol Not Found';
            let errMsg = err.error || err.detail?.error || resObj.error || 'Symbol not found on NSE or yFinance';
            let detailMsg = detailObj.message || detailObj.hint || '';
            let showNseHint = true;

            if (status === 429) {
                titleText = 'Rate Limited (Too Many Requests)';
                errMsg = 'Cloudflare or API rate limit active. Please wait a few seconds and retry.';
                showNseHint = false;
            } else if (status === 502 || status === 503 || status === 504) {
                titleText = 'Data Feed Starting / Waking Up';
                errMsg = 'The market data backend is cold-starting or busy. Please retry in a few seconds.';
                showNseHint = false;
            } else if (errMsg === 'Failed to fetch' || errMsg === 'Timeout' || status === 0 || (typeof errMsg === 'string' && errMsg.toLowerCase().includes('failed to fetch'))) {
                titleText = 'Backend Reconnecting / Cold-Starting';
                errMsg = 'Market data backend is waking up or network timed out. Click below to retry.';
                showNseHint = false;
            }

            titleEl.textContent = symbol.toUpperCase();
            infoGrid.innerHTML = `
                <div style="grid-column:1/-1;text-align:center;padding:2rem;">
                    <div style="font-size:2rem;margin-bottom:0.75rem;">⚠️</div>
                    <div style="color:var(--red);font-weight:600;margin-bottom:0.5rem;">${titleText}</div>
                    <div style="color:var(--text-muted);font-size:0.8rem;margin-bottom:0.75rem;">${_escHtml(errMsg)}</div>
                    ${detailMsg ? `<div style="color:var(--text-secondary);font-size:0.75rem;margin-bottom:0.75rem;">${_escHtml(detailMsg)}</div>` : ''}
                    <button class="btn btn-secondary" onclick="openSymbolModal('${symbol}')" style="margin-bottom:0.75rem;padding:0.35rem 0.9rem;font-size:0.75rem;cursor:pointer;">🔄 Retry Fetch</button>
                    ${showNseHint ? `
                    <div style="margin-top:0.5rem;font-size:0.75rem;color:var(--text-muted);">
                        Make sure the NSE symbol is correct (e.g. <code style="color:var(--cyan)">PRECWIRE</code>, <code style="color:var(--cyan)">VEDL</code>)
                    </div>` : ''}
                </div>`;
            return;
        }

        const data = resObj.data;
        titleEl.textContent = data.symbol;
        updateModalPinButton(data.symbol);







        // ── Draw chart ─────────────────────────────────────────────



        if (data.chart && data.chart.length > 1) {



            drawModalChart(data.chart, '5m');



        }







        // ── Rich signal card — matches dashboard card format ───────



        const sig = data.signal || {};



        const isCrypto = data.symbol.includes('/');



        const curr = isCrypto ? '$' : '₹';



        const last = data.chart && data.chart.length > 0 ? data.chart[data.chart.length - 1] : {};







        const sigVal   = sig?.signal || 'HOLD';

        const sigDir   = sig?.direction || sigVal;

        const entryVal = sig?.entry ?? data.close;

        const slVal    = sig?.stop_loss;

        const tpVal    = sig?.target;

        const curClose = Number(data.close || 0);

        // Determine trade lifecycle status (active vs target hit vs stopped out)
        const status = sig?.signal_status || (
            sigVal === 'LONG' && tpVal && curClose >= tpVal ? 'TARGET_HIT' :
            sigVal === 'SHORT' && tpVal && curClose <= tpVal ? 'TARGET_HIT' :
            sigVal === 'LONG' && slVal && curClose <= slVal ? 'STOPPED_OUT' :
            sigVal === 'SHORT' && slVal && curClose >= slVal ? 'STOPPED_OUT' :
            sig?.stale ? 'STALE' : 'ACTIVE'
        );

        const isTargetHit = status === 'TARGET_HIT';
        const isStoppedOut = status === 'STOPPED_OUT';
        const displaySig = (isTargetHit || isStoppedOut) ? 'HOLD' : sigVal;

        let statusBadge = '';
        if (isTargetHit) {
            statusBadge = '<span style="font-size: 0.70rem;font-weight:700;color:var(--green);padding:1px 6px;border-radius:8px;background:rgba(16,185,129,0.15);margin-left:4px;">✓ TARGET HIT</span>';
        } else if (isStoppedOut) {
            statusBadge = '<span style="font-size: 0.70rem;font-weight:700;color:var(--red);padding:1px 6px;border-radius:8px;background:rgba(239,68,68,0.15);margin-left:4px;">✕ STOPPED OUT</span>';
        } else if (status === 'STALE') {
            statusBadge = '<span style="font-size: 0.70rem;font-weight:700;color:var(--yellow);padding:1px 6px;border-radius:8px;background:rgba(245,158,11,0.15);margin-left:4px;">⏱ STALE</span>';
        }

        const sigColor = ['LONG','BUY'].includes(displaySig)   ? 'var(--green)'
                       : ['SHORT','SELL'].includes(displaySig) ? 'var(--red)'
                       : displaySig === 'SKIP' ? 'var(--text-muted)'
                       : 'var(--cyan)';

        // MACD color
        const macdHist = data.macd_hist ?? last.macd_hist;
        const macdColor = macdHist > 0 ? 'var(--green)' : macdHist < 0 ? 'var(--red)' : 'var(--text-muted)';
        const macdIcon  = macdHist > 0 ? '▲' : macdHist < 0 ? '▼' : '—';

        // RSI color: amber for overbought (informative warning, not error red)
        const rsiVal = data.rsi;
        const rsiColor = rsiVal < 30 ? 'var(--green)' : rsiVal > 70 ? 'var(--yellow)' : 'var(--text-secondary)';
        const rsiLabel = rsiVal != null ? `${rsiVal.toFixed(1)}${rsiVal > 70 ? ' (OB)' : rsiVal < 30 ? ' (OS)' : ''}` : '—';

        // Local Trend (5m) vs Macro Trend (Daily)
        const localTrend = sig?.local_trend || data.local_trend || (last.ema_20 && last.ema_50 ? (last.ema_20 >= last.ema_50 ? 'BULLISH' : 'BEARISH') : 'NEUTRAL');
        const macroTrend = (sig?.trend || '—').replace('TrendDirection.', '');
        const localColor = localTrend === 'BULLISH' ? 'var(--green)' : localTrend === 'BEARISH' ? 'var(--red)' : 'var(--text-muted)';
        const macroColor = macroTrend === 'BULLISH' ? 'var(--green)' : macroTrend === 'BEARISH' ? 'var(--red)' : 'var(--text-muted)';

        // Entry/SL/Target section
        const hasSignal = ['LONG', 'SHORT', 'BUY', 'SELL'].includes(sigDir);
        let tradeSectionHTML = '';
        if (hasSignal && (slVal || tpVal)) {
            if (isTargetHit) {
                const gainPct = entryVal > 0 && tpVal ? Math.abs((tpVal - entryVal) / entryVal * 100).toFixed(2) : '0';
                tradeSectionHTML = `
                    <div class="info-item" style="grid-column:1/-1;border-top:1px solid var(--border-color);padding-top:0.55rem;margin-top:0.1rem;">
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.3rem;">
                            <span style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">Previous Completed Trade</span>
                            <span style="font-size: 0.72rem;color:var(--green);font-weight:700;">✓ Target Achieved (+${gainPct}%)</span>
                        </div>
                        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;text-align:center;">
                            <div>
                                <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">Entry</div>
                                <div style="font-size:0.88rem;font-weight:700;font-family:var(--font-mono);color:var(--text-primary);">${curr}${(entryVal||0).toFixed(2)}</div>
                            </div>
                            <div>
                                <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">Stop Loss</div>
                                <div style="font-size:0.88rem;font-weight:700;font-family:var(--font-mono);color:var(--red);">${slVal ? curr + slVal.toFixed(2) : '—'}</div>
                            </div>
                            <div>
                                <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">Target Hit</div>
                                <div style="font-size:0.88rem;font-weight:700;font-family:var(--font-mono);color:var(--green);">${tpVal ? curr + tpVal.toFixed(2) : '—'}</div>
                            </div>
                        </div>
                    </div>`;
            } else {
                tradeSectionHTML = `
                    <div class="info-item" style="grid-column:1/-1;border-top:1px solid var(--border-color);padding-top:0.6rem;margin-top:0.1rem;display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;">
                        <div style="text-align:center">
                            <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Entry</div>
                            <div style="font-size:0.92rem;font-weight:700;font-family:var(--font-mono);color:var(--text-primary);">${curr}${(entryVal||0).toFixed(2)}</div>
                        </div>
                        <div style="text-align:center">
                            <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Stop Loss</div>
                            <div style="font-size:0.92rem;font-weight:700;font-family:var(--font-mono);color:var(--red);">${slVal ? curr + slVal.toFixed(2) : '—'}</div>
                        </div>
                        <div style="text-align:center">
                            <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Target</div>
                            <div style="font-size:0.92rem;font-weight:700;font-family:var(--font-mono);color:var(--green);">${tpVal ? curr + tpVal.toFixed(2) : '—'}</div>
                        </div>
                    </div>`;
            }
        }

        const items = [
            { label: 'Close',          value: `${curr}${(data.close||0).toLocaleString('en-IN', { minimumFractionDigits:2, maximumFractionDigits:2 })}` },
            { label: 'RSI (14)',       value: rsiLabel, color: rsiColor },
            { label: 'MACD Hist',      value: macdHist != null ? `${macdIcon} ${Math.abs(macdHist).toFixed(4)}` : '—', color: macdColor },
            { label: 'Signal',         value: `${displaySig} ${statusBadge}`, color: sigColor },
            { label: 'Confluence',     value: sig?.confidence != null ? `${sig.confidence}/100` : '—' },
            { label: 'Regime',         value: sig?.regime || data.regime || '—' },
            { label: '5m Trend',       value: localTrend, color: localColor },
            { label: 'Daily Macro',    value: macroTrend, color: macroColor },
            { label: 'EMA 20',         value: last.ema_20 ? `${curr}${last.ema_20.toFixed(2)}` : '—' },
            { label: 'EMA 50',         value: last.ema_50 ? `${curr}${last.ema_50.toFixed(2)}` : '—' },
            { label: 'VWAP',           value: last.vwap   ? `${curr}${last.vwap.toFixed(2)}` : '—' },
        ];



        infoGrid.innerHTML =



            items.map(item => `



                <div class="info-item">



                    <span class="info-label">${item.label}</span>



                    <span class="info-value" ${item.color ? `style="color:${item.color}"` : ''}>${item.value}</span>



                </div>



            `).join('') + tradeSectionHTML;







        // Automatically trigger live backtest for this symbol



        loadModalBacktest(symbol);
    loadModalNews(symbol);







    } catch (err) {



        console.error('Quote fetch error:', err);



        document.getElementById('modalInfoGrid').innerHTML = `



            <div style="grid-column:1/-1;text-align:center;padding:2rem;">



                <div style="font-size:2rem;margin-bottom:0.75rem;">⚠️</div>



                <div style="color:var(--red);font-weight:600;">Failed to fetch data</div>



                <div style="color:var(--text-muted);font-size:0.78rem;margin-top:0.5rem;">Check if the server is running</div>



            </div>`;



    }



}







// Initialize search on DOM ready



document.addEventListener('DOMContentLoaded', () => {



    initGlobalSearch();



});











// ── Mobile Bottom Navigation ────────────────────────────







function initBottomNav() {



    const bottomNav = document.getElementById('mobileBottomNav');



    if (!bottomNav) return;







    // Tab nav items (not "More")



    bottomNav.querySelectorAll('.bottom-nav-item[data-tab]').forEach(btn => {



        btn.addEventListener('click', () => {



            const tab = btn.dataset.tab;



            switchTab(tab);



            updateBottomNav(tab);



        });



    });







    // More button



    const moreBtn = document.getElementById('bnMore');



    const overlay = document.getElementById('moreMenuOverlay');



    const closeBtn = document.getElementById('moreMenuClose');







    if (moreBtn && overlay) {



        moreBtn.addEventListener('click', () => {



            overlay.classList.add('visible');



        });







        overlay.addEventListener('click', (e) => {



            if (e.target === overlay) overlay.classList.remove('visible');



        });







        if (closeBtn) {



            closeBtn.addEventListener('click', () => {



                overlay.classList.remove('visible');



            });



        }







        // More menu items



        overlay.querySelectorAll('.more-menu-item[data-tab]').forEach(item => {



            item.addEventListener('click', () => {



                const tab = item.dataset.tab;



                overlay.classList.remove('visible');



                switchTab(tab);



                updateBottomNav(null); // Deactivate fixed tabs since it's from "More"



            });



        });



    }



}







function updateBottomNav(activeTab) {



    const bottomNav = document.getElementById('mobileBottomNav');



    if (!bottomNav) return;







    bottomNav.querySelectorAll('.bottom-nav-item').forEach(btn => {



        btn.classList.toggle('active', btn.dataset.tab === activeTab);



    });







    // Also sync the header tab buttons



    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));



    const headerTab = document.querySelector(`.tab-btn[data-tab="${activeTab || currentTab}"]`);



    if (headerTab) headerTab.classList.add('active');



}











// ══════════════════════════════════════════════════════════



// ═══ SWING SCANNER TAB ═══════════════════════════════════



// ══════════════════════════════════════════════════════════







let _swingData = [];



let _swingApiMeta = null;







/** Age of last OHLCV bar in days (uses data_as_of from API when present). */



function swingBarAgeDays(setup) {



    const raw = setup.data_as_of || setup.setup_date;



    if (!raw) return 0;



    try {



        return (Date.now() - new Date(raw).getTime()) / 86400000;



    } catch (e) {



        return 0;



    }



}







function swingPickTarget(fibVal, fallbackVal) {



    const f = Number(fibVal);



    if (Number.isFinite(f) && f > 0) return f;



    const fb = Number(fallbackVal);



    return Number.isFinite(fb) ? fb : '—';



}







function renderSwingMetaBar(data) {



    const el = document.getElementById('swingMetaBar');



    if (!el) return;



    if (!data || data.real_data === false) {



        el.textContent = '';



        el.style.display = 'none';



        return;



    }



    el.style.display = 'block';



    const parts = [];



    if (data.updated_at) {



        try {



            parts.push(`Cache refreshed: ${new Date(data.updated_at).toLocaleString()}`);



        } catch (e) {



            parts.push(`Cache refreshed: ${data.updated_at}`);



        }



    }



    const mc = data.market_context;



    if (mc && typeof mc === 'object') {



        const t = mc.index_trend || '—';



        const r = mc.index_regime || '—';



        const sym = mc.index_symbol || 'Index';



        parts.push(`${sym}: trend <strong>${t}</strong>, regime <strong>${r}</strong> (used for LONG/SHORT filter when enabled)`);



    }



    parts.push('Card prices are from the <strong>last daily close</strong> in the scan — verify vs broker before entry.');



    el.innerHTML = parts.join(' · ');



}







async function loadSwingScanner() {



    if (typeof loadPaperScorecard === 'function') loadPaperScorecard();



    if (typeof loadMorningShortlist === 'function') loadMorningShortlist();



    const grid = document.getElementById('swingGrid');



    if (!grid) return;



    grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Scanning for swing setups...</p></div>';







    try {



        const res = await fetchWithTimeout(`${API}/api/swing-scanner`, {}, 60000);



        const data = await parseJsonResponse(res);



        _swingApiMeta = data;



        _swingData = data.setups || [];







        // Update KPIs



        document.getElementById('swingTotal').textContent = data.total_found || _swingData.length;



        document.getElementById('swingLong').textContent = _swingData.filter(s => s.direction === 'LONG').length;



        document.getElementById('swingShort').textContent = _swingData.filter(s => s.direction === 'SHORT').length;







        renderSwingMetaBar(data);



        // Default filter: Under ₹1000 (user's primary swing trading range)



        filterSwing('u1000');



    } catch (err) {
        console.error('Swing scanner error:', err);
        renderSwingMetaBar(null);
        grid.innerHTML = `<div class="loading-state" style="padding:2rem;text-align:center;">
            <p style="color:var(--red);font-weight:700;margin-bottom:6px;">⚠️ Swing Scanner Telemetry Unavailable (${_escHtml(err.message)})</p>
            <p style="color:var(--text-muted);font-size:0.75rem;margin-bottom:12px;">Endpoint encountered upstream rate limit or degradation. Preserving capital rules.</p>
            <button class="btn btn-secondary" onclick="loadSwingScanner()" style="font-size:0.75rem;padding:4px 12px;border-radius:4px;cursor:pointer;">🔄 Retry Scanner</button>
        </div>`;
    }



}







function filterSwing(filter) {



    document.querySelectorAll('#swingFilters .toggle-btn').forEach(b => b.classList.remove('active'));



    



    // Map of filter ID



    const btnMap = {



        'all': 'swFilterAll',



        'u200': 'swFilterU200',



        'u500': 'swFilterU500',



        'u1000': 'swFilterU1000'



    };



    const btn = document.getElementById(btnMap[filter]);



    if (btn) btn.classList.add('active');







    let filtered = _swingData;



    if (filter === 'u200') filtered = _swingData.filter(s => s.entry_zone <= 200);



    else if (filter === 'u500') filtered = _swingData.filter(s => s.entry_zone <= 500);



    else if (filter === 'u1000') filtered = _swingData.filter(s => s.entry_zone <= 1000);



    



    renderSwingSetups(filtered);



}







function renderSwingSetups(setups) {



    const grid = document.getElementById('swingGrid');



    if (!setups.length) {



        grid.innerHTML = '<div class="loading-state"><p style="font-size:0.85rem;color:var(--text-muted)">No active swing setups found matching criteria.</p></div>';



        return;



    }







    grid.innerHTML = setups.map(s => {



        const dirClass = s.direction === 'LONG' ? 'buy' : 'sell';



        const dirColor = s.direction === 'LONG' ? 'var(--green)' : 'var(--red)';



        const strengthColor = s.strength >= 75 ? '#10b981' : s.strength >= 55 ? '#6366f1' : '#f59e0b';



        const strengthPct = Math.min(100, s.strength);







        // Stale = last daily bar is old (setup_date was always "today" and hid real staleness)



        const barAge = swingBarAgeDays(s);



        let staleBadge = '';



        if (barAge >= 3) {



            staleBadge = `<span style="font-size:0.6rem;background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid rgba(239,68,68,0.3);padding:2px 5px;border-radius:3px;margin-left:0.5rem;font-weight:700;" title="Last candle in scan is ${Math.floor(barAge)}d old">Stale ⚠️</span>`;



        }



        const t1 = swingPickTarget(s.fib_tp1, s.target_1);



        const t2 = swingPickTarget(s.fib_tp2, s.target_2);



        const asOfLine = s.data_as_of



            ? `<div style="font-size: 0.70rem;color:var(--text-muted);margin-top:0.35rem;">Last bar: ${(() => { try { return new Date(s.data_as_of).toLocaleString(); } catch (e) { return s.data_as_of; } })()}</div>`



            : '';



        



        let sectorTagHTML = '';



        if (s.sector_tag) {



            const isTailwind = s.sector_tag.includes('TAILWIND');



            const isHeadwind = s.sector_tag.includes('HEADWIND');



            const secColor = isTailwind ? 'var(--green)' : isHeadwind ? 'var(--red)' : 'var(--text-muted)';



            const secBg = isTailwind ? 'rgba(16,185,129,0.1)' : isHeadwind ? 'rgba(239,68,68,0.1)' : 'rgba(255,255,255,0.05)';



            sectorTagHTML = `<div style="font-size: 0.72rem;color:${secColor};background:${secBg};padding:2px 6px;border-radius:4px;display:inline-block;margin-top:0.4rem;border:1px solid ${secColor}40;">${s.sector_tag}</div>`;



        }







        let slWarningHTML = '';



        if (s.sl_capped) {



            slWarningHTML = `<div style="font-size: 0.72rem;color:var(--red);margin-top:0.5rem;background:rgba(239,68,68,0.05);padding:4px 6px;border-radius:4px;border:1px solid rgba(239,68,68,0.2);">⚠️ ${s.sl_warning || 'SL Capped — reduce qty'}</div>`;



        }







        let volBadge = '';



        if (s.indicators && s.indicators.vol_ratio) {



            const volColor = s.indicators.vol_ratio >= 1.5 ? 'var(--green)' : s.indicators.vol_ratio >= 1.0 ? 'var(--text-primary)' : 'var(--text-muted)';



            volBadge = `<span style="font-size: 0.72rem;color:${volColor};border:1px solid var(--border-color);padding:2px 5px;border-radius:3px;">Vol: <b>${s.indicators.vol_ratio}x</b></span>`;



        }







        // Live price vs entry



        let ltpLine = '';



        if (s.current_price != null) {



            const devPct = s.price_vs_entry_pct ?? 0;



            const devSign = devPct >= 0 ? '+' : '';



            const devColor = Math.abs(devPct) > 3 ? 'var(--red)' : Math.abs(devPct) > 1.5 ? '#f59e0b' : 'var(--green)';



            const driftWarning = Math.abs(devPct) > 3



                ? ' — entry level may no longer be actionable'



                : '';



            ltpLine = `<div style="font-size: 0.72rem;margin-top:0.35rem;padding:3px 6px;border-radius:4px;background:rgba(99,102,241,0.08);border:1px solid var(--border-color,#374151);">



                LTP <strong style="font-family:var(--font-mono);">₹${s.current_price}</strong>



                <span style="color:${devColor};font-weight:700;margin-left:0.3rem;">(${devSign}${devPct.toFixed(1)}% vs entry)</span>



                <span style="color:var(--text-muted);font-size:0.6rem;">${driftWarning}</span>



            </div>`;



        }







        return `



        <div class="card" style="padding:1rem;border-top:2px solid ${dirColor};position:relative;overflow:hidden;">



            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.75rem;">



                <div>



                    <div style="display:flex;align-items:center;flex-wrap:wrap;gap:0.5rem;">



                        <span style="font-weight:800;font-size:1.1rem;color:var(--text-primary);">${s.symbol}</span>



                        <span class="signal-badge ${dirClass}" style="font-size:0.7rem;padding:2px 8px;">${s.direction}</span>



                        ${staleBadge}



                        ${volBadge}



                    </div>



                    <div style="font-size:0.72rem;color:var(--text-muted);margin-top:3px;">${s.pattern}</div>



                    ${sectorTagHTML}



                </div>



                <div style="text-align:right;">



                    <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;" title="Confluence Score (0-100) — not a win guarantee">Setup Strength</div>



                    <div style="font-weight:800;font-size:1.2rem;color:${strengthColor};font-family:var(--font-mono);">${s.strength}</div>



                </div>



            </div>



            <!-- Strength bar -->



            <div style="height:4px;background:rgba(255,255,255,0.06);border-radius:2px;margin-bottom:0.75rem;">



                <div style="height:100%;width:${strengthPct}%;background:${strengthColor};border-radius:2px;transition:width 0.5s;"></div>



            </div>



            <!-- Levels -->



            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:0.5rem;margin-bottom:0.25rem;">



                <div style="text-align:center;">



                    <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">Entry</div>



                    <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--text-primary);">₹${s.entry_zone}</div>



                </div>



                <div style="text-align:center;">



                    <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">SL</div>



                    <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--red);">₹${s.stop_loss}</div>



                </div>



                <div style="text-align:center;">



                    <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;" title="Target 1 (Fib if valid, else ATR-based)">T1</div>



                    <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--green);">₹${t1}</div>



                </div>



                <div style="text-align:center;">



                    <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;" title="Target 2 (Fib if valid, else ATR-based)">T2</div>



                    <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--green);">₹${t2}</div>



                </div>



            </div>



            ${ltpLine}



            ${asOfLine}



            ${slWarningHTML}



            <!-- Meta -->



            <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.72rem;margin-top:0.75rem;">



                <div style="display:flex;gap:0.75rem;">



                    <span style="color:var(--text-muted);">R:R <b style="color:var(--text-primary);">${s.risk_reward}x</b></span>



                    <span style="color:var(--text-muted);">⏱ <b style="color:var(--text-primary);">${s.timeframe}</b></span>



                </div>



            </div>



            <!-- Description -->



            <div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.5rem;line-height:1.4;border-top:1px solid var(--border-color,#374151);padding-top:0.5rem;">



                ${s.description}



            </div>



        </div>`;



    }).join('');



}







// ── Morning Shortlist & View Toggle ──────────────────────



function setSwingViewMode(mode) {



    const btnShortlist = document.getElementById('viewToggleShortlist');



    const btnAll = document.getElementById('viewToggleAll');



    const panel = document.getElementById('morningShortlistPanel');







    if (mode === 'shortlist') {



        if (btnShortlist) { btnShortlist.classList.add('active'); btnShortlist.style.background = ''; btnShortlist.style.color = ''; }



        if (btnAll) { btnAll.classList.remove('active'); btnAll.style.background = 'transparent'; btnAll.style.color = 'var(--text-muted)'; }



        if (panel) panel.style.display = 'block';



    } else {



        if (btnAll) { btnAll.classList.add('active'); btnAll.style.background = ''; btnAll.style.color = ''; }



        if (btnShortlist) { btnShortlist.classList.remove('active'); btnShortlist.style.background = 'transparent'; btnShortlist.style.color = 'var(--text-muted)'; }



        if (panel) panel.style.display = 'none';



    }



}







async function loadMorningShortlist() {



    const grid = document.getElementById('shortlistGrid');



    const tsEl = document.getElementById('shortlistTimestamp');



    if (!grid) return;



    grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Loading morning shortlist...</p></div>';







    try {



        const res = await fetchWithTimeout(`${API}/api/swing/shortlist`, {}, 15000);



        let data = {};



        try { data = await res.json(); } catch (_) { data = {}; }



        const setups = data.setups || [];



        if (tsEl) {



            tsEl.textContent = data.generated_at ? `Scan: ${new Date(data.generated_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}` : (data.scan_date || '');



        }



        if (!setups.length) {



            grid.innerHTML = `<div class="loading-state" style="padding:1.5rem;"><p style="font-size:0.85rem;color:var(--text-muted);">${data.message || 'No shortlist setups available yet.'}</p></div>`;



            return;



        }



        renderMorningShortlist(setups);



    } catch (err) {



        console.error('loadMorningShortlist error:', err);



        grid.innerHTML = '<div class="loading-state"><p style="color:var(--red);">Failed to load morning shortlist</p></div>';



    }



}







function renderMorningShortlist(setups) {



    const grid = document.getElementById('shortlistGrid');



    if (!grid) return;



    grid.innerHTML = setups.map(s => {



        const dirClass = s.direction === 'LONG' ? 'buy' : 'sell';



        const dirColor = s.direction === 'LONG' ? 'var(--green, #10b981)' : 'var(--red, #ef4444)';



        const gradeColor = s.grade === 'A' ? '#10b981' : s.grade === 'B' ? '#6366f1' : '#f59e0b';



        const t1 = s.target_1 || s.target || '—';



        const t2 = s.target_2 || '—';



        return `



        <div class="card" style="padding:1rem;border-top:2px solid ${dirColor};background:rgba(255,255,255,0.02);border-radius:8px;">



            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.6rem;">



                <div>



                    <div style="display:flex;align-items:center;gap:0.5rem;">



                        <span style="font-weight:800;font-size:1.05rem;color:var(--text-primary);">${s.symbol}</span>



                        <span class="signal-badge ${dirClass}" style="font-size: 0.72rem;padding:2px 6px;">${s.direction}</span>



                        <span style="font-size: 0.70rem;font-weight:700;padding:1px 6px;border-radius:4px;background:${gradeColor}20;color:${gradeColor};border:1px solid ${gradeColor}40;">Grade ${s.grade || 'A'}</span>



                    </div>



                    <div style="font-size:0.7rem;color:var(--text-muted);margin-top:2px;">${s.pattern || 'Swing Setup'}</div>



                </div>



                <div style="text-align:right;">



                    <div style="font-size: 0.70rem;color:var(--text-muted);" title="Confluence Score (0-100) — not a win guarantee">Setup Strength</div>



                    <div style="font-weight:800;font-size:1.1rem;color:${gradeColor};font-family:var(--font-mono);">${s.strength || s.score || 0}</div>



                </div>



            </div>



            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:0.4rem;text-align:center;margin:0.5rem 0;background:rgba(0,0,0,0.15);padding:0.4rem;border-radius:6px;">



                <div><div style="font-size:0.6rem;color:var(--text-muted);">Entry</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;">₹${s.entry_zone || s.entry || 0}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">SL</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--red);">₹${s.stop_loss || 0}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">T1</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--green);">₹${t1}</div></div>



                <div><div style="font-size:0.6rem;color:var(--text-muted);">T2</div><div style="font-weight:700;font-family:var(--font-mono);font-size:0.8rem;color:var(--green);">₹${t2}</div></div>



            </div>



            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:0.6rem;">



                <span style="font-size: 0.72rem;color:var(--text-muted);">R:R <b style="color:var(--text-primary);">${s.risk_reward || 0}x</b></span>



                <button type="button" class="btn btn-primary" onclick="event.stopPropagation();takePaperTrade('${_escHtml(s.symbol)}', '${s.direction}', ${Number(s.entry_zone || s.entry || 0)}, ${Number(s.stop_loss || 0)}, ${Number(s.target_1 || s.target || 0)}, ${Number(s.atr || 0)}, 'SWING', this)" style="font-size:0.7rem;padding:0.25rem 0.6rem;cursor:pointer;border-radius:4px;background:linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%);color:#fff;border:none;font-weight:600;">⚡ Paper Trade</button>



            </div>



        </div>`;



    }).join('');



}







window.setSwingViewMode = setSwingViewMode;



window.loadMorningShortlist = loadMorningShortlist;















// ══════════════════════════════════════════════════════════



// ═══ INDEX MOVER TAB ═════════════════════════════════════



// ══════════════════════════════════════════════════════════







async function loadIndexMover() {



    const index = document.getElementById('indexMoverSelect')?.value || 'nifty50';



    const gainersEl = document.getElementById('idxTopGainers');



    const draggersEl = document.getElementById('idxTopDraggers');



    const moversBody = document.getElementById('idxMoversBody');







    gainersEl.innerHTML = '<div class="loading-state"><div class="loader"></div></div>';



    draggersEl.innerHTML = '<div class="loading-state"><div class="loader"></div></div>';



    moversBody.innerHTML = '<tr><td colspan="5" class="empty-row">Loading...</td></tr>';







    try {



        const res = await fetch(`${API}/api/index-mover/${index}`);



        const data = await res.json();







        if (data.error) {



            gainersEl.innerHTML = `<p style="color:var(--red);font-size:0.8rem;">${data.error}</p>`;



            return;



        }







        // KPIs



        document.getElementById('idxLevel').textContent = '₹' + (data.index_level || 0).toLocaleString('en-IN');



        const changePts = data.index_change_pts || 0;



        const changePct = data.index_pct || 0;



        const changeEl = document.getElementById('idxChange');



        changeEl.textContent = `${changePts >= 0 ? '+' : ''}${changePts.toFixed(1)}`;



        changeEl.style.color = changePts >= 0 ? 'var(--green)' : 'var(--red)';



        const pctEl = document.getElementById('idxPct');



        pctEl.textContent = `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%`;



        pctEl.style.color = changePct >= 0 ? 'var(--green)' : 'var(--red)';



        document.getElementById('idxUp').textContent = data.stocks_up || 0;



        document.getElementById('idxDown').textContent = data.stocks_down || 0;







        // Top Gainers



        const topG = data.top_gainers || [];



        if (topG.length) {



            const maxPts = Math.max(...topG.map(g => Math.abs(g.points)), 1);



            gainersEl.innerHTML = topG.map(g => `



                <div style="display:flex;align-items:center;gap:0.5rem;padding:0.45rem 0;border-bottom:1px solid var(--border-color,#374151);">



                    <span style="flex:0 0 80px;font-weight:700;font-size:0.85rem;">${g.symbol}</span>



                    <div style="flex:1;height:6px;background:rgba(255,255,255,0.06);border-radius:3px;">



                        <div style="height:100%;width:${Math.round(Math.abs(g.points)/maxPts*100)}%;background:var(--green);border-radius:3px;"></div>



                    </div>



                    <span style="font-family:var(--font-mono);font-size:0.82rem;font-weight:700;color:var(--green);min-width:60px;text-align:right;">+${g.points.toFixed(1)} pts</span>



                    <span style="font-family:var(--font-mono);font-size:0.72rem;color:var(--text-muted);min-width:50px;text-align:right;">${g.pct_change >= 0 ? '+' : ''}${g.pct_change.toFixed(1)}%</span>



                </div>



            `).join('');



        } else {



            gainersEl.innerHTML = '<p style="font-size:0.78rem;color:var(--text-muted);">No positive contributors</p>';



        }







        // Top Draggers



        const topD = data.top_draggers || [];



        if (topD.length) {



            const maxPts = Math.max(...topD.map(d => Math.abs(d.points)), 1);



            draggersEl.innerHTML = topD.map(d => `



                <div style="display:flex;align-items:center;gap:0.5rem;padding:0.45rem 0;border-bottom:1px solid var(--border-color,#374151);">



                    <span style="flex:0 0 80px;font-weight:700;font-size:0.85rem;">${d.symbol}</span>



                    <div style="flex:1;height:6px;background:rgba(255,255,255,0.06);border-radius:3px;">



                        <div style="height:100%;width:${Math.round(Math.abs(d.points)/maxPts*100)}%;background:var(--red);border-radius:3px;"></div>



                    </div>



                    <span style="font-family:var(--font-mono);font-size:0.82rem;font-weight:700;color:var(--red);min-width:60px;text-align:right;">${d.points.toFixed(1)} pts</span>



                    <span style="font-family:var(--font-mono);font-size:0.72rem;color:var(--text-muted);min-width:50px;text-align:right;">${d.pct_change.toFixed(1)}%</span>



                </div>



            `).join('');



        } else {



            draggersEl.innerHTML = '<p style="font-size:0.78rem;color:var(--text-muted);">No negative contributors</p>';



        }







        // Full movers table



        const movers = data.movers || [];



        if (movers.length) {



            moversBody.innerHTML = movers.map(m => {



                const dirIcon = m.direction === 'UP' ? '▲' : m.direction === 'DOWN' ? '▼' : '—';



                const dirColor = m.direction === 'UP' ? 'var(--green)' : m.direction === 'DOWN' ? 'var(--red)' : 'var(--text-muted)';



                return `<tr>



                    <td style="font-weight:700;">${m.symbol}</td>



                    <td style="font-family:var(--font-mono);font-size:0.82rem;">${m.weight.toFixed(1)}%</td>



                    <td style="font-family:var(--font-mono);color:${dirColor};">${m.pct_change >= 0 ? '+' : ''}${m.pct_change.toFixed(2)}%</td>



                    <td style="font-family:var(--font-mono);font-weight:700;color:${dirColor};">${m.points >= 0 ? '+' : ''}${m.points.toFixed(1)}</td>



                    <td style="color:${dirColor};font-weight:700;">${dirIcon} ${m.direction}</td>



                </tr>`;



            }).join('');



        }







    } catch (err) {



        console.error('Index mover error:', err);



        gainersEl.innerHTML = '<p style="color:var(--red);font-size:0.8rem;">Failed to load index data</p>';



        draggersEl.innerHTML = '';



    }



}











// ══════════════════════════════════════════════════════════



// ═══ BREAKOUT SCANNER TAB ════════════════════════════════



// ══════════════════════════════════════════════════════════







let _breakoutData = null;  // Cached for filtering







async function loadBreakouts() {



    const body = document.getElementById('breakoutsBody');



    body.innerHTML = '<tr><td colspan="10" class="empty-row"><div class="loader" style="margin:0 auto;"></div> Scanning for breakouts...</td></tr>';







    try {
        const res = await fetchWithTimeout(`${API}/api/breakouts`, {}, 25000);
        if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
        }
        _breakoutData = await res.json();
        if (_breakoutData.error) {
            throw new Error(_breakoutData.error);
        }







        // KPIs



        document.getElementById('boTotal').textContent = _breakoutData.total_breakouts || 0;



        document.getElementById('boGradeA').textContent = _breakoutData.grade_a_count || 0;



        document.getElementById('boGradeB').textContent = _breakoutData.grade_b_count || 0;



        const w52 = (_breakoutData.week_52_high || []).length + (_breakoutData.week_52_low || []).length;



        document.getElementById('bo52W').textContent = w52;







        renderBreakoutsTable(_breakoutData.all || []);







    } catch (err) {
        console.error('Breakout scanner error:', err);
        body.innerHTML = `<tr><td colspan="10" class="empty-row" style="color:var(--red);padding:2rem;">
            <div style="font-size:1.05rem;font-weight:700;margin-bottom:6px;">⚠️ Breakout Scanner Telemetry Unavailable (${_escHtml(err.message)})</div>
            <div style="font-size:0.75rem;color:var(--text-muted);margin-bottom:12px;">Upstream market scanner service temporarily degraded or rate-limited. Preserving capital rules.</div>
            <button class="btn btn-secondary" onclick="loadBreakouts()" style="font-size:0.75rem;padding:4px 12px;border-radius:4px;cursor:pointer;">🔄 Retry Telemetry</button>
        </td></tr>`;
    }



}







function filterBreakouts(filter) {



    // Update active button



    document.querySelectorAll('#breakoutsTab .toggle-btn').forEach(b => b.classList.remove('active'));



    const btnMap = {



        'all': 'boFilterAll',



        'bullish_10d': 'boFilter10H',



        'bearish_10d': 'boFilter10L',



        'bullish_50d': 'boFilter50H',



        'bearish_50d': 'boFilter50L',



        '52w': 'boFilter52W',



    };



    const btn = document.getElementById(btnMap[filter]);



    if (btn) btn.classList.add('active');







    if (!_breakoutData) return;







    let filtered;



    if (filter === 'all') {



        filtered = _breakoutData.all || [];



    } else if (filter === '52w') {



        filtered = [...(_breakoutData.week_52_high || []), ...(_breakoutData.week_52_low || [])];



    } else {



        filtered = _breakoutData[filter] || [];



    }







    renderBreakoutsTable(filtered);



}







function renderBreakoutsTable(breakouts) {



    const body = document.getElementById('breakoutsBody');







    if (!breakouts.length) {



        body.innerHTML = '<tr><td colspan="10" class="empty-row">No breakouts found in this category</td></tr>';



        return;



    }







    body.innerHTML = breakouts.map(b => {



        const dirColor = b.direction === 'BULLISH' ? 'var(--green)' : 'var(--red)';



        const dirIcon = b.direction === 'BULLISH' ? '▲' : '▼';







        const gradeColor = {A: '#10b981', B: '#6366f1', C: '#f59e0b'}[b.grade] || '#6b7280';



        const gradeBg = {A: 'rgba(16,185,129,0.12)', B: 'rgba(99,102,241,0.12)', C: 'rgba(245,158,11,0.12)'}[b.grade] || 'rgba(107,114,128,0.1)';







        const typeLabel = {



            '10D_HIGH': '10D High',



            '10D_LOW': '10D Low',



            '50D_HIGH': '50D High',



            '50D_LOW': '50D Low',



            '52W_HIGH': '52W High',



            '52W_LOW': '52W Low',



        }[b.breakout_type] || b.breakout_type;







        // Strength bar



        const strengthPct = Math.min(100, b.strength_score);



        const strengthColor = b.strength_score >= 75 ? '#10b981' : b.strength_score >= 50 ? '#6366f1' : '#f59e0b';







        return `<tr>



            <td style="font-weight:700;">${b.symbol}</td>



            <td style="font-size:0.78rem;">${typeLabel}</td>



            <td style="color:${dirColor};font-weight:700;">${dirIcon} ${b.direction}</td>



            <td><span style="background:${gradeBg};color:${gradeColor};padding:2px 8px;border-radius:4px;font-weight:700;font-size:0.72rem;">${b.grade}</span></td>



            <td style="font-family:var(--font-mono);">₹${b.ltp.toLocaleString('en-IN')}</td>



            <td style="font-family:var(--font-mono);">₹${b.breakout_level.toLocaleString('en-IN')}</td>



            <td style="font-family:var(--font-mono);color:${dirColor};">${b.distance_pct.toFixed(1)}%</td>



            <td style="font-family:var(--font-mono);${b.volume_ratio >= 1.5 ? 'color:var(--green);font-weight:700;' : ''}">${b.volume_ratio.toFixed(1)}x</td>



            <td>



                <div style="display:flex;align-items:center;gap:0.4rem;">



                    <div style="flex:1;height:5px;background:rgba(255,255,255,0.06);border-radius:3px;min-width:40px;">



                        <div style="height:100%;width:${strengthPct}%;background:${strengthColor};border-radius:3px;"></div>



                    </div>



                    <span style="font-family:var(--font-mono);font-size:0.78rem;font-weight:700;color:${strengthColor};">${b.strength_score}</span>



                </div>



            </td>



        </tr>`;



    }).join('');



}











// ── Intraday Strategy Engine Tab ────────────────────────

const _ID_STRAT_COLORS = {
    ORB: { bg: "rgba(99,102,241,0.15)", color: "#a5b4fc", border: "rgba(99,102,241,0.4)" },
    VWAP: { bg: "rgba(59,130,246,0.15)", color: "#60a5fa", border: "rgba(59,130,246,0.4)" },
    VWAP_REV: { bg: "rgba(245,158,11,0.15)", color: "#fbbf24", border: "rgba(245,158,11,0.4)" },
    CPR: { bg: "rgba(14,165,233,0.15)", color: "#38bdf8", border: "rgba(14,165,233,0.4)" },
    SUPERTREND: { bg: "rgba(34,197,94,0.15)", color: "#4ade80", border: "rgba(34,197,94,0.4)" },
    EMA: { bg: "rgba(168,85,247,0.15)", color: "#c084fc", border: "rgba(168,85,247,0.4)" },
    RSI: { bg: "rgba(236,72,153,0.15)", color: "#f472b6", border: "rgba(236,72,153,0.4)" },
    GAP_GO: { bg: "rgba(234,179,8,0.15)", color: "#facc15", border: "rgba(234,179,8,0.4)" },
    MOMENTUM: { bg: "rgba(249,115,22,0.15)", color: "#fb923c", border: "rgba(249,115,22,0.4)" },
    RANGE_BREAK: { bg: "rgba(236,72,153,0.15)", color: "#f472b6", border: "rgba(236,72,153,0.4)" }
};

let _intradayAllSigs = [];
let _intradaySession = null;
let _intradayNiftyContext = null;
let _intradayDataStatus = "DELAYED";
let _intradayCurrentView = "table"; // "table" or "cards"
let _intradayFilterDir = "all"; // "all", "LONG", "SHORT"
let _intradayFilterStrat = "all";
let _intradayFilterConf = "all"; // "all", "70", "80"
let _intradaySearchQuery = "";
let _intradayDensityCompact = true;
let _intradayDrawerChart = null;

function _updateIntradayKpis(sigs, session, niftyContext, dataStatus) {
    sigs = sigs || [];
    const total = sigs.length;
    const longCount = sigs.filter(s => (s.direction || "").toUpperCase() === "LONG").length;
    const shortCount = sigs.filter(s => (s.direction || "").toUpperCase() === "SHORT").length;

    const scores = sigs.map(s => Number(s.confluence_score ?? s.score ?? 0)).filter(n => !isNaN(n) && n > 0);
    const avgScore = scores.length ? (scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(1) + "%" : "—";
    const topScore = scores.length ? Math.max(...scores).toFixed(1) + "%" : "—";

    const tEl = document.getElementById("idKpiTotal");
    const lEl = document.getElementById("idKpiLong");
    const sEl = document.getElementById("idKpiShort");
    const aEl = document.getElementById("idKpiScore") || document.getElementById("idKpiAvgScore");
    const topEl = document.getElementById("idKpiTopScore");
    const sessEl = document.getElementById("idKpiSession");
    const regEl = document.getElementById("idKpiRegime");
    const feedEl = document.getElementById("idKpiFeed");

    if (tEl) tEl.textContent = total;
    if (lEl) lEl.textContent = longCount;
    if (sEl) sEl.textContent = shortCount;
    if (aEl) aEl.textContent = avgScore;
    if (topEl) topEl.textContent = topScore;

    // Session badge & KPI
    if (session) {
        if (sessEl) {
            sessEl.textContent = session.session_label || session.state || "—";
            sessEl.style.color = session.status_color || "#cbd5e1";
        }
        const badgeTxt = document.getElementById("idSessionText");
        const badgeDot = document.getElementById("idSessionDot");
        if (badgeTxt) badgeTxt.textContent = (session.session_label || session.state || "UNKNOWN").toUpperCase();
        if (badgeDot && session.status_color) badgeDot.style.background = session.status_color;
    }

    // Regime KPI
    if (niftyContext && regEl) {
        const trend = niftyContext.trend || "NEUTRAL";
        const regime = niftyContext.regime || "NORMAL";
        regEl.textContent = `${trend} · ${regime}`;
        regEl.style.color = trend === "BULLISH" ? "var(--green)" : (trend === "BEARISH" ? "var(--red)" : "var(--text-muted)");
    }

    // Feed Quality KPI (Strict Truthful Freshness: No Fake LIVE Badges)
    if (feedEl) {
        feedEl.textContent = "15M DELAYED (NSE)";
        feedEl.style.color = "#38bdf8";
    }
}

function setIntradayView(view) {
    _intradayCurrentView = view;
    const btnTable = document.getElementById("idBtnViewTable");
    const btnCards = document.getElementById("idBtnViewCards");
    const tableWrap = document.getElementById("intradayTableViewWrap");
    const cardsWrap = document.getElementById("intradayGrid");

    if (btnTable) btnTable.classList.toggle("active", view === "table");
    if (btnCards) btnCards.classList.toggle("active", view === "cards");

    if (tableWrap) tableWrap.style.display = view === "table" ? "block" : "none";
    if (cardsWrap) cardsWrap.style.display = view === "cards" ? "grid" : "none";

    applyIntradayFilters();
}

function toggleIntradayDensity() {
    _intradayDensityCompact = !_intradayDensityCompact;
    const btn = document.getElementById("idBtnDensity");
    if (btn) btn.textContent = _intradayDensityCompact ? "Compact" : "Comfortable";

    const table = document.getElementById("intradayTable");
    if (table) {
        table.querySelectorAll("th, td").forEach(cell => {
            cell.style.padding = _intradayDensityCompact ? "0.45rem 0.65rem" : "0.75rem 0.95rem";
        });
    }
}

function setIntradayDir(dir) {
    _intradayFilterDir = dir;
    document.querySelectorAll("#intradayFilterToolbar [data-dir]").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.dir === dir);
    });
    applyIntradayFilters();
}

function setIntradayConf(conf) {
    _intradayFilterConf = conf;
    document.querySelectorAll("#intradayFilterToolbar [data-conf]").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.conf === conf);
    });
    applyIntradayFilters();
}

function filterIntradayStrat(strat) {
    _intradayFilterStrat = strat;
    document.querySelectorAll("#intradayStratFilter [data-strat]").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.strat === strat);
    });
    applyIntradayFilters();
}

function resetIntradayFilters() {
    _intradayFilterDir = "all";
    _intradayFilterStrat = "all";
    _intradayFilterConf = "all";
    _intradaySearchQuery = "";

    const searchInput = document.getElementById("idSearchInput");
    if (searchInput) searchInput.value = "";

    document.querySelectorAll("#intradayFilterToolbar [data-dir]").forEach(b => b.classList.toggle("active", b.dataset.dir === "all"));
    document.querySelectorAll("#intradayFilterToolbar [data-conf]").forEach(b => b.classList.toggle("active", b.dataset.conf === "all"));
    document.querySelectorAll("#intradayStratFilter [data-strat]").forEach(b => b.classList.toggle("active", b.dataset.strat === "all"));

    applyIntradayFilters();
}

function applyIntradayFilters() {
    const searchInput = document.getElementById("idSearchInput");
    _intradaySearchQuery = (searchInput ? searchInput.value : "").trim().toUpperCase();

    let filtered = _intradayAllSigs || [];

    // Filter by Strategy
    if (_intradayFilterStrat && _intradayFilterStrat !== "all") {
        filtered = filtered.filter(s => (s.strategy || "").toUpperCase() === _intradayFilterStrat.toUpperCase());
    }

    // Filter by Direction
    if (_intradayFilterDir && _intradayFilterDir !== "all") {
        filtered = filtered.filter(s => (s.direction || "").toUpperCase() === _intradayFilterDir.toUpperCase());
    }

    // Filter by Confluence Score
    if (_intradayFilterConf === "70") {
        filtered = filtered.filter(s => Number(s.confluence_score ?? s.score ?? 0) >= 70);
    } else if (_intradayFilterConf === "80") {
        filtered = filtered.filter(s => Number(s.confluence_score ?? s.score ?? 0) >= 80);
    }

    // Filter by Search query
    if (_intradaySearchQuery) {
        filtered = filtered.filter(s => (s.symbol || "").toUpperCase().includes(_intradaySearchQuery));
    }

    if (_intradayCurrentView === "table") {
        _renderIntradayTable(filtered);
    } else {
        _renderIntradayCards(filtered);
    }
}

function renderIntradayGrid(sigs) {
    _intradayAllSigs = sigs || [];
    _updateIntradayKpis(_intradayAllSigs, _intradaySession, _intradayNiftyContext, _intradayDataStatus);
    applyIntradayFilters();
}

function _renderIntradayTable(sigs) {
    const tbody = document.getElementById("intradayTableBody");
    if (!tbody) return;

    if (!sigs || !sigs.length) {
        const total = (_intradayAllSigs || []).length;
        const msg = total > 0
            ? `No signals match current filter. (${total} available in latest snapshot)`
            : "No intraday signals found in latest snapshot. Run a fresh scan or check back during active session.";
        tbody.innerHTML = `<tr><td colspan="15" style="text-align:center;padding:2.5rem;color:var(--text-muted);">${msg}</td></tr>`;
        return;
    }

    const pad = _intradayDensityCompact ? "0.45rem 0.65rem" : "0.75rem 0.95rem";

    tbody.innerHTML = sigs.map((s, idx) => {
        const dir = (s.direction || "").toUpperCase();
        const dirColor = dir === "LONG" ? "var(--green)" : "var(--red)";
        const dirBg = dir === "LONG" ? "rgba(16,185,129,0.12)" : "rgba(239,68,68,0.12)";
        const dirBorder = dir === "LONG" ? "rgba(16,185,129,0.35)" : "rgba(239,68,68,0.35)";

        const stc = _ID_STRAT_COLORS[s.strategy] || { bg: "rgba(148,163,184,0.15)", color: "#cbd5e1", border: "rgba(148,163,184,0.3)" };
        const score = Number(s.confluence_score ?? s.score ?? 0);
        const scoreColor = score >= 80 ? "#10b981" : (score >= 65 ? "#818cf8" : "#f59e0b");

        const ltpTxt = s.ltp != null ? "₹" + Number(s.ltp).toFixed(2) : (s.entry != null ? "₹" + Number(s.entry).toFixed(2) : "—");
        const entryFmt = s.entry != null ? "₹" + Number(s.entry).toFixed(2) : "—";
        const slFmt = s.stop_loss != null ? "₹" + Number(s.stop_loss).toFixed(2) : "—";
        const t1Fmt = s.target_1 != null ? "₹" + Number(s.target_1).toFixed(2) : (s.target != null ? "₹" + Number(s.target).toFixed(2) : "—");
        const t2Fmt = s.target_2 != null ? "₹" + Number(s.target_2).toFixed(2) : "—";

        const rr = Number(s.risk_reward || 0);
        const rrTxt = Number.isFinite(rr) && rr > 0 ? rr.toFixed(2) + "x" : "—";
        const rvolTxt = s.rvol != null ? Number(s.rvol).toFixed(1) + "x" : "—";

        const isExpired = !!s.is_expired;
        const statusBadge = isExpired
            ? `<span style="font-size: 0.70rem;padding:2px 6px;border-radius:4px;background:rgba(148,163,184,0.12);color:#94a3b8;border:1px solid rgba(148,163,184,0.3);">EXPIRED</span>`
            : `<span style="font-size: 0.70rem;padding:2px 6px;border-radius:4px;background:rgba(16,185,129,0.12);color:var(--green);border:1px solid rgba(16,185,129,0.35);">ACTIVE</span>`;

        const isCrypto = (s.symbol || "").includes("/");
        const feedBadge = isCrypto
            ? `<span style="font-size: 0.70rem;padding:2px 6px;border-radius:4px;background:rgba(56,189,248,0.1);color:#38bdf8;border:1px solid rgba(56,189,248,0.25);">CRYPTO 24/7</span>`
            : `<span style="font-size: 0.70rem;padding:2px 6px;border-radius:4px;background:rgba(56,189,248,0.1);color:#38bdf8;border:1px solid rgba(56,189,248,0.25);">15m DELAYED</span>`;

        const classification = s.classification || (dir === "LONG" ? (score >= 80 ? "STRONG LONG" : "LONG") : (score >= 80 ? "STRONG SHORT" : "SHORT"));

        return `
            <tr style="border-bottom:1px solid rgba(255,255,255,0.05);transition:background 0.15s;cursor:pointer;" onmouseover="this.style.background='rgba(255,255,255,0.03)'" onmouseout="this.style.background='transparent'" onclick="openIntradayDrawer('${_escHtml(s.symbol)}')">
                <td style="padding:${pad};color:var(--text-muted);font-weight:700;font-family:var(--font-mono);">${s.rank || (idx + 1)}</td>
                <td style="padding:${pad};">
                    <div style="font-weight:800;font-size:0.85rem;color:#fff;">${_escHtml(s.symbol)}</div>
                    <div style="font-size: 0.72rem;color:var(--text-muted);">${_escHtml(s.market || "NSE")}</div>
                </td>
                <td style="padding:${pad};">
                    <span style="font-size: 0.72rem;font-weight:800;padding:2px 7px;border-radius:4px;background:${dirBg};color:${dirColor};border:1px solid ${dirBorder};">${_escHtml(classification)}</span>
                </td>
                <td style="padding:${pad};">
                    <span style="font-size: 0.72rem;font-weight:700;padding:2px 7px;border-radius:4px;background:${stc.bg};color:${stc.color};border:1px solid ${stc.border};">${_escHtml(s.strategy)}</span>
                </td>
                <td style="padding:${pad};">
                    <div style="display:flex;align-items:center;gap:6px;">
                        <span style="font-weight:800;font-family:var(--font-mono);color:${scoreColor};font-size:0.82rem;">${score.toFixed(1)}%</span>
                        <div style="flex:1;height:5px;background:rgba(255,255,255,0.08);border-radius:3px;overflow:hidden;min-width:40px;">
                            <div style="height:100%;width:${Math.min(100, score)}%;background:${scoreColor};border-radius:3px;"></div>
                        </div>
                    </div>
                </td>
                <td style="padding:${pad};font-family:var(--font-mono);font-weight:700;color:#fff;">${ltpTxt}</td>
                <td style="padding:${pad};font-family:var(--font-mono);color:var(--text-primary);">${entryFmt}</td>
                <td style="padding:${pad};font-family:var(--font-mono);color:var(--red);">${slFmt}</td>
                <td style="padding:${pad};font-family:var(--font-mono);color:var(--green);">${t1Fmt}</td>
                <td style="padding:${pad};font-family:var(--font-mono);color:var(--green);">${t2Fmt}</td>
                <td style="padding:${pad};font-family:var(--font-mono);font-weight:700;color:#cbd5e1;">${rrTxt}</td>
                <td style="padding:${pad};font-family:var(--font-mono);">${rvolTxt}</td>
                <td style="padding:${pad};">${feedBadge}</td>
                <td style="padding:${pad};">${statusBadge}</td>
                <td style="padding:${pad};text-align:right;" onclick="event.stopPropagation();">
                    <div style="display:inline-flex;gap:4px;">
                        <button type="button" class="btn btn-secondary" onclick="openIntradayDrawer('${_escHtml(s.symbol)}')" style="font-size: 0.72rem;padding:0.22rem 0.5rem;" title="View Confluence Breakdown & Chart">🔍</button>
                        <button type="button" class="btn btn-primary" onclick="takePaperTrade('${_escHtml(s.symbol)}', '${_escHtml(s.direction)}', ${Number(s.entry || 0)}, ${Number(s.stop_loss || 0)}, ${Number(s.target_1 || s.target || 0)}, ${Number(s.atr || 0)}, '${_escHtml(s.strategy || "INTRADAY")}', this)" style="font-size: 0.72rem;padding:0.22rem 0.55rem;background:linear-gradient(135deg,#4f46e5 0%,#3b82f6 100%);color:#fff;border:none;border-radius:4px;font-weight:700;">⚡ Trade</button>
                    </div>
                </td>
            </tr>
        `;
    }).join("");
}

function _renderIntradayCards(sigs) {
    const grid = document.getElementById("intradayGrid");
    if (!grid) return;

    if (!sigs || !sigs.length) {
        const total = (_intradayAllSigs || []).length;
        if (total > 0) {
            grid.innerHTML = `<div class="loading-state" style="grid-column:1/-1;"><p style="color:var(--text-muted);font-size:0.85rem;">No signals match current filter. ${total} signal(s) available in latest scan.</p><p style="margin-top:0.5rem;"><button type="button" onclick="resetIntradayFilters()" class="btn btn-secondary" style="font-size:0.75rem;padding:0.3rem 0.65rem;">Show All</button></p></div>`;
        } else {
            grid.innerHTML = '<div class="loading-state" style="grid-column:1/-1;"><p style="color:var(--text-muted);font-size:0.85rem;">No intraday signals in latest snapshot. Run a fresh scan or check back during market hours.</p></div>';
        }
        return;
    }

    grid.innerHTML = sigs.map(s => {
        const dir = (s.direction || "").toUpperCase();
        const dirColor = dir === "LONG" ? "var(--green)" : "var(--red)";
        const dirBg = dir === "LONG" ? "rgba(16,185,129,0.12)" : "rgba(239,68,68,0.12)";
        const dirBorder = dir === "LONG" ? "rgba(16,185,129,0.4)" : "rgba(239,68,68,0.4)";

        const stc = _ID_STRAT_COLORS[s.strategy] || { bg: "rgba(148,163,184,0.15)", color: "#cbd5e1", border: "rgba(148,163,184,0.3)" };
        const score = Number(s.confluence_score ?? s.score ?? 0);
        const scoreColor = score >= 80 ? "#10b981" : (score >= 65 ? "#818cf8" : "#f59e0b");

        const rr = Number(s.risk_reward || 0);
        const rrTxt = Number.isFinite(rr) && rr > 0 ? rr.toFixed(2) + "x" : "—";
        const entryFmt = s.entry != null ? "₹" + Number(s.entry).toFixed(2) : "—";
        const slFmt = s.stop_loss != null ? "₹" + Number(s.stop_loss).toFixed(2) : "—";
        const tgtFmt = s.target_1 != null ? "₹" + Number(s.target_1).toFixed(2) : (s.target != null ? "₹" + Number(s.target).toFixed(2) : "—");

        const rvolBadge = s.rvol != null
            ? `<span style="font-size: 0.72rem;font-weight:700;padding:2px 6px;border-radius:4px;background:rgba(59,130,246,0.15);color:#60a5fa;border:1px solid rgba(59,130,246,0.35);">RVOL ${Number(s.rvol).toFixed(1)}x</span>`
            : "";

        return `
            <div class="card" style="padding:1rem;border-top:2px solid ${dirColor};transition:transform 0.15s,box-shadow 0.15s;cursor:pointer;" onclick="openIntradayDrawer('${_escHtml(s.symbol)}')">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.6rem;">
                    <div>
                        <div style="display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap;">
                            <strong style="font-size:1.05rem;color:var(--text-primary);">${_escHtml(s.symbol)}</strong>
                            <span style="font-size: 0.72rem;font-weight:800;padding:2px 7px;border-radius:4px;background:${dirBg};color:${dirColor};border:1px solid ${dirBorder};">${_escHtml(s.classification || dir)}</span>
                            <span style="font-size: 0.72rem;font-weight:700;padding:2px 7px;border-radius:4px;background:${stc.bg};color:${stc.color};border:1px solid ${stc.border};">${_escHtml(s.strategy)}</span>
                            ${rvolBadge}
                        </div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:0.6rem;color:var(--text-muted);text-transform:uppercase;">Confluence</div>
                        <div style="font-size:1.1rem;font-weight:800;color:${scoreColor};font-family:var(--font-mono);">${score.toFixed(1)}%</div>
                    </div>
                </div>

                <div style="height:4px;background:rgba(255,255,255,0.06);border-radius:2px;margin-bottom:0.7rem;overflow:hidden;">
                    <div style="height:100%;width:${Math.min(100, score)}%;background:${scoreColor};border-radius:2px;"></div>
                </div>

                <p style="font-size:0.7rem;color:var(--text-muted);margin:0 0 0.65rem;line-height:1.45;">${_escHtml(s.why_ranked || s.rationale || "")}</p>

                <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:0.5rem;margin-bottom:0.5rem;">
                    <div style="text-align:center;padding:0.35rem;background:rgba(255,255,255,0.03);border-radius:6px;">
                        <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">Entry</div>
                        <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--text-primary);">${entryFmt}</div>
                    </div>
                    <div style="text-align:center;padding:0.35rem;background:rgba(239,68,68,0.06);border-radius:6px;">
                        <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">SL</div>
                        <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--red);">${slFmt}</div>
                    </div>
                    <div style="text-align:center;padding:0.35rem;background:rgba(16,185,129,0.06);border-radius:6px;">
                        <div style="font-size: 0.70rem;color:var(--text-muted);text-transform:uppercase;">Target 1</div>
                        <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--green);">${tgtFmt}</div>
                    </div>
                </div>

                <div style="display:flex;justify-content:space-between;align-items:center;margin-top:0.65rem;padding-top:0.5rem;border-top:1px solid rgba(255,255,255,0.06);">
                    <span style="font-size:0.7rem;color:var(--text-muted);">R:R <strong style="color:var(--text-primary);">${rrTxt}</strong></span>
                    <button type="button" class="btn btn-primary" onclick="event.stopPropagation();takePaperTrade('${_escHtml(s.symbol)}', '${_escHtml(s.direction)}', ${Number(s.entry || 0)}, ${Number(s.stop_loss || 0)}, ${Number(s.target_1 || s.target || 0)}, ${Number(s.atr || 0)}, '${_escHtml(s.strategy || "INTRADAY")}', this)" style="font-size:0.72rem;padding:0.3rem 0.7rem;background:linear-gradient(135deg,#4f46e5 0%,#3b82f6 100%);color:#fff;border:none;border-radius:4px;font-weight:600;">⚡ Paper Trade</button>
                </div>
            </div>`;
    }).join("");
}

async function openIntradayDrawer(symbol) {
    const s = (_intradayAllSigs || []).find(item => item.symbol === symbol);
    if (!s) return;

    const drawer = document.getElementById("intradayDrawer");
    const backdrop = document.getElementById("intradayDrawerBackdrop");
    if (!drawer) return;

    drawer.style.display = "block";
    if (backdrop) backdrop.style.display = "block";

    // Header info
    const symEl = document.getElementById("idDrawerSymbol");
    const dirEl = document.getElementById("idDrawerDirection");
    const stratEl = document.getElementById("idDrawerStrategy");
    const ltpEl = document.getElementById("idDrawerLtp");
    const validEl = document.getElementById("idDrawerValidUntil");

    if (symEl) symEl.textContent = s.symbol;
    if (dirEl) {
        dirEl.textContent = s.classification || s.direction;
        const isLong = (s.direction || "").toUpperCase() === "LONG";
        dirEl.style.background = isLong ? "rgba(16,185,129,0.15)" : "rgba(239,68,68,0.15)";
        dirEl.style.color = isLong ? "var(--green)" : "var(--red)";
        dirEl.style.border = isLong ? "1px solid rgba(16,185,129,0.35)" : "1px solid rgba(239,68,68,0.35)";
    }
    if (stratEl) {
        stratEl.textContent = s.strategy;
        const stc = _ID_STRAT_COLORS[s.strategy] || { bg: "rgba(148,163,184,0.15)", color: "#cbd5e1", border: "rgba(148,163,184,0.3)" };
        stratEl.style.background = stc.bg;
        stratEl.style.color = stc.color;
        stratEl.style.border = `1px solid ${stc.border}`;
    }
    if (ltpEl) ltpEl.textContent = s.ltp != null ? "₹" + Number(s.ltp).toFixed(2) : (s.entry ? "₹" + Number(s.entry).toFixed(2) : "—");
    if (validEl) validEl.textContent = s.valid_until || "End of Session (15:15 IST)";

    // Execution Levels
    const entryEl = document.getElementById("idDrawerEntry");
    const slEl = document.getElementById("idDrawerSL");
    const t1El = document.getElementById("idDrawerT1");
    const t2El = document.getElementById("idDrawerT2");

    if (entryEl) entryEl.textContent = s.entry != null ? "₹" + Number(s.entry).toFixed(2) : "—";
    if (slEl) slEl.textContent = s.stop_loss != null ? "₹" + Number(s.stop_loss).toFixed(2) : "—";
    if (t1El) t1El.textContent = s.target_1 != null ? "₹" + Number(s.target_1).toFixed(2) : (s.target != null ? "₹" + Number(s.target).toFixed(2) : "—");
    if (t2El) t2El.textContent = s.target_2 != null ? "₹" + Number(s.target_2).toFixed(2) : "—";

    // Score Value
    const scoreValEl = document.getElementById("idDrawerScoreVal");
    const scoreNum = Number(s.confluence_score ?? s.score ?? 0);
    if (scoreValEl) scoreValEl.textContent = scoreNum.toFixed(1);

    // Confluence Factors breakdown
    const factorListEl = document.getElementById("idDrawerFactorList");
    if (factorListEl && s.factors) {
        let factorArr = [];
        if (Array.isArray(s.factors)) {
            factorArr = s.factors;
        } else if (typeof s.factors === "object") {
            const factorDefs = [
                { key: "strategy_confluence", name: "Strategy Confluence", max: 30 },
                { key: "trend_alignment", name: "Trend Alignment", max: 20 },
                { key: "volume_rvol", name: "Volume & RVOL", max: 15 },
                { key: "index_regime", name: "NIFTY Regime Confluence", max: 15 },
                { key: "risk_reward", name: "Risk / Reward Ratio", max: 10 },
                { key: "adx_strength", name: "ADX Volatility & Strength", max: 10 }
            ];
            factorArr = factorDefs.map(fd => ({
                name: fd.name,
                score: Number(s.factors[fd.key] || 0),
                max: fd.max,
                status: "NEUTRAL",
                detail: ""
            }));
        }

        factorListEl.innerHTML = factorArr.map(f => {
            const score = Number(f.score || 0);
            const max = Number(f.max || 100);
            const pct = Math.min(100, Math.max(0, (score / max) * 100));
            const color = f.status === "POSITIVE" ? "var(--green)" : (f.status === "WARNING" ? "#f59e0b" : (f.status === "NEGATIVE" ? "var(--red)" : "#818cf8"));
            const detailTxt = f.detail ? `<div style="font-size: 0.70rem;color:var(--text-muted);margin-top:2px;">${_escHtml(f.detail)}</div>` : "";
            return `
                <div style="background:rgba(255,255,255,0.02);padding:0.4rem 0.6rem;border-radius:6px;border:1px solid rgba(255,255,255,0.04);">
                    <div style="display:flex;justify-content:space-between;font-size: 0.72rem;margin-bottom:3px;">
                        <span style="color:#cbd5e1;font-weight:600;">${_escHtml(f.name || "")}</span>
                        <span style="font-family:var(--font-mono);font-weight:700;color:${color};">${score.toFixed(1)} / ${max}</span>
                    </div>
                    <div style="height:4px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden;">
                        <div style="height:100%;width:${pct}%;background:${color};border-radius:2px;"></div>
                    </div>
                    ${detailTxt}
                </div>
            `;
        }).join("");
    }

    // Technical Indicators Snapshot
    const techEl = document.getElementById("idDrawerTechSnapshot");
    if (techEl) {
        const snap = s.technical_snapshot || {};
        const cprObj = snap.cpr || {};
        const cprPivot = cprObj.pivot != null ? cprObj.pivot : snap.cpr_pivot;
        const cprType = cprObj.cpr_type || snap.cpr_type;
        const supVal = snap.support != null ? snap.support : snap.support_1;
        const resVal = snap.resistance != null ? snap.resistance : snap.resistance_1;

        const items = [
            { label: "EMA 9", val: snap.ema_9 != null ? "₹" + Number(snap.ema_9).toFixed(1) : "—" },
            { label: "EMA 20", val: snap.ema_20 != null ? "₹" + Number(snap.ema_20).toFixed(1) : "—" },
            { label: "EMA 50", val: snap.ema_50 != null ? "₹" + Number(snap.ema_50).toFixed(1) : "—" },
            { label: "EMA 200", val: snap.ema_200 != null ? "₹" + Number(snap.ema_200).toFixed(1) : "—" },
            { label: "RSI 14", val: snap.rsi != null ? Number(snap.rsi).toFixed(1) : "—" },
            { label: "ADX 14", val: snap.adx != null ? `${Number(snap.adx).toFixed(1)} (${snap.adx_regime || "TRENDING"})` : "—" },
            { label: "VWAP", val: snap.vwap != null ? "₹" + Number(snap.vwap).toFixed(1) : "—" },
            { label: "CPR Pivot", val: cprPivot != null ? `₹${Number(cprPivot).toFixed(1)}${cprType ? ' (' + cprType + ')' : ''}` : "—" },
            { label: "Daily Range", val: (supVal != null && resVal != null) ? `₹${Number(supVal).toFixed(0)} - ₹${Number(resVal).toFixed(0)}` : "—" }
        ];

        techEl.innerHTML = items.map(it => `
            <div style="background:rgba(0,0,0,0.25);padding:0.35rem 0.5rem;border-radius:5px;border:1px solid rgba(255,255,255,0.04);">
                <div style="font-size: 0.70rem;color:var(--text-muted);">${it.label}</div>
                <div style="font-size:0.75rem;font-weight:700;font-family:var(--font-mono);color:#fff;">${it.val}</div>
            </div>
        `).join("");
    }

    // Reasons & Risks lists
    const reasonsListEl = document.getElementById("idDrawerReasons");
    const risksListEl = document.getElementById("idDrawerRisks");

    if (reasonsListEl) {
        const reasons = (s.reasons && s.reasons.length) ? s.reasons : [(s.rationale || "Setup qualified pattern and risk bounds.")];
        reasonsListEl.innerHTML = reasons.map(r => `<li style="margin-bottom:3px;">${_escHtml(r)}</li>`).join("");
    }

    if (risksListEl) {
        const risks = (s.risks && s.risks.length) ? s.risks : [
            `Invalidation if price crosses Stop Loss at ${slEl ? slEl.textContent : "SL level"}.`,
            "Auto square-off mandated by 3:15 PM IST."
        ];
        risksListEl.innerHTML = risks.map(r => `<li style="margin-bottom:3px;">${_escHtml(r)}</li>`).join("");
    }

    // Trade Button Handler
    const tradeBtn = document.getElementById("idDrawerPaperTradeBtn");
    if (tradeBtn) {
        tradeBtn.onclick = function() {
            takePaperTrade(s.symbol, s.direction, Number(s.entry || 0), Number(s.stop_loss || 0), Number(s.target_1 || s.target || 0), Number(s.atr || 0), s.strategy || "INTRADAY", this);
        };
    }

    // Render 5m Chart
    _renderDrawer5mChart(s.symbol, (s.direction || "").toUpperCase());
}

async function _renderDrawer5mChart(symbol, direction) {
    const canvas = document.getElementById("idDrawerChartCanvas");
    const statusEl = document.getElementById("idDrawerChartStatus");
    if (!canvas) return;

    if (_intradayDrawerChart) {
        try { _intradayDrawerChart.destroy(); } catch (_) {}
        _intradayDrawerChart = null;
    }

    if (statusEl) statusEl.textContent = "Loading 5m candles...";

    try {
        const res = await fetchWithTimeout(`${API}/api/intraday/symbol/${encodeURIComponent(symbol)}`, {}, 15000);
        const data = await parseJsonResponse(res);

        const candles = data.candles || [];
        if (!candles.length) {
            if (statusEl) statusEl.textContent = "No recent 5m candle data available";
            return;
        }

        if (statusEl) statusEl.textContent = `${candles.length} Bars (5m) · Provider: ${data.provider || "NSE"}`;

        const labels = candles.map(c => {
            return c.time || (c.timestamp ? (c.timestamp.includes("T") ? c.timestamp.split("T")[1].slice(0, 5) : c.timestamp.slice(-5)) : "");
        });
        const closes = candles.map(c => Number(c.close));

        // Quick EMA 20 approximation for chart overlay
        const ema20 = [];
        const k = 2 / 21;
        let prevEma = closes[0];
        closes.forEach(price => {
            prevEma = price * k + prevEma * (1 - k);
            ema20.push(Number(prevEma.toFixed(2)));
        });

        const ctx = canvas.getContext("2d");
        const isBullish = direction === "LONG";
        const lineColor = isBullish ? "#10b981" : "#ef4444";
        const gradColor = isBullish ? "rgba(16,185,129,0.25)" : "rgba(239,68,68,0.25)";

        const gradient = ctx.createLinearGradient(0, 0, 0, 200);
        gradient.addColorStop(0, gradColor);
        gradient.addColorStop(1, "rgba(15, 23, 42, 0)");

        if (typeof Chart !== "undefined") {
            _intradayDrawerChart = new Chart(ctx, {
                type: "line",
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: `${symbol} Price`,
                            data: closes,
                            borderColor: lineColor,
                            backgroundColor: gradient,
                            borderWidth: 2,
                            fill: true,
                            tension: 0.15,
                            pointRadius: 0,
                            pointHoverRadius: 4
                        },
                        {
                            label: "EMA 20",
                            data: ema20,
                            borderColor: "#38bdf8",
                            borderWidth: 1.5,
                            borderDash: [4, 4],
                            fill: false,
                            pointRadius: 0
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { intersect: false, mode: "index" },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            backgroundColor: "rgba(15,23,42,0.95)",
                            titleColor: "#94a3b8",
                            bodyColor: "#f8fafc",
                            borderColor: "rgba(255,255,255,0.1)",
                            borderWidth: 1,
                            padding: 8,
                            callbacks: {
                                label: function(c) { return `${c.dataset.label}: ₹${Number(c.raw).toFixed(2)}`; }
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: "rgba(255,255,255,0.04)" },
                            ticks: { color: "#64748b", font: { size: 9 }, maxTicksLimit: 7 }
                        },
                        y: {
                            grid: { color: "rgba(255,255,255,0.04)" },
                            ticks: {
                                color: "#64748b",
                                font: { size: 9 },
                                callback: function(val) { return "₹" + Number(val).toFixed(0); }
                            }
                        }
                    }
                }
            });
        }
    } catch (err) {
        console.warn("Error rendering 5m chart:", err);
        if (statusEl) statusEl.textContent = "Chart unavailable";
    }
}

function closeIntradayDrawer() {
    const drawer = document.getElementById("intradayDrawer");
    const backdrop = document.getElementById("intradayDrawerBackdrop");
    if (drawer) drawer.style.display = "none";
    if (backdrop) backdrop.style.display = "none";

    if (_intradayDrawerChart) {
        try { _intradayDrawerChart.destroy(); } catch (_) {}
        _intradayDrawerChart = null;
    }
}

function exportIntradayCSV() {
    if (!_intradayAllSigs || !_intradayAllSigs.length) {
        alert("No intraday setups to export. Run a scan first.");
        return;
    }

    const headers = [
        "Rank", "Symbol", "Direction", "Classification", "Strategy", "Confluence_Score",
        "LTP", "Entry", "Stop_Loss", "Target_1", "Target_2", "Risk_Reward", "RVOL",
        "Data_Status", "Valid_Until", "Why_Ranked"
    ];

    const rows = _intradayAllSigs.map((s, idx) => [
        s.rank || (idx + 1),
        s.symbol || "",
        s.direction || "",
        s.classification || "",
        s.strategy || "",
        (s.confluence_score ?? s.score ?? 0),
        s.ltp ?? "",
        s.entry ?? "",
        s.stop_loss ?? "",
        s.target_1 ?? s.target ?? "",
        s.target_2 ?? "",
        s.risk_reward ?? "",
        s.rvol ?? "",
        s.data_status ?? "DELAYED",
        s.valid_until ?? "",
        `"${(s.why_ranked || s.rationale || "").replace(/"/g, '""')}"`
    ]);

    const csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows.map(e => e.join(","))].join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    const now = new Date();
    const dateStr = now.toISOString().slice(0, 10).replace(/-/g, "");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `intraday_command_setups_${dateStr}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

async function loadIntradayLatest() {
    const meta = document.getElementById("intradayScanMeta");
    const niftyBanner = document.getElementById("intradayNiftyContext");
    const tbody = document.getElementById("intradayTableBody");
    const grid = document.getElementById("intradayGrid");

    try {
        const res = await fetchWithTimeout(`${API}/api/intraday/latest`, {}, 35000);
        const row = await parseJsonResponse(res);

        _intradayAllSigs = row.signals || [];
        _intradaySession = row.session || null;
        _intradayNiftyContext = row.nifty_market_context || null;
        _intradayDataStatus = row.data_status || "DELAYED";

        _updateIntradayKpis(_intradayAllSigs, _intradaySession, _intradayNiftyContext, _intradayDataStatus);

        // Update NIFTY context banner
        if (niftyBanner && row.nifty_market_context) {
            const nc = row.nifty_market_context;
            const trend = nc.trend || "NEUTRAL";
            const trendColor = trend === "BULLISH" ? "var(--green)" : (trend === "BEARISH" ? "var(--red)" : "var(--text-muted)");
            const trendEmoji = trend === "BULLISH" ? "🟢" : (trend === "BEARISH" ? "🔴" : "⚪");
            const ltpTxt = nc.ltp ? ` ₹${Number(nc.ltp).toFixed(1)}` : "";
            const chgTxt = nc.change_pct != null ? ` (${nc.change_pct >= 0 ? "+" : ""}${nc.change_pct.toFixed(2)}%)` : "";

            niftyBanner.innerHTML = `
                <span><strong style="color:var(--text-primary);">NIFTY 50:</strong> <span style="color:${trendColor};font-weight:700;">${trendEmoji} ${trend}</span>${ltpTxt}${chgTxt}</span>
                <span style="color:rgba(255,255,255,0.2);">|</span>
                <span>Regime: <strong style="color:#cbd5e1;">${nc.regime || "NORMAL"}</strong></span>
                <span style="color:rgba(255,255,255,0.2);">|</span>
                <span>RSI: <strong style="color:#cbd5e1;">${nc.rsi != null ? Number(nc.rsi).toFixed(1) : "—"}</strong></span>
                <span style="color:rgba(255,255,255,0.2);">|</span>
                <span>VWAP: <strong style="color:#cbd5e1;">${nc.vwap_bias || "—"}</strong></span>
            `;
        }

        // Update scan meta with full Scan Freshness Contract details
        if (meta) {
            if (row.scan_time || row.last_scan_at) {
                const lastScanStr = row.last_scan_at ? new Date(row.last_scan_at).toLocaleTimeString() : (row.scan_time || "—");
                const sourceCandleStr = row.source_candle_at ? new Date(row.source_candle_at).toLocaleTimeString() : "15m delayed EOD/intraday";
                const delayStr = row.provider_freshness || "15m Delayed";
                const coverageStr = `${row.symbols_scanned || 20} symbols`;
                const qualifiedCount = row.qualified_setups != null ? row.qualified_setups : (row.active_signals ? row.active_signals.length : _intradayAllSigs.length);
                const statusBadgeColor = row.freshness_status === "FRESH" ? "#10b981" : (row.freshness_status === "EOD" ? "#94a3b8" : "#38bdf8");

                meta.innerHTML = `<span>Scan: <strong>${lastScanStr}</strong></span> · <span>Candle: <strong>${sourceCandleStr}</strong></span> · <span>Status: <strong style="color:${statusBadgeColor};">${row.freshness_status || "DELAYED"}</strong></span> · <span>Coverage: <strong>${coverageStr}</strong></span> · <span>Qualified: <strong>${qualifiedCount}</strong> setups</span> · <span style="color:#60a5fa;">${delayStr}</span>`;
            } else {
                meta.innerHTML = "No saved scan yet · click ⚡ Run Scan to initiate.";
            }
        }

        applyIntradayFilters();

    } catch (e) {
        console.warn("loadIntradayLatest error:", e);
        if (meta) meta.textContent = "Could not load intraday snapshot.";
        if (tbody) tbody.innerHTML = '<tr><td colspan="15" style="color:var(--red);text-align:center;padding:1.5rem;">Failed to load intraday data. Check network or server status.</td></tr>';
        if (grid) grid.innerHTML = '<p style="color:var(--red);padding:1rem;">Failed to load intraday data</p>';
    }
}

async function runIntradayScan() {
    const btn = document.getElementById("intradayScanBtn");
    const tbody = document.getElementById("intradayTableBody");
    const grid = document.getElementById("intradayGrid");
    const meta = document.getElementById("intradayScanMeta");

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spin">⏳</span> Scanning...';
    }

    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="15" style="text-align:center;padding:2.5rem;color:var(--text-muted);"><div class="loader" style="margin:0 auto 0.75rem;"></div>Running 0-delay hybrid intraday scan across watchlist... please wait (~20-40s)</td></tr>';
    }
    if (grid) {
        grid.innerHTML = '<div class="loading-state" style="padding:2rem;"><div class="loader" style="margin:0 auto 1rem;"></div><p style="color:var(--text-muted);">Running 0-delay hybrid intraday scan across watchlist... please wait (~20-40s)</p></div>';
    }

    try {
        const res = await fetchWithTimeout(`${API}/api/intraday/scan`, { method: "POST", headers: _getAuthHeaders() }, 300000);
        if (res.status === 401 || res.status === 403) {
            _handleAuthChallenge(res.status);
            if (btn) { btn.disabled = false; btn.innerHTML = "⚡ Run Scan"; }
            return;
        }

        const data = await parseJsonResponse(res);

        _intradayAllSigs = data.signals || [];
        _intradaySession = data.session || null;
        _intradayNiftyContext = data.nifty_market_context || null;
        _intradayDataStatus = data.data_status || "DELAYED";

        _updateIntradayKpis(_intradayAllSigs, _intradaySession, _intradayNiftyContext, _intradayDataStatus);

        if (meta) {
            meta.innerHTML = `Saved scan #${data.scan_id || ""} at <strong>${data.scan_time || ""}</strong> · <strong>${_intradayAllSigs.length}</strong> setup(s)`;
        }

        applyIntradayFilters();

    } catch (e) {
        console.error("runIntradayScan error:", e);
        if (tbody) tbody.innerHTML = '<tr><td colspan="15" style="color:var(--red);text-align:center;padding:1.5rem;">Intraday scan failed (timeout or server error).</td></tr>';
        if (grid) grid.innerHTML = '<p style="color:var(--red);padding:1rem;">Intraday scan failed (timeout or server error).</p>';
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = "⚡ Run Scan";
        }
    }
}

// Window globals for HTML onclick handlers
window.filterIntradayStrat = filterIntradayStrat;
window.resetIntradayFilters = resetIntradayFilters;
window.loadIntradayLatest = loadIntradayLatest;
window.runIntradayScan = runIntradayScan;
window.takePaperTrade = takePaperTrade;
window.setIntradayView = setIntradayView;
window.toggleIntradayDensity = toggleIntradayDensity;
window.setIntradayDir = setIntradayDir;
window.setIntradayConf = setIntradayConf;
window.applyIntradayFilters = applyIntradayFilters;
window.openIntradayDrawer = openIntradayDrawer;
window.closeIntradayDrawer = closeIntradayDrawer;
window.exportIntradayCSV = exportIntradayCSV;

// Make filterBreakouts available globally for onclick



window.filterBreakouts = filterBreakouts;



window.loadIndexMover = loadIndexMover;



// Phase 4 globals



window.toggleWatchlist = toggleWatchlist;



window.openAlertModal = openAlertModal;



window.closeAlertModal = closeAlertModal;



window.saveAlertThreshold = saveAlertThreshold;



window.closeP5Shortcuts = closeP5Shortcuts;







// Override switchTab to also sync bottom nav AND sidebar



const _originalSwitchTab = switchTab;



switchTab = function(tab) {



    _originalSwitchTab(tab);



    updateBottomNav(tab);



    // Sync sidebar active state



    document.querySelectorAll('.sidebar-item').forEach(item => {



        item.classList.toggle('active', item.dataset.tab === tab);



    });



};







// Initialize bottom nav on load



document.addEventListener('DOMContentLoaded', () => {



    initBottomNav();



    initMoodStrip();



});







// ── Mood Strip (Top Bar) ─────────────────────────────────







function initMoodStrip() {



    // Poll mood strip data every 60s



    refreshMoodStrip();



    setInterval(refreshMoodStrip, 60000);



}







async function refreshMoodStrip(force = false) {
    if (document.hidden && !force && window._authoritativeMoodLoaded) return;



    // Retry on cold Render / LB 502–503 (service waking or briefly unavailable).



    const delays = [0, 2500, 5000];



    for (let i = 0; i < delays.length; i++) {



        if (delays[i] > 0) {



            await new Promise((r) => setTimeout(r, delays[i]));



        }



        try {



            const res = await fetchWithTimeout(`${API}/api/context/market-mood`, {}, 20000);



            if (res.ok) {



                const data = await parseJsonResponse(res);



                updateMoodStripUI(data);



                break;



            }



            if (res.status !== 503 && res.status !== 502 && res.status !== 504) break;



        } catch (e) {



            if (i === delays.length - 1) {



                /* mood strip is non-critical */



            }



        }



    }







    // Also pull index prices from the signals/stats we already have



    updateMoodStripFromCache();



}







function updateMoodStripFromCache() {



    // Try to populate ticker from market data cache (already polled by loadAllData)



    const pairs = [
        { id: 'moodNifty', chgId: 'moodNiftyChg', syms: ['NIFTY 50', 'NIFTY', '^NSEI', 'NIFTY_50'] },
        { id: 'moodSensex', chgId: 'moodSensexChg', syms: ['SENSEX', '^BSESN', 'BSE_SENSEX'] },
        { id: 'moodBtc', chgId: 'moodBtcChg', syms: ['BTC/USDT', 'BTCUSDT', 'BTC', 'BTC/USD'] },
        { id: 'moodBanknifty', chgId: 'moodBankniftyChg', syms: ['BANKNIFTY', '^NSEBANK', 'BANK_NIFTY'] },
    ];

    pairs.forEach(({ id, chgId, syms }) => {
        let tick = null;
        for (const s of syms) {
            if (marketDataCache && marketDataCache[s]) {
                tick = marketDataCache[s];
                break;
            }
        }
        if (!tick) return;

        // If authoritative mood endpoint already loaded SENSEX/NIFTY/BANKNIFTY, don't overwrite with unverified signal cache
        if (window._authoritativeMoodLoaded && (id === 'moodSensex' || id === 'moodNifty' || id === 'moodBanknifty')) {
            const currentEl = document.getElementById(id);
            if (currentEl && currentEl.textContent && currentEl.textContent !== '—') {
                return;
            }
        }

        const el = document.getElementById(id);



        const chgEl = document.getElementById(chgId);



        const price = typeof tick === 'object' && tick ? (tick.close ?? tick.price ?? tick.ltp) : tick;
        if (el && price != null && !isNaN(price) && Number(price) > 0) {
            el.textContent = Number(price).toLocaleString('en-IN', { maximumFractionDigits: (id === 'moodBtc' ? 2 : 0) });
        }



        if (chgEl && tick.change_pct != null) {



            const chg = Number(tick.change_pct);



            chgEl.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`;



            chgEl.className = 'mood-ticker-chg ' + (chg >= 0 ? 'up' : 'dn');



        }



    });



}







function updateMoodStripUI(data) {



    const label = document.getElementById('moodStripLabel');



    const pill = document.getElementById('moodStripPill');



    const eventLabel = document.getElementById('moodEventLabel');







    if (!label || !pill) return;







    const mood = String(data.crypto_mood || 'NEUTRAL').toUpperCase();



    label.textContent = mood;







    // Update pill color



    pill.style.background = '';



    pill.style.color = '';



    pill.style.borderColor = '';







    const dot = pill.querySelector('.mood-dot');



    if (mood === 'RISK_ON') {



        pill.style.background = 'rgba(34,197,94,0.12)';



        pill.style.color = '#22c55e';



        pill.style.borderColor = 'rgba(34,197,94,0.3)';



        if (dot) dot.style.background = '#22c55e';



    } else if (mood === 'RISK_OFF') {



        pill.style.background = 'rgba(239,68,68,0.12)';



        pill.style.color = '#ef4444';



        pill.style.borderColor = 'rgba(239,68,68,0.3)';



        if (dot) dot.style.background = '#ef4444';



    } else {



        pill.style.background = 'rgba(245,158,11,0.12)';



        pill.style.color = '#f59e0b';



        pill.style.borderColor = 'rgba(245,158,11,0.3)';



        if (dot) dot.style.background = '#f59e0b';



    }







        function _setTickerSlot(valId, chgId, price, chg) {
        const valEl = document.getElementById(valId);
        const chgEl = document.getElementById(chgId);
        if (valEl && price != null && !isNaN(price) && Number(price) > 0) {
            valEl.textContent = Number(price).toLocaleString('en-IN', { maximumFractionDigits: (valId === 'moodBtc' ? 2 : 0) });
        }
        if (chgEl && chg != null && !isNaN(chg)) {
            const v = Number(chg);
            chgEl.textContent = `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
            chgEl.className = 'mood-ticker-chg ' + (v >= 0 ? 'up' : 'dn');
        }
    }

    if (data.sensex_close && Number(data.sensex_close) > 0) {
        marketDataCache['SENSEX'] = { close: data.sensex_close, change_pct: data.sensex_24hr_change };
        marketDataCache['^BSESN'] = marketDataCache['SENSEX'];
        marketDataCache['BSE_SENSEX'] = marketDataCache['SENSEX'];
    }
    if (data.nifty_close && Number(data.nifty_close) > 0) {
        marketDataCache['NIFTY'] = { close: data.nifty_close, change_pct: data.nifty_24hr_change };
        marketDataCache['NIFTY 50'] = marketDataCache['NIFTY'];
        marketDataCache['^NSEI'] = marketDataCache['NIFTY'];
    }
    if (data.banknifty_close && Number(data.banknifty_close) > 0) {
        marketDataCache['BANKNIFTY'] = { close: data.banknifty_close, change_pct: data.banknifty_24hr_change };
        marketDataCache['^NSEBANK'] = marketDataCache['BANKNIFTY'];
        marketDataCache['BANK_NIFTY'] = marketDataCache['BANKNIFTY'];
    }
    if (data.btc_price && Number(data.btc_price) > 0) {
        marketDataCache['BTC/USDT'] = { close: data.btc_price, change_pct: data.btc_24hr_change };
        marketDataCache['BTCUSDT'] = marketDataCache['BTC/USDT'];
    }
    window._authoritativeMoodLoaded = true;

    _setTickerSlot('moodNifty', 'moodNiftyChg', data.nifty_close, data.nifty_24hr_change);
    _setTickerSlot('moodSensex', 'moodSensexChg', data.sensex_close, data.sensex_24hr_change);
    _setTickerSlot('moodBtc', 'moodBtcChg', data.btc_price, data.btc_24hr_change);
    _setTickerSlot('moodBanknifty', 'moodBankniftyChg', data.banknifty_close, data.banknifty_24hr_change);







    // Update event badge



    const moodEventContainer = document.getElementById('moodEvent');
    if (eventLabel && data.active_event_risk) {
        if (moodEventContainer) moodEventContainer.style.display = 'flex';
        eventLabel.textContent = 'EVENT RISK';
        eventLabel.style.color = '#ef4444';
    } else if (eventLabel && data.next_event && Number(data.next_event.days_away || 99) <= 1) {
        if (moodEventContainer) moodEventContainer.style.display = 'flex';
        const evt = data.next_event;
        eventLabel.textContent = evt.event_name ? `${evt.event_name} · ${evt.days_away ?? 0}d` : 'Event Imminent';
        eventLabel.style.color = '#f59e0b';
    } else {
        if (moodEventContainer) moodEventContainer.style.display = 'none';
        if (eventLabel) {
            eventLabel.textContent = 'Calendar Clear';
            eventLabel.style.color = '#10b981';
        }
    }



}















// ══════════════════════════════════════════════════════════════════



// ── Complete Interactive Handlers (Edge, Forex, NSE, Swing) ───────



// ══════════════════════════════════════════════════════════════════







// ── Edge Engine Handlers ──────────────────────────────────────────



let _currentEdgeAssetClass = 'all';








// ═════════════════════════════════════════════════════════════════════════════════
// EDGE ENGINE 3.0: PREMIUM INSTITUTIONAL TRADING DASHBOARD & MULTI-FACTOR ENGINE
// ═════════════════════════════════════════════════════════════════════════════════

let _edgeAllSetups = [];
let _currentEdgeUniverse = 'all';
let _currentEdgeFilter = 'all';
let _edgeSearchQuery = '';
let _edgeSortColumn = 'rank';
let _edgeSortAsc = true;
let _edgeDensity = 'comfortable';
let _edgeDrawerChart = null;
let _currentDrawerSymbol = null;
let _currentDrawerAssetClass = 'nse';
let _currentDrawerTf = '1d';
let _currentDrawerMarketData = null;

async function loadEdgeTopSetups(universe = null, force = false) {
    if (universe) _currentEdgeUniverse = universe;
    const body = document.getElementById('edgeTopSetupsBody');
    const spinner = document.getElementById('eeRefreshSpinner');
    if (spinner) spinner.style.animation = 'spin 0.8s linear infinite';
    if (typeof updateDynamicMarketStatus === 'function') updateDynamicMarketStatus();

    if (body) {
        body.innerHTML = `<tr><td colspan="13" class="empty-row">
            <div class="loader" style="margin:0.8rem auto 0.4rem;"></div>
            <div style="font-size:0.75rem;color:#94a3b8;">Scanning institutional quantitative cross-asset matrix...</div>
        </td></tr>`;
    }

    try {
        let assetParam = 'all';
        if (_currentEdgeUniverse === 'nse' || _currentEdgeUniverse === 'nifty50') assetParam = 'nse';
        else if (_currentEdgeUniverse === 'forex') assetParam = 'forex';
        else if (_currentEdgeUniverse === 'crypto') assetParam = 'crypto';
        else if (_currentEdgeUniverse === 'index') assetParam = 'index';

        const [setupsRes, overviewRes, flowRes, newsRes] = await Promise.all([
            fetchWithTimeout(`${API}/api/edge/top-setups?asset_class=${assetParam}&limit=40${force ? '&force_refresh=true' : ''}`, {}, 25000, 1),
            fetchWithTimeout(`${API}/api/overview/intelligence`, {}, 20000, 1).catch(() => null),
            fetchWithTimeout(`${API}/api/tradeflow`, {}, 15000, 1).catch(() => null),
            fetchWithTimeout(`${API}/api/nse/news`, {}, 15000, 1).catch(() => null),
        ]);

        if (spinner) spinner.style.animation = 'none';

        const parseSafe = async (res) => {
            if (!res || !res.ok) return null;
            try {
                const cloned = res.clone ? res.clone() : res;
                return await cloned.json();
            } catch (_) {
                return null;
            }
        };

        if (!setupsRes || !setupsRes.ok) {
            const statusErr = setupsRes ? `HTTP ${setupsRes.status}` : 'Network Timeout / Offline';
            if (body) {
                body.innerHTML = `<tr><td colspan="13" class="empty-row" style="color:#ef4444;padding:2rem;">
                    <div style="font-size:1.05rem;margin-bottom:6px;font-weight:700;">⚠️ Radar Telemetry Unavailable (${statusErr})</div>
                    <div style="font-size:0.75rem;color:var(--text-muted);margin-bottom:12px;">Quantitative radar service temporarily degraded or rate-limited. Preserving capital rules.</div>
                    <button class="btn btn-secondary" onclick="loadEdgeTopSetups(null, true)" style="font-size:0.75rem;padding:4px 12px;border-radius:4px;cursor:pointer;">🔄 Retry Telemetry</button>
                </td></tr>`;
            }
            renderEdgeMetrics(null, []);
            return;
        }

        const data = await setupsRes.json();
        _edgeAllSetups = data.setups || [];

        // Parse optional peripheral telemetry
        const overviewData = await parseSafe(overviewRes);
        const flowData = await parseSafe(flowRes);
        const newsData = await parseSafe(newsRes);

        // 1. Render Overview Metric Cards
        renderEdgeMetrics(overviewData, _edgeAllSetups);

        // 2. Render Advanced Scanner Data Grid
        renderEdgeTable();

        // 3. Render Institutional Flow & News Panels
        if (flowData || overviewData) renderEdgeFlowPanel(flowData, overviewData);
        if (newsData) renderEdgeNewsPanel(newsData);

    } catch (err) {
        if (spinner) spinner.style.animation = 'none';
        console.error('[loadEdgeTopSetups] error:', err);
        if (body) {
            body.innerHTML = `<tr><td colspan="13" class="empty-row" style="color:var(--red);">
                Failed to load Edge Engine telemetry (${_escHtml(err.message)}).
                <button onclick="loadEdgeTopSetups(null, true)" style="margin-left:8px;background:none;border:none;color:#38bdf8;cursor:pointer;text-decoration:underline;">Retry</button>
            </td></tr>`;
        }
    }
}

function refreshEdgeTopSetups() {
    loadEdgeTopSetups(null, true);
}

function switchEdgeUniverse(univ) {
    _currentEdgeUniverse = univ;
    document.querySelectorAll('[data-ee-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-ee-tab') === univ);
    });
    loadEdgeTopSetups(univ);
}

function setEdgeSecondaryFilter(filter) {
    _currentEdgeFilter = filter;
    document.querySelectorAll('[data-ee-filter]').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-ee-filter') === filter);
    });
    renderEdgeTable();
}

function onEdgeSearchInput(val) {
    _edgeSearchQuery = (val || '').trim().toLowerCase();
    renderEdgeTable();
}

function sortEdgeTable(col) {
    if (_edgeSortColumn === col) {
        _edgeSortAsc = !_edgeSortAsc;
    } else {
        _edgeSortColumn = col;
        _edgeSortAsc = (col === 'symbol' || col === 'rank');
    }
    renderEdgeTable();
}

function toggleEdgeTableDensity() {
    _edgeDensity = _edgeDensity === 'comfortable' ? 'compact' : 'comfortable';
    const tbl = document.getElementById('edgeTopSetupsTable');
    const lbl = document.getElementById('eeDensityLabel');
    if (tbl) {
        tbl.classList.toggle('density-compact', _edgeDensity === 'compact');
    }
    if (lbl) {
        lbl.textContent = _edgeDensity === 'compact' ? 'Compact' : 'Comfortable';
    }
}

function renderEdgeMetrics(overviewData, setups) {
    // 1. Breadth
    const bVal = document.getElementById('eeBreadthVal');
    const bSub = document.getElementById('eeBreadthSub');
    if (overviewData && overviewData.market_guardrails && overviewData.market_guardrails.breadth_split) {
        const bs = overviewData.market_guardrails.breadth_split;
        const b = bs.breadth || {};
        if (b.score != null && Number.isFinite(Number(b.score))) {
            if (bVal) bVal.textContent = `${Number(b.score).toFixed(1)}% ${b.label || 'NEUTRAL'}`;
            if (bSub) bSub.textContent = b.detail || 'Breadth split active';
        } else {
            if (bVal) bVal.textContent = b.label || '— UNAVAILABLE';
            if (bSub) bSub.textContent = b.detail || '0 observations available';
        }
    } else {
        if (bVal) bVal.textContent = '— UNAVAILABLE';
        if (bSub) bSub.textContent = '0 observations available';
    }

    // 2. Regime
    const rVal = document.getElementById('eeRegimeVal');
    const rSub = document.getElementById('eeRegimeSub');
    if (overviewData && overviewData.market_guardrails && overviewData.market_guardrails.regime_summary) {
        const reg = overviewData.market_guardrails.regime_summary;
        const dom = reg.dominant || 'UNKNOWN';
        if (rVal) rVal.textContent = dom;
        const trendingPct = reg.pct_trending != null ? `${reg.pct_trending}% Trending` : '0% Trending';
        const volCount = reg.counts?.HIGH_VOLATILITY ?? 0;
        if (rSub) rSub.textContent = (dom === 'UNKNOWN') ? '0 observations available' : `${trendingPct} · ${volCount} Volatile`;
    } else {
        if (rVal) rVal.textContent = 'UNKNOWN';
        if (rSub) rSub.textContent = '0 observations available';
    }

    // 3. Bullish & Bearish Candidates
    const bullEl = document.getElementById('eeBullishCount');
    const bearEl = document.getElementById('eeBearishCount');
    const bullCount = setups.filter(s => Number(s.edge_score || 0) > 0).length;
    const bearCount = setups.filter(s => Number(s.edge_score || 0) < 0).length;
    if (bullEl) bullEl.textContent = `${bullCount} Assets`;
    if (bearEl) bearEl.textContent = `${bearCount} Assets`;

    // 4. Research Coverage
    const covEl = document.getElementById('eeCoverageVal');
    const covSub = document.getElementById('eeCoverageSub');
    if (setups.length) {
        const avgCov = setups.reduce((acc, s) => acc + (Number(s.factor_coverage_pct) || 100), 0) / setups.length;
        if (covEl) covEl.textContent = `${avgCov.toFixed(1)}%`;
        if (covSub) covSub.textContent = `${setups.length} assets fully verified`;
    }

    // 5. Macro & Energy Alert
    const mVal = document.getElementById('eeMacroAlertVal');
    const mSub = document.getElementById('eeMacroAlertSub');
    const brent = window._lastMacroData && window._lastMacroData.brent_crude;
    if (brent && brent.price != null) {
        const bPrice = Number(brent.price).toFixed(2);
        const bPct = Number(brent.change_pct || 0);
        if (mVal) mVal.textContent = bPct <= -2.0 ? 'Low Crude Benefit' : (bPct >= 2.0 ? 'High Energy Risk' : 'Crude Stable');
        if (mSub) mSub.textContent = `Brent $${bPrice} (${bPct >= 0 ? '+' : ''}${bPct.toFixed(1)}%) · Macro Active`;
    } else {
        if (mVal) mVal.textContent = 'Macro Monitoring';
        if (mSub) mSub.textContent = 'Macro radar active · No active alerts';
    }
}

function renderEdgeTable() {
    const body = document.getElementById('edgeTopSetupsBody');
    const countBadge = document.getElementById('eeScannedCount');
    if (!body) return;

    let filtered = [..._edgeAllSetups];

    // Universe filtering
    if (_currentEdgeUniverse === 'nifty50') {
        const n50 = ['RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 'BHARTIARTL', 'SBIN', 'ITC', 'BAJFINANCE', 'WIPRO', 'KOTAKBANK', 'TATAMOTORS'];
        filtered = filtered.filter(s => n50.includes((s.symbol || '').replace('.NS', '').toUpperCase()));
    } else if (_currentEdgeUniverse === 'nse') {
        filtered = filtered.filter(s => (s.asset_class || '').toLowerCase() === 'nse');
    } else if (_currentEdgeUniverse === 'forex') {
        filtered = filtered.filter(s => (s.asset_class || '').toLowerCase() === 'forex');
    } else if (_currentEdgeUniverse === 'crypto') {
        filtered = filtered.filter(s => (s.asset_class || '').toLowerCase() === 'crypto');
    } else if (_currentEdgeUniverse === 'index') {
        filtered = filtered.filter(s => (s.asset_class || '').toLowerCase() === 'index');
    }

    // Secondary filtering
    if (_currentEdgeFilter === 'leaders') {
        filtered = filtered.filter(s => Number(s.edge_score || 0) >= 4.0 || Number(s.edge_score || 0) <= -4.0);
    } else if (_currentEdgeFilter === 'bullish') {
        filtered = filtered.filter(s => Number(s.edge_score || 0) > 0);
    } else if (_currentEdgeFilter === 'bearish') {
        filtered = filtered.filter(s => Number(s.edge_score || 0) < 0);
    } else if (_currentEdgeFilter === 'momentum') {
        filtered = filtered.filter(s => s.momentum != null && Math.abs(Number(s.momentum)) >= 10);
    } else if (_currentEdgeFilter === 'lowrisk') {
        filtered = filtered.filter(s => s.risk != null && Number(s.risk) <= 15);
    }

    // Search query filter
    if (_edgeSearchQuery) {
        filtered = filtered.filter(s => (s.symbol || '').toLowerCase().includes(_edgeSearchQuery));
    }

    // Sorting
    filtered.sort((a, b) => {
        let vA = 0, vB = 0;
        if (_edgeSortColumn === 'rank') return _edgeSortAsc ? 0 : -1;
        if (_edgeSortColumn === 'symbol') {
            vA = (a.symbol || '').toLowerCase();
            vB = (b.symbol || '').toLowerCase();
            return _edgeSortAsc ? vA.localeCompare(vB) : vB.localeCompare(vA);
        }
        if (_edgeSortColumn === 'ltp') {
            vA = Number(a.ltp || 0);
            vB = Number(b.ltp || 0);
        } else if (_edgeSortColumn === 'change') {
            vA = Number(a.change_pct || 0);
            vB = Number(b.change_pct || 0);
        } else if (_edgeSortColumn === 'score') {
            vA = Number(a.edge_score || 0);
            vB = Number(b.edge_score || 0);
        } else if (_edgeSortColumn === 'macro') {
            vA = Number(a.macro || 0);
            vB = Number(b.macro || 0);
        } else if (_edgeSortColumn === 'institutional') {
            vA = Number(a.cot || 0);
            vB = Number(b.cot || 0);
        } else if (_edgeSortColumn === 'sentiment') {
            vA = Number(a.sentiment || 0);
            vB = Number(b.sentiment || 0);
        } else if (_edgeSortColumn === 'momentum') {
            vA = Number(a.momentum || 0);
            vB = Number(b.momentum || 0);
        } else if (_edgeSortColumn === 'risk') {
            vA = Number(a.risk || 0);
            vB = Number(b.risk || 0);
        } else if (_edgeSortColumn === 'confidence') {
            vA = Number(a.confidence || 0);
            vB = Number(b.confidence || 0);
        }
        return _edgeSortAsc ? (vA - vB) : (vB - vA);
    });

    if (countBadge) countBadge.textContent = `${filtered.length} of ${_edgeAllSetups.length} setups`;

    if (!filtered.length) {
        body.innerHTML = `<tr><td colspan="13" class="empty-row">
            No active setups matching current universe/filters.
            <button onclick="switchEdgeUniverse('all');setEdgeSecondaryFilter('all');" style="margin-left:8px;background:none;border:none;color:#38bdf8;cursor:pointer;text-decoration:underline;">Reset Filters</button>
        </td></tr>`;
        return;
    }

    body.innerHTML = filtered.map((s, idx) => {
        const score = Number(s.edge_score || 0);
        const scoreColor = score >= 50 ? '#10b981' : score >= 20 ? '#38bdf8' : score >= 0 ? '#60a5fa' : score >= -20 ? '#f59e0b' : '#ef4444';
        const sym = _escHtml(s.symbol || '—');
        const rank = idx + 1;
        let cls = _escHtml((s.asset_class || 'nse').toLowerCase());
        if (cls === 'forex' && !sym.includes('/')) {
            cls = 'nse';
        }

        // Format LTP & Change
        const isForex = cls === 'forex';
        const isCrypto = cls === 'crypto';
        const curr = isForex ? '' : (isCrypto ? '$' : '₹');
        let effectiveLtp = s.ltp;
        let effectiveChg = s.change_pct;
        if (effectiveLtp == null && typeof _lastSignals !== 'undefined' && Array.isArray(_lastSignals)) {
            const match = _lastSignals.find(sig => sig && (sig.symbol === s.symbol || sig.symbol === (s.symbol || '').replace('.NS', '')));
            if (match) {
                effectiveLtp = match.close || match.entry || match.ltp;
                effectiveChg = match.change_pct != null ? match.change_pct : match.chg_pct;
            }
        }
        const ltpStr = effectiveLtp != null ? `${curr}${Number(effectiveLtp).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}` : '—';
        const chgVal = effectiveChg != null ? Number(effectiveChg) : null;
        const chgStr = chgVal != null ? `${chgVal >= 0 ? '+' : ''}${chgVal.toFixed(2)}%` : '—';
        const chgColor = chgVal != null ? (chgVal > 0 ? '#10b981' : (chgVal < 0 ? '#ef4444' : '#94a3b8')) : '#94a3b8';

        // Bias Pill
        const bias = score >= 20 ? 'BULLISH' : score <= -20 ? 'BEARISH' : 'NEUTRAL';
        const biasCls = bias.toLowerCase();

        // Mini Score Bar
        const barPct = Math.min(100, Math.max(8, Math.abs(score)));
        const barFill = `<div class="ee-score-bar-fill" style="width:${barPct}%;background:${scoreColor};"></div>`;

        // Format Factors
        const momStr = s.momentum != null ? `${Number(s.momentum) >= 0 ? '+' : ''}${Number(s.momentum).toFixed(1)}` : '—';
        const momColor = s.momentum != null ? (Number(s.momentum) >= 0 ? '#10b981' : '#ef4444') : '#94a3b8';

        const macroStr = s.macro != null ? Number(s.macro).toFixed(1) : '—';
        const instStr = s.cot != null && cls === 'forex' ? Number(s.cot).toFixed(1) : (cls === 'nse' ? '+FII Buy' : 'N/A');
        const sentStr = s.sentiment != null ? `${Number(s.sentiment) > 0 ? '+' : ''}${Number(s.sentiment).toFixed(1)}` : '—';
        const sentColor = s.sentiment != null ? (Number(s.sentiment) > 0 ? '#10b981' : (Number(s.sentiment) < 0 ? '#ef4444' : '#94a3b8')) : '#94a3b8';
        const riskStr = s.risk != null ? Number(s.risk).toFixed(1) : '—';
        const confStr = s.confidence != null ? `${(Number(s.confidence) * 100).toFixed(0)}%` : '100%';

        return `<tr>
            <td style="font-family:var(--font-mono);font-weight:700;color:#64748b;">#${rank}</td>
            <td>
                <div style="display:flex;align-items:center;gap:6px;">
                    <strong style="color:#f8fafc;font-size:0.84rem;">${sym}</strong>
                    <span class="asset-class-badge asset-class-${cls}">${cls.toUpperCase()}</span>
                </div>
            </td>
            <td style="font-family:var(--font-mono);font-weight:700;">${ltpStr}</td>
            <td style="font-family:var(--font-mono);font-weight:700;color:${chgColor};">${chgStr}</td>
            <td>
                <div class="ee-score-bar-wrap">
                    <span style="font-family:var(--font-mono);font-weight:800;color:${scoreColor};font-size:0.9rem;">${score.toFixed(1)}</span>
                    <div class="ee-score-bar">${barFill}</div>
                </div>
            </td>
            <td><span class="ee-bias-pill ${biasCls}">${bias}</span></td>
            <td style="font-family:var(--font-mono);color:#cbd5e1;">${macroStr}</td>
            <td style="font-family:var(--font-mono);color:#94a3b8;font-size:0.74rem;">${instStr}</td>
            <td style="font-family:var(--font-mono);color:${sentColor};">${sentStr}</td>
            <td style="font-family:var(--font-mono);color:${momColor};font-weight:600;">${momStr}</td>
            <td style="font-family:var(--font-mono);color:#cbd5e1;">${riskStr}</td>
            <td style="font-family:var(--font-mono);color:#60a5fa;font-weight:700;">${confStr}</td>
            <td style="text-align:center;">
                <button class="ee-action-btn" onclick="openEdgeResearchDrawer('${sym}', '${cls}')" style="padding:0.25rem 0.55rem;font-size:0.7rem;margin:0 auto;">
                    <span>Deep-Dive</span>
                </button>
            </td>
        </tr>`;
    }).join('');
}

function renderEdgeFlowPanel(flowData, overviewData) {
    const fiiVal = document.getElementById('eeFiiNetVal');
    const diiVal = document.getElementById('eeDiiNetVal');
    const rotStrip = document.getElementById('eeSectorRotationStrip');
    const datePill = document.getElementById('eeFlowDatePill');

    if (flowData) {
        const helpers = window.TradeflowHelpers || {};
        const fiiNet = helpers.getInstitutionalFlowNet ? helpers.getInstitutionalFlowNet(flowData, 'FII/FPI') : null;
        const diiNet = helpers.getInstitutionalFlowNet ? helpers.getInstitutionalFlowNet(flowData, 'DII') : null;
        if (datePill) {
            const source = flowData.source || 'Source unavailable';
            datePill.textContent = `${flowData.date || 'Latest available'} · ${source}${flowData.stale ? ' · STALE' : ''}`;
        }
        if (fiiVal) fiiVal.textContent = helpers.formatInstitutionalNetCr ? helpers.formatInstitutionalNetCr(fiiNet) : '—';
        if (diiVal) diiVal.textContent = helpers.formatInstitutionalNetCr ? helpers.formatInstitutionalNetCr(diiNet) : '—';
    }

    if (overviewData && overviewData.sector_rotation && rotStrip) {
        rotStrip.innerHTML = '<strong>Sector Institutional Rotation:</strong> ' + overviewData.sector_rotation.slice(0, 5).map(sec => {
            const isUp = sec.pct_change >= 0;
            return `<span style="display:inline-block;margin-right:8px;font-size:0.72rem;color:${isUp ? '#10b981' : '#ef4444'};">${sec.icon || ''} ${sec.name}: ${isUp ? '+' : ''}${sec.pct_change.toFixed(2)}%</span>`;
        }).join('');
    }
}

function renderEdgeNewsPanel(newsData) {
    const list = document.getElementById('eeNewsListBlock');
    if (!list || !newsData || !newsData.items) return;

    list.innerHTML = newsData.items.slice(0, 4).map(item => {
        const titleSafe = _escHtml(item.title || '');
        const srcSafe = _escHtml(item.source || 'News Feed');
        const pubSafe = _escHtml(item.published || 'Just now');
        return `
        <div style="background:rgba(255,255,255,0.02);padding:0.45rem 0.6rem;border-radius:6px;border:1px solid rgba(255,255,255,0.04);display:flex;justify-content:space-between;align-items:center;gap:8px;">
            <div style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
                <a href="${item.url || '#'}" target="_blank" rel="noopener noreferrer" style="color:#e2e8f0;text-decoration:none;font-size:0.75rem;font-weight:500;" title="${titleSafe}">
                    ${titleSafe}
                </a>
            </div>
            <span style="font-size: 0.72rem;color:#64748b;white-space:nowrap;">${srcSafe}</span>
        </div>`;
    }).join('');
}

function exportEdgeSetupsToCSV() {
    if (!_edgeAllSetups || !_edgeAllSetups.length) {
        alert('No setups to export.');
        return;
    }
    const headers = ['Rank', 'Symbol', 'AssetClass', 'LTP', 'ChangePct', 'EdgeScore', 'Bias', 'Macro', 'COT', 'Sentiment', 'Momentum', 'Risk', 'Confidence'];
    const rows = _edgeAllSetups.map((s, i) => {
        const score = Number(s.edge_score || 0);
        const bias = score >= 20 ? 'BULLISH' : score <= -20 ? 'BEARISH' : 'NEUTRAL';
        return [
            i + 1,
            `"${s.symbol || ''}"`,
            `"${s.asset_class || ''}"`,
            s.ltp != null ? s.ltp : '',
            s.change_pct != null ? s.change_pct : '',
            score.toFixed(1),
            `"${bias}"`,
            s.macro != null ? Number(s.macro).toFixed(1) : '',
            s.cot != null ? Number(s.cot).toFixed(1) : '',
            s.sentiment != null ? Number(s.sentiment).toFixed(1) : '',
            s.momentum != null ? Number(s.momentum).toFixed(1) : '',
            s.risk != null ? Number(s.risk).toFixed(1) : '',
            s.confidence != null ? Number(s.confidence).toFixed(2) : ''
        ].join(',');
    });

    const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows].join('\n');
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement('a');
    link.setAttribute('href', encodedUri);
    link.setAttribute('download', `tradeai_edge_engine_${_currentEdgeUniverse}_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

async function openEdgeResearchDrawer(symbol, assetClass = 'nse') {
    _currentDrawerSymbol = symbol;
    _currentDrawerAssetClass = assetClass;
    const drawer = document.getElementById('edgeResearchDrawer');
    if (!drawer) return;
    drawer.style.display = 'flex';

    // Header info
    document.getElementById('researchSymbol').textContent = symbol;
    const clsEl = document.getElementById('researchAssetClass');
    if (clsEl) {
        clsEl.textContent = assetClass.toUpperCase();
        clsEl.className = `asset-class-badge asset-class-${assetClass.toLowerCase()}`;
    }

    // Default tab
    switchDrawerTab('snapshot');

    // Load Snapshot & Chart
    loadDrawerChartData(symbol, _currentDrawerTf);

    // Load Multi-Factor & News Telemetry
    try {
        const resp = await fetchWithTimeout(`${API}/api/edge/research/${encodeURIComponent(symbol)}?asset_class=${assetClass || ''}`, {}, 15000);
        if (resp && resp.ok) {
            const data = await resp.json();
            const sc = Number(data.edge_score || 0);

            // Score badge & bias
            const scoreBadge = document.getElementById('researchScoreBadge');
            if (scoreBadge) {
                scoreBadge.textContent = `${sc >= 0 ? '+' : ''}${sc.toFixed(1)}`;
                scoreBadge.style.color = sc >= 50 ? '#10b981' : sc >= 20 ? '#38bdf8' : sc >= 0 ? '#60a5fa' : sc >= -20 ? '#f59e0b' : '#ef4444';
            }
            const biasPill = document.getElementById('researchBiasPill');
            if (biasPill) {
                const bText = sc >= 20 ? 'BULLISH' : sc <= -20 ? 'BEARISH' : 'NEUTRAL';
                biasPill.textContent = bText;
                biasPill.className = `ee-bias-pill ${bText.toLowerCase()}`;
            }

            // Rationale
            document.getElementById('researchRationale').textContent = data.rationale || 'Institutional quantitative setup.';

            // Factors Decomposition
            const fList = document.getElementById('researchFactorsList');
            if (fList && data.factors) {
                const items = Object.values(data.factors);
                fList.innerHTML = items.map(f => {
                    const statusBadge = f.status === 'LIVE' ? '<span class="status-dot live" style="width:6px;height:6px;display:inline-block;"></span> <span style="color:#10b981;font-weight:700;">LIVE</span>' : (f.status === 'NOT_APPLICABLE' ? '<span style="color:#64748b;">N/A</span>' : `<span style="color:#f59e0b;">${f.status}</span>`);
                    const valStr = f.value != null ? `${Number(f.value) >= 0 ? '+' : ''}${Number(f.value).toFixed(1)}` : 'N/A';
                    const effWeightStr = f.effective_weight != null ? `${(Number(f.effective_weight) * 100).toFixed(0)}% weight` : '';
                    return `
                    <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.02);padding:0.5rem 0.75rem;border-radius:6px;border:1px solid rgba(255,255,255,0.05);">
                        <div style="display:flex;align-items:center;gap:0.5rem;">
                            <span style="font-size: 0.72rem;">${statusBadge}</span>
                            <strong style="font-size:0.8rem;color:#f8fafc;">${_escHtml(f.label || f.name)}</strong>
                            <span style="font-size: 0.72rem;color:#64748b;">${effWeightStr}</span>
                        </div>
                        <div style="text-align:right;">
                            <span style="font-family:var(--font-mono);font-weight:800;font-size:0.85rem;color:${f.value > 0 ? '#10b981' : (f.value < 0 ? '#ef4444' : '#8b949e')};">${valStr}</span>
                            <div style="font-size: 0.72rem;color:#94a3b8;max-width:280px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${_escHtml(f.detail || '')}</div>
                        </div>
                    </div>`;
                }).join('');
            }

            // News Headlines
            const hList = document.getElementById('researchHeadlinesList');
            if (hList) {
                const sentObj = data.factors && data.factors.sentiment;
                hList.innerHTML = `
                    <div style="font-size:0.75rem;color:#94a3b8;margin-bottom:4px;">Verified Feed: ${sentObj ? sentObj.source : 'GDELT / Google News RSS'}</div>
                    <div style="font-size:0.82rem;color:#f1f5f9;line-height:1.45;">${_escHtml(sentObj?.detail || 'No negative anomalies detected in public feeds. Sentiment supportive.')}</div>
                `;
            }

            // Trading Context
            const ctxBlock = document.getElementById('researchContextBlock');
            if (ctxBlock) {
                ctxBlock.innerHTML = `
                    <div><strong>Directional Edge:</strong> ${sc >= 0 ? 'LONG / BULLISH BIAS' : 'SHORT / BEARISH BIAS'} (Confidence: ${(Number(data.confidence || 1) * 100).toFixed(0)}%)</div>
                    <div><strong>Key Invalidation:</strong> Edge is considered invalidated if price closes beyond trailing ATR band or sentiment shifts to severe conflict.</div>
                    <div><strong>Volatility Rating:</strong> ATR Volatility Risk Score: ${Number(data.factors?.risk?.value || 3.5).toFixed(1)} / 100 (Manageable risk bracket).</div>
                `;
            }
        }
    } catch (e) {
        console.error('Error fetching deep research:', e);
    }
}

async function loadDrawerChartData(symbol, interval = '1d') {
    _currentDrawerTf = interval;
    const cleanSym = symbol.replace('.NS', '').replace('/', '-');
    try {
        const res = await fetchWithTimeout(`${API}/api/market/${cleanSym}?limit=60&interval=${interval}`, {}, 10000);
        if (!res.ok) throw new Error('Market data unavailable');
        const json = await res.json();
        const bars = json.data || [];
        if (bars.length) {
            const last = bars[bars.length - 1];
            const prev = bars.length >= 2 ? bars[bars.length - 2] : last;
            const chg = prev.close > 0 ? ((last.close - prev.close) / prev.close) * 100 : 0;

            const curr = (_currentDrawerAssetClass === 'forex' ? '' : (_currentDrawerAssetClass === 'crypto' ? '$' : '₹'));
            document.getElementById('dSnapPrice').textContent = `${curr}${Number(last.close).toFixed(2)}`;
            const chgEl = document.getElementById('dSnapChange');
            chgEl.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`;
            chgEl.style.color = chg >= 0 ? '#10b981' : '#ef4444';
            document.getElementById('dSnapRsi').textContent = last.rsi != null ? Number(last.rsi).toFixed(1) : '52.4';
            document.getElementById('dSnapRegime').textContent = last.signal || 'TRENDING';

            renderDrawerChart(bars, interval);
        }
    } catch (e) {
        console.warn('Failed to load chart data for drawer:', e);
    }
}

function renderDrawerChart(bars, interval) {
    const canvas = document.getElementById('eeDrawerChartCanvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    if (_edgeDrawerChart) {
        _edgeDrawerChart.destroy();
        _edgeDrawerChart = null;
    }

    const labels = bars.map(b => (b.timestamp || '').slice(5, 16).replace('T', ' '));
    const closes = bars.map(b => b.close);
    const ema20 = bars.map(b => b.ema_20 || null);

    const isUp = closes[closes.length - 1] >= closes[0];
    const lineColor = isUp ? '#10b981' : '#ef4444';
    const gradColor = isUp ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.15)';

    const gradient = ctx.createLinearGradient(0, 0, 0, 200);
    gradient.addColorStop(0, gradColor);
    gradient.addColorStop(1, 'rgba(15, 23, 42, 0)');

    _edgeDrawerChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Price',
                    data: closes,
                    borderColor: lineColor,
                    backgroundColor: gradient,
                    borderWidth: 2,
                    fill: true,
                    tension: 0.15,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                },
                {
                    label: 'EMA 20',
                    data: ema20,
                    borderColor: '#38bdf8',
                    borderWidth: 1.5,
                    borderDash: [4, 4],
                    fill: false,
                    pointRadius: 0,
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'index',
            },
            plugins: {
                legend: {
                    display: true,
                    labels: { color: '#94a3b8', font: { size: 10 } }
                },
                tooltip: {
                    backgroundColor: '#090d16',
                    titleColor: '#cbd5e1',
                    bodyColor: '#f8fafc',
                    borderColor: 'rgba(255,255,255,0.1)',
                    borderWidth: 1,
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.04)' },
                    ticks: { color: '#64748b', font: { size: 9 }, maxTicksLimit: 6 }
                },
                y: {
                    position: 'right',
                    grid: { color: 'rgba(255,255,255,0.04)' },
                    ticks: { color: '#94a3b8', font: { size: 10 } }
                }
            }
        }
    });
}

function switchDrawerTab(tabName) {
    ['snapshot', 'factors', 'news', 'context'].forEach(t => {
        const pane = document.getElementById(`drawerPane${t.charAt(0).toUpperCase() + t.slice(1)}`);
        const btn = document.getElementById(`drawerTabBtn${t.charAt(0).toUpperCase() + t.slice(1)}`);
        if (pane) pane.style.display = t === tabName ? 'flex' : 'none';
        if (btn) btn.classList.toggle('active', t === tabName);
    });
}

function changeDrawerChartTimeframe(tf) {
    document.querySelectorAll('[data-tf]').forEach(b => {
        b.classList.toggle('active', b.getAttribute('data-tf') === tf);
    });
    if (_currentDrawerSymbol) {
        loadDrawerChartData(_currentDrawerSymbol, tf);
    }
}

function closeEdgeResearchDrawer() {
    const drawer = document.getElementById('edgeResearchDrawer');
    if (drawer) drawer.style.display = 'none';
    if (_edgeDrawerChart) {
        _edgeDrawerChart.destroy();
        _edgeDrawerChart = null;
    }
}







async function loadEdgeMacroHeatmap() {



    const grid = document.getElementById('edgeMacroHeatmapGrid');



    if (!grid) return;



    grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Loading macro heatmap...</p></div>';



    try {



        const resp = await fetchWithTimeout(`${API}/api/edge/macro-heatmap`, {}, 20000, 1);



        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);



        const data = await resp.json();



        const rawHeatmap = data.heatmap || {};
        const heatmap = Array.isArray(rawHeatmap)
            ? rawHeatmap
            : Object.entries(rawHeatmap).map(([k, v]) => ({
                symbol: `${k.toUpperCase()} (${v.sample_symbol || ''})`,
                category: k.toUpperCase(),
                score: Number(v.edge_score || 50),
                summary: `Macro ${v.macro_score ?? '—'} · Sentiment ${v.sentiment_score ?? '—'} · Risk ${v.risk_score ?? '—'}`,
                regime: v.status || 'READY'
            }));
        if (!heatmap.length) {
            grid.innerHTML = '<p style="color:var(--text-muted);font-size:0.9rem;">No macro heatmap data available.</p>';
            return;
        }



        grid.innerHTML = `



            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:1rem;">



                ${heatmap.map(item => {



                    const score = Number(item.score || 0);



                    const color = score >= 70 ? 'var(--green)' : score >= 50 ? 'var(--amber)' : 'var(--red)';



                    const bg = score >= 70 ? 'rgba(16,185,129,0.08)' : score >= 50 ? 'rgba(245,158,11,0.08)' : 'rgba(239,68,68,0.08)';



                    return `



                        <div class="intel-card" style="padding:1rem;background:${bg};border:1px solid var(--border-color);border-radius:8px;">



                            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;">



                                <strong>${_escHtml(item.symbol || item.category || 'Asset')}</strong>



                                <span style="font-family:var(--font-mono);font-weight:800;color:${color};font-size:1.1rem;">${score.toFixed(1)}</span>



                            </div>



                            <div style="font-size:0.75rem;color:var(--text-muted);">${_escHtml(item.regime || item.summary || 'Macro regime aligned')}</div>



                        </div>



                    `;



                }).join('')}



            </div>



        `;



    } catch (err) {



        console.error('[loadEdgeMacroHeatmap] error:', err);



        grid.innerHTML = `<p style="color:var(--red);font-size:0.9rem;">Failed to load macro heatmap (${_escHtml(err.message)})</p>`;



    }



}







async function loadEdgeRiskGauge() {



    const label = document.getElementById('riskRegimeLabel');



    const scoreVal = document.getElementById('riskScoreValue');



    const compBox = document.getElementById('riskComponentsBreakdown');



    const canvas = document.getElementById('riskGaugeCanvas');



    if (!label || !scoreVal) return;







    label.textContent = 'UPDATING...';



    try {



        const resp = await fetchWithTimeout(`${API}/api/edge/risk-gauge`);



        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);



        const data = await resp.json();



        const score = Number(data.risk_score || 0);



        const regime = data.regime || 'NEUTRAL';



        



        let color = '#10b981';



        if (score >= 70) color = '#ef4444';



        else if (score >= 45) color = '#f59e0b';







        label.textContent = regime.toUpperCase();



        label.style.color = color;



        scoreVal.textContent = score.toFixed(1);



        scoreVal.style.color = color;







        if (canvas) {



            const ctx = canvas.getContext('2d');



            const w = canvas.width;



            const h = canvas.height;



            ctx.clearRect(0, 0, w, h);



            const cx = w / 2;



            const cy = h / 2 + 20;



            const radius = 65;



            



            ctx.beginPath();



            ctx.arc(cx, cy, radius, Math.PI, 2 * Math.PI, false);



            ctx.lineWidth = 14;



            ctx.strokeStyle = 'rgba(255,255,255,0.08)';



            ctx.stroke();







            const pct = Math.min(100, Math.max(0, score)) / 100;



            const endAngle = Math.PI + pct * Math.PI;



            ctx.beginPath();



            ctx.arc(cx, cy, radius, Math.PI, endAngle, false);



            ctx.lineWidth = 14;



            ctx.strokeStyle = color;



            ctx.lineCap = 'round';



            ctx.stroke();



        }







        if (compBox && data.indicators) {



            const inds = Object.entries(data.indicators);



            compBox.innerHTML = `



                <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;margin-top:0.5rem;">



                    ${inds.map(([k, v]) => `



                        <div style="padding:0.6rem;background:rgba(255,255,255,0.03);border-radius:6px;">



                            <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">${_escHtml(k)}</div>



                            <div style="font-size:0.95rem;font-weight:700;font-family:var(--font-mono);margin-top:2px;">${(typeof v === 'number' && Number.isFinite(v)) ? Number(v).toFixed(2) : (v != null && !isNaN(Number(v)) && String(v).trim() !== '' ? Number(v).toFixed(2) : _escHtml(String(v ?? '—')))}</div>



                        </div>



                    `).join('')}



                </div>



            `;



        }



    } catch (err) {



        console.error('[loadEdgeRiskGauge] error:', err);



        label.textContent = 'ERROR';



        label.style.color = 'var(--red)';



    }



}







// ── Forex Handlers ────────────────────────────────────────────────



let _forexAllSetups = [];

let _forexCurrentFilter = 'all';



let _lastKnownForexRates = null;
let _forexRetryTimeoutId = null;
let _forexRetryCount = 0;
const _MAX_FOREX_RETRIES = 5;
const _BASE_FOREX_BACKOFF_MS = 8000;

function resetForexRetry() {
    if (_forexRetryTimeoutId) {
        clearTimeout(_forexRetryTimeoutId);
        _forexRetryTimeoutId = null;
    }
    _forexRetryCount = 0;
}

function getForexRetryCount() {
    return _forexRetryCount;
}

/**
 * Compute real-time Forex session intelligence directly on client in IST.
 * Evaluates session status, overlap description, and liquidity rating deterministically from IST clock time without API dependency.
 */
function computeClientForexSessions() {
    const now = new Date();
    const formatter = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        weekday: 'short',
        hour12: false
    });
    const parts = formatter.formatToParts(now);
    let hour = '00', minute = '00', second = '00', weekday = '';
    for (const p of parts) {
        if (p.type === 'hour') hour = p.value;
        if (p.type === 'minute') minute = p.value;
        if (p.type === 'second') second = p.value;
        if (p.type === 'weekday') weekday = p.value;
    }
    const currentIst = `${hour}:${minute}`;
    const isWeekend = weekday === 'Sat' || (weekday === 'Sun' && parseInt(hour, 10) < 23);

    const inSession = (start, end, cur) => {
        if (start <= end) return cur >= start && cur < end;
        return cur >= start || cur < end;
    };

    const sessionDefs = {
        ASIAN:   { start: '05:30', end: '14:30', best_pairs: ['USD/JPY', 'AUD/USD'] },
        LONDON:  { start: '13:30', end: '22:30', best_pairs: ['EUR/USD', 'GBP/USD', 'EUR/INR'] },
        NEWYORK: { start: '18:30', end: '03:30', best_pairs: ['EUR/USD', 'USD/CAD', 'GBP/USD'] },
        NSE_INR: { start: '09:00', end: '17:00', best_pairs: ['USD/INR', 'EUR/INR', 'GBP/INR'] }
    };

    const sessions = {};
    const active = [];
    for (const [name, def] of Object.entries(sessionDefs)) {
        const isOpen = isWeekend ? false : inSession(def.start, def.end, currentIst);
        sessions[name] = {
            open: isOpen,
            start: def.start,
            end: def.end,
            best_pairs: def.best_pairs
        };
        if (isOpen) active.push(name);
    }

    const majorActive = active.filter(s => ['ASIAN', 'LONDON', 'NEWYORK'].includes(s));
    const overlap = isWeekend ? 'WEEKEND CLOSED' : (majorActive.length ? majorActive.join(' + ') : 'OFF-HOURS');

    let liquidity = 'LOW';
    if (!isWeekend) {
        if (active.includes('LONDON') && active.includes('NEWYORK')) liquidity = 'HIGH';
        else if (active.includes('LONDON') || active.includes('NEWYORK') || active.includes('ASIAN')) liquidity = 'MODERATE';
    }

    return {
        sessions,
        active_sessions: active,
        overlap,
        liquidity,
        current_ist: `${currentIst}:${second} IST`,
        is_weekend: isWeekend
    };
}

async function loadForexData(isAutoRetry = false) {
    if (!isAutoRetry) {
        resetForexRetry();
    }

    const meta = document.getElementById('forexScanMeta');
    const grid = document.getElementById('forexSignalGrid');
    const liqBadge = document.getElementById('forexLiquidityBadge');
    const ovText = document.getElementById('forexOverlapText');
    const topCountBadge = document.getElementById('forexTopCountBadge');
    const topSetupsGrid = document.getElementById('forexTopSetupsGrid');

    if (meta) meta.textContent = 'Updating sessions & scanning universe...';

    // Compute deterministic client sessions immediately so UI never hangs on loading placeholders
    const clientIntel = computeClientForexSessions();
    renderForexSessions(clientIntel.sessions, clientIntel.current_ist);
    if (ovText) ovText.textContent = clientIntel.overlap;
    if (liqBadge) {
        liqBadge.innerHTML = `<span class="status-dot live"></span> LIQUIDITY: ${_escHtml(clientIntel.liquidity)}`;
        liqBadge.style.color = clientIntel.liquidity === 'HIGH' ? 'var(--green)' : (clientIntel.liquidity === 'MODERATE' ? 'var(--cyan)' : 'var(--text-muted)');
    }

    // 1. Fetch Intelligence & Session Status
    try {
        const iResp = await fetchWithTimeout(`${API}/api/forex/intelligence`, {}, 15000);
        if (iResp.ok) {
            const iData = await iResp.json();
            const sessIntel = iData.session_intelligence || clientIntel;
            renderForexSessions(sessIntel.sessions || clientIntel.sessions, sessIntel.current_ist || clientIntel.current_ist);
            renderCurrencyStrength(iData.currency_strengths || {}, iData.risk_sentiment || 'NEUTRAL');

            if (liqBadge && sessIntel.liquidity) {
                liqBadge.innerHTML = `<span class="status-dot live"></span> LIQUIDITY: ${_escHtml(sessIntel.liquidity)}`;
                liqBadge.style.color = sessIntel.liquidity === 'HIGH' ? 'var(--green)' : (sessIntel.liquidity === 'MODERATE' ? 'var(--cyan)' : 'var(--text-muted)');
            }
            if (ovText && sessIntel.overlap) {
                ovText.textContent = sessIntel.overlap;
            }
        } else {
            // Non-2xx response (HTTP 403 under Section 6A, HTTP 429 under Twelve Data rate limit, etc.)
            renderForexSessions(clientIntel.sessions, clientIntel.current_ist);
            const fallbackSentiment = iResp.status === 403 ? 'STANDBY' : (iResp.status === 429 ? 'COOLING' : 'NEUTRAL');
            renderCurrencyStrength({}, fallbackSentiment);
            if (ovText) ovText.textContent = clientIntel.overlap;
            if (liqBadge) {
                if (iResp.status === 403) {
                    liqBadge.innerHTML = '<span class="status-dot" style="background:#60a5fa;"></span> STANDBY: NSE FOCUS';
                    liqBadge.style.color = '#60a5fa';
                } else if (iResp.status === 429) {
                    liqBadge.innerHTML = '<span class="status-dot" style="background:#f59e0b;"></span> PROVIDER RATE LIMIT COOLING';
                    liqBadge.style.color = '#f59e0b';
                } else {
                    liqBadge.innerHTML = `<span class="status-dot live"></span> LIQUIDITY: ${_escHtml(clientIntel.liquidity)}`;
                    liqBadge.style.color = clientIntel.liquidity === 'HIGH' ? 'var(--green)' : (clientIntel.liquidity === 'MODERATE' ? 'var(--cyan)' : 'var(--text-muted)');
                }
            }
        }
    } catch (e) {
        console.warn('[loadForexData] intelligence fetch err:', e);
        renderForexSessions(clientIntel.sessions, clientIntel.current_ist);
        renderCurrencyStrength({}, 'NEUTRAL');
        if (ovText) ovText.textContent = clientIntel.overlap;
    }

    // 2. Fetch Live Rates for INR Strip
    try {
        const rResp = await fetchWithTimeout(`${API}/api/forex/rates`, {}, 15000);
        if (rResp.ok) {
            const rData = await rResp.json();
            renderInrStrip(rData.rates || {});
        } else {
            const statusPill = rResp.status === 403 ? 'NSE CURRENCY STANDBY' : (rResp.status === 429 ? 'RATE LIMIT COOLING' : null);
            renderInrStrip(null, statusPill);
        }
    } catch (e) {
        console.warn('[loadForexData] rates fetch err:', e);
        renderInrStrip(null, null);
    }

    // 3. Fetch Swing Signals
    if (grid) {
        grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Scanning forex universe across 4h &amp; 1D timeframes...</p></div>';
        try {
            const resp = await fetchWithTimeout(`${API}/api/forex/signals`, {}, 30000);
            if (resp.status === 403) {
                grid.innerHTML = `
                    <div class="card" style="padding:2rem;text-align:center;grid-column:1/-1;">
                        <div class="live-pill" style="background:rgba(59,130,246,0.12);color:#60a5fa;border:1px solid rgba(59,130,246,0.3);font-size:0.75rem;padding:3px 10px;font-weight:700;margin-bottom:0.75rem;display:inline-flex;align-items:center;gap:6px;">
                            <span class="status-dot" style="background:#60a5fa;"></span> STANDBY: NSE FOCUS
                        </div>
                        <p style="color:var(--text-muted);font-size:0.85rem;max-width:540px;margin:0 auto 1rem auto;line-height:1.5;">
                            Forex Paper Engine is in standby under strict NSE Focus Mode (Section 6A). Multi-Asset processing can be unlocked via Market Control.
                        </p>
                        <button class="toggle-btn" onclick="handleCryptoForexToggleClick()" style="font-size:0.78rem;padding:0.4rem 1rem;background:rgba(59,130,246,0.15);color:#60a5fa;border:1px solid rgba(59,130,246,0.4);">
                            Unlock Multi-Asset Mode
                        </button>
                    </div>
                `;
                if (meta) meta.textContent = 'Standby (NSE Focus Mode Active)';
                if (topSetupsGrid) {
                    topSetupsGrid.innerHTML = `
                        <div class="card" style="padding:1.5rem;text-align:center;grid-column:1/-1;">
                            <span class="live-pill" style="background:rgba(59,130,246,0.15);color:#60a5fa;border:1px solid rgba(59,130,246,0.3);font-size:0.75rem;padding:3px 10px;font-weight:700;">SECTION 6A ACTIVE</span>
                            <p style="color:var(--text-muted);font-size:0.82rem;margin:0.5rem 0 0 0;">Forex setup generation paused in strict NSE Focus Mode.</p>
                        </div>
                    `;
                }
                if (topCountBadge) topCountBadge.textContent = 'Standby';
                return;
            }

            if (resp.status === 429) {
                let retryAfterSec = 0;
                try {
                    const header = resp.headers && resp.headers.get ? resp.headers.get('retry-after') : null;
                    if (header) {
                        const parsed = parseInt(header, 10);
                        if (!isNaN(parsed) && parsed > 0) retryAfterSec = Math.min(parsed, 65);
                    }
                } catch (_) {}

                if (_forexRetryCount < _MAX_FOREX_RETRIES) {
                    _forexRetryCount++;
                    const delaySec = retryAfterSec > 0
                        ? retryAfterSec
                        : Math.min(Math.round((_BASE_FOREX_BACKOFF_MS * Math.pow(1.8, _forexRetryCount - 1)) / 1000), 65);

                    if (meta) meta.textContent = `Rate limited · Retrying in ${delaySec}s (${_forexRetryCount}/${_MAX_FOREX_RETRIES})`;
                    grid.innerHTML = `
                        <div class="loading-state" style="padding:2rem;text-align:center;grid-column:1/-1;">
                            <div class="live-pill" style="background:rgba(245,158,11,0.12);color:#f59e0b;border:1px solid rgba(245,158,11,0.3);font-size:0.75rem;padding:3px 10px;font-weight:700;margin-bottom:0.75rem;display:inline-flex;align-items:center;gap:6px;">
                                <span class="status-dot" style="background:#f59e0b;"></span> PROVIDER RATE LIMIT COOLING
                            </div>
                            <p style="color:var(--text-muted);font-size:0.85rem;max-width:540px;margin:0 auto 1rem auto;line-height:1.5;">
                                Twelve Data free tier quota reached. Backing off <strong>${delaySec}s</strong> before retry (${_forexRetryCount}/${_MAX_FOREX_RETRIES}).
                            </p>
                            <div style="font-size:0.75rem;color:var(--text-muted);">Keyless secondary feeds remain active for session intelligence.</div>
                        </div>
                    `;
                    if (topSetupsGrid) {
                        topSetupsGrid.innerHTML = `
                            <div class="card" style="padding:1.5rem;text-align:center;grid-column:1/-1;">
                                <span class="live-pill" style="background:rgba(245,158,11,0.15);color:#f59e0b;border:1px solid rgba(245,158,11,0.3);font-size:0.75rem;padding:3px 10px;font-weight:700;">PROVIDER RATE LIMIT COOLING</span>
                                <p style="color:var(--text-muted);font-size:0.82rem;margin:0.5rem 0 0 0;">Rate limit cooling down. Re-evaluating candidate pairs in ${delaySec}s...</p>
                            </div>
                        `;
                    }
                    if (topCountBadge) topCountBadge.textContent = 'Cooling Down';

                    _forexRetryTimeoutId = setTimeout(() => {
                        loadForexData(true);
                    }, delaySec * 1000);
                } else {
                    if (meta) meta.textContent = 'Rate limit cooling paused (Retries exhausted)';
                    grid.innerHTML = `
                        <div class="card" style="padding:2rem;text-align:center;grid-column:1/-1;">
                            <div class="live-pill" style="background:rgba(245,158,11,0.12);color:#f59e0b;border:1px solid rgba(245,158,11,0.3);font-size:0.75rem;padding:3px 10px;font-weight:700;margin-bottom:0.75rem;display:inline-flex;align-items:center;gap:6px;">
                                <span class="status-dot" style="background:#f59e0b;"></span> PROVIDER RATE LIMIT COOLING
                            </div>
                            <p style="color:var(--text-muted);font-size:0.85rem;max-width:540px;margin:0 auto 1rem auto;line-height:1.5;">
                                Maximum retries reached while Twelve Data quota cools down. You can resume scanner requests manually when ready.
                            </p>
                            <button class="toggle-btn" onclick="loadForexData(false)" style="font-size:0.78rem;padding:0.4rem 1rem;">
                                ↺ Retry Scanner Now
                            </button>
                        </div>
                    `;
                }
                return;
            }

            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

            const data = await resp.json();
            _forexAllSetups = data.setups || [];
            const qualified = data.qualified_setups || _forexAllSetups.filter(s => s.status === 'QUALIFIED');
            const topSetups = data.top_setups || qualified.slice(0, 3);

            if (topCountBadge) {
                topCountBadge.textContent = `${qualified.length} Qualified Setup${qualified.length === 1 ? '' : 's'}`;
            }

            if (meta) {
                meta.textContent = `Last scan: ${new Date().toLocaleTimeString()} • ${qualified.length} qualified / ${_forexAllSetups.length} evaluated`;
            }

            renderTopForexSetups(topSetups);
            renderForexSignals(_forexAllSetups, _forexCurrentFilter);
            _forexRetryCount = 0;
        } catch (err) {
            console.error('[loadForexData] signals fetch err:', err);
            grid.innerHTML = `
                <div class="loading-state" style="padding:2rem;text-align:center;grid-column:1/-1;">
                    <div class="live-pill" style="background:rgba(239,68,68,0.12);color:var(--red);border:1px solid rgba(239,68,68,0.3);font-size:0.75rem;padding:3px 10px;font-weight:700;margin-bottom:0.75rem;display:inline-flex;align-items:center;gap:6px;">
                        <span class="status-dot" style="background:var(--red);"></span> SERVICE TEMPORARILY OFFLINE
                    </div>
                    <p style="color:var(--text-muted);font-size:0.85rem;max-width:500px;margin:0 auto 1rem auto;">
                        Unable to synchronize forex setups at this time. Network connection or upstream gateway is recovering.
                    </p>
                    <button class="toggle-btn" onclick="loadForexData(false)" style="font-size:0.78rem;padding:0.4rem 1rem;">
                        ↺ Retry Scanner
                    </button>
                </div>
            `;
            if (meta) meta.textContent = 'Scan temporarily unavailable';
            renderTopForexSetups([]);
        }
    }
}

function renderInrStrip(rates, statusPill = null) {
    const row = document.getElementById('forexInrRow');
    if (!row) return;

    const inrPairs = ['USD/INR', 'EUR/INR', 'GBP/INR', 'JPY/INR'];
    const referenceRates = {
        'USD/INR': 86.85,
        'EUR/INR': 91.40,
        'GBP/INR': 110.10,
        'JPY/INR': 0.565
    };

    let effectiveRates = null;
    let rateSourceLabel = '';

    if (rates && typeof rates === 'object') {
        const hasPositiveRate = inrPairs.some(p => Number(rates[p] || 0) > 0);
        if (hasPositiveRate) {
            effectiveRates = rates;
            _lastKnownForexRates = { ...rates };
            rateSourceLabel = '';
        }
    }

    if (!effectiveRates && _lastKnownForexRates) {
        effectiveRates = _lastKnownForexRates;
        rateSourceLabel = 'Cached';
    }

    if (!effectiveRates) {
        effectiveRates = referenceRates;
        rateSourceLabel = 'Indicative';
    }

    const renderPair = (sym) => {
        const rate = Number(effectiveRates[sym] || 0);
        if (rate <= 0) return '';
        const decimals = sym.includes('JPY') ? 3 : 2;
        const subLabel = rateSourceLabel ? `<span style="font-size: 0.72rem;color:var(--text-muted);font-weight:400;margin-left:4px;">(${rateSourceLabel})</span>` : '';
        return `
            <div style="display:flex;flex-direction:column;gap:2px;">
                <span style="font-size:0.75rem;font-weight:600;color:var(--text-primary,#f8fafc);">${escapeHtml(sym)}${subLabel}</span>
                <span style="font-family:monospace;font-size:0.85rem;color:var(--cyan,#38bdf8);font-weight:600;">₹${rate.toFixed(decimals)}</span>
            </div>
        `;
    };

    const inrHtml = inrPairs.map(renderPair).filter(Boolean).join('<div style="width:1px;background:rgba(255,255,255,0.08);margin:0 0.5rem;"></div>');

    let pillHtml = '';
    if (statusPill === 'NSE CURRENCY STANDBY') {
        pillHtml = '<div style="margin-left:auto;display:flex;align-items:center;"><span class="live-pill" style="background:rgba(59,130,246,0.12);color:#60a5fa;border:1px solid rgba(59,130,246,0.25);font-size:0.7rem;padding:2px 8px;font-weight:700;">NSE CURRENCY STANDBY</span></div>';
    } else if (statusPill === 'RATE LIMIT COOLING') {
        pillHtml = '<div style="margin-left:auto;display:flex;align-items:center;"><span class="live-pill" style="background:rgba(245,158,11,0.12);color:#f59e0b;border:1px solid rgba(245,158,11,0.25);font-size:0.7rem;padding:2px 8px;font-weight:700;">RATE LIMIT COOLING</span></div>';
    }

    row.innerHTML = (inrHtml ? `${inrHtml}${pillHtml}` : '<span style="color:var(--text-muted);font-size:0.82rem;">Reference INR benchmarks active</span>');
}

function renderForexSessions(sessions, currentIst) {
    const clock = document.getElementById('forexSessionClock');
    if (!clock) return;

    let entries = Object.entries(sessions || {});
    if (!entries.length) {
        const fallback = computeClientForexSessions();
        entries = Object.entries(fallback.sessions);
    }

    clock.innerHTML = entries.map(([name, s]) => {
        const isOpen = s.open === true;
        const statusColor = isOpen ? 'var(--green)' : 'var(--text-muted)';
        const statusBg = isOpen ? 'rgba(16,185,129,0.12)' : 'rgba(255,255,255,0.02)';
        const statusBdr = isOpen ? 'rgba(16,185,129,0.35)' : 'var(--border-color)';
        const pairs = (s.best_pairs || []).slice(0, 3).join(', ');

        return `
            <div class="intel-card" style="padding:0.6rem 0.75rem;background:${statusBg};border:1px solid ${statusBdr};border-radius:6px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.25rem;">
                    <strong style="font-size:0.82rem;">${_escHtml(name)}</strong>
                    <span style="font-size: 0.70rem;font-weight:800;padding:1px 5px;border-radius:3px;color:${statusColor};border:1px solid ${statusColor};">${isOpen ? '● OPEN' : 'CLOSED'}</span>
                </div>
                <div style="font-size:0.7rem;color:var(--text-muted);">${_escHtml(s.start || '')} - ${_escHtml(s.end || '')} IST</div>
                <div style="font-size: 0.72rem;color:var(--text-muted);margin-top:0.2rem;">Pairs: <span style="color:var(--text-primary);">${_escHtml(pairs || '—')}</span></div>
            </div>
        `;
    }).join('');
}

function renderCurrencyStrength(strengths, riskSentiment) {
    const matrix = document.getElementById('forexCurrencyMatrix');
    const badge = document.getElementById('forexRiskSentimentBadge');

    if (badge) {
        const sentiment = String(riskSentiment || 'NEUTRAL').toUpperCase();
        badge.textContent = sentiment;
        if (sentiment === 'RISK ON') {
            badge.style.background = 'rgba(16,185,129,0.15)';
            badge.style.color = 'var(--green)';
            badge.style.borderColor = 'rgba(16,185,129,0.3)';
        } else if (sentiment === 'STANDBY') {
            badge.style.background = 'rgba(59,130,246,0.15)';
            badge.style.color = '#60a5fa';
            badge.style.borderColor = 'rgba(59,130,246,0.3)';
        } else if (sentiment.includes('COOLING')) {
            badge.style.background = 'rgba(245,158,11,0.15)';
            badge.style.color = '#f59e0b';
            badge.style.borderColor = 'rgba(245,158,11,0.3)';
        } else if (sentiment === 'RISK OFF') {
            badge.style.background = 'rgba(239,68,68,0.15)';
            badge.style.color = 'var(--red)';
            badge.style.borderColor = 'rgba(239,68,68,0.3)';
        } else {
            badge.style.background = 'rgba(148,163,184,0.12)';
            badge.style.color = 'var(--text-muted)';
            badge.style.borderColor = 'rgba(148,163,184,0.25)';
        }
    }

    if (!matrix) return;

    const standardCurrencies = ['USD', 'EUR', 'GBP', 'JPY', 'INR', 'AUD', 'CAD', 'CHF'];
    const hasData = strengths && typeof strengths === 'object' && Object.keys(strengths).length > 0;
    const entries = hasData 
        ? Object.entries(strengths) 
        : standardCurrencies.map(c => [c, 0.0]);

    matrix.innerHTML = entries.map(([curr, val]) => {
        const num = Number(val || 0);
        const isPos = num > 0.0001;
        const isNeg = num < -0.0001;
        const color = isPos ? 'var(--green)' : (isNeg ? 'var(--red)' : 'var(--text-muted)');
        const sign = isPos ? '+' : '';

        return `
            <div style="background:rgba(255,255,255,0.03);padding:0.4rem 0.2rem;border-radius:4px;border:1px solid rgba(255,255,255,0.05);">
                <div style="font-size:0.75rem;font-weight:700;">${_escHtml(curr)}</div>
                <div style="font-family:var(--font-mono);font-size:0.78rem;font-weight:700;color:${color};">${sign}${num.toFixed(2)}%</div>
            </div>
        `;
    }).join('');
}



function renderTopForexSetups(topSetups) {

    const container = document.getElementById('forexTopSetupsGrid');

    if (!container) return;



    if (!topSetups || !topSetups.length) {

        container.innerHTML = `

            <div class="card" style="padding:1.5rem;text-align:center;grid-column:1/-1;">

                <span style="color:var(--text-muted);font-size:0.85rem;">No setups currently satisfy strict qualification gates (R:R &ge; 1.5, fresh quote, 4h/1D trend alignment). Setups will appear as market patterns form.</span>

            </div>

        `;

        return;

    }



    container.innerHTML = topSetups.map((s, idx) => {

        const dir = s.direction || 'LONG';

        const isLong = dir === 'LONG' || dir === 'BUY';

        const dirColor = isLong ? 'var(--green)' : 'var(--red)';

        const dirBg = isLong ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)';

        const dirBdr = isLong ? 'rgba(16,185,129,0.35)' : 'rgba(239,68,68,0.35)';



        const entry = Number(s.entry || s.entry_price || 0).toFixed(4);

        const sl = Number(s.sl || s.stop_loss || 0).toFixed(4);

        const tp1 = Number(s.tp1 || s.take_profit || 0).toFixed(4);

        const price = Number(s.current_price || s.entry || 0).toFixed(4);

        const rr = Number(s.rr_ratio || 0).toFixed(2);

        const score = Number(s.strength_score || 0);

        const winProb = Number(s.win_probability || 65).toFixed(1);

        const pattern = _escHtml(s.pattern || 'Trend Setup');

        const consensus = _escHtml(s.ai_consensus || '—');



        const atr = (s.indicators && s.indicators.atr) ? Number(s.indicators.atr) : 0.002;

        const rsi = (s.indicators && s.indicators.rsi) ? Number(s.indicators.rsi) : 50;



        return `

            <div class="card" style="padding:1.25rem;border-top:3px solid ${dirColor};background:linear-gradient(180deg, rgba(255,255,255,0.02) 0%, rgba(255,255,255,0) 100%);">

                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.75rem;">

                    <div style="display:flex;align-items:center;gap:0.5rem;">

                        <span style="font-size:0.75rem;font-weight:800;color:var(--accent);background:rgba(99,102,241,0.15);padding:2px 7px;border-radius:4px;">#${idx + 1}</span>

                        <strong style="font-size:1.15rem;color:var(--text-primary);">${_escHtml(s.symbol)}</strong>

                        <span style="font-size:0.7rem;font-weight:800;padding:2px 7px;border-radius:4px;background:${dirBg};color:${dirColor};border:1px solid ${dirBdr};">${dir}</span>

                    </div>

                    <div style="text-align:right;">

                        <span style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">Score</span>

                        <span style="font-size:1.2rem;font-weight:800;color:var(--green);font-family:var(--font-mono);margin-left:4px;">${score}</span>

                        <span style="font-size:0.7rem;color:var(--text-muted);">/100</span>

                    </div>

                </div>



                <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:0.4rem;text-align:center;background:rgba(255,255,255,0.03);padding:0.6rem 0.4rem;border-radius:6px;margin-bottom:0.8rem;">

                    <div>

                        <div style="font-size:0.6rem;color:var(--text-muted);">Price</div>

                        <div style="font-size:0.82rem;font-weight:700;font-family:var(--font-mono);">${price}</div>

                    </div>

                    <div>

                        <div style="font-size:0.6rem;color:var(--text-muted);">Entry</div>

                        <div style="font-size:0.82rem;font-weight:700;font-family:var(--font-mono);">${entry}</div>

                    </div>

                    <div>

                        <div style="font-size:0.6rem;color:var(--text-muted);">Stop Loss</div>

                        <div style="font-size:0.82rem;font-weight:700;font-family:var(--font-mono);color:var(--red);">${sl}</div>

                    </div>

                    <div>

                        <div style="font-size:0.6rem;color:var(--text-muted);">Target</div>

                        <div style="font-size:0.82rem;font-weight:700;font-family:var(--font-mono);color:var(--green);">${tp1}</div>

                    </div>

                    <div>

                        <div style="font-size:0.6rem;color:var(--text-muted);">R:R</div>

                        <div style="font-size:0.82rem;font-weight:700;font-family:var(--font-mono);color:var(--teal);">${rr}x</div>

                    </div>

                </div>



                <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.72rem;color:var(--text-muted);margin-bottom:0.85rem;">

                    <span>Pattern: <strong style="color:var(--text-primary);">${pattern}</strong></span>

                    <span>AI Win Prob: <strong style="color:var(--green);">${winProb}%</strong> (${consensus})</span>

                </div>



                <div style="display:flex;justify-content:space-between;align-items:center;">

                    <span style="font-size: 0.72rem;font-weight:700;color:var(--green);background:rgba(16,185,129,0.12);padding:3px 8px;border-radius:4px;border:1px solid rgba(16,185,129,0.3);">

                        ● QUALIFIED SETUP

                    </span>

                    <button type="button" class="btn btn-primary" style="padding:0.35rem 0.9rem;font-size:0.75rem;" onclick="event.stopPropagation();takePaperTrade('${s.symbol}', '${dir}', ${entry}, ${sl}, ${tp1}, ${atr}, 'FOREX_SWING', this, {confidence: ${score}, rsi: ${rsi}})">

                        ⚡ Paper Trade

                    </button>

                </div>

            </div>

        `;

    }).join('');

}



function filterForexPairs(category) {

    _forexCurrentFilter = category;

    const btnMap = {

        'all': 'fxFilterAll',

        'qualified': 'fxFilterQualified',

        'majors': 'fxFilterMajors',

        'crosses': 'fxFilterCrosses',

        'inr': 'fxFilterInr',

    };

    Object.values(btnMap).forEach(id => {

        const el = document.getElementById(id);

        if (el) el.classList.remove('active');

    });

    const activeBtn = document.getElementById(btnMap[category]);

    if (activeBtn) activeBtn.classList.add('active');



    renderForexSignals(_forexAllSetups, _forexCurrentFilter);

}



function renderForexSignals(setups, filter = 'all') {

    const grid = document.getElementById('forexSignalGrid');

    if (!grid) return;



    if (!setups || !setups.length) {

        grid.innerHTML = '<div class="loading-state" style="padding:2rem;"><p style="color:var(--text-muted);font-size:0.9rem;">Scanning forex pairs... Data will populate shortly.</p></div>';

        return;

    }



    let filtered = setups;

    if (filter === 'qualified') {

        filtered = setups.filter(s => s.status === 'QUALIFIED');

    } else if (filter === 'majors') {

        const majors = ['EUR/USD', 'GBP/USD', 'USD/JPY', 'USD/CHF', 'AUD/USD', 'NZD/USD', 'USD/CAD'];

        filtered = setups.filter(s => majors.includes(s.symbol));

    } else if (filter === 'crosses') {

        const crosses = ['EUR/GBP', 'EUR/JPY', 'GBP/JPY', 'AUD/JPY'];

        filtered = setups.filter(s => crosses.includes(s.symbol));

    } else if (filter === 'inr') {

        filtered = setups.filter(s => s.symbol.endsWith('/INR'));

    }



    if (!filtered.length) {

        grid.innerHTML = `<div class="loading-state" style="padding:2rem;"><p style="color:var(--text-muted);">No setups matching filter "${_escHtml(filter)}".</p></div>`;

        return;

    }



    grid.innerHTML = filtered.map(s => {

        const isQualified = s.status === 'QUALIFIED';

        const dir = s.direction || 'LONG';

        const isLong = dir === 'LONG' || dir === 'BUY';

        const dirColor = isQualified ? (isLong ? 'var(--green)' : 'var(--red)') : 'var(--text-muted)';

        const dirBg = isLong ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)';

        const dirBdr = isLong ? 'rgba(16,185,129,0.35)' : 'rgba(239,68,68,0.35)';



        const price = s.current_price != null && s.current_price > 0 ? Number(s.current_price).toFixed(4) : '—';

        const entry = (s.entry || s.entry_price) != null && (s.entry || s.entry_price) > 0 ? Number(s.entry || s.entry_price).toFixed(4) : '—';

        const sl = (s.sl || s.stop_loss) != null && (s.sl || s.stop_loss) > 0 ? Number(s.sl || s.stop_loss).toFixed(4) : '—';

        const tgt = (s.tp1 || s.take_profit) != null && (s.tp1 || s.take_profit) > 0 ? Number(s.tp1 || s.take_profit).toFixed(4) : '—';

        const rr = s.rr_ratio != null && s.rr_ratio > 0 ? `${Number(s.rr_ratio).toFixed(2)}x` : '—';

        const score = Number(s.strength_score || 0);

        const winProb = Number(s.win_probability || 50).toFixed(0);

        const pattern = _escHtml(s.pattern || 'Trend Monitor');

        const indicators = s.indicators || {};

        const rsiVal = indicators.rsi != null ? Number(indicators.rsi).toFixed(0) : '—';

        const atrVal = indicators.atr != null ? Number(indicators.atr).toFixed(4) : '0.0020';



        const atr = Number(atrVal);

        const rsiNum = Number(rsiVal) || 50;



        return `

            <div class="card" style="padding:1rem;border-top:2px solid ${dirColor};background:${isQualified ? 'var(--bg-card)' : 'rgba(255,255,255,0.015)'};">

                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.75rem;">

                    <div>

                        <div style="display:flex;align-items:center;gap:0.4rem;">

                            <strong style="font-size:1.1rem;color:var(--text-primary);">${_escHtml(s.symbol)}</strong>

                            <span style="font-size: 0.72rem;font-weight:800;padding:2px 7px;border-radius:4px;background:${dirBg};color:${isLong ? 'var(--green)' : 'var(--red)'};border:1px solid ${dirBdr};">${_escHtml(dir)}</span>

                            <span style="font-size: 0.72rem;padding:2px 6px;background:rgba(255,255,255,0.06);border-radius:4px;color:var(--text-muted);">${pattern}</span>

                        </div>

                        <div style="font-size:0.7rem;color:var(--text-muted);margin-top:0.25rem;">

                            Trend: <strong style="color:${s.daily_trend === 'BULLISH' ? 'var(--green)' : 'var(--red)'};">${_escHtml(s.daily_trend || 'NEUTRAL')}</strong>

                            &bull; Overlap: ${_escHtml(s.session_overlap || 'NORMAL')}

                        </div>

                    </div>

                    <div style="text-align:right;">

                        <div style="font-size:0.6rem;color:var(--text-muted);text-transform:uppercase;">Strength</div>

                        <div style="font-size:1.15rem;font-weight:800;color:${isQualified ? 'var(--green)' : 'var(--text-muted)'};font-family:var(--font-mono);">${score}</div>

                    </div>

                </div>



                <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:0.4rem;text-align:center;background:rgba(255,255,255,0.03);padding:0.5rem 0.3rem;border-radius:6px;margin-bottom:0.75rem;">

                    <div>

                        <div style="font-size: 0.70rem;color:var(--text-muted);">Price</div>

                        <div style="font-size:0.78rem;font-weight:700;font-family:var(--font-mono);">${price}</div>

                    </div>

                    <div>

                        <div style="font-size: 0.70rem;color:var(--text-muted);">Entry</div>

                        <div style="font-size:0.78rem;font-weight:700;font-family:var(--font-mono);">${entry}</div>

                    </div>

                    <div>

                        <div style="font-size: 0.70rem;color:var(--text-muted);">SL</div>

                        <div style="font-size:0.78rem;font-weight:700;font-family:var(--font-mono);color:var(--red);">${sl}</div>

                    </div>

                    <div>

                        <div style="font-size: 0.70rem;color:var(--text-muted);">Target</div>

                        <div style="font-size:0.78rem;font-weight:700;font-family:var(--font-mono);color:var(--green);">${tgt}</div>

                    </div>

                    <div>

                        <div style="font-size: 0.70rem;color:var(--text-muted);">R:R</div>

                        <div style="font-size:0.78rem;font-weight:700;font-family:var(--font-mono);color:var(--teal);">${rr}</div>

                    </div>

                </div>



                <div style="display:flex;gap:0.4rem;align-items:center;flex-wrap:wrap;font-size: 0.72rem;color:var(--text-muted);margin-bottom:0.75rem;">

                    <span style="background:rgba(255,255,255,0.04);padding:2px 6px;border-radius:3px;">RSI: <strong style="color:var(--text-primary);">${rsiVal}</strong></span>

                    <span style="background:rgba(255,255,255,0.04);padding:2px 6px;border-radius:3px;">ATR: <strong style="color:var(--text-primary);">${atrVal}</strong></span>

                    <span style="background:rgba(255,255,255,0.04);padding:2px 6px;border-radius:3px;">AI Prob: <strong style="color:var(--text-primary);">${winProb}%</strong></span>

                    <span style="background:rgba(255,255,255,0.04);padding:2px 6px;border-radius:3px;">Consensus: <strong style="color:var(--text-primary);">${_escHtml(s.ai_consensus || '—')}</strong></span>

                </div>



                <div style="display:flex;justify-content:space-between;align-items:center;border-top:1px solid rgba(255,255,255,0.05);padding-top:0.6rem;">

                    ${isQualified ? `

                        <span style="font-size: 0.72rem;font-weight:700;color:var(--green);background:rgba(16,185,129,0.12);padding:2px 7px;border-radius:4px;border:1px solid rgba(16,185,129,0.3);">

                            QUALIFIED PAPER SETUP

                        </span>

                        <button type="button" class="btn btn-primary" style="padding:0.25rem 0.75rem;font-size:0.72rem;" onclick="event.stopPropagation();takePaperTrade('${s.symbol}', '${dir}', ${s.entry || s.entry_price}, ${s.sl || s.stop_loss}, ${s.tp1 || s.take_profit}, ${atr}, 'FOREX_SWING', this, {confidence: ${score}, rsi: ${rsiNum}})">

                            ⚡ Paper Trade

                        </button>

                    ` : `

                        <span style="font-size: 0.72rem;font-weight:600;color:var(--amber,#f59e0b);background:rgba(245,158,11,0.1);padding:2px 7px;border-radius:4px;">

                            ${_escHtml(s.skip_reason || 'SKIP — Filtered')}

                        </span>

                        <span style="font-size: 0.72rem;color:var(--text-muted);">Paper trade inactive</span>

                    `}

                </div>

            </div>

        `;

    }).join('');

}



// Window globals
window.filterForexPairs = filterForexPairs;
window.renderTopForexSetups = renderTopForexSetups;
window.renderCurrencyStrength = renderCurrencyStrength;
window.renderForexSignals = renderForexSignals;
window.loadForexData = loadForexData;
window.computeClientForexSessions = computeClientForexSessions;
window.renderForexSessions = renderForexSessions;
window.renderInrStrip = renderInrStrip;
window.resetForexRetry = resetForexRetry;
window.getForexRetryCount = getForexRetryCount;







// ── NSE Prediction Handlers ───────────────────────────────────────



async function loadNsePredictionTab() {



    try {



        const resp = await fetchWithTimeout(`${API}/api/nse/prediction`);



        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);



        const data = await resp.json();



        



        const pred = data.prediction || {};



        const dir = pred.direction || 'NEUTRAL';



        const conf = pred.confidence || 0;



        const acc = data.accuracy_tracker || {};



        const sigs = data.signals || {};



        



        const dirEl = document.getElementById('nseTabDirection');



        if (dirEl) {



            dirEl.textContent = dir;



            dirEl.style.color = dir.includes('BULL') ? 'var(--green)' : dir.includes('BEAR') ? 'var(--red)' : 'var(--text-muted)';



        }



        const confEl = document.getElementById('nseTabConfidence');



        if (confEl) confEl.textContent = `${conf}%`;



        



        const hitEl = document.getElementById('nseTabHitRate');



        if (hitEl) hitEl.textContent = acc.hit_rate_pct != null ? `${acc.hit_rate_pct}%` : '—';



        



        const resEl = document.getElementById('nseTabResolved');



        if (resEl) resEl.textContent = acc.resolved || '0';







        const reasonsEl = document.getElementById('nseTabReasons');



        if (reasonsEl && pred.reasons) {



            reasonsEl.innerHTML = pred.reasons.map(r => `<li>${_escHtml(r)}</li>`).join('');



        }







        const topBuyEl = document.getElementById('nseTabTopBuy');



        if (topBuyEl) {



            const buys = sigs.top_buy || [];



            topBuyEl.innerHTML = buys.length ? buys.map(b => `<div style="padding:0.25rem 0;font-size:0.75rem;"><strong>${_escHtml(b.symbol)}</strong>: ₹${b.price} (conf ${b.confidence}%)</div>`).join('') : '<span style="font-size:0.75rem;color:var(--text-muted);">None in current scan</span>';



        }







        const topSellEl = document.getElementById('nseTabTopSell');



        if (topSellEl) {



            const sells = sigs.top_sell || [];



            topSellEl.innerHTML = sells.length ? sells.map(s => `<div style="padding:0.25rem 0;font-size:0.75rem;"><strong>${_escHtml(s.symbol)}</strong>: ₹${s.price} (conf ${s.confidence}%)</div>`).join('') : '<span style="font-size:0.75rem;color:var(--text-muted);">None in current scan</span>';



        }







        // ── Render Latest Indian Market Headlines in NSE Tab ───



        const newsGrid = document.getElementById('nseTabNews');



        if (newsGrid) {



            const newsItems = (data.news && data.news.items) ? data.news.items : [];



            if (newsItems.length) {



                newsGrid.innerHTML = newsItems.slice(0, 6).map(item => {



                    const titleSafe = _escHtml(item.title || '');



                    const srcSafe = _escHtml(item.source || 'NSE Feed');



                    const timeSafe = _escHtml(item.published || 'Today');



                    return `



                        <div class="nse-news-card" style="background:var(--bg-card);border:1px solid var(--border-color);border-radius:8px;padding:0.75rem 0.9rem;display:flex;flex-direction:column;justify-content:space-between;gap:0.4rem;">



                            <a href="${item.url || '#'}" target="_blank" rel="noopener noreferrer" style="color:var(--text-primary);text-decoration:none;font-size:0.78rem;font-weight:600;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;" title="${titleSafe}">



                                ${titleSafe}



                            </a>



                            <div style="display:flex;justify-content:space-between;align-items:center;font-size: 0.72rem;color:var(--text-muted);margin-top:2px;">



                                <span style="color:var(--cyan);font-weight:600;">📰 ${srcSafe}</span>



                                <span>${timeSafe}</span>



                            </div>



                        </div>



                    `;



                }).join('');



            } else {



                newsGrid.innerHTML = '<div style="color:var(--text-muted);font-size:0.75rem;padding:0.5rem;">No breaking headlines at this moment.</div>';



            }



        }



    } catch (err) {



        console.error('[loadNsePredictionTab] error:', err);



    }



}







// ── Swing Scanner Controls & Search ───────────────────────────────



function runSwingScanNow() {



    showToast('Recomputing full universe swing scan...', 'info');



    loadSwingScanner(true);



}







async function loadSwingOutcomeStats() {



    const panel = document.getElementById('swingOutcomeStatsPanel');



    if (!panel) return;



    if (panel.style.display !== 'none' && panel.innerHTML) {



        panel.style.display = 'none';



        return;



    }



    panel.style.display = 'block';



    panel.innerHTML = '<span class="cockpit-skeleton">Loading SQLite outcome stats...</span>';



    try {



        const resp = await fetchWithTimeout(`${API}/api/swing/stats`);



        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);



        const data = await resp.json();



        panel.innerHTML = `



            <strong>📊 Swing Outcome Stats:</strong> Total Recorded: <strong>${data.total_setups || 0}</strong> · 



            Filled: <strong>${data.filled || 0}</strong> · 



            Wins: <strong style="color:var(--green)">${data.wins || 0}</strong> · 



            Losses: <strong style="color:var(--red)">${data.losses || 0}</strong> · 



            Win Rate: <strong style="color:var(--teal)">${data.win_rate != null ? Number(data.win_rate).toFixed(1) + '%' : '—'}</strong> · 



            Profit Factor: <strong>${data.profit_factor != null ? Number(data.profit_factor).toFixed(2) : '—'}</strong>



        `;



    } catch (err) {



        panel.innerHTML = `<span style="color:var(--red)">Failed to load outcome stats (${_escHtml(err.message)})</span>`;



    }



}







async function lookupSwingSymbol() {



    const input = document.getElementById('swingSymbolSearch');



    const clearBtn = document.getElementById('swingSymbolClearBtn');



    const grid = document.getElementById('swingGrid');



    if (!input || !grid) return;



    const sym = input.value.trim().toUpperCase();



    if (!sym) {



        showToast('Please enter an NSE symbol (e.g. RELIANCE, TCS)', 'info');



        return;



    }



    if (clearBtn) clearBtn.style.display = 'inline-block';



    grid.innerHTML = `<div class="loading-state"><div class="loader"></div><p>Searching swing setups for ${sym}...</p></div>`;



    try {



        const resp = await fetchWithTimeout(`${API}/api/swing/symbol/${encodeURIComponent(sym)}`);



        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);



        const data = await resp.json();



        const setups = data.setups || [];



        if (!setups.length) {



            grid.innerHTML = `<div class="loading-state" style="padding:2rem;"><p style="color:var(--text-muted);font-size:0.9rem;">No qualified swing setups for <strong>${_escHtml(sym)}</strong>. (Reason: ${_escHtml(data.message || 'Filtered by regime or R:R')})</p></div>`;



            return;



        }



        renderSwingSetups(setups);



    } catch (err) {



        grid.innerHTML = `<div class="loading-state"><p style="color:var(--red)">Symbol lookup failed (${_escHtml(err.message)})</p></div>`;



    }



}







function clearSwingSymbolSearch() {



    const input = document.getElementById('swingSymbolSearch');



    const clearBtn = document.getElementById('swingSymbolClearBtn');



    const suggest = document.getElementById('swingSearchSuggest');



    if (input) input.value = '';



    if (clearBtn) clearBtn.style.display = 'none';



    if (suggest) suggest.style.display = 'none';



    loadSwingScanner();



}







function clearAllSwingBookmarks() {



    try {



        localStorage.removeItem('tradeai_swing_pins');



        const count = document.getElementById('swingPinnedCount');



        const panel = document.getElementById('swingPinnedPanel');



        if (count) count.textContent = '0';



        if (panel) panel.style.display = 'none';



        showToast('Cleared all pinned swing symbols', 'info');



    } catch (e) {



        console.warn('Error clearing bookmarks:', e);



    }



}







async function paperResolveEOD() {



    showToast('Resolving open paper swing trades against EOD bars...', 'info');



    try {



        const resp = await fetchWithTimeout(`${API}/api/swing/resolve-eod`, { method: 'POST' });



        if (resp.ok) {



            showToast('EOD Paper trades resolved successfully', 'success');



            loadPortfolio();



        } else {



            showToast('EOD resolution processed', 'info');



        }



    } catch (e) {



        showToast('EOD resolution triggered', 'info');



    }



}











function applyAiBadgeState(ai) {
    if (!ai) return;
    const badgeHeader = document.getElementById('aiEngineStatusBadge');
    if (badgeHeader) {
        badgeHeader.textContent = ai.badge_short || (ai.lightweight_mode ? '⚡ Lightweight Mode' : (ai.fitted_count === 4 
            ? 'Models: 4/4 Active' 
            : (ai.fitted_count > 0 ? `Hybrid (${ai.fitted_count}/4 Models)` : 'Rule-Based Engine')));
        badgeHeader.title = ai.badge_text || 'AI Engine Status';
        badgeHeader.dataset.hydrated = 'true';
        if (ai.lightweight_mode) {
            badgeHeader.style.background = 'rgba(56,189,248,0.15)';
            badgeHeader.style.color = '#38bdf8';
            badgeHeader.style.borderColor = 'rgba(56,189,248,0.3)';
        } else if (ai.fitted_count >= 3) {
            badgeHeader.style.background = 'rgba(16,185,129,0.15)';
            badgeHeader.style.color = '#10b981';
            badgeHeader.style.borderColor = 'rgba(16,185,129,0.3)';
        } else if (ai.fitted_count > 0) {
            badgeHeader.style.background = 'rgba(245,158,11,0.12)';
            badgeHeader.style.color = 'var(--amber, #f59e0b)';
            badgeHeader.style.borderColor = 'rgba(245,158,11,0.3)';
        } else {
            badgeHeader.style.background = 'rgba(239,68,68,0.12)';
            badgeHeader.style.color = 'var(--red, #ef4444)';
            badgeHeader.style.borderColor = 'rgba(239,68,68,0.3)';
        }
    }

    const badgeCockpit = document.getElementById('aiModelBadgeCockpit');
    if (badgeCockpit) {
        badgeCockpit.textContent = ai.badge_text || `AI Engine: ${ai.mode}`;
        if (ai.lightweight_mode) {
            badgeCockpit.style.background = 'rgba(56,189,248,0.15)';
            badgeCockpit.style.color = '#38bdf8';
            badgeCockpit.style.borderColor = 'rgba(56,189,248,0.3)';
        } else if (ai.fitted_count >= 3) {
            badgeCockpit.style.background = 'rgba(16,185,129,0.15)';
            badgeCockpit.style.color = '#10b981';
            badgeCockpit.style.borderColor = 'rgba(16,185,129,0.3)';
        } else if (ai.fitted_count > 0) {
            badgeCockpit.style.background = 'rgba(245,158,11,0.12)';
            badgeCockpit.style.color = 'var(--amber, #f59e0b)';
            badgeCockpit.style.borderColor = 'rgba(245,158,11,0.3)';
        } else {
            badgeCockpit.style.background = 'rgba(239,68,68,0.12)';
            badgeCockpit.style.color = 'var(--red, #ef4444)';
            badgeCockpit.style.borderColor = 'rgba(239,68,68,0.3)';
        }
    }
}

async function updateAiModelHealthBadge() {
    try {
        const res = await fetchWithTimeout(`${API}/api/health`, {}, 15000);



        if (!res.ok) return;



        const data = await res.json();



        const ai = data.ai_engine;
        if (!ai) return;
        applyAiBadgeState(ai);
        try { localStorage.setItem('tradeai_ai_engine_v2', JSON.stringify(ai)); } catch (_) {}







        // Update V2 Multi-Provider Pipeline Telemetry



        const pipelineText = document.getElementById('v2PipelineText');



        const pipelineDot = document.getElementById('v2PipelineDot');



        if (pipelineText && data.providers) {



            const yf = data.providers.yahoo || {};



            const lat = yf.avg_latency_ms ? `${Math.round(yf.avg_latency_ms)}ms` : 'fast';



            const healthy = yf.healthy !== false;



            pipelineText.textContent = `V2 (${lat})`;



            if (pipelineDot) {



                pipelineDot.style.background = healthy ? '#10b981' : '#f59e0b';



            }



        }







        // Update Sidebar System Health & Data Age (Health v2)
        const titleEl = document.getElementById('sidebarStatusTitle');
        const subEl = document.getElementById('sidebarStatusSub');
        const dotEl = document.getElementById('sidebarStatusDot');

        if (titleEl || subEl || dotEl) {
            const status = String(data.status || 'HEALTHY').toUpperCase();
            const dataAgeHuman = data.data_age_human;
            const tier = data.health_tier || {};

            if (titleEl) {
                if (status === 'HEALTHY' || status === 'OK') {
                    titleEl.textContent = 'System Healthy';
                    titleEl.style.color = '#10b981';
                } else if (status === 'DEGRADED' || tier.freshness === 'EOD_UNVERIFIED') {
                    if (tier.infra === 'UNHEALTHY' || tier.infra === 'DOWN') {
                        titleEl.textContent = 'System Degraded';
                        titleEl.style.color = '#ef4444';
                    } else {
                        titleEl.textContent = 'Freshness Unverified';
                        titleEl.style.color = '#f59e0b';
                    }
                } else if (status === 'STALE_DATA') {
                    titleEl.textContent = 'Data Stale';
                    titleEl.style.color = '#f59e0b';
                } else if (status === 'WORKER_DOWN') {
                    titleEl.textContent = 'Worker Lagging';
                    titleEl.style.color = '#f59e0b';
                } else {
                    titleEl.textContent = 'System Degraded';
                    titleEl.style.color = '#ef4444';
                }
            }

            if (dotEl) {
                if (status === 'HEALTHY' || status === 'OK') {
                    dotEl.style.background = '#10b981';
                } else if (status === 'DEGRADED' || tier.freshness === 'EOD_UNVERIFIED') {
                    if (tier.infra === 'UNHEALTHY' || tier.infra === 'DOWN') {
                        dotEl.style.background = '#ef4444';
                    } else {
                        dotEl.style.background = '#f59e0b';
                    }
                } else if (status === 'STALE_DATA' || status === 'WORKER_DOWN') {
                    dotEl.style.background = '#f59e0b';
                } else {
                    dotEl.style.background = '#ef4444';
                }
            }

            if (subEl) {
                if (dataAgeHuman) {
                    subEl.textContent = dataAgeHuman;
                } else if (data.feed && !data.feed.is_market_open) {
                    subEl.textContent = 'EOD Snapshot · Market Closed';
                } else {
                    subEl.textContent = 'Data: 15m Delayed';
                }
                const tooltipParts = [];
                if (tier.infra) tooltipParts.push(`Infra: ${tier.infra}`);
                if (tier.worker) tooltipParts.push(`Worker: ${tier.worker}`);
                if (tier.freshness) tooltipParts.push(`Feed: ${tier.freshness}`);
                if (data.overall_reason) tooltipParts.push(`(${data.overall_reason})`);
                subEl.title = tooltipParts.join(' | ') || 'System Health Telemetry';
            }
        }

    } catch (_) { /* ignore */ }



}







// ══════════════════════════════════════════════════════════════════════════════

// Self-Learning Capital & Autonomous Controls Modal

// ══════════════════════════════════════════════════════════════════════════════



window.openSelfLearningModal = function() {

    const modal = document.getElementById('selfLearningModalBackdrop');

    if (!modal) return;

    modal.style.display = 'flex';

    fetchWithTimeout(`${API}/api/stats`, {}, 5000).then(r => r.json()).then(data => {

        if (!data) return;

        const fmt = (v) => `₹${Number(v || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

        const eqEl = document.getElementById('slModalEquity');

        if (eqEl) eqEl.textContent = fmt(data.capital);

        const mtmEl = document.getElementById('slModalMtm');

        if (mtmEl) {

            const u = Number(data.unrealized_pnl || 0);

            mtmEl.textContent = `${u >= 0 ? '+' : ''}${fmt(u)}`;

            mtmEl.style.color = u >= 0 ? '#3fb950' : '#f85149';

        }

        const rEl = document.getElementById('slModalRealized');

        if (rEl) {

            const p = Number(data.total_pnl || 0);

            rEl.textContent = `${p >= 0 ? '+' : ''}${fmt(p)}`;

            rEl.style.color = p >= 0 ? '#3fb950' : '#f85149';

        }

        const pEl = document.getElementById('slModalPositions');

        if (pEl) pEl.textContent = `${data.open_trades || 0} Active`;

        const inp = document.getElementById('inputLearningCapital');

        if (inp && data.initial_capital) inp.value = data.initial_capital;

    }).catch(() => {});

};



window.closeSelfLearningModal = function() {

    const modal = document.getElementById('selfLearningModalBackdrop');

    if (modal) modal.style.display = 'none';

};



window.applyNewCapital = async function() {
    const inp = document.getElementById('inputLearningCapital');
    const fb = document.getElementById('slModalFeedback');
    const cap = parseFloat(inp ? inp.value : 100000);
    if (isNaN(cap) || cap < 1000) {
        if (fb) fb.textContent = 'Please enter a valid amount (min ₹1,000)';
        return;
    }
    try {
        if (fb) fb.textContent = 'Updating capital...';
        const res = await fetchWithTimeout(`${API}/api/paper/capital`, {
            method: 'POST',
            headers: _getAuthHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ capital: cap, clear_trades: false })
        }, 15000);

        const text = await res.text();
        let data = null;
        try {
            data = JSON.parse(text);
        } catch (_) {
            throw new Error(`Server returned HTTP ${res.status} (Backend unreachable or starting up)`);
        }

        if (res.ok && data.status === 'ok') {
            if (fb) fb.textContent = data.message || 'Capital updated';
            loadOverviewFast();
            setTimeout(closeSelfLearningModal, 1200);
        } else {
            if (fb) fb.textContent = data?.error || data?.detail || 'Failed to update';
        }
    } catch (e) {
        if (fb) fb.textContent = 'Network error: ' + e.message;
    }
};



window.resetLearningAccount = async function() {
    if (!confirm('Are you sure you want to reset the Self-Learning paper account? This will clear open/closed trades and reset capital.')) return;
    const inp = document.getElementById('inputLearningCapital');
    const fb = document.getElementById('slModalFeedback');
    const cap = parseFloat(inp ? inp.value : 100000);
    try {
        if (fb) fb.textContent = 'Resetting paper account...';
        const res = await fetchWithTimeout(`${API}/api/paper/reset?capital=${cap}`, {
            method: 'POST',
            headers: _getAuthHeaders(),
        }, 15000);

        const text = await res.text();
        let data = null;
        try {
            data = JSON.parse(text);
        } catch (_) {
            throw new Error(`Server returned HTTP ${res.status} (Backend unreachable or starting up)`);
        }

        if (res.ok && data.status === 'ok') {
            if (fb) fb.textContent = data.message || 'Account reset';
            loadOverviewFast();
            setTimeout(closeSelfLearningModal, 1200);
        } else {
            if (fb) fb.textContent = data?.error || data?.detail || 'Reset failed';
        }
    } catch (e) {
        if (fb) fb.textContent = 'Reset failed: ' + e.message;
    }
};



window.triggerLearningCycleNow = async function() {
    const fb = document.getElementById('slModalFeedback');
    try {
        if (fb) fb.textContent = 'Triggering scan and autonomous trade evaluation...';
        const res = await fetchWithTimeout(`${API}/api/autonomous/review-now`, {
            method: 'POST',
            headers: _getAuthHeaders({ 'Content-Type': 'application/json' }),
        }, 30000);

        const text = await res.text();
        let data = null;
        try {
            data = JSON.parse(text);
        } catch (_) {
            if (!res.ok) {
                throw new Error(`Server returned HTTP ${res.status} (Backend is starting up or unreachable)`);
            }
            throw new Error(`Invalid response from server (HTTP ${res.status})`);
        }

        if (!res.ok) {
            throw new Error(data?.detail || data?.message || `HTTP ${res.status}`);
        }

        if (data && data.ok) {
            const review = data.review || {};
            const count = review.today_trade_count ?? 0;
            const wr = review.today_win_rate != null ? `${review.today_win_rate}%` : '';
            if (fb) fb.textContent = `⚡ Review completed! (${count} trades evaluated${wr ? ', Win Rate: ' + wr : ''})`;
            loadOverviewFast();
            setTimeout(closeSelfLearningModal, 1800);
        } else {
            if (fb) fb.textContent = data?.message || 'Review completed.';
        }
    } catch (e) {
        if (fb) fb.textContent = 'Trigger failed: ' + e.message;
    }
};
async function refreshPaperPositions() {
    try {
        const res = await fetchWithTimeout(`${API}/api/paper/open`, {}, 8000);
        if (res && res.ok) {
            const data = await parseJsonResponse(res);
            _lastOpenTrades = data.open_trades || [];
            renderOverviewActiveTrades(_lastOpenTrades);
            if (_lastStatsData) {
                updateStatsStrip(_lastStatsData);
            }
        }
    } catch (_) {}
}


// ── Global Macro & Crude Intelligence ──────────────────────────
async function loadMacroIntelligence(force = false) {
    const now = Date.now();
    if (!force && _lastMacroData && (now - _lastMacroFetchTs < 300000)) {
        renderMacroIntelligence(_lastMacroData);
        return;
    }
    try {
        const res = await fetchWithTimeout(`${API}/api/macro/intelligence${force ? '?force_refresh=true' : ''}`, {}, 15000);
        if (!res.ok) return;
        const data = await parseJsonResponse(res);
        if (data.status === 'ok' && data.macro) {
            _lastMacroData = data.macro;
            _lastMacroFetchTs = now;
            renderMacroIntelligence(_lastMacroData);
        }
    } catch (err) {
        console.warn('Macro intelligence load failed:', err);
    }
}

function renderMacroIntelligence(macro) {
    if (!macro) return;
    window._lastMacroData = macro;
    const brent = macro.brent_crude;
    const wti = macro.wti_crude;
    const y10 = macro.us_10y_yield;
    const dxy = macro.dxy_dollar_index;
    const sp = macro.sp500;
    const gold = macro.gold;

    const fmtPct = (p) => `${p >= 0 ? '+' : ''}${Number(p || 0).toFixed(2)}%`;
    const colorPct = (el, p, inverse = false) => {
        if (!el) return;
        const isBull = inverse ? p < 0 : p > 0;
        const isBear = inverse ? p > 0 : p < 0;
        el.style.color = isBull ? 'var(--green, #10b981)' : (isBear ? 'var(--red, #ef4444)' : 'var(--text-muted, #8b949e)');
    };

    // Brent Crude
    const bPrice = document.getElementById('macroBrentPrice');
    const bSub = document.getElementById('macroBrentSub');
    if (bPrice && brent) {
        bPrice.textContent = `$${Number(brent.price).toFixed(2)}`;
        if (bSub) {
            bSub.textContent = `${fmtPct(brent.change_pct)} • $/bbl`;
            colorPct(bSub, brent.change_pct, false);
        }
    }

    // WTI Crude
    const wPrice = document.getElementById('macroWtiPrice');
    const wSub = document.getElementById('macroWtiSub');
    if (wPrice && wti) {
        wPrice.textContent = `$${Number(wti.price).toFixed(2)}`;
        if (wSub) {
            wSub.textContent = `${fmtPct(wti.change_pct)} • $/bbl`;
            colorPct(wSub, wti.change_pct, false);
        }
    }

    // US 10Y Yield
    const yPrice = document.getElementById('macroYieldPrice');
    const ySub = document.getElementById('macroYieldSub');
    if (yPrice && y10) {
        yPrice.textContent = `${Number(y10.price).toFixed(2)}%`;
        if (ySub) {
            ySub.textContent = `${fmtPct(y10.change_pct)} • Treasuries`;
            colorPct(ySub, y10.change_pct, false);
        }
    }

    // Dollar Index DXY
    const dPrice = document.getElementById('macroDxyPrice');
    const dSub = document.getElementById('macroDxySub');
    if (dPrice && dxy) {
        dPrice.textContent = Number(dxy.price).toFixed(2);
        if (dSub) {
            dSub.textContent = `${fmtPct(dxy.change_pct)} • DXY`;
            colorPct(dSub, dxy.change_pct, false);
        }
    }

    // S&P 500
    const sPrice = document.getElementById('macroSp500Price');
    const sSub = document.getElementById('macroSp500Sub');
    if (sPrice && sp) {
        sPrice.textContent = Number(sp.price).toLocaleString('en-US', { maximumFractionDigits: 0 });
        if (sSub) {
            sSub.textContent = `${fmtPct(sp.change_pct)} • Wall St`;
            colorPct(sSub, sp.change_pct, false);
        }
    }

    // Gold
    const gPrice = document.getElementById('macroGoldPrice');
    const gSub = document.getElementById('macroGoldSub');
    if (gPrice && gold) {
        gPrice.textContent = `$${Number(gold.price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
        if (gSub) {
            gSub.textContent = `${fmtPct(gold.change_pct)} • $/oz`;
            colorPct(gSub, gold.change_pct, false);
        }
    }

    // Cue Pill
    const cuePill = document.getElementById('macroCuePill');
    if (cuePill) {
        const regime = macro.macro_regime || 'NEUTRAL';
        cuePill.textContent = regime.replace(/_/g, ' ');
        if (regime === 'RISK_ON' || regime === 'LOW_CRUDE_BENEFIT') {
            cuePill.className = 'macro-cue-pill cue-bullish';
        } else if (regime === 'HIGH_CRUDE_RISK' || regime === 'RISK_OFF') {
            cuePill.className = 'macro-cue-pill cue-bearish';
        } else {
            cuePill.className = 'macro-cue-pill';
        }
    }

    // Macro Summary Text with Clickable Sector Stock Chips
    const sumEl = document.getElementById('macroSummaryText');
    if (sumEl && macro.macro_cue_summary) {
        let text = (macro.macro_cue_summary || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        const symbolMap = {
            'Asian Paints': 'ASIANPAINT',
            'Berger': 'BERGEPAINT',
            'IndiGo': 'INDIGO',
            'BPCL': 'BPCL',
            'IOC': 'IOC',
            'HPCL': 'HPCL',
            'ONGC': 'ONGC',
            'Oil India': 'OIL',
            'Reliance': 'RELIANCE'
        };
        for (const [name, sym] of Object.entries(symbolMap)) {
            const regex = new RegExp(`\\b${name}\\b`, 'gi');
            text = text.replace(regex, `<span class="macro-stock-chip" onclick="fetchAndShowQuote('${sym}')" title="Inspect ${sym} on NSE">${name}</span>`);
        }
        sumEl.innerHTML = text;
    }

    // Breaking News Stream with clickable verified links
    const truncateWords = (str, maxLen) => {
        if (!str || str.length <= maxLen) return str;
        const sub = str.substring(0, maxLen);
        const lastSpace = sub.lastIndexOf(' ');
        return (lastSpace > 35 ? sub.substring(0, lastSpace) : sub) + '...';
    };

    const newsEl = document.getElementById('macroNewsStream');
    if (newsEl && Array.isArray(macro.breaking_news) && macro.breaking_news.length > 0) {
        newsEl.innerHTML = macro.breaking_news.slice(0, 4).map(n => {
            const cat = n.category || 'NEWS';
            const tagClass = `tag-${cat.toLowerCase()}`;
            const sentClass = n.sentiment === 'BULLISH' ? 'text-profit' : (n.sentiment === 'BEARISH' ? 'text-loss' : '');
            const titleSafe = (n.title || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const shortTitle = truncateWords(titleSafe, 72);
            const sourceSafe = (n.source || 'News').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const link = n.link || `https://news.google.com/search?q=${encodeURIComponent(n.title || cat)}`;
            return `
                <a href="${link}" target="_blank" rel="noopener noreferrer" class="macro-news-pill macro-news-link" title="${titleSafe} — Source: ${sourceSafe}">
                    <span class="macro-news-tag ${tagClass}">${cat.replace(/_/g, ' ')}</span>
                    <span class="${sentClass}">${shortTitle}</span>
                    <span class="macro-news-src">${sourceSafe} ↗</span>
                </a>
            `;
        }).join('');
    }
}

// (Enhanced openEdgeResearchDrawer and closeEdgeResearchDrawer moved to Edge Engine 3.0 section above)


// ── Section 6A: Market Control & Protected Crypto / Forex Switch ─────────────
let _marketControlState = { crypto_forex_enabled: false };

async function fetchMarketControlStatus() {
    try {
        const res = await fetchWithTimeout(`${API}/api/market-control/status`, {}, 12000);
        if (res.ok) {
            const data = await res.json();
            _marketControlState = data;
            renderMarketControlUI(data);
        }
    } catch (e) {
        console.debug("Market control status check skipped:", e);
    }
}

function renderMarketControlUI(data) {
    const btn = document.getElementById("cryptoForexToggleBtn");
    const badge = document.getElementById("marketControlModeBadge");
    if (!btn || !badge) return;

    const isEnabled = Boolean(data.crypto_forex_enabled);
    if (isEnabled) {
        btn.textContent = "ON";
        btn.style.background = "#059669";
        btn.style.borderColor = "#10b981";
        btn.style.color = "#ffffff";
        badge.textContent = "Multi-Asset";
        badge.style.background = "rgba(16, 185, 129, 0.2)";
        badge.style.color = "#34d399";
    } else {
        btn.textContent = "OFF";
        btn.style.background = "#1e293b";
        btn.style.borderColor = "#475569";
        btn.style.color = "#94a3b8";
        badge.textContent = "NSE Focus";
        badge.style.background = "rgba(59, 130, 246, 0.2)";
        badge.style.color = "#60a5fa";
    }
}

function handleCryptoForexToggleClick() {
    const isCurrentlyOn = Boolean(_marketControlState.crypto_forex_enabled);
    if (isCurrentlyOn) {
        if (confirm("Switch back to strict NSE Focus Mode? This disables all Crypto and Forex background feeds and tasks.")) {
            executeMarketControlToggle(false, null);
        }
    } else {
        const modal = document.getElementById("marketControlPasswordModal");
        const err = document.getElementById("marketControlErrorMsg");
        const inp = document.getElementById("marketControlPasswordInput");
        if (err) { err.style.display = "none"; err.textContent = ""; }
        if (inp) { inp.value = ""; }
        if (modal) { modal.style.display = "flex"; inp && inp.focus(); }
    }
}

function closeMarketControlModal() {
    const modal = document.getElementById("marketControlPasswordModal");
    if (modal) modal.style.display = "none";
}

async function submitMarketControlPassword() {
    const inp = document.getElementById("marketControlPasswordInput");
    const err = document.getElementById("marketControlErrorMsg");
    const submitBtn = document.getElementById("marketControlSubmitBtn");
    const pwd = inp ? inp.value.trim() : "";

    if (!pwd) {
        if (err) { err.textContent = "Please enter operator password."; err.style.display = "block"; }
        return;
    }

    if (submitBtn) { submitBtn.textContent = "Verifying..."; submitBtn.disabled = true; }
    if (err) { err.style.display = "none"; }

    try {
        const res = await fetch(`${API}/api/market-control/toggle`, {
            method: "POST",
            headers: { "Content-Type": "application/json", ..._getAuthHeaders() },
            body: JSON.stringify({ enabled: true, password: pwd, reason: "Operator activated Crypto/Forex override" }),
        });

        let data = {};
        const ct = res.headers.get("content-type") || "";
        if (ct.includes("application/json")) {
            try {
                data = await res.json();
            } catch (jsonErr) {
                data = { success: false, message: "Response parse error." };
            }
        } else {
            const rawText = await res.text();
            if (res.status === 503 || rawText.includes("hibernate-wake-error")) {
                data = { success: false, message: "Backend is warming up from sleep. Please wait 10 seconds and retry." };
            } else if (res.status === 429 || rawText.includes("Just a moment")) {
                data = { success: false, message: "Security rate limit active. Please wait a moment and retry." };
            } else {
                data = { success: false, message: `Server error (HTTP ${res.status}). Please try again.` };
            }
        }

        if (res.ok && data.success) {
            closeMarketControlModal();
            _marketControlState = data.state;
            renderMarketControlUI(data.state);
            if (typeof showToast === "function") showToast("Crypto & Forex processing successfully enabled.", "success");
        } else {
            if (err) {
                err.textContent = data.message || "Authorization failed. Check password.";
                err.style.display = "block";
            }
        }
    } catch (e) {
        if (err) {
            err.textContent = "Network error during verification: " + e.message;
            err.style.display = "block";
        }
    } finally {
        if (submitBtn) { submitBtn.textContent = "Authorize & Enable"; submitBtn.disabled = false; }
    }
}

async function executeMarketControlToggle(enabled, password) {
    const btn = document.getElementById("cryptoForexToggleBtn");
    if (btn) { btn.textContent = "..."; btn.disabled = true; }

    try {
        const res = await fetch(`${API}/api/market-control/toggle`, {
            method: "POST",
            headers: { "Content-Type": "application/json", ..._getAuthHeaders() },
            body: JSON.stringify({ enabled: enabled, password: password, reason: enabled ? "Operator ON" : "Operator OFF" }),
        });

        let data = {};
        const ct = res.headers.get("content-type") || "";
        if (ct.includes("application/json")) {
            try {
                data = await res.json();
            } catch (jsonErr) {
                data = { success: false, message: "Response parse error." };
            }
        } else {
            const rawText = await res.text();
            if (res.status === 503 || rawText.includes("hibernate-wake-error")) {
                data = { success: false, message: "Backend is warming up from sleep. Please wait 10 seconds and retry." };
            } else if (res.status === 429 || rawText.includes("Just a moment")) {
                data = { success: false, message: "Security rate limit active. Please wait a moment and retry." };
            } else {
                data = { success: false, message: `Server error (HTTP ${res.status}).` };
            }
        }

        if (res.ok && data.success) {
            _marketControlState = data.state;
            renderMarketControlUI(data.state);
            if (typeof showToast === "function") showToast(data.message, "info");
        } else {
            alert(data.message || "Failed to change market control state.");
            renderMarketControlUI(_marketControlState);
        }
    } catch (e) {
        alert("Error updating market control state: " + e.message);
        renderMarketControlUI(_marketControlState);
    } finally {
        if (btn) btn.disabled = false;
    }
}


// ── Next Trading Day Picks Research Tab (Section 6B) ───────────────
let _currentNdpReport = null;

async function loadNextDayPicks(reportId = null) {
    const sessionPill = document.getElementById('ndpSessionText');
    const screenedCountEl = document.getElementById('ndpScreenedCount');
    const qualifiedCountEl = document.getElementById('ndpQualifiedCount');
    const generatedAtEl = document.getElementById('ndpGeneratedAt');
    const reportIdEl = document.getElementById('ndpReportId');
    const grid = document.getElementById('ndpPicksGrid');
    const emptyCard = document.getElementById('ndpEmptyStateCard');
    const outcomeBody = document.getElementById('ndpOutcomeTableBody');

    if (sessionPill) sessionPill.textContent = 'Loading Research...';
    if (grid) grid.innerHTML = '<div style="grid-column: 1/-1; padding: 2rem; text-align: center; color: var(--text-muted);">Fetching post-close research report...</div>';

    try {
        const url = reportId ? `${API}/api/research/next-day-picks/${encodeURIComponent(reportId)}` : `${API}/api/research/next-day-picks`;
        const resp = await fetchWithTimeout(url, { headers: { 'Accept': 'application/json' } });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const report = await resp.json();
        _currentNdpReport = report;

        // Render Session & Header KPI
        const targetSession = report.target_session || {};
        if (sessionPill) {
            const dateStr = targetSession.date || report.target_session_label || report.target_session_date || 'T+1 Session';
            const hoursStr = targetSession.hours || '09:15 - 15:30 IST';
            sessionPill.textContent = `Target: ${dateStr} (${hoursStr})`;
        }
        if (screenedCountEl) {
            screenedCountEl.textContent = `${report.universe_screened || report.screened_count || '20'} Tier-1`;
        }
        const screenedDetailEl = document.getElementById('ndpScreenedDetail');
        if (screenedDetailEl) {
            screenedDetailEl.textContent = 'Exchange EOD Data (Real NSE)';
            screenedDetailEl.style.color = '#10b981';
        }
        if (qualifiedCountEl) {
            const qCount = report.qualified_count || (report.picks ? report.picks.length : 0);
            qualifiedCountEl.textContent = `${qCount} Setups`;
            qualifiedCountEl.style.color = qCount > 0 ? '#10b981' : '#f59e0b';
        }
        if (generatedAtEl) {
            const rawGen = report.generated_at_ist || report.generated_at;
            let genTime = '--';
            if (rawGen) {
                const istMatch = String(rawGen).match(/(\d{2}:\d{2}(?::\d{2})?)\s*(IST)?/);
                if (istMatch) {
                    genTime = istMatch[1] + (istMatch[2] ? ' ' + istMatch[2] : ' IST');
                } else {
                    const d = new Date(rawGen);
                    genTime = !isNaN(d.getTime()) ? d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }) : String(rawGen);
                }
            }
            generatedAtEl.textContent = genTime;
        }
        if (reportIdEl) {
            reportIdEl.textContent = `ID: ${report.report_id || '--'}`;
        }

        // Render Picks or Empty State
        const picks = report.picks || [];
        if (picks.length === 0 || report.is_empty) {
            if (grid) grid.style.display = 'none';
            if (emptyCard) {
                emptyCard.style.display = 'block';
                const emptyTitle = document.getElementById('ndpEmptyTitle');
                const emptyDesc = document.getElementById('ndpEmptyDesc');
                if (emptyTitle) emptyTitle.textContent = 'No Qualified Setups Passed Risk/Reward Gates';
                if (emptyDesc) emptyDesc.textContent = report.empty_reason || 'All screened Tier-1 equities failed the minimum 1.5x Risk-Reward threshold or lacked confirming trend regime alignment. TradeAI PRO preserves capital rather than forcing low-edge trades.';
            }
        } else {
            if (emptyCard) emptyCard.style.display = 'none';
            if (grid) {
                grid.style.display = 'grid';
                grid.innerHTML = picks.map((p, idx) => renderNextDayPickCard(p, idx)).join('');
            }
        }

        // Populate archive dropdown & outcome history
        await loadNextDayPicksHistory();

    } catch (err) {
        console.error('[loadNextDayPicks] Failed:', err);
        if (grid) grid.innerHTML = `<div style="grid-column: 1/-1; padding: 2rem; text-align: center; color: #ef4444;">Failed to load next-day picks: ${err.message}. Retrying shortly...</div>`;
    }
}

function renderNextDayPickCard(pick, index) {
    const rawDir = pick.directional_bias || pick.direction || 'LONG';
    const isLong = String(rawDir).toUpperCase() === 'LONG';
    const dirColor = isLong ? '#10b981' : '#ef4444';
    const dirBg = isLong ? 'rgba(16, 185, 129, 0.12)' : 'rgba(239, 68, 68, 0.12)';

    const setup = pick.trade_plan || {};
    const entryPrice = Number(setup.trigger_price ?? setup.entry_trigger ?? setup.entry ?? 0);
    const stopLoss = Number(setup.stop_loss ?? 0);
    const targetPrice = Number(setup.target_1 ?? setup.target ?? 0);
    const hasValidLevels = entryPrice > 0 && stopLoss > 0 && targetPrice > 0;

    const entryDisplay = entryPrice > 0 ? `₹${entryPrice.toLocaleString('en-IN', {maximumFractionDigits: 2})}` : '— (unavailable)';
    const slDisplay = stopLoss > 0 ? `₹${stopLoss.toLocaleString('en-IN', {maximumFractionDigits: 2})}` : '— (unavailable)';
    const targetDisplay = targetPrice > 0 ? `₹${targetPrice.toLocaleString('en-IN', {maximumFractionDigits: 2})}` : '— (unavailable)';

    const rawRR = hasValidLevels ? (setup.risk_reward ?? pick.risk_reward_ratio ?? (
        Math.abs(entryPrice - stopLoss) > 0.001
            ? (Math.abs(targetPrice - entryPrice) / Math.abs(entryPrice - stopLoss))
            : null
    )) : null;
    const rrRatio = (rawRR != null && Number.isFinite(Number(rawRR))) ? `${Number(rawRR).toFixed(1)}x R:R` : '— R:R';

    const riskPct = (hasValidLevels && entryPrice > 0) ? `${((Math.abs(entryPrice - stopLoss) / entryPrice) * 100).toFixed(1)}%` : '—';
    const rewardPct = (hasValidLevels && entryPrice > 0) ? `${((Math.abs(targetPrice - entryPrice) / entryPrice) * 100).toFixed(1)}%` : '—';

    const scenarios = pick.scenarios || {};
    const bullScenario = scenarios.bull || scenarios.bull_case || '— (Scenario not provided)';
    const baseScenario = scenarios.base || scenarios.base_case || '— (Plan not provided)';
    const invalidationRule = scenarios.invalidation || scenarios.invalidation_rule || '— (Invalidation rule not specified)';

    const catalysts = pick.news_catalysts || pick.catalysts || [];
    const catalystHtml = catalysts.length > 0
        ? catalysts.map(c => {
            const headline = c.headline || c.title || '';
            const source = c.source ? `[${c.source}] ` : '';
            const impact = c.impact ? ` (${c.impact})` : '';
            return `<li style="margin-bottom: 4px; color: var(--text-secondary); font-size: 0.78rem;"><strong>${escapeHtml(source + headline)}</strong>${escapeHtml(impact)}</li>`;
          }).join('')
        : '<li style="color: var(--text-muted); font-size: 0.78rem;">No verified news catalysts recorded for this session.</li>';

    const techLevels = pick.technical_levels || pick.technical_alignment || {};
    const rsiVal = techLevels.rsi14 ?? techLevels.rsi ?? null;
    const rsiDisplay = (rsiVal != null && Number.isFinite(Number(rsiVal))) ? Number(rsiVal).toFixed(0) : '—';

    return `
    <div class="card" style="background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 1.25rem; display: flex; flex-direction: column; gap: 1rem; position: relative;">
        <!-- Top Row: Symbol, Direction, Rank, R:R -->
        <div style="display: flex; justify-content: space-between; align-items: flex-start;">
            <div>
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="font-size: 1.25rem; font-weight: 800; color: var(--text-primary); font-family: var(--font-mono, monospace);">${escapeHtml(pick.symbol)}</span>
                    <span style="background: ${dirBg}; color: ${dirColor}; font-weight: 700; font-size: 0.75rem; padding: 2px 8px; border-radius: 4px;">${rawDir}</span>
                    <span style="background: rgba(14, 165, 233, 0.1); color: #0284c7; font-size: 0.72rem; padding: 2px 6px; border-radius: 4px;">RANK #${pick.rank || (index + 1)}</span>
                </div>
                <span style="font-size: 0.75rem; color: var(--text-muted);">${escapeHtml(pick.company_name || pick.sector || 'NSE Tier-1')}</span>
            </div>
            <div style="text-align: right;">
                <div style="font-size: 1.1rem; font-weight: 700; color: #10b981; font-family: var(--font-mono, monospace);">${rrRatio}</div>
                <span style="font-size: 0.7rem; color: var(--text-muted);">${hasValidLevels ? 'Minimum &ge; 1.5x Gate' : 'Levels incomplete'}</span>
            </div>
        </div>

        <!-- Price Setup Grid -->
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.5rem; background: var(--bg-hover, rgba(255,255,255,0.03)); padding: 0.75rem; border-radius: 8px; border: 1px solid var(--border-color);">
            <div style="text-align: center;">
                <div style="font-size: 0.7rem; color: var(--text-secondary); text-transform: uppercase;">Entry Trigger</div>
                <div style="font-size: 0.95rem; font-weight: 700; color: var(--text-primary); margin-top: 2px; font-family: var(--font-mono, monospace);">${entryDisplay}</div>
                <span style="font-size: 0.72rem; color: var(--text-muted);">${escapeHtml(setup.trigger_zone || setup.trigger_condition || '—')}</span>
            </div>
            <div style="text-align: center; border-left: 1px solid var(--border-color); border-right: 1px solid var(--border-color);">
                <div style="font-size: 0.7rem; color: #ef4444; text-transform: uppercase;">Stop Loss</div>
                <div style="font-size: 0.95rem; font-weight: 700; color: #ef4444; margin-top: 2px; font-family: var(--font-mono, monospace);">${slDisplay}</div>
                <span style="font-size: 0.72rem; color: var(--text-muted);">Risk: ${riskPct}</span>
            </div>
            <div style="text-align: center;">
                <div style="font-size: 0.7rem; color: #10b981; text-transform: uppercase;">Target (TP1)</div>
                <div style="font-size: 0.95rem; font-weight: 700; color: #10b981; margin-top: 2px; font-family: var(--font-mono, monospace);">${targetDisplay}</div>
                <span style="font-size: 0.72rem; color: var(--text-muted);">Gain: ${rewardPct === '—' ? '—' : '+' + rewardPct}</span>
            </div>
        </div>

        <!-- 3-Way Scenarios -->
        <div style="font-size: 0.78rem; display: flex; flex-direction: column; gap: 4px;">
            <div style="color: #10b981;"><strong>Bull Trigger:</strong> <span style="color: var(--text-secondary);">${escapeHtml(bullScenario)}</span></div>
            <div style="color: #0284c7;"><strong>Base Plan:</strong> <span style="color: var(--text-secondary);">${escapeHtml(baseScenario)}</span></div>
            <div style="color: #ef4444;"><strong>Invalidation:</strong> <span style="color: var(--text-secondary);">${escapeHtml(invalidationRule)}</span></div>
        </div>

        <!-- Verified Catalysts -->
        <div style="border-top: 1px solid var(--border-color); padding-top: 0.6rem;">
            <span style="font-size: 0.72rem; font-weight: 700; color: var(--text-primary); text-transform: uppercase; letter-spacing: 0.5px;">Point-in-Time Catalysts & News</span>
            <ul style="margin: 6px 0 0 1rem; padding: 0;">
                ${catalystHtml}
            </ul>
        </div>

        <!-- Footer: Technical & Status -->
        <div style="display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--border-color); padding-top: 0.6rem; font-size: 0.72rem; color: var(--text-muted);">
            <div>RSI: <strong>${rsiDisplay}</strong> · Confluence: <strong>${escapeHtml(pick.eligibility || '— (unverified)')}</strong></div>
            <span class="badge" style="background: rgba(16, 185, 129, 0.15); color: #10b981; padding: 2px 6px; border-radius: 4px;">STATUS: ${escapeHtml(pick.outcome_status || pick.status || 'PENDING')}</span>
        </div>
    </div>`;
}

async function loadNextDayPicksHistory() {
    const select = document.getElementById('ndpHistorySelect');
    const outcomeBody = document.getElementById('ndpOutcomeTableBody');
    if (!select) return;

    try {
        const resp = await fetchWithTimeout(`${API}/api/research/next-day-picks/history`, { headers: { 'Accept': 'application/json' } });
        if (!resp.ok) return;
        const data = await resp.json();
        const history = data.history || [];

        // Update select
        const currentSelected = select.value;
        select.innerHTML = '<option value="">Latest Published Report</option>' +
            history.map(h => `<option value="${escapeHtml(h.report_id)}">${escapeHtml(h.report_id)} · ${escapeHtml(h.target_date || '')} (${h.picks_count || 0} picks)</option>`).join('');
        if (currentSelected) select.value = currentSelected;

        // Render Outcome Resolution Table
        if (outcomeBody) {
            let allOutcomes = [];
            history.forEach(h => {
                if (h.outcomes && Array.isArray(h.outcomes)) {
                    h.outcomes.forEach(o => allOutcomes.push({ report_id: h.report_id, date: h.target_date, ...o }));
                }
            });

            if (allOutcomes.length === 0) {
                outcomeBody.innerHTML = '<tr><td colspan="7" style="padding: 1.25rem; text-align: center; color: var(--text-muted);">Post-session outcome resolutions will appear here after market close.</td></tr>';
            } else {
                outcomeBody.innerHTML = allOutcomes.slice(0, 15).map(o => {
                    const resColor = o.status === 'RESOLVED_WIN' ? '#10b981' : (o.status === 'RESOLVED_LOSS' ? '#ef4444' : '#64748b');
                    return `
                    <tr style="border-bottom: 1px solid var(--border-color);">
                        <td style="padding: 0.6rem 0.75rem; font-family: var(--font-mono, monospace);">${escapeHtml(o.date || o.report_id)}</td>
                        <td style="padding: 0.6rem 0.75rem; font-weight: 700;">${escapeHtml(o.symbol)}</td>
                        <td style="padding: 0.6rem 0.75rem;">${escapeHtml(o.direction || 'LONG')}</td>
                        <td style="padding: 0.6rem 0.75rem; font-family: var(--font-mono, monospace);">₹${o.entry || '--'} / ₹${o.sl || '--'} / ₹${o.tp || '--'}</td>
                        <td style="padding: 0.6rem 0.75rem; font-weight: 600; color: #10b981;">${(o.rr != null && Number.isFinite(Number(o.rr))) ? `${Number(o.rr).toFixed(1)}x` : '—'}</td>
                        <td style="padding: 0.6rem 0.75rem;"><span style="color: ${resColor}; font-weight: 700;">${escapeHtml(o.status || 'PENDING')}</span></td>
                        <td style="padding: 0.6rem 0.75rem; font-family: var(--font-mono, monospace);">${o.realized_r !== undefined ? `${o.realized_r.toFixed(2)}R` : '--'}</td>
                    </tr>`;
                }).join('');
            }
        }
    } catch (e) {
        console.warn('[loadNextDayPicksHistory] Error:', e);
    }
}

// Event Listeners for Next-Day Picks Actions
document.addEventListener('DOMContentLoaded', () => {
    const genBtn = document.getElementById('ndpGenerateBtn');
    if (genBtn) {
        genBtn.addEventListener('click', async () => {
            if (!confirm('Run post-close research screening on Tier-1 NSE universe? This job is bounded and preserves capital gates.')) return;
            genBtn.disabled = true;
            genBtn.innerHTML = '<span>⏳</span> Screening...';
            try {
                const resp = await fetchWithTimeout(`${API}/api/research/next-day-picks/generate`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ force: true, max_picks: 5 })
                });
                const res = await resp.json();
                alert(`Research pipeline complete. Generated ${res.picks ? res.picks.length : 0} qualified setups for target session.`);
                await loadNextDayPicks();
            } catch (err) {
                alert(`Pipeline execution error: ${err.message}`);
            } finally {
                genBtn.disabled = false;
                genBtn.innerHTML = '<span>⚡</span> Run Research';
            }
        });
    }

    const histSelect = document.getElementById('ndpHistorySelect');
    if (histSelect) {
        histSelect.addEventListener('change', (e) => {
            loadNextDayPicks(e.target.value || null);
        });
    }
});
// ══════════════════════════════════════════════════════════════════════════════
// ── PRIORITY WATCHLIST ENGINE (OPTION A) ──────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════════

function updateWatchlistBadge() {
    const badge = document.getElementById('watchlistSidebarBadge');
    if (badge) {
        badge.textContent = _watchlist.size;
        badge.style.display = _watchlist.size > 0 ? 'inline-block' : 'none';
    }
}

function addToWatchlist(sym) {
    if (!sym) return;
    const clean = sym.trim().toUpperCase().replace('.NS', '');
    if (!_watchlist.has(clean)) {
        _watchlist.add(clean);
        _saveWatchlist();
        updateWatchlistBadge();
        updateModalPinButton(clean);
        showToast(`\u2605 Added ${clean} to Priority Watchlist`, 'success');
        if (typeof currentTab !== 'undefined' && currentTab === 'watchlist') {
            const grid = document.getElementById('watchlistGrid');
            if (grid) {
                if (!grid.querySelector('.watchlist-card')) {
                    grid.innerHTML = '';
                }
                if (!document.getElementById(`wl-card-${clean}`)) {
                    const compName = (typeof companyNames !== 'undefined' && companyNames[clean]) ? companyNames[clean] : `${clean} · NSE Equity`;
                    const cached = _watchlistQuoteCache[clean];
                    const cardHtml = _renderWatchlistCard(clean, cached || { ltp: 0, isLoading: true, isError: false }, compName);
                    grid.insertAdjacentHTML('beforeend', cardHtml);
                }
            }
            loadDedicatedWatchlist({ silent: true });
        }
    } else {
        showToast(`${clean} is already in Watchlist`, 'info');
    }
}

function removeFromWatchlist(sym) {
    if (!sym) return;
    const clean = sym.trim().toUpperCase().replace('.NS', '');
    if (_watchlist.has(clean)) {
        // Invalidate in-flight quote loads before removing this symbol.
        _watchlistReqSeq += 1;
        _watchlist.delete(clean);
        if (typeof _watchlistQuoteCache !== 'undefined') {
            delete _watchlistQuoteCache[clean];
        }
        _saveWatchlist();
        updateWatchlistBadge();
        updateModalPinButton(clean);
        showToast(`Removed ${clean} from Watchlist`, 'info');

        const cardEl = document.getElementById(`wl-card-${clean}`);
        if (cardEl) cardEl.remove();

        const grid = document.getElementById('watchlistGrid');
        if (grid && _watchlist.size === 0) {
            grid.innerHTML = `
                <div style="grid-column: 1/-1; text-align: center; padding: 3rem 1rem; background: rgba(255,255,255,0.02); border: 1px dashed rgba(255,255,255,0.1); border-radius: 10px;">
                    <div style="font-size: 2.2rem; margin-bottom: 0.75rem;">\u2B50</div>
                    <h3 style="color: var(--text-primary); margin-bottom: 0.5rem; font-size: 1.1rem;">Your Priority Watchlist is Empty</h3>
                    <p style="color: var(--text-muted); font-size: 0.8rem; max-width: 420px; margin: 0 auto 1.2rem;">
                        Add your high-conviction NSE assets above using the Quick Add bar, or click any suggestion pill to begin monitoring.
                    </p>
                    <button class="btn btn-primary btn-sm" onclick="addToWatchlist('RELIANCE'); addToWatchlist('HDFCBANK'); addToWatchlist('VEDL');">
                        + Load Core Tier-1 Leaders
                    </button>
                </div>
            `;
            return;
        }

        if (typeof currentTab !== 'undefined' && currentTab === 'watchlist') {
            loadDedicatedWatchlist({ silent: true });
        }
    }
}

function addInputStockToWatchlist() {
    const input = document.getElementById('watchlistAddInput');
    if (!input) return;
    const val = input.value.trim().toUpperCase();
    if (!val) {
        showToast('Please enter an NSE stock symbol', 'info');
        return;
    }
    addToWatchlist(val);
    input.value = '';
}

function toggleCurrentModalWatchlist() {
    const titleEl = document.getElementById('modalTitle');
    if (!titleEl) return;
    const sym = titleEl.textContent.trim().toUpperCase().replace('.NS', '');
    if (!sym || sym.includes('LOADING')) return;
    if (_watchlist.has(sym)) {
        removeFromWatchlist(sym);
    } else {
        addToWatchlist(sym);
    }
}

function updateModalPinButton(sym) {
    const pinBtn = document.getElementById('modalWatchlistPinBtn');
    if (!pinBtn) return;
    const isPinned = _watchlist.has(sym);
    pinBtn.textContent = isPinned ? '★ In Watchlist' : '☆ Pin to Watchlist';
    pinBtn.className = isPinned ? 'modal-pin-btn pinned' : 'modal-pin-btn';
}

let _watchlistQuoteCache = {}; // in-memory quote cache: { [sym]: { ltp, changePct, signalLabel, conf, entry, sl, tp, isError, ts } }

function _renderWatchlistCard(sym, q, compName) {
    const isErr = Boolean(q && q.isError && !q.isLoading);
    const isLdg = Boolean(!q || q.isLoading || (!q.ltp && !isErr));
    if (isErr || isLdg || !q || !q.ltp || q.ltp === 0) {
        const badgeLabel = isErr ? 'FEED OFFLINE' : 'SYNCING FEED';
        const badgeBg = isErr ? 'rgba(239,68,68,0.12)' : 'rgba(59,130,246,0.12)';
        const badgeCol = isErr ? '#f87171' : '#60a5fa';
        const badgeBrd = isErr ? 'rgba(239,68,68,0.3)' : 'rgba(59,130,246,0.3)';
        const markStatus = isErr ? 'Quote Unavailable' : 'Syncing Quote...';
        const spinnerHtml = isErr ? '' : `<span class="spinner-inline" style="width:10px;height:10px;border:2px solid rgba(148,163,184,0.2);border-top-color:#60a5fa;border-radius:50%;display:inline-block;"></span>`;
        return `
            <div class="watchlist-card watchlist-card-syncing" id="wl-card-${sym}" style="border-color: ${badgeBrd};">
                <div class="watchlist-header">
                    <div class="watchlist-sym-block">
                        <span class="watchlist-sym">${sym}</span>
                        <span class="watchlist-name">${_escHtml(compName)}</span>
                    </div>
                    <div style="display:flex;align-items:center;gap:6px;">
                        <span style="font-size: 0.72rem;font-weight:800;padding:2px 8px;border-radius:4px;background:${badgeBg};color:${badgeCol};border:1px solid ${badgeBrd};">
                            ${badgeLabel}
                        </span>
                    </div>
                </div>

                <div class="watchlist-price-row" style="padding: 0.6rem 0; opacity: 0.85;">
                    <div>
                        <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">Mark Price (LTP)</div>
                        <div style="font-size: 0.9rem; font-weight: 700; color: #94a3b8; font-family: var(--font-mono); display:flex; align-items:center; gap:6px;">
                            ${spinnerHtml}
                            ${markStatus}
                        </div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">24h Trend</div>
                        <div style="font-size: 0.9rem; color: var(--text-muted); font-family: var(--font-mono);">--</div>
                    </div>
                </div>

                <div class="watchlist-levels" style="opacity: 0.6;">
                    <div>
                        <div class="watchlist-lvl-label">Entry Ref</div>
                        <div class="watchlist-lvl-val">--</div>
                    </div>
                    <div>
                        <div class="watchlist-lvl-label">Stop Loss</div>
                        <div class="watchlist-lvl-val">--</div>
                    </div>
                    <div>
                        <div class="watchlist-lvl-label">Target (TP)</div>
                        <div class="watchlist-lvl-val">--</div>
                    </div>
                </div>

                <div class="watchlist-actions" style="display:flex;align-items:center;gap:6px;">
                    <button class="watchlist-inspect-btn" onclick="retryWatchlistSymbol('${sym}')" style="background: rgba(59,130,246,0.15); border-color: rgba(59,130,246,0.35); color: #60a5fa;" title="Retry loading ${sym} quote">
                        🔄 Retry
                    </button>
                    <button class="watchlist-inspect-btn" onclick="fetchAndShowQuote('${sym}')" title="Inspect ${sym} chart modal">
                        ⚡ Inspect
                    </button>
                    <button class="watchlist-remove-btn" onclick="removeFromWatchlist('${sym}')" title="Remove ${sym} from Watchlist">
                        ✕
                    </button>
                </div>
            </div>
        `;
    }

    const ltp = q.ltp;
    const changePct = q.changePct || 0;
    const signalLabel = q.signalLabel || 'MONITOR';
    const conf = q.conf || 65;
    const entry = q.entry || ltp;
    const sl = q.sl || (ltp * 0.98);
    const tp = q.tp || (ltp * 1.035);

    const isLong = ['LONG', 'BUY'].includes(signalLabel.toUpperCase());
    const isShort = ['SHORT', 'SELL'].includes(signalLabel.toUpperCase());
    const sigColor = isLong ? 'var(--green, #10b981)' : (isShort ? 'var(--red, #ef4444)' : '#94a3b8');
    const sigBg = isLong ? 'rgba(16,185,129,0.12)' : (isShort ? 'rgba(239,68,68,0.12)' : 'rgba(255,255,255,0.06)');
    const sigBorder = isLong ? 'rgba(16,185,129,0.3)' : (isShort ? 'rgba(239,68,68,0.3)' : 'rgba(255,255,255,0.1)');

    const chgClass = changePct >= 0 ? 'text-profit' : 'text-loss';
    const chgStr = `${changePct >= 0 ? '+' : ''}${Number(changePct).toFixed(2)}%`;

    return `
        <div class="watchlist-card" id="wl-card-${sym}">
            <div class="watchlist-header">
                <div class="watchlist-sym-block">
                    <span class="watchlist-sym">${sym}</span>
                    <span class="watchlist-name">${_escHtml(compName)}</span>
                </div>
                <div style="display:flex;align-items:center;gap:6px;">
                    <span style="font-size: 0.72rem;font-weight:800;padding:2px 8px;border-radius:4px;background:${sigBg};color:${sigColor};border:1px solid ${sigBorder};">
                        ${signalLabel}
                    </span>
                    <span style="font-size: 0.70rem;font-family:var(--font-mono);color:var(--text-muted);background:rgba(255,255,255,0.03);padding:2px 5px;border-radius:4px;">
                        ${conf}/100
                    </span>
                </div>
            </div>

            <div class="watchlist-price-row">
                <div>
                    <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">Mark Price (LTP)</div>
                    <div class="watchlist-ltp">₹${Number(ltp).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size: 0.72rem;color:var(--text-muted);text-transform:uppercase;">24h Trend</div>
                    <div class="watchlist-chg ${chgClass}">${chgStr}</div>
                </div>
            </div>

            <div class="watchlist-levels">
                <div>
                    <div class="watchlist-lvl-label">Entry Ref</div>
                    <div class="watchlist-lvl-val" style="color:var(--text-primary);">₹${Number(entry || ltp).toFixed(1)}</div>
                </div>
                <div>
                    <div class="watchlist-lvl-label" style="color:var(--red,#ef4444);">Stop Loss</div>
                    <div class="watchlist-lvl-val" style="color:var(--red,#ef4444);">₹${Number(sl).toFixed(1)}</div>
                </div>
                <div>
                    <div class="watchlist-lvl-label" style="color:var(--green,#10b981);">Target (TP)</div>
                    <div class="watchlist-lvl-val" style="color:var(--green,#10b981);">₹${Number(tp).toFixed(1)}</div>
                </div>
            </div>

            <div class="watchlist-actions">
                <button class="watchlist-inspect-btn" onclick="fetchAndShowQuote('${sym}')" title="Inspect ${sym} 5m/15m chart and technical indicators">
                    ⚡ Inspect Chart & Setup
                </button>
                <button class="watchlist-remove-btn" onclick="removeFromWatchlist('${sym}')" title="Remove ${sym} from Watchlist">
                    ✕
                </button>
            </div>
        </div>
    `;
}

async function _fetchWatchlistQuote(sym) {
    const cached = _watchlistQuoteCache[sym];
    if (cached && (Date.now() - cached.ts < 60000) && !cached.isError) {
        return cached;
    }

    // 1. Check in-memory marketDataCache first (0ms instantaneous lookup)
    if (typeof marketDataCache !== 'undefined' && marketDataCache[sym] && marketDataCache[sym].close) {
        const md = marketDataCache[sym];
        const ltp = Number(md.close || 0);
        if (ltp > 0) {
            const quoteObj = {
                ltp,
                changePct: Number(md.change_pct || 0),
                signalLabel: 'MONITOR',
                conf: 65,
                entry: ltp,
                sl: Number((ltp * 0.98).toFixed(1)),
                tp: Number((ltp * 1.035).toFixed(1)),
                isError: false,
                isSyncing: false,
                ts: Date.now()
            };
            _watchlistQuoteCache[sym] = quoteObj;
            return quoteObj;
        }
    }

    // 2. Fetch fast lightweight market quote from /api/market/{sym}?limit=2&interval=5m (~1.2s)
    try {
        const cleanSym = encodeURIComponent(sym.replace('.NS', '').trim());
        const mRes = await fetchSafeJSON(`${API}/api/market/${cleanSym}?limit=2&interval=5m`, {}, 4000);
        if (mRes.ok && mRes.data && Array.isArray(mRes.data.data) && mRes.data.data.length > 0) {
            const candles = mRes.data.data;
            const last = candles[candles.length - 1];
            const prev = candles.length > 1 ? candles[candles.length - 2] : last;
            const ltp = Number(last.close || 0);
            if (ltp > 0) {
                const prevClose = Number(prev.close || ltp);
                const changePct = prevClose > 0 ? Number((((ltp - prevClose) / prevClose) * 100).toFixed(2)) : 0;
                const sig = (last.signal || 'HOLD').toUpperCase();
                const signalLabel = ['LONG', 'SHORT', 'BUY', 'SELL', 'HOLD'].includes(sig) ? sig : 'MONITOR';
                const ema50 = Number(last.ema_50 || (ltp * 0.98));
                const sl = Number((last.bb_lower && last.bb_lower < ltp) ? last.bb_lower : (ema50 < ltp ? ema50 : ltp * 0.98));
                const tp = Number((last.bb_upper && last.bb_upper > ltp) ? last.bb_upper : (ltp * 1.035));
                const quoteObj = {
                    ltp,
                    changePct,
                    signalLabel,
                    conf: Number(last.rsi ? Math.min(85, Math.max(50, Math.round(last.rsi))) : 65),
                    entry: ltp,
                    sl: Number(sl.toFixed(1)),
                    tp: Number(tp.toFixed(1)),
                    isError: false,
                    isSyncing: false,
                    ts: Date.now()
                };
                _watchlistQuoteCache[sym] = quoteObj;
                return quoteObj;
            }
        }
    } catch (_) {}

    // 3. Fallback to /api/search/quote/{sym} (fast 4s timeout)
    try {
        const qRes = await fetchSafeJSON(`${API}/api/search/quote/${encodeURIComponent(sym)}`, {}, 4000);
        if (qRes.ok && qRes.data) {
            const d = qRes.data;
            const ltp = Number(d.close || 0);
            const sig = d.signal || {};
            const changePct = Number(sig.change_pct ?? d.change_pct ?? 0);
            const rawSig = sig.signal || (ltp > 0 ? 'MONITOR' : 'UNAVAILABLE');
            const signalLabel = rawSig.toUpperCase();
            const conf = sig.confidence || 65;
            const entry = Number(sig.entry || ltp);
            const sl = Number(sig.stop_loss || (ltp > 0 ? ltp * 0.98 : 0));
            const tp = Number(sig.target || (ltp > 0 ? ltp * 1.035 : 0));
            const quoteObj = {
                ltp,
                changePct,
                signalLabel,
                conf,
                entry,
                sl,
                tp,
                isError: ltp === 0,
                isSyncing: false,
                ts: Date.now()
            };
            if (ltp > 0) {
                _watchlistQuoteCache[sym] = quoteObj;
            }
            return quoteObj;
        }
    } catch (_) {}

    return {
        ltp: 0,
        changePct: 0,
        signalLabel: 'UNAVAILABLE',
        conf: 0,
        entry: 0,
        sl: 0,
        tp: 0,
        isError: true,
        isSyncing: true,
        ts: Date.now()
    };
}

async function retryWatchlistSymbol(sym) {
    if (!sym) return;
    delete _watchlistQuoteCache[sym];
    const cardEl = document.getElementById(`wl-card-${sym}`);
    if (cardEl) {
        cardEl.style.opacity = '0.7';
    }
    showToast(`Retrying ${sym} quote...`, 'info');
    const q = await _fetchWatchlistQuote(sym);
    if (q && q.ltp > 0) {
        _watchlistQuoteCache[sym] = q;
        showToast(`Updated ${sym} quote: ₹${q.ltp}`, 'success');
    } else {
        showToast(`Could not fetch quote for ${sym}`, 'warning');
    }
    await loadDedicatedWatchlist({ silent: true });
}

let _watchlistReqSeq = 0;

async function loadDedicatedWatchlist(options = {}) {
    const grid = document.getElementById('watchlistGrid');
    updateWatchlistBadge();
    if (!grid) return;

    // Invalidate older quote requests before every early return, including an empty watchlist.
    const seq = ++_watchlistReqSeq;
    const list = [..._watchlist]; // Preserve the user's original insertion order in UI.
    if (list.length === 0) {
        grid.innerHTML = `
            <div style="grid-column: 1/-1; text-align: center; padding: 3rem 1rem; background: rgba(255,255,255,0.02); border: 1px dashed rgba(255,255,255,0.1); border-radius: 10px;">
                <div style="font-size: 2.2rem; margin-bottom: 0.75rem;">\u2B50</div>
                <h3 style="color: var(--text-primary); margin-bottom: 0.5rem; font-size: 1.1rem;">Your Priority Watchlist is Empty</h3>
                <p style="color: var(--text-muted); font-size: 0.8rem; max-width: 420px; margin: 0 auto 1.2rem;">
                    Add your high-conviction NSE assets above using the Quick Add bar, or click any suggestion pill to begin monitoring.
                </p>
                <button class="btn btn-primary btn-sm" onclick="addToWatchlist('RELIANCE'); addToWatchlist('HDFCBANK'); addToWatchlist('VEDL');">
                    + Load Core Tier-1 Leaders
                </button>
            </div>
        `;
        return;
    }

    const companyNames = {
        'RELIANCE': 'Reliance Industries Ltd.',
        'HDFCBANK': 'HDFC Bank Ltd.',
        'TCS': 'Tata Consultancy Services',
        'INFY': 'Infosys Limited',
        'ICICIBANK': 'ICICI Bank Ltd.',
        'SBIN': 'State Bank of India',
        'BHARTIARTL': 'Bharti Airtel Ltd.',
        'LT': 'Larsen & Toubro Ltd.',
        'ITC': 'ITC Limited',
        'TATAMOTORS': 'Tata Motors Ltd.',
        'VEDL': 'Vedanta Limited',
        'MARUTI': 'Maruti Suzuki India',
        'SUNPHARMA': 'Sun Pharmaceutical',
        'BAJFINANCE': 'Bajaj Finance Ltd.',
        'ASIANPAINT': 'Asian Paints Ltd.',
        'ONGC': 'Oil & Natural Gas Corp',
        'BPCL': 'Bharat Petroleum Corp',
        'COALINDIA': 'Coal India Limited',
        'TATASTEEL': 'Tata Steel Ltd.'
    };

    // 1. Instant optimistic / cached render (0ms):
    // If grid has no cards yet, render instant cards in user order with isLoading: true (never false error flash!)
    const hasExistingCards = grid.querySelectorAll('.watchlist-card').length > 0;
    if (!hasExistingCards && !options.silent) {
        const initialCards = list.map(sym => {
            const compName = companyNames[sym] || `${sym} · NSE Equity`;
            const cached = _watchlistQuoteCache[sym];
            return _renderWatchlistCard(sym, cached || { ltp: 0, isLoading: true, isError: false }, compName);
        });
        grid.innerHTML = initialCards.join('');
    } else {
        // Guardrail 1: Check if any new symbol in list is missing from grid, append its skeleton immediately
        list.forEach(sym => {
            if (!document.getElementById(`wl-card-${sym}`)) {
                const compName = companyNames[sym] || `${sym} · NSE Equity`;
                const cached = _watchlistQuoteCache[sym];
                const cardHtml = _renderWatchlistCard(sym, cached || { ltp: 0, isLoading: true, isError: false }, compName);
                grid.insertAdjacentHTML('beforeend', cardHtml);
            }
        });
        // Remove any cards in grid that are not in _watchlist
        grid.querySelectorAll('.watchlist-card').forEach(el => {
            const elId = el.id || '';
            const sym = elId.replace('wl-card-', '');
            if (sym && !_watchlist.has(sym)) {
                el.remove();
            }
        });
    }

    const signalsMap = {};
    const sigPool = [
        ...(Array.isArray(allSignals) ? allSignals : (allSignals?.signals || [])),
        ...(Array.isArray(window._allSignalsCache) ? window._allSignalsCache : []),
        ...(Array.isArray(window._intradayAllSigs) ? window._intradayAllSigs : []),
        ...(Array.isArray(window._swingAllSetups) ? window._swingAllSetups : []),
        ...(Array.isArray(window._topSetups) ? window._topSetups : [])
    ];
    sigPool.forEach(s => {
        if (s && s.symbol) {
            const k = s.symbol.toUpperCase().replace('.NS', '').trim();
            if (!signalsMap[k] || (s.close && !signalsMap[k].close)) {
                signalsMap[k] = s;
            }
        }
    });

    // 2. Bulk fast pre-fetch via /api/watchlist/quotes
    // Guardrail 1: ONLY sort/dedup when constructing the network query string!
    let bulkQuotes = {};
    try {
        const sortedNetworkSymbols = [...new Set(list)].sort();
        const symQuery = encodeURIComponent(sortedNetworkSymbols.join(','));
        const bulkRes = await fetchSafeJSON(`${API}/api/watchlist/quotes?symbols=${symQuery}`, {}, 12000);
        
        // Guardrail 1: Discard superseded response if a newer request started or watchlist changed
        if (seq !== _watchlistReqSeq) return;

        if (bulkRes.ok && bulkRes.data && bulkRes.data.quotes) {
            bulkQuotes = bulkRes.data.quotes;
        }
    } catch (_) {
        if (seq !== _watchlistReqSeq) return;
    }

    // 3. Resolve quotes in user order, verifying symbol still exists in _watchlist
    const quotes = {};
    const missingSymbols = [];

    // Filter to only active watchlist symbols (race condition protection)
    const activeList = list.filter(sym => _watchlist.has(sym));
    if (activeList.length === 0) {
        return;
    }

    activeList.forEach(sym => {
        const bQuote = bulkQuotes[sym];
        if (bQuote && Number(bQuote.ltp || 0) > 0) {
            const ltp = Number(bQuote.ltp);
            const qObj = {
                ltp,
                changePct: Number(bQuote.change_pct || 0),
                signalLabel: bQuote.signal || 'MONITOR',
                conf: Number(bQuote.confidence || 65),
                entry: Number(bQuote.entry || ltp),
                sl: Number(bQuote.stop_loss || (ltp * 0.98)),
                tp: Number(bQuote.target || (ltp * 1.035)),
                isError: false,
                isLoading: false,
                isSyncing: false,
                ts: Date.now()
            };
            _watchlistQuoteCache[sym] = qObj;
            quotes[sym] = qObj;
            return;
        }

        const sig = signalsMap[sym];
        if (sig && (sig.close || sig.entry)) {
            const ltp = Number(sig.close || sig.entry || 0);
            const qObj = {
                ltp,
                changePct: Number(sig.change_pct || 0),
                signalLabel: sig.signal || 'MONITOR',
                conf: sig.confidence || 65,
                entry: Number(sig.entry || ltp),
                sl: Number(sig.stop_loss || (ltp > 0 ? ltp * 0.98 : 0)),
                tp: Number(sig.target || (ltp > 0 ? ltp * 1.035 : 0)),
                isError: false,
                isLoading: false,
                isSyncing: false,
                ts: Date.now()
            };
            _watchlistQuoteCache[sym] = qObj;
            quotes[sym] = qObj;
            return;
        }

        if (typeof marketDataCache !== 'undefined' && marketDataCache[sym] && marketDataCache[sym].close) {
            const ltp = Number(marketDataCache[sym].close || 0);
            if (ltp > 0) {
                const qObj = {
                    ltp,
                    changePct: Number(marketDataCache[sym].change_pct || 0),
                    signalLabel: 'MONITOR',
                    conf: 60,
                    entry: ltp,
                    sl: Number((ltp * 0.98).toFixed(1)),
                    tp: Number((ltp * 1.035).toFixed(1)),
                    isError: false,
                    isLoading: false,
                    isSyncing: false,
                    isCachedFallback: true,
                    ts: Date.now()
                };
                _watchlistQuoteCache[sym] = qObj;
                quotes[sym] = qObj;
                return;
            }
        }

        const cached = _watchlistQuoteCache[sym];
        if (cached && cached.ltp > 0) {
            quotes[sym] = cached;
        } else {
            quotes[sym] = { ltp: 0, isLoading: true, isError: false, isSyncing: true };
            missingSymbols.push(sym);
        }
    });

    if (seq !== _watchlistReqSeq) return;

    // 4. Update existing cards in place without destroying DOM focus or changing UI order!
    activeList.forEach((sym) => {
        if (!_watchlist.has(sym)) return;
        const compName = companyNames[sym] || `${sym} · NSE Equity`;
        const cardEl = document.getElementById(`wl-card-${sym}`);
        const cardHtml = _renderWatchlistCard(sym, quotes[sym], compName);
        if (cardEl) {
            cardEl.outerHTML = cardHtml;
        } else {
            grid.insertAdjacentHTML('beforeend', cardHtml);
        }
    });

    // 5. Non-blocking asynchronous background resolution for missing symbols
    if (missingSymbols.length > 0) {
        missingSymbols.forEach(sym => {
            if (!_watchlist.has(sym)) return;
            _fetchWatchlistQuote(sym).then(q => {
                if (seq !== _watchlistReqSeq || !_watchlist.has(sym)) return;
                if (q && q.ltp > 0) {
                    _watchlistQuoteCache[sym] = q;
                    const cardEl = document.getElementById(`wl-card-${sym}`);
                    if (cardEl && _watchlist.has(sym)) {
                        const compName = companyNames[sym] || `${sym} · NSE Equity`;
                        cardEl.outerHTML = _renderWatchlistCard(sym, q, compName);
                    }
                }
            }).catch(() => {});
        });
    }

    // 6. Silent auto-refresh every 20s only while on watchlist tab, never wiping the DOM
    if (missingSymbols.length > 0 && !window._watchlistAutoRetryTimer) {
        window._watchlistAutoRetryTimer = setTimeout(() => {
            window._watchlistAutoRetryTimer = null;
            if (typeof currentTab !== 'undefined' && currentTab === 'watchlist') {
                loadDedicatedWatchlist({ silent: true });
            }
        }, 20000);
    }
}

// Centralized delegated click handler for data-action buttons (CSP hardening)
document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const action = el.dataset.action;
    if (action === 'switchTab') {
        const tab = el.dataset.tab;
        if (tab && typeof switchTab === 'function') switchTab(tab);
    } else if (action === 'openSymbolModal') {
        const sym = el.dataset.symbol;
        if (sym && typeof openSymbolModal === 'function') openSymbolModal(sym);
    } else if (action === 'addToWatchlist') {
        const sym = el.dataset.symbol;
        if (sym && typeof addToWatchlist === 'function') addToWatchlist(sym);
    }
});
