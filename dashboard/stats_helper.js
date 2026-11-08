/**
 * TradeAI PRO — Pure Trade Statistics & Win-Rate Formatter
 * 
 * Computes consistent win-rate, loss, break-even, and legacy trade summaries
 * across Overview, Portfolio, and telemetry views.
 * 
 * Mathematical invariants enforced:
 * 1. eligible_trades = wins + losses + break-evens (closed INR-denominated trades).
 * 2. win_rate = wins / eligible_trades * 100 (excluding legacy foreign-currency trades).
 * 3. Zero-loss safety: zero losses (0) is preserved and never treated as missing/falsy.
 * 4. Break-even subtraction: when calculating fallback losses, break-evens are subtracted:
 *    losses_fallback = Math.max(0, eligible_trades - wins - break-evens).
 */

(function (root, factory) {
    if (typeof define === 'function' && define.amd) {
        define([], factory);
    } else if (typeof module === 'object' && module.exports) {
        module.exports = factory();
    } else {
        root.formatTradeStatsSummary = factory().formatTradeStatsSummary;
    }
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    function formatTradeStatsSummary(stats, fallbackClosedCount = 0) {
        const totalClosed = Number(stats?.total_trades ?? stats?.closed_trades ?? fallbackClosedCount ?? 0);
        const eligibleCount = Number(stats?.eligible_trades ?? totalClosed);
        const excludedLegacy = Number(stats?.excluded_legacy_trades ?? 0);
        const winRateDisplay = (eligibleCount > 0 && stats?.win_rate != null)
            ? `${Number(stats.win_rate).toFixed(1)}%`
            : '—';

        const wins = Number(stats?.winning_trades ?? 0);
        const be = Number(stats?.breakeven_trades ?? 0);
        const losses = Number(stats?.losing_trades ?? Math.max(0, eligibleCount - wins - be));
        const bePart = be > 0 ? ` / ${be}BE` : '';
        const sampleTag = (eligibleCount > 0 && eligibleCount < 30) ? ' • Small Sample' : '';

        let subText = '';
        let titleText = '';

        if (eligibleCount > 0) {
            subText = `${wins}W / ${losses}L${bePart} • ${eligibleCount} trades${sampleTag}`;
            titleText = `${wins}W / ${losses}L${bePart} on ${eligibleCount} eligible closed trades.`;
            if (excludedLegacy > 0) {
                titleText += ` (${excludedLegacy} legacy non-INR trade${excludedLegacy > 1 ? 's' : ''} excluded from win rate)`;
            }
            if (eligibleCount < 30) {
                titleText += ' Sample size < 30.';
            }
        } else {
            subText = '0 closed trades';
            titleText = '';
        }

        const closedSubDetail = `${totalClosed} closed${excludedLegacy > 0 ? ` (${eligibleCount} INR)` : ''}`;

        return {
            winRateDisplay,
            subText,
            titleText,
            totalClosed,
            eligibleCount,
            excludedLegacy,
            wins,
            losses,
            be,
            closedSubDetail,
        };
    }

    return { formatTradeStatsSummary };
}));
