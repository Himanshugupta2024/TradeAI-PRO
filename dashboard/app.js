/**
 * TradeAI PRO — Dashboard v3.0
 *
 * Tabs: Overview, Market (Gainers/Losers + Sector Heatmap),
 * Options (PCR + Chain), Trade Flow (FII/DII), Portfolio, Performance.
 */

const API = '';  // Same origin

// ── State ───────────────────────────────────────────────

let currentTab = 'overview';
let allSignals = [];
let marketDataCache = {};
let priceWs = null;
let wsReconnectTimer = null;
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

// ── Init ────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    initTabs();
    initSignalControls();
    updateMarketSessionBanner();
    initModal();
    startClock();
    loadAllData({ background: false });
    connectPriceWebSocket();
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
        await fetchWithTimeout(`${API}/api/refresh`, { method: 'POST' }, 30000);
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
        _refreshTimer = setTimeout(tick, REFRESH_INTERVAL_MS);
    };

    _refreshTimer = setTimeout(tick, REFRESH_INTERVAL_MS);
}

document.addEventListener('visibilitychange', () => {
    // When user comes back to tab, do one immediate silent refresh.
    if (!document.hidden && !_isRefreshing) {
        loadAllData({ background: true });
    }
});


// ── WebSocket for Real-Time Prices ──────────────────────

function connectPriceWebSocket() {
    if (priceWs && priceWs.readyState <= 1) return;  // Already connected or connecting

    const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProto}//${location.host}/ws/prices`;

    try {
        priceWs = new WebSocket(wsUrl);

        priceWs.onopen = () => {
            console.log('[WS] Connected to price stream');
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
                }
                if (data.timestamp) {
                    document.getElementById('lastUpdated').textContent =
                        new Date(data.timestamp).toLocaleTimeString('en-IN', {
                            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true,
                        });
                }
            } catch (e) {
                console.warn('[WS] Failed to parse message:', e);
            }
        };

        priceWs.onclose = () => {
            console.log('[WS] Disconnected — reconnecting in 5s...');
            updateWsIndicator(false);
            wsReconnectTimer = setTimeout(connectPriceWebSocket, 5000);
        };

        priceWs.onerror = (err) => {
            console.error('[WS] Error:', err);
            updateWsIndicator(false);
        };
    } catch (e) {
        console.error('[WS] Failed to connect:', e);
        wsReconnectTimer = setTimeout(connectPriceWebSocket, 10000);
    }
}

function updateWsIndicator(connected) {
    const dot = document.getElementById('refreshDot');
    if (dot) {
        dot.title = connected ? 'Live (WebSocket)' : 'Polling';
        dot.style.background = connected ? 'var(--green)' : 'var(--text-muted)';
    }
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
    if (currentTab === 'portfolio') {
        updatePortfolioPrices(prices);
    }

    // Cache numeric close prices for chart/modal use
    for (const [sym, tick] of Object.entries(prices)) {
        marketDataCache[sym] = typeof tick === 'object' ? tick : { close: tick };
    }
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
    const saved = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    updateThemeIcon(saved);

    document.getElementById('themeToggle').addEventListener('click', () => {
        const current = document.documentElement.getAttribute('data-theme');
        const next = current === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
        updateThemeIcon(next);
    });
}

function updateThemeIcon(theme) {
    document.getElementById('themeIcon').textContent = theme === 'dark' ? '🌙' : '☀️';
}


// ── Tab Navigation ──────────────────────────────────────

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
    document.querySelector(`[data-tab="${tab}"]`).classList.add('active');

    // Update content
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(`${tab}Tab`).classList.add('active');

    // Load tab-specific data
    if (tab === 'portfolio') loadPortfolio();
    if (tab === 'performance') loadPerformance();
    if (tab === 'market') loadMarket();
    if (tab === 'options') loadPCR();
    if (tab === 'tradeflow') loadTradeFlow();
    if (tab === 'scorecard') loadScorecard();
    if (tab === 'swing') loadSwingScanner();
    if (tab === 'indexmover') loadIndexMover();
    if (tab === 'learning') loadLearningStats();
    if (tab === 'breakouts') loadBreakouts();
}

// ── Fetch with Timeout Helper ──────────────────────────
// Ensures no API call hangs indefinitely. Default: 12 seconds.
function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
    const ac = new AbortController();
    const tid = setTimeout(() => ac.abort(), timeoutMs);
    return fetch(url, { ...options, signal: ac.signal })
        .finally(() => clearTimeout(tid));
}


