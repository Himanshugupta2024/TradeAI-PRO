(function () {
  'use strict';

  let _modalEl = null;
  let _activeMode = 'oauth'; // 'oauth' | 'token' | 'totp'
  let _onSuccessCb = null;

  function _apiBase() {
    if (window.__TRADEAI_CONFIG__ && typeof window.__TRADEAI_CONFIG__.API_BASE_URL === 'string') {
      return window.__TRADEAI_CONFIG__.API_BASE_URL.replace(/\/$/, '');
    }
    return '';
  }

  function _storedAdminKey() {
    try {
      return sessionStorage.getItem('tradeai_admin_key') || '';
    } catch (_) {
      return '';
    }
  }

  function _extractAuthCode(raw) {
    const val = (raw || '').trim();
    if (!val) return '';
    if (val.includes('code=')) {
      try {
        const u = new URL(val.startsWith('http') ? val : `https://dummy.local/${val.replace(/^\?/, '?')}`);
        const c = (u.searchParams.get('code') || '').trim();
        if (c) return c;
      } catch (_) {}
      const m = val.match(/[?&]?code=([^&\s#]+)/i);
      if (m && m[1]) return decodeURIComponent(m[1]).trim();
    }
    return val;
  }

  function _ensureModal() {
    if (_modalEl) return _modalEl;
    const wrap = document.createElement('div');
    wrap.id = 'shoonyaLoginModal';
    wrap.style.cssText = [
      'display:none',
      'position:fixed',
      'inset:0',
      'background:rgba(4,7,14,0.82)',
      'z-index:10050',
      'align-items:center',
      'justify-content:center',
      'padding:16px',
      'backdrop-filter:blur(6px)',
    ].join(';');

    wrap.innerHTML = `
      <div style="background:var(--surface-1,#0e1118);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:10px;padding:1.35rem;max-width:480px;width:100%;box-shadow:var(--shadow-lg,0 25px 50px -12px rgba(0,0,0,0.75));color:var(--text-1,#f1f5f9);font-family:var(--font-sans,Inter,system-ui,sans-serif);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.55rem;">
          <h3 style="margin:0;font-size:1.05rem;font-weight:700;display:flex;align-items:center;gap:8px;color:var(--text-1,#f1f5f9);">
            <span>⚡</span> Shoonya Live Feed Connect
          </h3>
          <button type="button" id="shCloseBtn" aria-label="Close Shoonya modal" style="background:none;border:none;color:var(--text-2,#94a3b8);font-size:1.35rem;cursor:pointer;line-height:1;">&times;</button>
        </div>
        <p style="margin:0 0 0.75rem;color:var(--text-2,#94a3b8);font-size:0.78rem;line-height:1.45;">
          Paper trading mode stays active (market-data ticks only, zero live orders). Credentials are sent once to Box C memory and never saved in browser storage.
        </p>

        <div id="shStatusBanner" style="display:flex;align-items:center;justify-content:space-between;gap:8px;background:var(--surface-2,#141923);border:1px solid var(--border-subtle,rgba(148,163,184,0.14));border-radius:8px;padding:0.55rem 0.75rem;margin-bottom:0.85rem;font-size:0.75rem;">
          <span id="shStatusText" style="color:var(--text-2,#94a3b8);word-break:break-word;">Status: Not checked yet</span>
          <button type="button" id="shRefreshStatusBtn" style="flex-shrink:0;background:var(--surface-3,#1b2230);color:var(--text-1,#f1f5f9);border:1px solid var(--border-subtle,rgba(148,163,184,0.14));border-radius:5px;padding:3px 8px;font-size:0.72rem;cursor:pointer;">Check</button>
        </div>

        <div style="display:flex;gap:6px;margin-bottom:0.85rem;">
          <button type="button" id="shTabOauth" style="flex:1.1;padding:6px 8px;border-radius:6px;border:1px solid var(--border-accent,rgba(34,211,238,0.4));background:var(--accent-brand-soft,rgba(34,211,238,0.12));color:var(--accent-brand,#22d3ee);font-size:0.74rem;font-weight:600;cursor:pointer;">OAuth Login</button>
          <button type="button" id="shTabToken" style="flex:1;padding:6px 8px;border-radius:6px;border:1px solid var(--border-subtle,rgba(148,163,184,0.16));background:var(--surface-2,#141923);color:var(--text-2,#94a3b8);font-size:0.74rem;font-weight:600;cursor:pointer;">Daily Token</button>
          <button type="button" id="shTabTotp" style="flex:0.9;padding:6px 8px;border-radius:6px;border:1px solid var(--border-subtle,rgba(148,163,184,0.16));background:var(--surface-2,#141923);color:var(--text-2,#94a3b8);font-size:0.74rem;font-weight:600;cursor:pointer;">Legacy TOTP</button>
        </div>

        <form id="shLoginForm" autocomplete="off">
          <div id="shAdminKeyRow" style="margin-bottom:0.6rem;">
            <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">Operator Admin Key (X-Admin-Key)</label>
            <input type="password" id="shAdminKey" autocomplete="off" placeholder="Enter Admin Key" style="width:100%;box-sizing:border-box;padding:0.5rem 0.7rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
          </div>

          <div id="shOauthFields" style="display:block;margin-bottom:0.65rem;">
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.55rem;margin-bottom:0.55rem;">
              <div>
                <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">OAuth Client ID</label>
                <input type="text" id="shOauthClientId" autocomplete="off" spellcheck="false" placeholder="e.g. FN243453_U (blank = env)" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
              </div>
              <div>
                <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">Secret Code</label>
                <input type="password" id="shOauthSecret" autocomplete="off" placeholder="Blank = keep Box C env" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
              </div>
            </div>
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:0.55rem;padding:0.5rem 0.65rem;background:var(--surface-2,#141923);border:1px solid var(--border-subtle,rgba(148,163,184,0.16));border-radius:6px;">
              <span style="font-size:0.72rem;color:var(--text-2,#94a3b8);line-height:1.35;">Step 1: Log in at Shoonya — you redirect right back here with <code style="color:var(--accent-brand,#22d3ee);">?code=...</code>.</span>
              <button type="button" id="shOpenOauthBtn" style="flex-shrink:0;padding:0.42rem 0.7rem;background:var(--accent-brand-soft,rgba(34,211,238,0.15));color:var(--accent-brand,#22d3ee);border:1px solid var(--border-accent,rgba(34,211,238,0.4));border-radius:6px;font-size:0.74rem;font-weight:700;cursor:pointer;">Open OAuth ↗</button>
            </div>
            <div>
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">Step 2: OAuth Code or Full Redirect URL</label>
              <input type="text" id="shAuthCode" autocomplete="off" spellcheck="false" placeholder="Auto-filled on redirect, or paste ?code=... URL" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
            </div>
          </div>

          <div id="shTotpFields" style="display:none;grid-template-columns:1fr 1fr;gap:0.55rem;margin-bottom:0.65rem;">
            <div>
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">Shoonya User ID</label>
              <input type="text" id="shUserId" autocomplete="off" spellcheck="false" placeholder="e.g. FA12345 (blank = keep env)" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
            </div>
            <div>
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">Password</label>
              <input type="password" id="shPassword" autocomplete="new-password" placeholder="Blank = keep env" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
            </div>
            <div>
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">Vendor Code</label>
              <input type="text" id="shVendorCode" autocomplete="off" placeholder="e.g. FA12345_U" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
            </div>
            <div>
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">API Key (Secret)</label>
              <input type="password" id="shApiKey" autocomplete="off" placeholder="Blank = keep env" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
            </div>
            <div style="grid-column:1 / -1;">
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">6-Digit OTP or TOTP Secret</label>
              <input type="password" id="shTotp" autocomplete="one-time-code" placeholder="Enter 6-digit authenticator OTP or Base32 secret" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
            </div>
          </div>

          <div id="shTokenFields" style="display:none;margin-bottom:0.65rem;">
            <div style="margin-bottom:0.5rem;">
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">Shoonya User ID</label>
              <input type="text" id="shTokenUserId" autocomplete="off" spellcheck="false" placeholder="e.g. FN243453 (blank = keep env)" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
            </div>
            <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:var(--text-2,#94a3b8);margin-bottom:3px;">Shoonya Daily susertoken</label>
            <input type="password" id="shUserToken" autocomplete="off" placeholder="Paste today's susertoken" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:var(--surface-input,#0b0e14);border:1px solid var(--border-strong,rgba(148,163,184,0.24));border-radius:6px;color:var(--text-1,#f1f5f9);font-size:0.82rem;outline:none;">
          </div>

          <div id="shFeedbackMsg" style="display:none;font-size:0.76rem;line-height:1.4;margin-bottom:0.75rem;padding:0.45rem 0.65rem;border-radius:6px;"></div>

          <div style="display:flex;justify-content:flex-end;gap:0.6rem;">
            <button type="button" id="shCancelBtn" style="padding:0.5rem 0.9rem;background:var(--surface-3,#1b2230);color:var(--text-2,#94a3b8);border:1px solid var(--border-subtle,rgba(148,163,184,0.14));border-radius:6px;cursor:pointer;font-size:0.82rem;font-weight:600;">Cancel</button>
            <button type="submit" id="shSubmitBtn" style="padding:0.5rem 1.15rem;background:var(--bull,#10b981);color:#042f2e;border:none;border-radius:6px;cursor:pointer;font-size:0.82rem;font-weight:700;">Connect Live Feed</button>
          </div>
        </form>
      </div>
    `;

    document.body.appendChild(wrap);
    _modalEl = wrap;

    wrap.addEventListener('click', (e) => {
      if (e.target === wrap) window.closeShoonyaLoginModal();
    });
    document.getElementById('shCloseBtn').addEventListener('click', window.closeShoonyaLoginModal);
    document.getElementById('shCancelBtn').addEventListener('click', window.closeShoonyaLoginModal);
    document.getElementById('shRefreshStatusBtn').addEventListener('click', () => window.refreshShoonyaStatus());
    document.getElementById('shTabOauth').addEventListener('click', () => _setMode('oauth'));
    document.getElementById('shTabToken').addEventListener('click', () => _setMode('token'));
    document.getElementById('shTabTotp').addEventListener('click', () => _setMode('totp'));
    document.getElementById('shOpenOauthBtn').addEventListener('click', _openOauthPage);
    document.getElementById('shLoginForm').addEventListener('submit', _submitShoonyaLogin);
    return wrap;
  }

  function _openOauthPage() {
    const cidEl = document.getElementById('shOauthClientId');
    let clientId = cidEl ? cidEl.value.trim() : '';
    if (!clientId) {
      const uidFallback = (document.getElementById('shUserId')?.value || '').trim();
      clientId = uidFallback ? (uidFallback.endsWith('_U') ? uidFallback : `${uidFallback}_U`) : 'FN243453_U';
    }
    const oauthUrl = `https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id=${encodeURIComponent(clientId)}`;
    const popup = window.open(oauthUrl, '_blank', 'noopener');
    if (!popup) {
      window.location.href = oauthUrl;
    }
  }

  function _setMode(mode) {
    _activeMode = mode === 'token' ? 'token' : (mode === 'totp' ? 'totp' : 'oauth');
    const oauthBtn = document.getElementById('shTabOauth');
    const tokBtn = document.getElementById('shTabToken');
    const totpBtn = document.getElementById('shTabTotp');
    const oauthBox = document.getElementById('shOauthFields');
    const tokBox = document.getElementById('shTokenFields');
    const totpBox = document.getElementById('shTotpFields');

    const activeCss = (flex) => `flex:${flex};padding:6px 8px;border-radius:6px;border:1px solid var(--border-accent,rgba(34,211,238,0.4));background:var(--accent-brand-soft,rgba(34,211,238,0.12));color:var(--accent-brand,#22d3ee);font-size:0.74rem;font-weight:600;cursor:pointer;`;
    const idleCss = (flex) => `flex:${flex};padding:6px 8px;border-radius:6px;border:1px solid var(--border-subtle,rgba(148,163,184,0.16));background:var(--surface-2,#141923);color:var(--text-2,#94a3b8);font-size:0.74rem;font-weight:600;cursor:pointer;`;

    oauthBox.style.display = _activeMode === 'oauth' ? 'block' : 'none';
    tokBox.style.display = _activeMode === 'token' ? 'block' : 'none';
    totpBox.style.display = _activeMode === 'totp' ? 'grid' : 'none';

    oauthBtn.style.cssText = _activeMode === 'oauth' ? activeCss('1.1') : idleCss('1.1');
    tokBtn.style.cssText = _activeMode === 'token' ? activeCss('1') : idleCss('1');
    totpBtn.style.cssText = _activeMode === 'totp' ? activeCss('0.9') : idleCss('0.9');
  }

  function _showFeedback(msg, isError) {
    const el = document.getElementById('shFeedbackMsg');
    if (!el) return;
    el.textContent = msg;
    el.style.display = 'block';
    el.style.background = isError ? 'rgba(239,68,68,0.14)' : 'rgba(16,185,129,0.14)';
    el.style.color = isError ? '#fca5a5' : '#6ee7b7';
    el.style.border = isError ? '1px solid rgba(239,68,68,0.35)' : '1px solid rgba(16,185,129,0.35)';
  }

  function _clearSensitiveInputs() {
    ['shPassword', 'shApiKey', 'shOauthSecret', 'shAuthCode', 'shTotp', 'shUserToken'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
  }

  async function _submitShoonyaLogin(e) {
    e.preventDefault();
    const keyInput = document.getElementById('shAdminKey');
    const adminKey = (keyInput && keyInput.value.trim()) || _storedAdminKey();
    if (!adminKey) {
      _showFeedback('Operator Admin Key is required before connecting live feed.', true);
      if (keyInput) keyInput.focus();
      return;
    }

    const payload = {};
    if (_activeMode === 'oauth') {
      const clientId = (document.getElementById('shOauthClientId')?.value || '').trim();
      const secret = (document.getElementById('shOauthSecret')?.value || '').trim();
      const code = _extractAuthCode(document.getElementById('shAuthCode')?.value || '');
      if (clientId) {
        payload.vendor_code = clientId;
        if (clientId.toUpperCase().endsWith('_U')) {
          payload.userid = clientId.slice(0, -2);
        }
      }
      if (secret) payload.api_key = secret;
      if (!code) {
        _showFeedback('Click "Open OAuth ↗" first, or paste the Shoonya ?code=... redirect URL.', true);
        return;
      }
      payload.auth_code = code;
    } else if (_activeMode === 'token') {
      payload.usertoken = (document.getElementById('shUserToken')?.value || '').trim();
      const uid = (document.getElementById('shTokenUserId')?.value || document.getElementById('shUserId')?.value || '').trim();
      if (uid) payload.userid = uid;
      if (!payload.usertoken) {
        _showFeedback('Please paste a Shoonya susertoken.', true);
        return;
      }
    } else {
      payload.userid = (document.getElementById('shUserId')?.value || '').trim();
      payload.password = (document.getElementById('shPassword')?.value || '').trim();
      payload.vendor_code = (document.getElementById('shVendorCode')?.value || '').trim();
      payload.api_key = (document.getElementById('shApiKey')?.value || '').trim();
      payload.totp = (document.getElementById('shTotp')?.value || '').replace(/\s+/g, '');
    }

    const btn = document.getElementById('shSubmitBtn');
    const origText = btn ? btn.textContent : 'Connect Live Feed';
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Connecting...';
    }
    try {
      const res = await fetch(`${_apiBase()}/api/admin/feed/shoonya-login`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'X-Admin-Key': adminKey,
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      }
      try {
        sessionStorage.setItem('tradeai_admin_key', adminKey);
      } catch (_) {}
      _clearSensitiveInputs();
      _showFeedback(data.message || 'Shoonya live feed connected!', false);
      await window.refreshShoonyaStatus(adminKey);
      if (typeof _onSuccessCb === 'function') {
        try { _onSuccessCb(data); } catch (_) {}
      }
    } catch (err) {
      _showFeedback(`Connection failed: ${err.message}`, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }
  }

  window.refreshShoonyaStatus = async function (explicitKey) {
    const statusEl = document.getElementById('shStatusText');
    const keyInput = document.getElementById('shAdminKey');
    const adminKey = explicitKey || (keyInput && keyInput.value.trim()) || _storedAdminKey();
    if (!statusEl) return null;
    if (!adminKey) {
      statusEl.textContent = 'Enter Admin Key above and click Check';
      statusEl.style.color = '#94a3b8';
      return null;
    }
    statusEl.textContent = 'Checking Box C status...';
    try {
      const res = await fetch(`${_apiBase()}/api/admin/feed/shoonya-status`, {
        headers: { 'Accept': 'application/json', 'X-Admin-Key': adminKey },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        statusEl.textContent = `Status check error (${res.status}): ${data.detail || 'Unauthorized'}`;
        statusEl.style.color = '#fca5a5';
        return null;
      }
      const state = String(data.feed_state || 'UNKNOWN').toUpperCase();
      const uid = data.masked_uid ? ` · UID ${data.masked_uid}` : '';
      const ticks = data.total_ticks != null ? ` · ${data.total_ticks} ticks` : '';
      const syms = data.watchlist_count ? ` · ${data.watchlist_count} symbols` : '';
      const ipNote = data.egress_ip ? ` · Box C IP ${data.egress_ip}` : '';
      statusEl.textContent = `Box C: ${state}${uid}${syms}${ticks}${ipNote}`;
      statusEl.style.color = state === 'LIVE' ? '#34d399' : (state === 'SIMULATED' ? '#fbbf24' : '#cbd5e1');

      const cidInput = document.getElementById('shOauthClientId');
      if (cidInput && !cidInput.value.trim() && data.oauth_client_id) {
        cidInput.value = data.oauth_client_id;
      }
      if (data.last_error && state !== 'LIVE') {
        _showFeedback(`Last feed note: ${data.last_error}`, true);
      }
      return data;
    } catch (err) {
      statusEl.textContent = `Box C check failed: ${err.message}`;
      statusEl.style.color = '#fca5a5';
      return null;
    }
  };

  window.openShoonyaLoginModal = function (opts) {
    _onSuccessCb = opts && typeof opts.onSuccess === 'function' ? opts.onSuccess : null;
    const modal = _ensureModal();
    const keyInput = document.getElementById('shAdminKey');
    const keyRow = document.getElementById('shAdminKeyRow');
    const saved = _storedAdminKey();
    if (keyInput && saved) {
      keyInput.value = saved;
    }
    if (keyRow) {
      keyRow.style.display = saved && (opts && opts.hideAdminKeyIfPresent) ? 'none' : 'block';
    }
    const fb = document.getElementById('shFeedbackMsg');
    if (fb) fb.style.display = 'none';
    _setMode((opts && opts.mode) || _activeMode || 'oauth');
    modal.style.display = 'flex';
    if (saved) {
      window.refreshShoonyaStatus(saved);
    }
  };

  window.closeShoonyaLoginModal = function () {
    _clearSensitiveInputs();
    if (_modalEl) {
      _modalEl.style.display = 'none';
    }
  };

  function _checkOAuthRedirectCode() {
    try {
      const params = new URLSearchParams(window.location.search || '');
      const code = (params.get('code') || '').trim();
      if (!code) return;
      params.delete('code');
      const remaining = params.toString();
      const cleanUrl = window.location.pathname + (remaining ? `?${remaining}` : '') + (window.location.hash || '');
      window.history.replaceState({}, document.title, cleanUrl);
      window.openShoonyaLoginModal({ mode: 'oauth' });
      const codeEl = document.getElementById('shAuthCode');
      if (codeEl) codeEl.value = code;
      _showFeedback('Shoonya OAuth code captured! Click "Connect Live Feed" (enter Admin Key if blank).', false);
    } catch (_) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _checkOAuthRedirectCode);
  } else {
    _checkOAuthRedirectCode();
  }
})();
