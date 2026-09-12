/** Engine status, resource tiles, the regeneration countdown and the run strip. */

import { $, fmt } from './util.js';
import { store } from './store.js';

const STATUS = {
  idle: ['', 'idle'], syncing: ['run', 'syncing'], running: ['run', 'running'],
  waiting: ['wait', 'awaiting regen'], stopped: ['', 'stopped'], error: ['err', 'error'],
  exhausted: ['wait', 'out of packs'],
};

export function setStatus(s) {
  store.engineStatus = s;
  const [cls, label] = STATUS[s] || ['', s];
  $('#st-dot').className = 'dot ' + cls;
  $('#st-txt').textContent = label;
  $('#status-chip').className = 'chip ' + (s === 'running' ? 'good' : s === 'error' ? 'bad' : '');
  $('#btn-start').disabled = (s === 'running' || s === 'syncing');
  $('#btn-stop').disabled = !(s === 'running' || s === 'waiting' || s === 'syncing');
  renderRun();
}

export function renderRun() {
  const s = store.engineStatus;
  const open = store.seriesList.filter(x => Number(x.missing) > 0)
    .sort((a, b) => (Number(a.pct) || 0) - (Number(b.pct) || 0));
  const target = open.length ? open[open.length - 1] : null;
  const account = store.currentAccountName ? ' · account “' + store.currentAccountName + '”' : '';

  $('#runstrip').className = 'runstrip'
    + (s === 'running' ? ' live' : s === 'error' ? ' bad' : '');

  let title, sub;
  if (s === 'running') {
    title = 'Opening packs' + (target ? ' — ' + target.name : '');
    sub = store.packsNow + ' of ' + (store.packsMax || '—') + ' packs left' + account;
  } else if (s === 'syncing') {
    title = 'Syncing with wikitcg';
    sub = 'Reading inventory and catalogue.';
  } else if (s === 'waiting' || s === 'exhausted') {
    const regen = $('#s-regen').textContent;
    title = 'Out of packs';
    sub = 'Waiting for regeneration' + (regen ? ' · ' + regen : '') + '.';
  } else if (s === 'error') {
    title = 'Stopped on an error';
    sub = 'See the activity log for the cause.';
  } else {
    title = 'Stopped';
    sub = 'Nothing is running' + account + '.';
  }
  $('#run-title').textContent = title;
  $('#run-sub').textContent = sub;
}

export function setResources(r) {
  if (!r) return;
  $('#s-ink').textContent = fmt(r.ink);
  store.packsNow = r.total_available || r.free_packs || 0;
  store.packsMax = r.max_free_packs || 0;
  $('#s-packs').textContent = fmt(store.packsNow);
  $('#s-packs-max').textContent = store.packsMax ? ' / ' + fmt(store.packsMax) : '';
  $('#s-packbar').style.width = store.packsMax
    ? Math.min(100, Math.round(100 * store.packsNow / store.packsMax)) + '%' : '0%';
  $('#s-level').textContent = fmt(r.level);
  $('#s-xpbar').style.width = Math.round((r.xp_progress != null ? r.xp_progress : 0) * 100) + '%';
  $('#s-xp').textContent = r.xp_to_next ? fmt(r.xp_to_next) + ' xp to the next level' : '';
  store.regenAt = r.next_regen_at || null;
  tickRegen();
}

export function tickRegen() {
  const el = $('#s-regen');
  if (store.packsMax && store.packsNow >= store.packsMax) {
    el.textContent = 'Full — regeneration paused';
    return;
  }
  if (!store.regenAt) { el.textContent = ''; return; }
  const ms = store.regenAt - Date.now();
  if (ms <= 0) { el.textContent = 'Regeneration imminent…'; return; }
  const s = Math.floor(ms / 1000), m = Math.floor(s / 60);
  el.innerHTML = 'Next pack in <b>'
    + (m > 0 ? m + 'm ' + String(s % 60).padStart(2, '0') + 's' : s + 's') + '</b>';
}
