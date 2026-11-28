/**
 * TradeAI PRO — skin switch (Obsidian Matte default / Obsidian Frost toggle).
 *
 * Enables the opt-in Frost skin without breaking dark/light tokens.
 * Priority: ?skin=glass|default URL param  →  localStorage  →  Obsidian Matte ('default').
 *
 * Loaded in <head> before first paint so there is no flash of the wrong surface.
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'tradeai_skin_v2';
    var root = document.documentElement;

    function readStored() {
        try {
            return window.localStorage.getItem(STORAGE_KEY);
        } catch (e) {
            return null;
        }
    }

    function store(value) {
        try {
            window.localStorage.setItem(STORAGE_KEY, value);
        } catch (e) {
            /* private mode / quota — the skin still applies for this page load */
        }
    }

    function apply(value) {
        var glass = value === 'glass';
        if (glass) {
            root.setAttribute('data-skin', 'glass');
        } else {
            root.removeAttribute('data-skin');
        }
        store(glass ? 'glass' : 'default');

        var button = document.getElementById('skinToggle');
        if (button) {
            button.setAttribute('aria-pressed', glass ? 'true' : 'false');
            button.title = glass
                ? 'Obsidian Frost: on — switch to Matte Terminal'
                : 'Obsidian Matte: on — switch to Frost';
            var icon = document.getElementById('skinIcon');
            if (icon) icon.textContent = glass ? '✦' : '✧';
        }

        // Let other modules (chart theming, etc.) react without polling.
        try {
            document.dispatchEvent(new CustomEvent('tradeai:skinchange', {
                detail: { skin: glass ? 'glass' : 'default' }
            }));
        } catch (e) {
            /* CustomEvent unsupported — charts simply keep the previous palette */
        }
    }

    var params = new URLSearchParams(window.location.search);
    var requested = params.get('skin');
    var stored = readStored();
    // Obsidian Matte ('default') is active by default; Frost ('glass') is opt-in.
    var initial = (requested === 'glass')
        ? 'glass'
        : ((requested === 'default' || requested === 'classic' || requested === 'matte')
            ? 'default'
            : (stored === 'glass' ? 'glass' : 'default'));

    apply(initial);

    function bind() {
        var button = document.getElementById('skinToggle');
        if (!button) return;
        button.addEventListener('click', function () {
            apply(root.getAttribute('data-skin') === 'glass' ? 'default' : 'glass');
        });
        apply(root.getAttribute('data-skin') === 'glass' ? 'glass' : 'default');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bind);
    } else {
        bind();
    }
})();
