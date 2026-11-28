(function () {
  'use strict';

  let _modalEl = null;
  let _activeMode = 'totp'; // 'totp' | 'token'
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
      <div style="background:#0f172a;border:1px solid #334155;border-radius:14px;padding:1.35rem;max-width:460px;width:100%;box-shadow:0 25px 50px -12px rgba(0,0,0,0.75);color:#f8fafc;font-family:Inter,system-ui,sans-serif;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.55rem;">
          <h3 style="margin:0;font-size:1.05rem;font-weight:700;display:flex;align-items:center;gap:8px;">
            <span>⚡</span> Shoonya Live Feed Connect
          </h3>
          <button type="button" id="shCloseBtn" aria-label="Close Shoonya modal" style="background:none;border:none;color:#94a3b8;font-size:1.35rem;cursor:pointer;line-height:1;">&times;</button>
        </div>
        <p style="margin:0 0 0.75rem;color:#94a3b8;font-size:0.78rem;line-height:1.45;">
          Paper trading mode stays active (market-data ticks only, zero live orders). Credentials are sent once to Box C memory and never saved in browser storage.
        </p>

        <div id="shStatusBanner" style="display:flex;align-items:center;justify-content:space-between;gap:8px;background:#1e293b;border:1px solid #334155;border-radius:8px;padding:0.55rem 0.75rem;margin-bottom:0.85rem;font-size:0.76rem;">
          <span id="shStatusText" style="color:#cbd5e1;">Status: Not checked yet</span>
          <button type="button" id="shRefreshStatusBtn" style="background:#334155;color:#e2e8f0;border:none;border-radius:5px;padding:3px 8px;font-size:0.72rem;cursor:pointer;">Check</button>
        </div>

        <div style="display:flex;gap:6px;margin-bottom:0.85rem;">
          <button type="button" id="shTabTotp" style="flex:1;padding:6px 10px;border-radius:6px;border:1px solid #6366f1;background:rgba(99,102,241,0.2);color:#e0e7ff;font-size:0.76rem;font-weight:600;cursor:pointer;">ID + Password + TOTP</button>
          <button type="button" id="shTabToken" style="flex:1;padding:6px 10px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#94a3b8;font-size:0.76rem;font-weight:600;cursor:pointer;">Daily User Token</button>
        </div>

        <form id="shLoginForm" autocomplete="off">
          <div id="shAdminKeyRow" style="margin-bottom:0.6rem;">
            <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:#94a3b8;margin-bottom:3px;">Operator Admin Key (X-Admin-Key)</label>
            <input type="password" id="shAdminKey" autocomplete="off" placeholder="Enter Admin Key" style="width:100%;box-sizing:border-box;padding:0.5rem 0.7rem;background:#1e293b;border:1px solid #475569;border-radius:6px;color:#f8fafc;font-size:0.82rem;outline:none;">
          </div>

          <div id="shTotpFields" style="display:grid;grid-template-columns:1fr 1fr;gap:0.55rem;margin-bottom:0.65rem;">
            <div>
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:#94a3b8;margin-bottom:3px;">Shoonya User ID</label>
              <input type="text" id="shUserId" autocomplete="off" spellcheck="false" placeholder="e.g. FA12345 (blank = keep env)" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:#1e293b;border:1px solid #475569;border-radius:6px;color:#f8fafc;font-size:0.82rem;outline:none;">
            </div>
            <div>
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:#94a3b8;margin-bottom:3px;">Password</label>
              <input type="password" id="shPassword" autocomplete="new-password" placeholder="Blank = keep env" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:#1e293b;border:1px solid #475569;border-radius:6px;color:#f8fafc;font-size:0.82rem;outline:none;">
            </div>
            <div>
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:#94a3b8;margin-bottom:3px;">Vendor Code</label>
              <input type="text" id="shVendorCode" autocomplete="off" placeholder="e.g. FA12345_U" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:#1e293b;border:1px solid #475569;border-radius:6px;color:#f8fafc;font-size:0.82rem;outline:none;">
            </div>
            <div>
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:#94a3b8;margin-bottom:3px;">API Key (Secret)</label>
              <input type="password" id="shApiKey" autocomplete="off" placeholder="Blank = keep env" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:#1e293b;border:1px solid #475569;border-radius:6px;color:#f8fafc;font-size:0.82rem;outline:none;">
            </div>
            <div style="grid-column:1 / -1;">
              <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:#94a3b8;margin-bottom:3px;">6-Digit OTP or TOTP Secret</label>
              <input type="password" id="shTotp" autocomplete="one-time-code" placeholder="Enter 6-digit authenticator OTP or Base32 secret" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:#1e293b;border:1px solid #475569;border-radius:6px;color:#f8fafc;font-size:0.82rem;outline:none;">
            </div>
          </div>

          <div id="shTokenFields" style="display:none;margin-bottom:0.65rem;">
            <label style="display:block;font-size:0.68rem;font-weight:700;text-transform:uppercase;color:#94a3b8;margin-bottom:3px;">Shoonya Daily susertoken</label>
            <input type="password" id="shUserToken" autocomplete="off" placeholder="Paste today's susertoken" style="width:100%;box-sizing:border-box;padding:0.5rem 0.65rem;background:#1e293b;border:1px solid #475569;border-radius:6px;color:#f8fafc;font-size:0.82rem;outline:none;">
          </div>

          <div id="shFeedbackMsg" style="display:none;font-size:0.76rem;line-height:1.4;margin-bottom:0.75rem;padding:0.45rem 0.65rem;border-radius:6px;"></div>

          <div style="display:flex;justify-content:flex-end;gap:0.6rem;">
            <button type="button" id="shCancelBtn" style="padding:0.5rem 0.9rem;background:#334155;color:#cbd5e1;border:none;border-radius:6px;cursor:pointer;font-size:0.82rem;font-weight:600;">Cancel</button>
            <button type="submit" id="shSubmitBtn" style="padding:0.5rem 1.15rem;background:#10b981;color:#052e16;border:none;border-radius:6px;cursor:pointer;font-size:0.82rem;font-weight:700;">Connect Live Feed</button>
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
    document.getElementById('shTabTotp').addEventListener('click', () => _setMode('totp'));
    document.getElementById('shTabToken').addEventListener('click', () => _setMode('token'));
    document.getElementById('shLoginForm').addEventListener('submit', _submitShoonyaLogin);
    return wrap;
  }

  function _setMode(mode) {
    _activeMode = mode === 'token' ? 'token' : 'totp';
    const totpBtn = document.getElementById('shTabTotp');
    const tokBtn = document.getElementById('shTabToken');
    const totpBox = document.getElementById('shTotpFields');
    const tokBox = document.getElementById('shTokenFields');
    if (_activeMode === 'token') {
      totpBox.style.display = 'none';
      tokBox.style.display = 'block';
      tokBtn.style.cssText = 'flex:1;padding:6px 10px;border-radius:6px;border:1px solid #6366f1;background:rgba(99,102,241,0.2);color:#e0e7ff;font-size:0.76rem;font-weight:600;cursor:pointer;';
      totpBtn.style.cssText = 'flex:1;padding:6px 10px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#94a3b8;font-size:0.76rem;font-weight:600;cursor:pointer;';
    } else {
      totpBox.style.display = 'grid';
      tokBox.style.display = 'none';
      totpBtn.style.cssText = 'flex:1;padding:6px 10px;border-radius:6px;border:1px solid #6366f1;background:rgba(99,102,241,0.2);color:#e0e7ff;font-size:0.76rem;font-weight:600;cursor:pointer;';
      tokBtn.style.cssText = 'flex:1;padding:6px 10px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#94a3b8;font-size:0.76rem;font-weight:600;cursor:pointer;';
    }
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
    ['shPassword', 'shApiKey', 'shTotp', 'shUserToken'].forEach((id) => {
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
    if (_activeMode === 'token') {
      payload.usertoken = (document.getElementById('shUserToken')?.value || '').trim();
      const uid = (document.getElementById('shUserId')?.value || '').trim();
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
      payload.totp = (document.getElementById('shTotp')?.value || '').trim();
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
      statusEl.textContent = `Box C: ${state}${uid}${syms}${ticks}`;
      statusEl.style.color = state === 'LIVE' ? '#34d399' : (state === 'SIMULATED' ? '#fbbf24' : '#cbd5e1');
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
})();
