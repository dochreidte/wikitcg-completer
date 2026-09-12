/** Account cards, the rail switcher, and the cookie and edit sheets. */

import { $, fmt, esc, toast } from './util.js';
import { store } from './store.js';
import { getJSON, postJSON, delJSON } from './api.js';
import { openModal, closeSheets } from './sheets.js';
import { seriesOptions } from './series.js';
import { loadState } from './live.js';

/** Token freshness as a chip; the cookie is the one thing that silently stops the farm. */
function sessChip(t) {
  if (!t || !t.present) return '<span class="chip bad">No session</span>';
  if (t.expired) return '<span class="chip bad">Expired</span>';
  if (t.expires_in != null) {
    const d = t.expires_in / 86400;
    return '<span class="chip ' + (d < 2 ? 'warn' : 'good') + '">'
      + (d >= 1 ? Math.floor(d) + ' d' : Math.floor(t.expires_in / 3600) + ' h') + '</span>';
  }
  return '<span class="chip good">Session</span>';
}

export function setToken(t) {
  const chip = $('#sess-chip'), txt = $('#sess-txt');
  if (!t || !t.present) { chip.className = 'chip click bad'; txt.textContent = 'No session'; return; }
  if (t.expired) { chip.className = 'chip click bad'; txt.textContent = 'Session expired'; return; }
  if (t.expires_in != null) {
    const d = t.expires_in / 86400;
    chip.className = 'chip click ' + (d < 2 ? 'warn' : 'good');
    txt.textContent = 'Session '
      + (d >= 1 ? Math.floor(d) + ' d' : Math.floor(t.expires_in / 3600) + ' h');
  } else {
    chip.className = 'chip click good';
    txt.textContent = 'Session';
  }
}

function accountCard(a) {
  const cur = a.id === store.currentAccountId;
  const t = a.token || {};
  const bad = !t.present || t.expired;
  const soon = !bad && t.expires_in != null && t.expires_in < 2 * 86400;
  const id = esc(a.id);

  let notice = '';
  if (bad || soon) {
    notice = '<div style="border:var(--bw) solid var(--danger-rule); border-radius:var(--r);'
      + ' padding:9px 11px; background:var(--danger-bg)">'
      + '<div style="font-size:11.5px; color:var(--bad); font-weight:500">'
      + (bad ? 'Idle until a fresh cookie is pasted.' : 'Expires soon — renew it before it stops.')
      + '</div><button class="btn sm" style="margin-top:8px" data-act="cookie" data-id="' + id
      + '">Paste session cookie</button></div>';
  }

  return '<div class="acard' + (cur ? ' cur' : '') + (bad ? ' expired' : '')
    + '" data-acct="' + id + '">'
    + '<div style="display:flex; align-items:flex-start; gap:10px">'
    + '<div style="flex-grow:1; min-width:0">'
    + '<div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">'
    + '<span class="runtitle" style="font-size:17px">' + esc(a.name) + '</span>'
    + (cur ? '<span class="chip" style="border-color:var(--accent); color:var(--accent)">Active</span>' : '')
    + '</div><div style="font-size:11.5px; color:var(--txt3); margin-top:3px">Focus: '
    + esc(a.series || 'auto') + ' · ' + esc(a.status || 'idle') + '</div></div>'
    + sessChip(a.token) + '</div>'
    + '<div class="kv">'
    + '<div><div class="k">Level</div><div class="v">' + fmt(a.level) + '</div></div>'
    + '<div><div class="k">Ink</div><div class="v">' + fmt(a.ink) + '</div></div>'
    + '<div><div class="k">Packs</div><div class="v">' + fmt(a.packs) + '</div></div>'
    + '<div><div class="k">Opened</div><div class="v">' + fmt(a.opened) + '</div></div>'
    + '</div>' + notice
    + '<div style="display:flex; gap:7px; flex-wrap:wrap; border-top:var(--bw) solid var(--rule);'
    + ' padding-top:11px">'
    + (cur ? '' : '<button class="btn sm" data-act="activate" data-id="' + id + '">Switch to</button>')
    + '<button class="btn sm" data-act="cookie" data-id="' + id + '">Cookie</button>'
    + '<button class="btn sm" data-act="edit" data-id="' + id + '">Edit</button>'
    + '<button class="btn sm" style="color:var(--bad)" data-act="delete" data-id="' + id
    + '">Delete</button></div></div>';
}

