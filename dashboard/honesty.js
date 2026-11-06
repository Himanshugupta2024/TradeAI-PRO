/**
 * TradeAI PRO — data honesty helpers.
 *
 * The dashboard ships with empty placeholders. This script makes the UI state
 * explicit when the backend does not deliver: no stale hardcoded numbers, no
 * "MONITORING" badge over an empty panel. It never fabricates a value.
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
        var cue = document.getElementById('macroCuePill');
        var time = istTime();

        if (hasMacroData()) {
            if (timestamp) timestamp.textContent = 'as of ' + time + ' IST';
            if (cue) {
                cue.textContent = 'LIVE';
                cue.style.background = 'rgba(16,185,129,0.12)';
                cue.style.color = '#10b981';
                cue.style.borderColor = 'rgba(16,185,129,0.25)';
            }
            return;
        }

        if (timestamp) timestamp.textContent = 'No live feed \u2022 last attempt ' + time + ' IST';
        if (cue) {
            cue.textContent = 'DATA UNAVAILABLE';
            cue.style.background = 'rgba(239,68,68,0.12)';
            cue.style.color = '#ef4444';
            cue.style.borderColor = 'rgba(239,68,68,0.3)';
        }
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
        // Give the first data fetch time to land, then label the panel honestly.
        window.setTimeout(stampMacroState, 12000);
        window.setTimeout(stampMacroState, 30000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
