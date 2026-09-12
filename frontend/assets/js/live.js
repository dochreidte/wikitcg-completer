/** Applies a live payload from /api/state or /api/live to every view. */

import { $, esc } from './util.js';
import { store } from './store.js';
import { getJSON } from './api.js';
import { setStatus, setResources } from './status.js';
import { renderSeries } from './series.js';
import { applyConfig, applyLogging } from './settings.js';
import { setFeed } from './feed.js';
import { loadStats } from './stats.js';

function updateBanner(d) {
  const msgs = [];
  if (d.token && d.token.expired) {
    msgs.push('<b>Session expired</b> — wikitcg has no refresh endpoint: log back in, then paste the cookie.');
  } else if (!d.has_session) {
    msgs.push('<b>No session cookie</b> — paste one to start farming.');
  } else if (d.token && d.token.expires_in != null && d.token.expires_in < 2 * 86400) {
    msgs.push('<b>Session expires soon</b> (~' + Math.floor(d.token.expires_in / 3600)
      + ' h) — renew it before it lapses.');
  }
  if (d.auth_error) msgs.push('<b>Authentication rejected</b>: ' + esc(d.auth_error));

  const el = $('#banner');
  if (!msgs.length) { el.classList.add('hide'); return; }
  // Without an account there is nowhere to PUT a cookie, so offer no button.
  const action = store.currentAccountId
    ? '<button class="btn sm" data-act="cookie" data-id="' + esc(store.currentAccountId)
      + '">Paste cookie</button>'
    : '';
  el.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="1.7" style="flex:0 0 auto; color:var(--warn)">'
    + '<path d="M12 8v5M12 16.5v.5"/><circle cx="12" cy="12" r="9"/></svg>'
    + '<div style="flex-grow:1; font-size:12px">' + msgs.join('<br>') + '</div>' + action;
  el.classList.remove('hide');
}

export function applyLive(d) {
  if (!d) return;
  setStatus(d.status);
  setResources(d.resources);
  renderSeries(d.series);
  applyConfig(d.config);
  applyLogging(d.logging);

  const ov = d.config_overridden || [];
  $('#cfg-over').textContent = ov.length
    ? ov.length + ' setting(s) changed from this dashboard'
    : 'All settings at their defaults';

  if (d.account) {
    store.currentAccountName = d.account.name;
    $('#acct-name').textContent = d.account.name;
  }
  updateBanner(d);
}

export async function loadState() {
  try {
    const d = await getJSON('/api/state');
    applyLive(d);
    setFeed(d.actions);
  } catch (e) { console.error(e); }
  loadStats();
}

export async function refreshLive() {
  try { applyLive(await getJSON('/api/live')); }
  catch (e) { /* a dropped poll is recovered by the next one */ }
}