export function renderAccounts() {
  const html = store.accounts.map(accountCard).join('');
  // Re-rendering on every 5s poll can swallow a click that lands mid-refresh.
  if (html !== store.accountsHtml) {
    store.accountsHtml = html;
    $('#accounts-list').innerHTML = html;
  }
  $('#accounts-empty').hidden = store.accounts.length > 0;
  $('#nav-accts').textContent = store.accounts.length || '';
  $('#btn-next').hidden = store.accounts.length < 2;
}

function renderAcctMenu() {
  $('#acct-menu').innerHTML = store.accounts.map(a =>
    '<div class="acctopt" data-act="activate" data-id="' + esc(a.id) + '">'
    + '<span class="dot ' + (a.status === 'running' ? 'live' : a.status === 'error' ? 'err' : '')
    + '"></span><div style="flex-grow:1; min-width:0">'
    + '<div style="font-size:12.5px; font-weight:500">' + esc(a.name) + '</div>'
    + '<div style="font-size:10.5px; color:var(--txt3)">Focus: ' + esc(a.series || 'auto')
    + '</div></div></div>').join('');
}

export async function loadAccounts() {
  try {
    const d = await getJSON('/api/accounts');
    store.accounts = d.accounts || [];
    store.currentAccountId = d.current;

    const cur = store.accounts.find(a => a.id === store.currentAccountId);
    if (cur) {
      store.currentAccountName = cur.name;
      $('#acct-name').textContent = cur.name;
      $('#acct-focus').textContent = 'Focus: ' + (cur.series || 'auto');
      $('#acct-dot').className = 'dot '
        + (cur.status === 'running' ? 'live' : cur.status === 'error' ? 'err' : '');
      setToken(cur.token);
    } else {
      $('#acct-name').textContent = 'No account';
      $('#acct-focus').textContent = '';
    }

    renderAccounts();
    renderAcctMenu();

    const vw = $('#vault-warn');
    if (d.vault && !d.vault.available) {
      vw.innerHTML = '<div style="font-size:12px">No secure OS keyring found — set '
        + '<span class="mono">WIKITCG_SESSION_&lt;ACCOUNT_ID&gt;</span> environment variables.</div>';
      vw.classList.remove('hide');
    } else {
      vw.classList.add('hide');
    }
  } catch (e) { console.error('loadAccounts', e); }
}

export function openCookieSheet(accountId, name) {
  openModal('<div style="display:flex; align-items:flex-start; gap:14px">'
    + '<div style="flex-grow:1"><div class="sheetttl">Session cookie — ' + esc(name) + '</div>'
    + '<div style="font-size:12px; color:var(--txt2); margin-top:8px; line-height:1.5">'
    + 'wikitcg has no refresh endpoint. Log in from your browser, open '
    + '<span class="mono" style="font-size:11px">DevTools → Application → Cookies</span>, copy the '
    + '<span class="mono" style="font-size:11px">wtcg_session</span> value and paste it below. '
    + 'It applies immediately — no restart.</div></div>'
    + '<button class="btn sm" data-close="1">Close</button></div>'
    + '<div><div class="caps" style="margin-bottom:6px">wtcg_session</div>'
    + '<textarea class="ta" id="ck-session" placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."></textarea></div>'
    + '<div><div class="caps" style="margin-bottom:6px">Extra cookies — optional</div>'
    + '<textarea class="ta" id="ck-extra" style="min-height:54px" placeholder="cf_clearance=...; other=..."></textarea></div>'
    + '<div style="display:flex; align-items:center; gap:10px">'
    + '<button class="btn primary" id="ck-save">Save and apply</button>'
    + '<button class="btn" data-close="1">Cancel</button><div style="flex-grow:1"></div>'
    + '<span style="font-size:11px; color:var(--txt3)">Stored in the OS keyring</span></div>');
  $('#ck-save').onclick = () => saveCookie(accountId);
  $('#ck-session').focus();
}

