/**
 * TradeAI PRO — data honesty helpers.
 *
 * The dashboard ships with empty placeholders. This script makes the UI state
 * explicit when the backend does not deliver: no stale hardcoded numbers.
 * It never fabricates a value and respects the backend feed_source.
 */
(function () {
    'use strict';

    var PRICE_IDS = [
        'macroBrentPrice',
        'macroWtiPrice',
        'macroYieldPrice',
        'macroDxyPrice',
        'macroSp500Price',
        'macroGoldPrice'
    ];

    function readPrice(id) {
        var el = document.getElementById(id);
        if (!el) return null;
        var value = (el.textContent || '').trim();
        return value && value !== '\u2014' ? value : null;
    }

    function hasMacroData() {
        for (var i = 0; i < PRICE_IDS.length; i += 1) {
            if (readPrice(PRICE_IDS[i])) return true;
        }
        return false;
    }

    function istTime() {
        try {
            return new Date().toLocaleTimeString('en-IN', {
                hour: '2-digit',
                minute: '2-digit',
                hour12: false,
                timeZone: 'Asia/Kolkata'
            });
        } catch (e) {
            return new Date().toTimeString().slice(0, 5);
        }
    }

    function stampMacroState() {
        var timestamp = document.getElementById('macroTimestamp');
        var dot = document.querySelector('.macro-live-dot');
        var time = istTime();

        // 1. If real macro data object exists from API, use its true source & timestamp
        if (window._lastMacroData) {
            var source = window._lastMacroData.feed_source || 'LIVE';
            var ts = window._lastMacroData.timestamp;
            var timeStr = '';
            if (ts) {
                try {
                    timeStr = new Date(ts).toLocaleTimeString('en-IN', {
                        hour: '2-digit',
                        minute: '2-digit',
                        hour12: false,
                        timeZone: 'Asia/Kolkata'
                    });
                } catch (_) {}
            }
            if (!timeStr) timeStr = time;

            if (timestamp) {
                if (source === 'LIVE') {
                    timestamp.textContent = 'as of ' + timeStr + ' IST';
                } else {
                    timestamp.textContent = 'est. ' + timeStr + ' IST';
                }
            }
            if (dot) {
                dot.style.background = source === 'LIVE' ? '#10b981' : '#f59e0b';
                dot.setAttribute('title', source === 'LIVE' ? 'Live Exchange Feed' : 'Fallback Market Estimate');
            }
            return;
        }

        if (hasMacroData()) {
            if (timestamp) timestamp.textContent = 'as of ' + time + ' IST';
            if (dot) dot.style.background = '#10b981';
            return;
        }

        if (timestamp) timestamp.textContent = 'No live feed \u2022 last attempt ' + time + ' IST';
        if (dot) dot.style.background = '#ef4444';
    }

    function watchMacroStrip() {
        var strip = document.querySelector('.macro-ticker-strip');
        if (!strip || typeof window.MutationObserver !== 'function') return;
        var pending = null;
        var observer = new window.MutationObserver(function () {
            if (pending) return;
            pending = window.setTimeout(function () {
                pending = null;
                stampMacroState();
            }, 250);
        });
        observer.observe(strip, { subtree: true, childList: true, characterData: true });
    }

    function init() {
        watchMacroStrip();
        window.setTimeout(stampMacroState, 2000);
        window.setTimeout(stampMacroState, 8000);
        window.setTimeout(stampMacroState, 20000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
