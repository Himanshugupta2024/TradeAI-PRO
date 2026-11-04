"""
Patch script for frontend Trade History clickability, detail modal enrichment, and accessibility.
"""

def patch_app_js():
    with open('dashboard/app.js', 'r', encoding='utf-8') as f:
        code = f.read()

    # 1. Update loadPortfolio caching of _cachedTradeHistory
    old_load = """        const hBody = document.getElementById('historyBody');
        const hCount = document.getElementById('historyTradeCount');
        const rawTrades = trades.trades || [];
        const closedTrades = rawTrades.filter(t => t.status !== 'OPEN');"""

    new_load = """        const hBody = document.getElementById('historyBody');
        const hCount = document.getElementById('historyTradeCount');
        const rawTrades = trades.trades || [];
        window._cachedTradeHistory = rawTrades;
        const closedTrades = rawTrades.filter(t => t.status !== 'OPEN');"""

    if old_load in code:
        code = code.replace(old_load, new_load, 1)
        print("Patched loadPortfolio trade caching")

    # 2. Add onclick to historyTable rows
    old_row = """                    return `
                        <tr>
                            <td class="perf-td-id" style="font-family: var(--font-mono); color: var(--text-muted);">#${t.id || '—'}</td>"""

    new_row = """                    return `
                        <tr onclick="openPositionDetail(${t.id})" style="cursor:pointer;" title="Click to view complete trade analytics and execution details">
                            <td class="perf-td-id" style="font-family: var(--font-mono); color: var(--text-muted);">#${t.id || '—'}</td>"""

    if old_row in code:
        code = code.replace(old_row, new_row, 1)
        print("Patched historyTable clickable rows")

    # 3. Add onclick to perfTradesBody rows
    old_perf_row = """        return `<tr>
            <td class="perf-td-id">#${t.id || '—'}</td>"""

    new_perf_row = """        return `<tr onclick="openPositionDetail(${t.id})" style="cursor:pointer;" title="Click to view complete trade analytics and execution details">
            <td class="perf-td-id">#${t.id || '—'}</td>"""

    if old_perf_row in code:
        code = code.replace(old_perf_row, new_perf_row, 1)
        print("Patched perfTradesBody clickable rows")

    # 4. Enhance openPositionDetail to support closed trades and fallback lookup
    old_detail_start = """// ── Open Position Detail Modal ───────────────────────────────
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

    if (!trade) {
        if (typeof showToast === 'function') {
            showToast('Position detail not found or trade already closed.', 'warning');
        } else {
            alert('Position detail not found or trade already closed.');
        }
        return;
    }"""

    new_detail_start = """// ── Open Position Detail Modal ───────────────────────────────
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
    }"""

    if old_detail_start in code:
        code = code.replace(old_detail_start, new_detail_start, 1)
        print("Patched openPositionDetail start and lookup")

    # 4b. Enhance body of openPositionDetail for closed trades
    old_detail_calc = """    const mult = isLong ? 1 : -1;
    const pnl = (ltp - entry) * qty * mult;
    const pnlPct = entry > 0 ? ((ltp - entry) / entry) * 100 * mult : 0;
    const pnlSign = pnl >= 0 ? '+' : '';
    const pnlBg = pnl >= 0 ? 'rgba(16, 185, 129, 0.08)' : 'rgba(239, 68, 68, 0.08)';
    const pnlBorder = pnl >= 0 ? 'rgba(16, 185, 129, 0.25)' : 'rgba(239, 68, 68, 0.25)';
    const pnlColor = pnl >= 0 ? 'var(--green, #10b981)' : 'var(--red, #ef4444)';

    // Distances
    const slDistPct = entry > 0 ? Math.abs((ltp - sl) / ltp * 100) : 0;
    const tpDistPct = ltp > 0 && tp > 0 ? (isLong ? ((tp - ltp) / ltp * 100) : ((ltp - tp) / ltp * 100)) : 0;

    // Duration
    const duration = trade.time_in_trade_human || _fmtDuration(trade.entry_time, new Date().toISOString());

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
        subMeta.textContent = `${trade.signal_type || 'AUTONOMOUS'} · ${trade.regime || 'MARKET'} Regime · Duration: ${duration}`;
    }

    // Render Rich Body
    const bodyEl = document.getElementById('posModalBody');
    if (bodyEl) {
        bodyEl.innerHTML = `
            <!-- Live P&L Card -->
            <div style="background:${pnlBg};border:1px solid ${pnlBorder};border-radius:10px;padding:14px 16px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;">
                <div>
                    <div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">Floating Unrealized P&amp;L</div>
                    <div style="font-size:1.5rem;font-weight:800;font-family:var(--font-mono);line-height:1.2;margin-top:2px;color:${pnlColor};">
                        ${pnlSign}${currSym}${Math.abs(pnl).toFixed(2)}
                        <span style="font-size:0.95rem;font-weight:600;margin-left:6px;">(${pnlSign}${pnlPct.toFixed(2)}%)</span>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">Status</div>
                    <div style="font-size:0.9rem;font-weight:700;color:var(--green);margin-top:4px;">● ACTIVE RUNNING</div>
                </div>
            </div>"""

    new_detail_calc = """    const isClosed = trade.status && trade.status !== 'OPEN';
    const exitPx = Number(trade.exit_price || ltp);
    const mult = isLong ? 1 : -1;
    const pnl = isClosed
        ? Number(trade.total_pnl != null ? trade.total_pnl : ((trade.pnl || 0) * qty))
        : (ltp - entry) * qty * mult;
    const pnlPct = isClosed
        ? Number(trade.pnl_pct || (entry > 0 ? ((exitPx - entry) / entry * 100 * mult) : 0))
        : (entry > 0 ? ((ltp - entry) / entry) * 100 * mult : 0);
    const pnlSign = pnl >= 0 ? '+' : '';
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
            </div>"""

    if old_detail_calc in code:
        code = code.replace(old_detail_calc, new_detail_calc, 1)
        print("Patched openPositionDetail calculation and status")

    # 4c. Update middle level card and Exit button logic
    old_middle_card = """                <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);padding:10px 12px;border-radius:8px;">
                    <div style="font-size:0.68rem;color:var(--text-muted);text-transform:uppercase;">Live Market LTP</div>
                    <div style="font-size:1.05rem;font-weight:700;font-family:var(--font-mono);color:#38bdf8;margin-top:2px;">${currSym}${ltp.toFixed(2)}</div>
                    <div style="font-size:0.65rem;color:#38bdf8;margin-top:2px;">Real-Time</div>
                </div>"""

    new_middle_card = """                <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);padding:10px 12px;border-radius:8px;">
                    <div style="font-size:0.68rem;color:var(--text-muted);text-transform:uppercase;">${isClosed ? 'Exit Price' : 'Live Market LTP'}</div>
                    <div style="font-size:1.05rem;font-weight:700;font-family:var(--font-mono);color:#38bdf8;margin-top:2px;">${currSym}${isClosed ? exitPx.toFixed(2) : ltp.toFixed(2)}</div>
                    <div style="font-size:0.65rem;color:${isClosed ? 'var(--text-muted)' : '#38bdf8'};margin-top:2px;">${isClosed ? (trade.exit_time ? 'Closed ' + new Date(trade.exit_time).toLocaleTimeString('en-IN', {hour:'2-digit', minute:'2-digit'}) : 'Closed') : 'Real-Time'}</div>
                </div>"""

    if old_middle_card in code:
        code = code.replace(old_middle_card, new_middle_card, 1)
        print("Patched openPositionDetail middle card")

    old_exit_logic = """    const exitBtn = document.getElementById('posModalExitBtn');
    if (exitBtn) {
        exitBtn.onclick = () => {
            closePositionDetailModal();
            if (typeof closePaperTrade === 'function') {
                closePaperTrade(trade.id, ltp);
            }
        };
    }"""

    new_exit_logic = """    const exitBtn = document.getElementById('posModalExitBtn');
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
    }"""

    if old_exit_logic in code:
        code = code.replace(old_exit_logic, new_exit_logic, 1)
        print("Patched openPositionDetail exit button toggle")

    with open('dashboard/app.js', 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(code)
    print("Finished patching dashboard/app.js")

def patch_index_html():
    with open('dashboard/index.html', 'rb') as f:
        html = f.read()

    replacements = [
        (b'id="globalSearchInput" type="text"', b'id="globalSearchInput" type="text" aria-label="Search stock symbol"'),
        (b'id="signalSortSelect" class="terminal-select"', b'id="signalSortSelect" class="terminal-select" aria-label="Sort signals"'),
        (b'id="p5TraderNotes" class="p5-trader-notes"', b'id="p5TraderNotes" class="p5-trader-notes" aria-label="Trader notes and journal"'),
        (b'id="screenerSearch" type="text"', b'id="screenerSearch" type="text" aria-label="Search screener symbols"'),
        (b'id="idFilterLong" onchange="filterIntradayStrat()"', b'id="idFilterLong" onchange="filterIntradayStrat()" aria-label="Filter long signals only"'),
        (b'id="indexMoverSelect" onchange="loadIndexMover()"', b'id="indexMoverSelect" onchange="loadIndexMover()" aria-label="Select benchmark index"'),
        (b'id="eeSearchInput" class="ee-search-input"', b'id="eeSearchInput" class="ee-search-input" aria-label="Search edge engine assets"'),
        (b'id="inputLearningCapital" value="100000"', b'id="inputLearningCapital" value="100000" aria-label="Learning capital seed amount"'),
        (b'id="bmcDonorName" class="bmc-donor-input"', b'id="bmcDonorName" class="bmc-donor-input" aria-label="Supporter name or social handle"'),
    ]

    for old, new in replacements:
        if old in html:
            html = html.replace(old, new, 1)
            print(f"Patched aria-label for {old[:25]}")
        else:
            print(f"WARN: not found: {old}")

    with open('dashboard/index.html', 'wb') as f:
        f.write(html)
    print("Finished patching dashboard/index.html")

if __name__ == '__main__':
    patch_app_js()
    patch_index_html()