async function saveCookie(accountId) {
  const session = $('#ck-session').value.trim();
  const extra = $('#ck-extra').value.trim();
  if (!session) { toast('Paste a wtcg_session cookie'); return; }
  try {
    const d = await postJSON('/api/accounts/' + encodeURIComponent(accountId) + '/cookie',
      { session_cookie: session, extra_cookies: extra });
    if (d.ok) {
      toast('Session updated');
      closeSheets();
      loadAccounts();
      loadState();
    } else toast(d.error || 'Cookie rejected');
  } catch (e) { toast('Update failed'); }
}

function openEditSheet(id) {
  const a = store.accounts.find(x => x.id === id);
  if (!a) return;
  openModal('<div style="display:flex; align-items:flex-start; gap:14px">'
    + '<div style="flex-grow:1"><div class="sheetttl">Edit — ' + esc(a.name) + '</div>'
    + '<div style="font-size:12px; color:var(--txt2); margin-top:6px">Auto focus: incomplete series '
    + 'first, then whichever pays best.</div></div>'
    + '<button class="btn sm" data-close="1">Close</button></div>'
    + '<div class="fld"><span class="fl" style="width:70px">Name</span>'
    + '<input type="text" id="ed-name" style="width:240px"></div>'
    + '<div class="fld"><span class="fl" style="width:70px">Series</span>'
    + '<select id="ed-series" style="width:240px">' + seriesOptions(a.series) + '</select></div>'
    + '<div style="display:flex; gap:10px"><button class="btn primary" id="ed-save">Save</button>'
    + '<button class="btn" data-close="1">Cancel</button></div>');
  $('#ed-name').value = a.name;
  $('#ed-save').onclick = async () => {
    try {
      const d = await postJSON('/api/accounts/' + encodeURIComponent(id),
        { name: $('#ed-name').value.trim(), series: $('#ed-series').value });
      if (d.ok) { toast('Account updated'); closeSheets(); loadAccounts(); }
      else toast(d.error || 'Update failed');
    } catch (e) { toast('Update failed'); }
  };
}

function handleAction(act, id) {
  if (act === 'activate') {
    $('#acct-menu').hidden = true;
    postJSON('/api/accounts/' + encodeURIComponent(id) + '/activate')
      .then(() => { loadAccounts(); loadState(); store.marketLoaded = false; })
      .catch(() => toast('Switch failed'));
  } else if (act === 'cookie') {
    const a = store.accounts.find(x => x.id === id);
    openCookieSheet(id, a ? a.name : id);
  } else if (act === 'edit') {
    openEditSheet(id);
  } else if (act === 'delete') {
    if (!confirm('Delete this account? Its local database is kept, the stored cookie is not.')) return;
    delJSON('/api/accounts/' + encodeURIComponent(id))
      .then(d => {
        if (d.ok) { toast('Account deleted'); loadAccounts(); }
        else toast(d.error || 'Delete failed');
      })
      .catch(() => toast('Delete failed'));
  }
}

export function initAccounts() {
  $('#acct-head').onclick = () => { $('#acct-menu').hidden = !$('#acct-menu').hidden; };

  $('#sess-chip').onclick = () => {
    if (store.currentAccountId) openCookieSheet(store.currentAccountId, store.currentAccountName);
    else toast('Add an account first');
  };

  document.addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (b) handleAction(b.dataset.act, b.dataset.id);
  });

  $('#new-acct-btn').onclick = async () => {
    const name = $('#new-acct-name').value.trim();
    const err = $('#acct-error');
    if (!name) { toast('Enter an account name'); return; }
    try {
      const d = await postJSON('/api/accounts',
        { name, series: $('#new-acct-series').value || null });
      if (d.ok) {
        $('#new-acct-name').value = '';
        $('#new-acct-series').value = '';
        err.textContent = '';
        toast('Account added');
        loadAccounts();
      } else err.textContent = d.error || 'Failed to add account';
    } catch (e) { err.textContent = 'Error adding account'; }
  };
}
