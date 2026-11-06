/**
 * Public track-record page.
 *
 * Fetches /api/track-record and renders every closed paper trade, including
 * losers. No filtering, no "best of" — the point of the page is that it can
 * be checked.
 */
(function () {
    'use strict';

    var API = '/api/track-record';

    function esc(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
        });
    }

    var inr = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });

    function money(value) {
        var n = Number(value || 0);
        return (n < 0 ? '-' : '') + '₹' + inr.format(Math.abs(n));
    }

    function signedMoney(value) {
        var n = Number(value || 0);
        return (n > 0 ? '+' : '') + money(n);
    }

    function pct(value) {
        if (value == null || Number.isNaN(Number(value))) return '—';
        return Number(value).toFixed(1) + '%';
    }

    function setText(id, text) {
        var el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    function renderKpis(data) {
        setText('kpiTrades', String(data.total_trades || 0));
        setText('kpiWinRate', pct(data.win_rate_pct));
        setText('kpiPnl', signedMoney(data.pnl));
        setText('kpiPf', data.profit_factor == null ? '—' : Number(data.profit_factor).toFixed(2));
        setText('kpiDrawdown', pct(data.max_drawdown_pct));
        setText('kpiExpectancy', signedMoney((Number(data.pnl || 0) / Math.max(Number(data.total_trades || 1), 1))));

        var pnlEl = document.getElementById('kpiPnl');
        if (pnlEl) pnlEl.className = 'kpi-value ' + (Number(data.pnl) >= 0 ? 'pos' : 'neg');

        var sample = document.getElementById('sampleBadge');
        if (sample) {
            if (data.sample_status === 'VALID') {
                sample.textContent = 'Sample valid · ' + data.total_trades + ' trades';
                sample.className = 'badge badge-ok';
            } else {
                sample.textContent = 'Insufficient sample · ' + (data.total_trades || 0) + ' / ' + (data.min_sample || 100) + ' trades';
                sample.className = 'badge badge-warn';
            }
        }

        setText('asOf', data.as_of ? new Date(data.as_of).toLocaleString('en-IN', {
            day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'
        }) : '—');

        var capital = document.getElementById('capitalLine');
        if (capital && data.initial_capital != null) {
            capital.textContent = 'Starting capital: ' + money(data.initial_capital) + ' · paper only';
        }
    }

    function renderTable(tbodyId, rows, mapper) {
        var body = document.getElementById(tbodyId);
        if (!body) return;
        if (!rows || rows.length === 0) {
            body.innerHTML = '<tr><td colspan="10" class="empty">No data yet</td></tr>';
            return;
        }
        body.innerHTML = rows.map(mapper).join('');
    }

    function renderPerYear(data) {
        renderTable('yearBody', (data.per_year || []).slice().reverse(), function (row) {
            return '<tr>' +
                '<td>' + esc(row.year) + '</td>' +
                '<td>' + esc(row.total_trades) + '</td>' +
                '<td>' + pct(row.win_rate_pct) + '</td>' +
                '<td class="' + (row.pnl >= 0 ? 'pos' : 'neg') + '">' + signedMoney(row.pnl) + '</td>' +
                '<td>' + (row.profit_factor == null ? '—' : Number(row.profit_factor).toFixed(2)) + '</td>' +
                '</tr>';
        });
    }

    function renderPerStrategy(data) {
        renderTable('strategyBody', data.per_strategy || [], function (row) {
            return '<tr>' +
                '<td>' + esc(row.strategy) + '</td>' +
                '<td>' + esc(row.total_trades) + '</td>' +
                '<td>' + pct(row.win_rate_pct) + '</td>' +
                '<td class="' + (row.pnl >= 0 ? 'pos' : 'neg') + '">' + signedMoney(row.pnl) + '</td>' +
                '</tr>';
        });
    }

    function renderTrades(data) {
        var rows = (data.trades || []).slice().reverse();
        renderTable('tradeBody', rows, function (t) {
            var dir = String(t.direction || '').toUpperCase();
            var dirClass = (dir === 'LONG' || dir === 'BUY') ? 'dir-long' : 'dir-short';
            var exitTime = t.exit_time ? new Date(t.exit_time).toLocaleDateString('en-IN', {
                day: '2-digit', month: 'short', year: '2-digit'
            }) : '—';
            return '<tr>' +
                '<td>' + esc(exitTime) + '</td>' +
                '<td><b>' + esc(t.symbol) + '</b></td>' +
                '<td><span class="' + dirClass + '">' + esc(dir || '—') + '</span></td>' +
                '<td>' + esc(t.strategy || '—') + '</td>' +
                '<td>' + Number(t.quantity || 0).toFixed(2) + '</td>' +
                '<td>' + money(t.entry_price) + '</td>' +
                '<td>' + money(t.exit_price) + '</td>' +
                '<td class="' + (Number(t.pnl) >= 0 ? 'pos' : 'neg') + '"><b>' + signedMoney(t.pnl) + '</b></td>' +
                '<td class="' + (Number(t.pnl) >= 0 ? 'pos' : 'neg') + '">' + (t.return_pct == null ? '—' : pct(t.return_pct)) + '</td>' +
                '<td>' + esc(t.exit_reason || '—') + '</td>' +
                '</tr>';
        });

        var count = document.getElementById('tradeCount');
        if (count) count.textContent = rows.length + ' closed trades';
    }

    function showError(message) {
        var el = document.getElementById('errorState');
        if (el) {
            el.style.display = 'block';
            el.textContent = message;
        }
    }

    function render(data) {
        if (!data || data.status === 'unavailable') {
            showError('Track record is temporarily unavailable. The dashboard is still up; this page needs the paper-trading database.');
            return;
        }
        if (!data.trades || data.trades.length === 0) {
            showError('No closed paper trades yet. This page fills in as trades close — nothing is hidden.');
        }
        renderKpis(data);
        renderPerYear(data);
        renderPerStrategy(data);
        renderTrades(data);
    }

    function load() {
        fetch(API, { headers: { 'Accept': 'application/json' } })
            .then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.json();
            })
            .then(render)
            .catch(function (err) {
                showError('Could not load the track record: ' + err.message);
            });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', load);
    } else {
        load();
    }
})();
