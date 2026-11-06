/**
 * TradeAI PRO — skin switch.
 *
 * Enables the opt-in Liquid Glass skin without touching the default theme.
 * Priority: ?skin=glass URL param  →  localStorage  →  default.
 *
 * The attribute is set before first paint (this file is loaded in <head>), so
 * there is no flash of the wrong theme. Toggling stores the choice.
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'tradeai_skin';
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
                ? 'Liquid Glass: on — switch to classic'
                : 'Liquid Glass: off — switch to glass';
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
    // Liquid Glass is ON by default unless explicitly set to 'default'/'classic'
    var initial = (requested === 'default' || requested === 'classic')
        ? 'default'
        : (requested === 'glass'
            ? 'glass'
            : (stored === 'default' || stored === 'classic' ? 'default' : 'glass'));

    apply(initial);

    function bind() {
        var button = document.getElementById('skinToggle');
        if (!button) return;
        button.addEventListener('click', function () {
            apply(root.getAttribute('data-skin') === 'glass' ? 'default' : 'glass');
        });
        // Reflect the current state on the button after binding.
        apply(root.getAttribute('data-skin') === 'glass' ? 'glass' : 'default');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bind);
    } else {
        bind();
    }
})();