async function loadAllData({ background = false } = {}) {
    if (_isRefreshing) return;
    _isRefreshing = true;

    const refreshDot = document.getElementById('refreshDot');
    if (refreshDot) refreshDot.classList.add('refreshing');

    try {
        const [signalsRes, statsRes, dashboardRes, oppsRes] = await Promise.all([
            fetchWithTimeout(`${API}/api/signals/all`, {}, 15000),
            fetchWithTimeout(`${API}/api/stats`, {}, 10000),
            fetchWithTimeout(`${API}/api/nse/dashboard`, {}, 12000),
            fetchWithTimeout(`${API}/api/screener/opportunities`, {}, 10000).catch(() => null),
        ]);

        const signalsData = await signalsRes.json();
        const statsData = await statsRes.json();
        const dashboardData = await dashboardRes.json();
        
        let oppsData = { opportunities: [] };
        if (oppsRes && oppsRes.ok) {
            oppsData = await oppsRes.json();
        }

        const nextSignals = Array.isArray(signalsData.signals) ? signalsData.signals : [];
        const nextSignature = getSignalsSignature(nextSignals);

        allSignals = nextSignals;
        if (nextSignature !== _lastSignalsSignature) {
            renderSignals(allSignals);
            _lastSignalsSignature = nextSignature;
        }
        updateSummary(allSignals);
        updateStatsStrip(statsData);
        updateMarketIntel(dashboardData);
        updateMarketSessionBanner();
        if (oppsData && oppsData.opportunities) {
            updateOpportunitiesGrid(oppsData.opportunities);
        }

        // Update timestamp
        const updated = new Date(signalsData.updated_at);
        document.getElementById('lastUpdated').textContent =
            updated.toLocaleTimeString('en-IN', {
                hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true,
            });

        const loader = document.getElementById('loadingState');
        if (loader) {
            if (nextSignals.length === 0) {
                loader.classList.remove('hidden');
                loader.innerHTML = `
                    <div class="loader"></div>
                    <p>Warming up market data... this can take 1-2 minutes on first load.</p>
                `;
            } else {
                loader.classList.add('hidden');
            }
        }

    } catch (err) {
        if (err.name === 'AbortError') {
            console.warn('Signal fetch timed out after 15s — server may be starting up');
        } else {
            console.error('Failed to fetch data:', err);
        }
        if (!background) {
            handleLoadError();
        }
    }

    // Background tab refreshes
    if (currentTab === 'learning') loadLearningStats();
    if (currentTab === 'portfolio') loadPortfolio();
    if (currentTab === 'performance') loadPerformance();

    if (refreshDot) refreshDot.classList.remove('refreshing');
    _isRefreshing = false;
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


// ── Stats Strip ─────────────────────────────────────────

function updateStatsStrip(stats) {
    const fmt = (v) => `₹${(v || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
    document.getElementById('statCapital').textContent = fmt(stats.capital || 100000);
    const pnlEl = document.getElementById('statPnl');
    const pnl = stats.total_pnl || 0;
    pnlEl.textContent = `${pnl >= 0 ? '+' : ''}${fmt(pnl)}`;
    pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
    const trades = Number(stats.total_trades || 0);
    document.getElementById('statWinRate').textContent = trades > 0 ? `${(stats.win_rate || 0).toFixed(1)}%` : '—';
    document.getElementById('statTrades').textContent = trades;

    const hint = document.getElementById('statsHint');
    if (hint) {
        if (trades === 0) {
            hint.style.display = 'block';
            hint.textContent = 'No trades yet — paper trading is active. Start with high-confidence setups to build performance stats.';
        } else {
            hint.style.display = 'none';
        }
    }
}


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
    const nowUtc = new Date(Date.UTC(t.year, t.month - 1, t.day, t.hour, t.minute, t.second));
    for (let i = 0; i < 8; i++) {
        const d = new Date(nowUtc);
        d.setUTCDate(d.getUTCDate() + i);
        const day = d.getUTCDay();
        if (day === 0 || day === 6) continue;
        const open = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 9, 15, 0));
        if (open > nowUtc) {
            return open.toLocaleString('en-IN', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short', hour12: true });
        }
    }
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
    banner.style.display = 'block';
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
                <div style="font-size:0.62rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">${title}</div>
                <div style="font-size:0.78rem;font-weight:700;color:${tone};margin-top:0.2rem;">${label} ${score}%</div>
                <div style="font-size:0.68rem;color:var(--text-muted);margin-top:0.12rem;">${detail}</div>
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
        const fmtPrice = (v) => v == null ? '—' : `${curr}${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

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

        card.innerHTML = `
            <div class="card-top">
                <div style="display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap;flex:1">
                    <span class="card-symbol signal-symbol">${sig.symbol}</span>${dqDot}
                    <span class="regime-badge regime-${regime}">${regimeLabel}</span>
                    ${staleBadge}${skipBadge}${conflictBadge}
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

function updateOpportunitiesGrid(opportunities) {
    const grid = document.getElementById('opportunityGrid');
    if (!grid) return;

    if (!opportunities || opportunities.length === 0) {
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
                    <span style="font-size:0.65rem;color:${sigColor};border:1px solid ${sigColor};padding:1px 4px;border-radius:3px;">${sigText}</span>
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
            fetch(`${API}/api/portfolio`),
            fetch(`${API}/api/trades?limit=50`),
        ]);
        const portfolio = await portRes.json();
        const trades = await tradesRes.json();

        // Summary
        document.getElementById('portOpenCount').textContent = `${portfolio.total_open} open`;
        const ur = portfolio.total_unrealized_pnl || 0;
        const urEl = document.getElementById('portUnrealized');
        urEl.textContent = `₹${ur.toFixed(2)} unrealized`;
        urEl.style.color = ur >= 0 ? 'var(--green)' : 'var(--red)';

        // Open positions table
        const tbody = document.getElementById('positionsBody');
        if (portfolio.open_positions.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No open positions</td></tr>';
        } else {
            tbody.innerHTML = portfolio.open_positions.map(p => {
                const pnlClass = (p.unrealized_pnl || 0) >= 0 ? 'pnl-positive' : 'pnl-negative';
                const sideClass = p.direction === 'BUY' ? 'badge-buy' : 'badge-sell';
                return `
                    <tr>
                        <td>${p.symbol}</td>
                        <td class="${sideClass}">${p.direction}</td>
                        <td>₹${p.entry_price.toFixed(2)}</td>
                        <td>₹${(p.current_price || p.entry_price).toFixed(2)}</td>
                        <td class="${pnlClass}">₹${(p.unrealized_pnl || 0).toFixed(2)}</td>
                        <td><span class="regime-badge regime-ranging">OPEN</span></td>
                    </tr>
                `;
            }).join('');
        }

        // Trade history table
        const hBody = document.getElementById('historyBody');
        const closedTrades = trades.trades.filter(t => t.status !== 'OPEN');
        if (closedTrades.length === 0) {
            hBody.innerHTML = '<tr><td colspan="7" class="empty-row">No trade history</td></tr>';
        } else {
            hBody.innerHTML = closedTrades.slice(0, 20).map(t => {
                const pnlClass = t.pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
                const sideClass = t.direction === 'BUY' ? 'badge-buy' : 'badge-sell';
                const time = new Date(t.timestamp).toLocaleString('en-IN', {
                    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'
                });
                const reason = {
                    'TARGET': '🎯 Target',
                    'STOP_LOSS': '🛑 SL',
                    'TRAILING_STOP': '📐 Trail',
                }[t.exit_reason] || t.exit_reason || '—';
                return `
                    <tr>
                        <td style="font-family: var(--font-body)">${time}</td>
                        <td>${t.symbol}</td>
                        <td class="${sideClass}">${t.direction}</td>
                        <td>₹${t.entry_price.toFixed(2)}</td>
                        <td>₹${t.exit_price.toFixed(2)}</td>
                        <td class="${pnlClass}">₹${t.pnl.toFixed(2)} (${t.pnl_pct}%)</td>
                        <td style="font-family: var(--font-body)">${reason}</td>
                    </tr>
                `;
            }).join('');
        }
    } catch (err) {
        console.error('Portfolio load error:', err);
    }
}


// ── Learning Tab ────────────────────────────────────────

async function loadLearningStats() {
    try {
        const [statsRes, outcomesRes] = await Promise.all([
            fetch(`${API}/api/learning/stats`),
            fetch(`${API}/api/learning/outcomes?limit=50`)
        ]);
        const data = await statsRes.json();
        const outcomesData = await outcomesRes.json();

        // Populate KPIs
        const o = data.insights?.overall || {};
        document.getElementById('lsTotalTradesVal').textContent = o.total_trades || 0;
        document.getElementById('lsAccuracyVal').textContent = ((o.model_accuracy || 0).toFixed(1)) + '%';
        document.getElementById('lsBestSignalVal').textContent = data.insights?.best_signal_type || '—';
        document.getElementById('lsWorstSignalVal').textContent = data.insights?.worst_signal_type || '—';
        document.getElementById('lsBestRegimeVal').textContent = data.insights?.best_regime || '—';

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
            tb.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:1.5rem;color:var(--text-muted)">No learning trades yet</td></tr>';
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
        const res = await fetch(`${API}/api/performance`);
        const data = await res.json();
        const m = data.metrics || {};

        // ── KPI cards ──────────────────────────────────
        const totalPnl = m.total_pnl || 0;
        const ret = m.total_return_pct || 0;
        const sharpe = m.sharpe_ratio || 0;
        const dd = m.max_drawdown_pct || 0;
        const pf = m.profit_factor || 0;

        _setKpi('pkCapitalVal', `₹${Math.round(m.capital || m.initial_capital || 100000).toLocaleString('en-IN')}`);
        _setKpi('pkPnlVal',     `${totalPnl >= 0 ? '+' : ''}₹${Math.round(totalPnl).toLocaleString('en-IN')}`, totalPnl >= 0 ? 'positive' : 'negative');
        _setKpi('pkReturnVal',  `${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%`, ret >= 0 ? 'positive' : 'negative');
        _setKpi('pkWinRateVal', `${(m.win_rate || 0).toFixed(1)}%`, (m.win_rate||0) >= 50 ? 'positive' : 'negative');
        _setKpi('pkTradesVal',  String(m.total_trades || 0));
        _setKpi('pkSharpeVal',  sharpe.toFixed(2), sharpe >= 1 ? 'positive' : sharpe >= 0 ? 'neutral' : 'negative');
        _setKpi('pkDrawdownVal',`${dd.toFixed(1)}%`, dd > 20 ? 'negative' : dd > 10 ? 'neutral' : 'positive');
        _setKpi('pkPFVal',      pf.toFixed(2), pf >= 1.5 ? 'positive' : pf >= 1 ? 'neutral' : 'negative');
        _setKpi('pkBestVal',    `+₹${(m.best_trade || 0).toFixed(0)}`, 'positive');
        _setKpi('pkWorstVal',   `₹${(m.worst_trade || 0).toFixed(0)}`, 'negative');

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
        const total = wins + losses || 1;
        const wPct = (wins / total * 100).toFixed(1);
        const lPct = (losses / total * 100).toFixed(1);
        document.getElementById('winBar').style.width   = `${wPct}%`;
        document.getElementById('lossBar').style.width  = `${lPct}%`;
        document.getElementById('winLabel').textContent  = `${wins} wins`;
        document.getElementById('lossLabel').textContent = `${losses} losses`;
        const wpEl = document.getElementById('perfWinPct');
        if (wpEl) wpEl.textContent = `${wPct}%`;
        document.getElementById('metAvgWin').textContent  = `₹${(m.avg_win  || 0).toFixed(0)}`;
        document.getElementById('metAvgLoss').textContent = `₹${(m.avg_loss || 0).toFixed(0)}`;

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
    const iconMap = { CLOSED_TP: '🎯', CLOSED_SL: '🛑', CLOSED_TRAIL: '📐', CLOSED_MANUAL: '✋', UNKNOWN: '❓' };
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
        const sign = m.pnl >= 0 ? '+' : '';
        return `<div class="perf-month-cell ${cls}" title="${label}: ${sign}₹${Math.abs(m.pnl).toLocaleString('en-IN')}" style="--intensity:${intPct}%">
            <span class="month-label">${label}</span>
            <span class="month-pnl">${sign}₹${Math.abs(m.pnl) >= 1000 ? (Math.abs(m.pnl)/1000).toFixed(1)+'K' : Math.abs(m.pnl).toFixed(0)}</span>
        </div>`;
    }).join('');
}

function _renderSymbolBreakdown(symbols) {
    const el = document.getElementById('perfSymbolBars');
    if (!el) return;
    if (!symbols.length) { el.innerHTML = '<p style="font-size:.75rem;color:var(--text-muted)">No symbol data yet</p>'; return; }

    const maxAbs = Math.max(...symbols.map(s => Math.abs(s.pnl)), 1);

    el.innerHTML = symbols.map(s => {
        const pct = Math.round(Math.abs(s.pnl) / maxAbs * 100);
        const cls = s.pnl >= 0 ? 'positive' : 'negative';
        const sign = s.pnl >= 0 ? '+' : '';
        return `<div class="perf-sym-row">
            <span class="perf-sym-name">${s.symbol}</span>
            <div class="perf-sym-bar-wrap"><div class="perf-sym-bar ${cls}" style="width:${pct}%"></div></div>
            <span class="perf-sym-pnl ${cls}">${sign}₹${Math.abs(s.pnl).toFixed(0)}</span>
            <span class="perf-sym-trades">${s.trades}T</span>
        </div>`;
    }).join('');
}

function _renderTradeHistory(trades) {
    const tbody = document.getElementById('perfTradesBody');
    const countEl = document.getElementById('perfTradeCount');
    if (!tbody) return;

    if (countEl) countEl.textContent = `${trades.length} trades`;

    if (!trades.length) {
        tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:1.5rem;color:var(--text-muted)">No closed trades yet</td></tr>';
        return;
    }

    const exitIcon = { CLOSED_TP: '🎯', CLOSED_SL: '🛑', CLOSED_TRAIL: '📐', CLOSED_MANUAL: '✋' };
    const exitLabel = { CLOSED_TP: 'TP', CLOSED_SL: 'SL', CLOSED_TRAIL: 'Trail', CLOSED_MANUAL: 'Manual' };

    tbody.innerHTML = trades.map(t => {
        const pnl = t.pnl || 0;
        const pnlCls = pnl >= 0 ? 'positive' : 'negative';
        const dirCls = t.direction === 'BUY' ? 'buy' : 'sell';
        const dt = t.exit_time ? new Date(t.exit_time).toLocaleString('en-IN', {
            day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'
        }) : '—';
        const sign = pnl >= 0 ? '+' : '';
        return `<tr>
            <td class="perf-td-id">#${t.id}</td>
            <td class="perf-td-sym">${t.symbol}</td>
            <td><span class="perf-dir-badge ${dirCls}">${t.direction}</span></td>
            <td class="mono">₹${(t.entry_price || 0).toFixed(2)}</td>
            <td class="mono">₹${(t.exit_price  || 0).toFixed(2)}</td>
            <td class="mono">${(t.quantity || 1).toFixed(2)}</td>
            <td class="mono ${pnlCls}">${sign}₹${Math.abs(pnl).toFixed(2)}</td>
            <td class="mono ${pnlCls}">${sign}${(t.pnl_pct || 0).toFixed(2)}%</td>
            <td><span class="perf-exit-badge">${exitIcon[t.status] || ''} ${exitLabel[t.status] || t.status || '—'}</span></td>
            <td class="perf-td-time">${dt}</td>
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

function initModal() {
    document.getElementById('modalBackdrop').addEventListener('click', (e) => {
        if (e.target.id === 'modalBackdrop') closeModal();
    });
    document.getElementById('modalClose').addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeModal();
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
            drawModalChart(data.data, interval);
            renderModalInfo(data.data, symbol, interval);
            loadModalBacktest(symbol);
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

    section.innerHTML = '<p style="color:var(--text-muted);font-size:0.8rem">Running backtest...</p>';

    try {
        const encodedSymbol = symbol.replace('/', '-');
        const res = await fetch(`${API}/api/backtest/${encodedSymbol}`);
        const data = await res.json();

        if (data.error) {
            section.innerHTML = `<p style="color:var(--text-muted);font-size:0.8rem">${data.error}</p>`;
            return;
        }

        const pfColor = data.profit_factor >= 1.5 ? 'var(--green)' : data.profit_factor >= 1 ? 'var(--yellow)' : 'var(--red)';
        const wrColor = data.win_rate >= 55 ? 'var(--green)' : data.win_rate >= 45 ? 'var(--yellow)' : 'var(--red)';

        section.innerHTML = `
            <div class="backtest-grid">
                <div class="bt-metric">
                    <span class="bt-val" style="color:${wrColor}">${data.win_rate}%</span>
                    <span class="bt-label">Win Rate</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val">${data.total_trades}</span>
                    <span class="bt-label">Trades</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val" style="color:${data.total_pnl >= 0 ? 'var(--green)' : 'var(--red)'}">₹${data.total_pnl >= 0 ? '+' : ''}${data.total_pnl.toLocaleString('en-IN', {maximumFractionDigits:0})}</span>
                    <span class="bt-label">Total PnL</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val" style="color:${pfColor}">${data.profit_factor}</span>
                    <span class="bt-label">Profit Factor</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val" style="color:var(--red)">${data.max_drawdown_pct}%</span>
                    <span class="bt-label">Max DD</span>
                </div>
                <div class="bt-metric">
                    <span class="bt-val">${data.sharpe_ratio}</span>
                    <span class="bt-label">Sharpe</span>
                </div>
            </div>
        `;
    } catch (err) {
        section.innerHTML = '<p style="color:var(--text-muted);font-size:0.8rem">Backtest unavailable</p>';
    }
}

function drawModalChart(data, interval) {
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
    ctx.fillText('Live · yfinance', W - PAD.right, H - 6);
}

function renderModalInfo(data, symbol, interval) {
    const last = data[data.length - 1];
    // Signal/Regime/Trend come from the live 5m signal cache (always most current)
    const sig = allSignals.find(s => s.symbol === symbol);
    const isCrypto = symbol.includes('/');
    const curr = isCrypto ? '$' : '₹';
    const tf = interval || currentModalInterval || '5m';

    const sigVal   = sig?.signal  || last.signal || 'HOLD';
    const sigColor = ['LONG','BUY'].includes(sigVal) ? 'var(--green)'
                   : ['SHORT','SELL'].includes(sigVal) ? 'var(--red)'
                   : sigVal === 'SKIP' ? 'var(--text-muted)'
                   : 'var(--cyan)';

    const items = [
        { label: 'Close',    value: `${curr}${Number(last.close).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` },
        { label: 'RSI',      value: last.rsi != null ? last.rsi.toFixed(1) : '—',
          color: last.rsi < 30 ? 'var(--green)' : last.rsi > 70 ? 'var(--red)' : null },
        { label: 'VWAP',     value: last.vwap     ? `${curr}${last.vwap.toFixed(2)}`    : '—' },
        { label: 'ADX',      value: last.adx      ? last.adx.toFixed(1)                 : '—' },
        { label: 'EMA 20',   value: last.ema_20   ? `${curr}${last.ema_20.toFixed(2)}`  : '—' },
        { label: 'EMA 50',   value: last.ema_50   ? `${curr}${last.ema_50.toFixed(2)}`  : '—' },
        { label: 'BB Upper', value: last.bb_upper ? `${curr}${last.bb_upper.toFixed(2)}`: '—' },
        { label: 'BB Lower', value: last.bb_lower ? `${curr}${last.bb_lower.toFixed(2)}`: '—' },
        { label: 'Signal',   value: sigVal, color: sigColor },
        { label: 'Confidence', value: sig?.confidence != null ? `${sig.confidence}%` : '—' },
        { label: 'Regime',   value: sig?.regime || '—' },
        { label: 'Trend',    value: (sig?.trend || '—').replace('TrendDirection.', '') },
        { label: 'TF',       value: tf.toUpperCase(), color: 'rgba(99,102,241,0.9)' },
    ];

    if (sig?.quality_score != null) {
        items.push({ label: 'Quality', value: `${sig.quality_score} ${sig.quality_grade || ''}`,
            color: sig.quality_grade === 'A' ? 'var(--green)' : sig.quality_grade === 'D' ? 'var(--red)' : null });
    }

    const grid = document.getElementById('modalInfoGrid');
    grid.innerHTML = items.map(item => `
        <div class="info-item">
            <span class="info-label">${item.label}</span>
            <span class="info-value" ${item.color ? `style="color:${item.color}"` : ''}>${item.value}</span>
        </div>
    `).join('');
}


// ── Clock ───────────────────────────────────────────────

function startClock() {
    const update = () => {
        const now = new Date();
        document.getElementById('clock').textContent = now.toLocaleTimeString('en-IN', {
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
        });
    };
    update();
    setInterval(update, 1000);
}


// ── Market Tab (Screener + Sectors) ─────────────────────

let screenerData = { gainers: [], losers: [] };
let currentScreenerMode = 'gainers';

async function loadMarket() {
    const loadSectors = async () => {
        const ac = new AbortController();
        const timeoutId = setTimeout(() => ac.abort(), 12_000);
        try {
            const sectorRes = await fetch(`${API}/api/sectors`, { signal: ac.signal });
            const sectors = await sectorRes.json();
            renderSectorHeatmap(sectors.sectors || []);
        } catch (err) {
            console.warn('Sector heatmap load error:', err);
            const grid = document.getElementById('sectorGrid');
            if (grid) grid.innerHTML = '<p style="color:var(--text-muted);padding:2rem;text-align:center;">Sector data unavailable</p>';
        } finally {
            clearTimeout(timeoutId);
        }
    };

    const loadScreenerData = async () => {
        // Use AbortController to enforce a 10-second timeout so the UI
        // doesn't hang indefinitely if yfinance is slow.
        const ac = new AbortController();
        const timeoutId = setTimeout(() => ac.abort(), 10_000);
        try {
            const [gainersRes, losersRes] = await Promise.all([
                fetch(`${API}/api/screener/gainers`, { signal: ac.signal }),
                fetch(`${API}/api/screener/losers`,  { signal: ac.signal }),
            ]);

            const gainers = await gainersRes.json();
            const losers  = await losersRes.json();

            screenerData.gainers = gainers.data || [];
            screenerData.losers  = losers.data  || [];

            renderScreener(screenerData[currentScreenerMode]);
        } catch (err) {
            if (err.name === 'AbortError') {
                console.warn('Screener timed out — showing retry state');
            } else {
                console.error('Screener load error:', err);
            }
            const tbody = document.getElementById('screenerBody');
            if (tbody) {
                tbody.innerHTML = '<tr><td colspan="5" class="empty-row" style="color:var(--text-muted)">⚠️ Screener data unavailable — <a href="#" onclick="loadMarket();return false;" style="color:var(--cyan);text-decoration:underline;cursor:pointer;">Retry</a></td></tr>';
            }
        } finally {
            clearTimeout(timeoutId);
        }
    };

    // Load both datasets independently so one slow API never blocks the other.
    await Promise.allSettled([loadSectors(), loadScreenerData()]);
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
            <tr>
                <td>
                    <div class="stock-symbol-cell">
                        <div class="stock-avatar" style="background:linear-gradient(135deg,${colors[colorIdx]},${colors[(colorIdx+3)%colors.length]})">${initials}</div>
                        <div>
                            <span class="stock-sym">${s.symbol}</span>
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
        const res = await fetch(`${API}/api/analytics/scorecard`);
        const data = await res.json();
        renderScorecard(data);
    } catch (err) {
        console.error('Scorecard load error:', err);
    }
}

function renderScorecard(data) {
    // ── Top KPI row ──────────────────────────────────────
    const total   = data.total_signals || 0;
    const wr      = total > 0 ? (data.overall_win_rate * 100).toFixed(1) + '%' : '—';
    const pending = data.pending_count ?? 0;

    _scKpi('scTotalSignals', total || '—');
    _scKpi('scWinRate', wr, total > 0 ? (data.overall_win_rate >= 0.55 ? 'var(--green)' : data.overall_win_rate >= 0.4 ? 'var(--yellow)' : 'var(--red)') : null);
    _scKpi('scPending', pending);

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


// ── Options Tab (PCR + Option Chain) ────────────────────

async function loadPCR() {
    const symbol = document.getElementById('pcrSymbol').value;
    try {
        const res = await fetchWithTimeout(`${API}/api/pcr/${encodeURIComponent(symbol)}`, {}, 10000);
        const data = await res.json();

        // ── Real data indicator banner ─────────────────
        let banner = document.getElementById('pcrDataBanner');
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'pcrDataBanner';
            banner.style.cssText = 'padding:0.4rem 0.75rem;border-radius:6px;font-size:0.78rem;font-weight:600;margin-bottom:0.75rem;';
            document.getElementById('pcrSummary').prepend(banner);
        }

        if (data.real_data === false) {
            banner.style.display = 'block';
            banner.style.background = 'rgba(239,68,68,0.12)';
            banner.style.color = '#ef4444';
            banner.style.border = '1px solid rgba(239,68,68,0.3)';
            banner.innerHTML = `
                ⚠️ <b>Option Chain OFFLINE</b> — NSE blocks Python (Akamai protection).
                ATM: <b>₹${data.atm?.toLocaleString('en-IN') || '—'}</b> (live).
                <br><span style="font-weight:400;font-size:0.73rem;opacity:0.85">
                Real OI data needs broker API: add <code>ZERODHA_API_KEY</code> or <code>ANGEL_API_KEY</code> in <code>.env</code>
                (see <code>.env.example</code> for instructions).
                </span>`;
        } else {
            banner.style.display = 'block';
            banner.style.background = 'rgba(16,185,129,0.1)';
            banner.style.color = '#10b981';
            banner.style.border = '1px solid rgba(16,185,129,0.25)';
            const expiry = data.expiry || 'N/A';
            const broker = data.source === 'NSE' ? 'NSE Direct' : data.source || 'Broker';
            banner.innerHTML = `✅ <b>Live option chain</b> — Source: <b>${broker}</b> | Expiry: <b>${expiry}</b> | Spot: <b>₹${(data.underlying || data.atm || 0).toLocaleString('en-IN')}</b>`;
        }

        // PCR value — only show if real data
        const pcrEl = document.getElementById('pcrValue');
        if (data.real_data === false) {
            pcrEl.textContent = 'N/A';
            pcrEl.style.color = 'var(--text-muted)';
        } else {
            const pcrVal = data.pcr || 0;
            pcrEl.textContent = pcrVal.toFixed(2);
            pcrEl.style.color = pcrVal > 1.2 ? 'var(--red)' : pcrVal < 0.8 ? 'var(--green)' : 'var(--yellow)';
        }

        // Sentiment
        const sent = data.sentiment || 'NEUTRAL';
        const sentEl = document.getElementById('pcrSentiment');
        sentEl.textContent = sent === 'UNAVAILABLE' ? 'DATA OFFLINE' : sent;
        sentEl.className = `pcr-sentiment ${sent === 'UNAVAILABLE' ? 'neutral' : sent.toLowerCase()}`;

        // Stats — show dashes when offline, real numbers when live
        const fmtOI = (v) => (data.real_data === false || !v) ? '—' : formatOI(v);
        document.getElementById('pcrCallOI').textContent  = fmtOI(data.total_call_oi);
        document.getElementById('pcrPutOI').textContent   = fmtOI(data.total_put_oi);
        document.getElementById('pcrMaxPain').textContent = (data.real_data === false || !data.max_pain) ? '—' : data.max_pain.toLocaleString('en-IN');
        document.getElementById('pcrATM').textContent     = data.atm ? data.atm.toLocaleString('en-IN') : '—';

        // PCR History Chart — only draw if we have real history points
        if (data.pcr_history && data.pcr_history.length >= 2) {
            drawPCRChart(data.pcr_history);
        }

        // Option Chain Table — hide when offline to avoid confusing zero rows
        if (data.real_data === false) {
            const tbody = document.getElementById('optionChainBody');
            tbody.innerHTML = `<tr><td colspan="7" class="empty-row" style="color:var(--text-muted)">⚠️ Option chain offline — NSE API unavailable</td></tr>`;
        } else {
            renderOptionChain(data.option_chain || [], data.atm);
        }

    } catch (err) {
        if (err.name === 'AbortError') {
            console.warn('PCR fetch timed out — showing unavailable state');
        } else {
            console.error('PCR load error:', err);
        }
        // Show graceful error — don't leave a spinning loader
        const tbody = document.getElementById('optionChainBody');
        if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="empty-row" style="color:var(--text-muted)">⚠️ Option chain data unavailable — NSE API may be blocked</td></tr>';
        const sentEl = document.getElementById('pcrSentiment');
        if (sentEl) { sentEl.textContent = 'OFFLINE'; sentEl.className = 'pcr-sentiment neutral'; }
    }
}

function formatOI(val) {
    if (!val) return '—';
    if (val >= 10000000) return (val / 10000000).toFixed(2) + ' Cr';
    if (val >= 100000) return (val / 100000).toFixed(2) + ' L';
    return val.toLocaleString('en-IN');
}

function drawPCRChart(history) {
    const canvas = document.getElementById('pcrChart');
    if (!canvas || history.length < 2) return;

    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = 80 * dpr;
    canvas.style.width = rect.width + 'px';
    canvas.style.height = '80px';
    ctx.scale(dpr, dpr);

    const W = rect.width;
    const H = 80;
    const pad = { top: 8, right: 10, bottom: 18, left: 35 };

    const vals = history.map(p => p.pcr);
    const minV = Math.min(...vals) * 0.95;
    const maxV = Math.max(...vals) * 1.05;
    const rangeV = maxV - minV || 1;

    const xStep = (W - pad.left - pad.right) / (history.length - 1);
    const toX = (i) => pad.left + i * xStep;
    const toY = (v) => pad.top + (1 - (v - minV) / rangeV) * (H - pad.top - pad.bottom);

    ctx.clearRect(0, 0, W, H);

    // Reference line at PCR = 1.0
    if (1.0 >= minV && 1.0 <= maxV) {
        const refY = toY(1.0);
        ctx.strokeStyle = 'rgba(245,158,11,0.3)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(pad.left, refY);
        ctx.lineTo(W - pad.right, refY);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = 'rgba(245,158,11,0.6)';
        ctx.font = '9px JetBrains Mono, monospace';
        ctx.textAlign = 'right';
        ctx.fillText('1.0', pad.left - 4, refY + 3);
    }

    // Gradient fill
    const gradient = ctx.createLinearGradient(0, pad.top, 0, H - pad.bottom);
    gradient.addColorStop(0, 'rgba(99, 102, 241, 0.25)');
    gradient.addColorStop(1, 'rgba(99, 102, 241, 0.02)');

    ctx.beginPath();
    ctx.moveTo(toX(0), toY(vals[0]));
    for (let i = 1; i < vals.length; i++) ctx.lineTo(toX(i), toY(vals[i]));
    ctx.lineTo(toX(vals.length - 1), H - pad.bottom);
    ctx.lineTo(toX(0), H - pad.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(vals[0]));
    for (let i = 1; i < vals.length; i++) ctx.lineTo(toX(i), toY(vals[i]));
    ctx.strokeStyle = '#6366f1';
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.stroke();

    // Time labels
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim() || '#6b7094';
    ctx.font = '8px JetBrains Mono, monospace';
    ctx.textAlign = 'center';
    for (let i = 0; i < history.length; i += Math.ceil(history.length / 5)) {
        ctx.fillText(history[i].time, toX(i), H - 4);
    }
}

function renderOptionChain(chain, atm) {
    const tbody = document.getElementById('optionChainBody');
    if (!chain || chain.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-row">No data</td></tr>';
        return;
    }

    tbody.innerHTML = chain.map(row => {
        const isATM = row.atm || row.strike === atm;
        const callChgClass = row.call_chg >= 0 ? 'oi-up' : 'oi-down';
        const putChgClass = row.put_chg >= 0 ? 'oi-up' : 'oi-down';

        return `
            <tr class="${isATM ? 'atm-row' : ''}">
                <td>${formatOI(row.call_oi)}</td>
                <td class="${callChgClass}">${row.call_chg >= 0 ? '+' : ''}${formatOI(Math.abs(row.call_chg))}</td>
                <td>₹${row.call_ltp.toFixed(1)}</td>
                <td class="strike-cell">${row.strike.toLocaleString('en-IN')}</td>
                <td>₹${row.put_ltp.toFixed(1)}</td>
                <td class="${putChgClass}">${row.put_chg >= 0 ? '+' : ''}${formatOI(Math.abs(row.put_chg))}</td>
                <td>${formatOI(row.put_oi)}</td>
            </tr>
        `;
    }).join('');
}


// ── Trade Flow Tab ──────────────────────────────────────

async function loadTradeFlow() {
    const grid = document.getElementById('tradeflowGrid');
    // Show a bounded loading state — will be replaced by error or real data
    grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Loading FII/DII flows...</p></div>';
    try {
        const res = await fetchWithTimeout(`${API}/api/tradeflow`, {}, 15000);
        const data = await res.json();
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
        BOTH_BUYING:        { label: '🟢 Both FII & DII Buying',   cls: 'buy' },
        BOTH_SELLING:       { label: '🔴 Both FII & DII Selling',  cls: 'sell' },
        DII_DOMINANT_BUY:   { label: '🟢 DII Dominant Buying',     cls: 'buy' },
        FII_DOMINANT_BUY:   { label: '🟢 FII Dominant Buying',     cls: 'buy' },
        FII_DOMINANT_SELL:  { label: '🔴 FII Dominant Selling',    cls: 'sell' },
        DII_DOMINANT_SELL:  { label: '🟡 DII Selling Pressure',    cls: 'neutral' },
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

    // Close on click outside
    document.addEventListener('click', (e) => {
        if (!wrap.contains(e.target)) closeSearchDropdown();
    });
}

async function searchSymbols(query) {
    const resultsEl = document.getElementById('searchResults');
    const dropdown = document.getElementById('searchDropdown');

    // Show "searching..." state immediately
    resultsEl.innerHTML = '<div class="search-empty" style="opacity:0.6">🔍 Searching NSE...</div>';
    dropdown.classList.add('visible');
    searchDropdownVisible = true;

    try {
        const res = await fetch(`${API}/api/search/symbols?q=${encodeURIComponent(query)}`);
        const data = await res.json();
        const results = data.results || [];

        const q = query.toUpperCase().trim();
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
                    ? `<span style="font-size:0.68rem;color:var(--green);font-family:var(--font-mono);font-weight:700;">₹${r.price.toLocaleString('en-IN')}</span>`
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
        resultsEl.innerHTML = '<div class="search-empty">Search failed — server error</div>';
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
        const res = await fetch(`${API}/api/search/quote/${encodeURIComponent(urlSym)}`);
        if (!res.ok) {
            const err = await res.json();
            titleEl.textContent = symbol.toUpperCase();
            infoGrid.innerHTML = `
                <div style="grid-column:1/-1;text-align:center;padding:2rem;">
                    <div style="font-size:2rem;margin-bottom:0.75rem;">⚠️</div>
                    <div style="color:var(--red);font-weight:600;margin-bottom:0.5rem;">Symbol Not Found</div>
                    <div style="color:var(--text-muted);font-size:0.8rem;">${err.error || 'Symbol not found on NSE or yFinance'}</div>
                    <div style="margin-top:1rem;font-size:0.75rem;color:var(--text-muted);">
                        💡 Make sure the NSE symbol is correct (e.g. <code style="color:var(--cyan)">PRECWIRE</code>, <code style="color:var(--cyan)">NAUKRI</code>)
                    </div>
                </div>`;
            return;
        }

        const data = await res.json();
        titleEl.textContent = data.symbol;

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
        const sigColor = ['LONG','BUY'].includes(sigVal)   ? 'var(--green)'
                       : ['SHORT','SELL'].includes(sigVal) ? 'var(--red)'
                       : sigVal === 'SKIP' ? 'var(--text-muted)'
                       : 'var(--cyan)';

        // MACD color
        const macdHist = data.macd_hist ?? last.macd_hist;
        const macdColor = macdHist > 0 ? 'var(--green)' : macdHist < 0 ? 'var(--red)' : 'var(--text-muted)';
        const macdIcon  = macdHist > 0 ? '▲' : macdHist < 0 ? '▼' : '—';

        // RSI color
        const rsiVal = data.rsi;
        const rsiColor = rsiVal < 30 ? 'var(--green)' : rsiVal > 70 ? 'var(--red)' : 'var(--text-secondary)';

        // Entry/SL/Target section
        const hasSignal = ['LONG', 'SHORT', 'BUY', 'SELL'].includes(sigDir);
        const entryVal  = sig?.entry ?? data.close;
        const slVal     = sig?.stop_loss;
        const tpVal     = sig?.target;

        const tradeSectionHTML = hasSignal && (slVal || tpVal) ? `
            <div class="info-item" style="grid-column:1/-1;border-top:1px solid var(--border-color);padding-top:0.6rem;margin-top:0.1rem;display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;">
                <div style="text-align:center">
                    <div style="font-size:0.65rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Entry</div>
                    <div style="font-size:0.92rem;font-weight:700;font-family:var(--font-mono);color:var(--text-primary);">${curr}${(entryVal||0).toFixed(2)}</div>
                </div>
                <div style="text-align:center">
                    <div style="font-size:0.65rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Stop Loss</div>
                    <div style="font-size:0.92rem;font-weight:700;font-family:var(--font-mono);color:var(--red);">${slVal ? curr + slVal.toFixed(2) : '—'}</div>
                </div>
                <div style="text-align:center">
                    <div style="font-size:0.65rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Target</div>
                    <div style="font-size:0.92rem;font-weight:700;font-family:var(--font-mono);color:var(--green);">${tpVal ? curr + tpVal.toFixed(2) : '—'}</div>
                </div>
            </div>` : '';

        const items = [
            { label: 'Close',      value: `${curr}${(data.close||0).toLocaleString('en-IN', { minimumFractionDigits:2, maximumFractionDigits:2 })}` },
            { label: 'RSI (14)',   value: rsiVal != null ? rsiVal.toFixed(1) : '—', color: rsiColor },
            { label: 'MACD Hist', value: macdHist != null ? `${macdIcon} ${Math.abs(macdHist).toFixed(4)}` : '—', color: macdColor },
            { label: 'Signal',     value: sigVal, color: sigColor },
            { label: 'Confidence', value: sig?.confidence != null ? `${sig.confidence}%` : '—' },
            { label: 'Regime',     value: sig?.regime || data.regime || '—' },
            { label: 'Trend',      value: (sig?.trend || '—').replace('TrendDirection.', '') },
            { label: 'EMA 20',     value: last.ema_20 ? `${curr}${last.ema_20.toFixed(2)}` : '—' },
            { label: 'EMA 50',     value: last.ema_50 ? `${curr}${last.ema_50.toFixed(2)}` : '—' },
            { label: 'VWAP',       value: last.vwap   ? `${curr}${last.vwap.toFixed(2)}` : '—' },
        ];

        infoGrid.innerHTML =
            items.map(item => `
                <div class="info-item">
                    <span class="info-label">${item.label}</span>
                    <span class="info-value" ${item.color ? `style="color:${item.color}"` : ''}>${item.value}</span>
                </div>
            `).join('') + tradeSectionHTML;

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

async function loadSwingScanner() {
    const grid = document.getElementById('swingGrid');
    grid.innerHTML = '<div class="loading-state"><div class="loader"></div><p>Scanning for swing setups...</p></div>';

    try {
        const res = await fetch(`${API}/api/swing-scanner`);
        const data = await res.json();
        const setups = data.setups || [];

        // Update KPIs
        document.getElementById('swingTotal').textContent = data.total_found || setups.length;
        document.getElementById('swingLong').textContent = setups.filter(s => s.direction === 'LONG').length;
        document.getElementById('swingShort').textContent = setups.filter(s => s.direction === 'SHORT').length;

        if (!setups.length) {
            grid.innerHTML = '<div class="loading-state"><p style="font-size:0.85rem;color:var(--text-muted)">No active swing setups found. Scanned ' + (data.symbols_scanned || 0) + ' symbols.</p></div>';
            return;
        }

        grid.innerHTML = setups.map(s => {
            const dirClass = s.direction === 'LONG' ? 'buy' : 'sell';
            const dirColor = s.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
            const strengthColor = s.strength >= 75 ? '#10b981' : s.strength >= 55 ? '#6366f1' : '#f59e0b';
            const strengthPct = Math.min(100, s.strength);

            return `
            <div class="card" style="padding:1rem;border-left:3px solid ${dirColor};position:relative;overflow:hidden;">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.75rem;">
                    <div>
                        <div style="display:flex;align-items:center;gap:0.5rem;">
                            <span style="font-weight:800;font-size:1.1rem;color:var(--text-primary);">${s.symbol}</span>
                            <span class="signal-badge ${dirClass}" style="font-size:0.7rem;padding:2px 8px;">${s.direction}</span>
                        </div>
                        <div style="font-size:0.72rem;color:var(--text-muted);margin-top:3px;">${s.pattern}</div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:0.65rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">Strength</div>
                        <div style="font-weight:800;font-size:1.2rem;color:${strengthColor};font-family:var(--font-mono);">${s.strength}</div>
                    </div>
                </div>
                <!-- Strength bar -->
                <div style="height:4px;background:rgba(255,255,255,0.06);border-radius:2px;margin-bottom:0.75rem;">
                    <div style="height:100%;width:${strengthPct}%;background:${strengthColor};border-radius:2px;transition:width 0.5s;"></div>
                </div>
                <!-- Levels -->
                <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:0.5rem;margin-bottom:0.75rem;">
                    <div style="text-align:center;">
                        <div style="font-size:0.62rem;color:var(--text-muted);text-transform:uppercase;">Entry</div>
                        <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--text-primary);">₹${s.entry_zone}</div>
                    </div>
                    <div style="text-align:center;">
                        <div style="font-size:0.62rem;color:var(--text-muted);text-transform:uppercase;">SL</div>
                        <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--red);">₹${s.stop_loss}</div>
                    </div>
                    <div style="text-align:center;">
                        <div style="font-size:0.62rem;color:var(--text-muted);text-transform:uppercase;">T1</div>
                        <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--green);">₹${s.target_1}</div>
                    </div>
                    <div style="text-align:center;">
                        <div style="font-size:0.62rem;color:var(--text-muted);text-transform:uppercase;">T2</div>
                        <div style="font-weight:700;font-family:var(--font-mono);font-size:0.85rem;color:var(--green);">₹${s.target_2}</div>
                    </div>
                </div>
                <!-- Meta -->
                <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.72rem;">
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

    } catch (err) {
        console.error('Swing scanner error:', err);
        grid.innerHTML = '<div class="loading-state"><p style="color:var(--red)">Failed to load swing setups</p></div>';
    }
}


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
    body.innerHTML = '<tr><td colspan="9" class="empty-row"><div class="loader" style="margin:0 auto;"></div> Scanning for breakouts...</td></tr>';

    try {
        const res = await fetch(`${API}/api/breakouts`);
        _breakoutData = await res.json();

        // KPIs
        document.getElementById('boTotal').textContent = _breakoutData.total_breakouts || 0;
        document.getElementById('boGradeA').textContent = _breakoutData.grade_a_count || 0;
        document.getElementById('boGradeB').textContent = _breakoutData.grade_b_count || 0;
        const w52 = (_breakoutData.week_52_high || []).length + (_breakoutData.week_52_low || []).length;
        document.getElementById('bo52W').textContent = w52;

        renderBreakoutsTable(_breakoutData.all || []);

    } catch (err) {
        console.error('Breakout scanner error:', err);
        body.innerHTML = '<tr><td colspan="9" class="empty-row" style="color:var(--red);">Failed to load breakouts</td></tr>';
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
        body.innerHTML = '<tr><td colspan="9" class="empty-row">No breakouts found in this category</td></tr>';
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

// Make filterBreakouts available globally for onclick
window.filterBreakouts = filterBreakouts;
window.loadIndexMover = loadIndexMover;

// Override switchTab to also sync bottom nav
const _originalSwitchTab = switchTab;
switchTab = function(tab) {
    _originalSwitchTab(tab);
    updateBottomNav(tab);
};

// Initialize bottom nav on load
document.addEventListener('DOMContentLoaded', () => {
    initBottomNav();
});

