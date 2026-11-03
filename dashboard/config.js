/**
 * TradeAI PRO — Frontend Environment Configuration.
 * Immutable runtime configuration for multi-cloud deployments.
 */
(function () {
    const isLocalhost = Boolean(
        window.location.hostname === 'localhost' ||
        window.location.hostname === '127.0.0.1' ||
        window.location.hostname === '[::1]' ||
        window.location.port === '5500' ||
        window.location.port === '3000' ||
        window.location.port === '5173'
    );

    const isCloudflarePages = window.location.hostname.endsWith('pages.dev');
    const isVercel = window.location.hostname.endsWith('vercel.app');

    let apiBase = '';
    let wsBase = window.location.host;
    let wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

    if (isLocalhost && (window.location.port === '5500' || window.location.port === '3000' || window.location.port === '5173')) {
        apiBase = 'http://127.0.0.1:8000';
        wsBase = '127.0.0.1:8000';
    } else if (isCloudflarePages || isVercel) {
        apiBase = 'https://trading-api-n1wt.onrender.com';
        wsBase = 'trading-api-n1wt.onrender.com';
        wsProto = 'wss:';
    }

    window.__TRADEAI_CONFIG__ = Object.freeze({
        API_BASE_URL: apiBase,
        WS_URL: `${wsProto}//${wsBase}/ws/prices`,
        ENVIRONMENT: isLocalhost ? 'development' : 'production',
        VERSION: '4.1.0'
    });
})();
