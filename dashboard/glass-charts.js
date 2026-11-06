/**
 * TradeAI PRO — Chart.js palette bridge for the Liquid Glass skin.
 *
 * The dashboard has a handful of Chart.js instances plus custom canvas
 * renderers. This module only handles the Chart.js ones: when the glass skin
 * is on it softens grid/axis colours so charts sit inside the frosted panels;
 * when it is off it restores the exact options each chart started with.
 *
 * It never changes data, scales, or chart types — colours only.
 */
(function () {
    'use strict';

    if (!window.Chart) return;

    var Chart = window.Chart;

    var baseline = {
        color: Chart.defaults.color,
        borderColor: Chart.defaults.borderColor,
        grid: Chart.defaults.scale && Chart.defaults.scale.grid ? Chart.defaults.scale.grid.color : undefined,
        ticks: Chart.defaults.scale && Chart.defaults.scale.ticks ? Chart.defaults.scale.ticks.color : undefined
    };

    var snapshots = new WeakMap();

    function isGlass() {
        return document.documentElement.getAttribute('data-skin') === 'glass';
    }

    function isLight() {
        return document.documentElement.getAttribute('data-theme') === 'light';
    }

    function palette() {
        if (!isGlass()) return null;
        return isLight()
            ? { text: '#475569', grid: 'rgba(15,23,42,0.08)', border: 'rgba(15,23,42,0.10)' }
            : { text: '#94a3b8', grid: 'rgba(255,255,255,0.07)', border: 'rgba(255,255,255,0.10)' };
    }

    function setDefaults(p) {
        if (p) {
            Chart.defaults.color = p.text;
            Chart.defaults.borderColor = p.border;
            if (Chart.defaults.scale && Chart.defaults.scale.grid) {
                Chart.defaults.scale.grid.color = p.grid;
            }
            if (Chart.defaults.scale && Chart.defaults.scale.ticks) {
                Chart.defaults.scale.ticks.color = p.text;
            }
            return;
        }
        Chart.defaults.color = baseline.color;
        Chart.defaults.borderColor = baseline.borderColor;
        if (Chart.defaults.scale && Chart.defaults.scale.grid) {
            Chart.defaults.scale.grid.color = baseline.grid;
        }
        if (Chart.defaults.scale && Chart.defaults.scale.ticks) {
            Chart.defaults.scale.ticks.color = baseline.ticks;
        }
    }

    function snapshot(chart) {
        if (snapshots.has(chart)) return;
        var record = { color: chart.options.color, scales: {}, legend: null };
        var scales = chart.options.scales || {};
        Object.keys(scales).forEach(function (key) {
            var scale = scales[key] || {};
            record.scales[key] = {
                grid: scale.grid ? Object.assign({}, scale.grid) : null,
                ticks: scale.ticks ? Object.assign({}, scale.ticks) : null,
                border: scale.border ? Object.assign({}, scale.border) : null
            };
        });
        var legend = chart.options.plugins && chart.options.plugins.legend;
        if (legend && legend.labels) {
            record.legend = Object.assign({}, legend.labels);
        }
        snapshots.set(chart, record);
    }

    function applyToChart(chart, p) {
        snapshot(chart);
        var record = snapshots.get(chart);

        if (p) {
            chart.options.color = p.text;
            var scales = chart.options.scales || {};
            Object.keys(scales).forEach(function (key) {
                var scale = scales[key];
                scale.grid = Object.assign({}, scale.grid, { color: p.grid });
                scale.ticks = Object.assign({}, scale.ticks, { color: p.text });
                scale.border = Object.assign({}, scale.border, { color: p.border });
            });
            var legend = chart.options.plugins && chart.options.plugins.legend;
            if (legend && legend.labels) {
                legend.labels.color = p.text;
            }
        } else {
            chart.options.color = record.color;
            var current = chart.options.scales || {};
            Object.keys(current).forEach(function (key) {
                var original = record.scales[key];
                if (!original) return;
                if (original.grid) current[key].grid = original.grid;
                if (original.ticks) current[key].ticks = original.ticks;
                if (original.border) current[key].border = original.border;
            });
            var legendOff = chart.options.plugins && chart.options.plugins.legend;
            if (legendOff && legendOff.labels && record.legend) {
                legendOff.labels = record.legend;
            }
        }

        try {
            chart.update('none');
        } catch (e) {
            /* chart may be mid-destroy; ignore */
        }
    }

    function applyAll() {
        var p = palette();
        setDefaults(p);
        var instances = Chart.instances || {};
        Object.keys(instances).forEach(function (id) {
            if (instances[id]) applyToChart(instances[id], p);
        });
    }

    document.addEventListener('tradeai:skinchange', applyAll);

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', applyAll);
    } else {
        applyAll();
    }

    // Theme toggles and skin changes both land on the <html> element.
    if (window.MutationObserver) {
        new MutationObserver(applyAll).observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['data-skin', 'data-theme']
        });
    }
})();
