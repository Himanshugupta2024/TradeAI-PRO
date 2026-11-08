/**
 * TradeAI PRO — Institutional flow payload helpers.
 * Reads the official NSE /api/tradeflow shape (flows[].category / flows[].net_cr).
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory();
    } else {
        root.TradeflowHelpers = factory();
    }
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    function getInstitutionalFlowNet(flowData, institution) {
        const flows = Array.isArray(flowData?.flows) ? flowData.flows : [];
        const requested = String(institution || '').trim().toUpperCase();
        const aliases = requested === 'FII/FPI' || requested === 'FII' || requested === 'FPI'
            ? ['FII/FPI', 'FII', 'FPI']
            : [requested];
        const entry = flows.find((flow) =>
            aliases.includes(String(flow?.category || '').trim().toUpperCase())
        );
        const raw = entry?.net_cr;
        if (raw == null || raw === '') return null;
        const value = Number(raw);
        return Number.isFinite(value) ? value : null;
    }

    function formatInstitutionalNetCr(value) {
        if (value == null || value === '') return '—';
        const amount = Number(value);
        if (!Number.isFinite(amount)) return '—';
        const sign = amount > 0 ? '+' : (amount < 0 ? '-' : '');
        return `${sign}₹${Math.abs(amount).toFixed(2)} Cr`;
    }

    return { getInstitutionalFlowNet, formatInstitutionalNetCr };
}));
