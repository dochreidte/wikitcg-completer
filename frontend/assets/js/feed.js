/** The activity feed: the overview preview and the filterable full log. */

import { $, fmt, esc, setSeg, onSeg } from './util.js';
import { store } from './store.js';

const MAX_ENTRIES = 200;

function feedRow(a) {
  const t = new Date(a.ts || Date.now());
  const hh = t.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const d = Number(a.ink_delta) || 0;
  const lvl = ['info', 'warn', 'error'].includes(a.level) ? a.level : 'info';
  return '<div class="fe ' + lvl + '"><span class="ts">' + esc(hh) + '</span>'
    + '<span class="ty">' + esc(a.type || '') + '</span>'
    + '<span class="mg">' + esc(a.message || '') + '</span>'
    + '<span class="dl" style="color:' + (d > 0 ? 'var(--good)' : 'var(--bad)') + '">'
    + (d ? (d > 0 ? '+' + fmt(d) : fmt(d)) : '') + '</span></div>';
}

function matchesFilter(a) {
  if (store.logFilter === 'all') return true;
  if (store.logFilter === 'warn') return a.level === 'warn' || a.level === 'error';
  return a.level === 'error';
}

export function renderFeed() {
  const shown = store.feed.filter(matchesFilter);
  $('#full-feed').innerHTML = shown.length
    ? shown.map(feedRow).join('') : '<div class="empty">Nothing matches this filter.</div>';
  $('#log-count').textContent = shown.length + ' of ' + store.feed.length + ' entries';
  $('#recent-feed').innerHTML = store.feed.length
    ? store.feed.slice(0, 7).map(feedRow).join('')
    : '<div class="empty">Waiting for activity…</div>';
}

export function setFeed(actions) {
  store.feed = (actions || []).slice(0, MAX_ENTRIES);
  renderFeed();
}

export function addEntry(a) {
  store.feed.unshift(a);
  if (store.feed.length > MAX_ENTRIES) store.feed.length = MAX_ENTRIES;
  renderFeed();
}

export function initFeed() {
  onSeg('log-filter', v => {
    store.logFilter = v;
    setSeg('log-filter', v);
    renderFeed();
  });
}
