/** Ink sparkline, pulls by rarity and recycling returns. */

import { $, fmt, rcol, cssVar, RARITIES } from './util.js';
import { store } from './store.js';
import { getJSON } from './api.js';

function renderSpark(history) {
  const svg = $('#spark');
  const pts = (history || []).filter(h => h.ink != null);
  if (pts.length < 2) {
    svg.innerHTML = '';
    $('#s-ink-note').textContent = 'Not enough history yet.';
    return;
  }
  const inks = pts.map(p => p.ink);
  const min = Math.min(...inks), max = Math.max(...inks), span = (max - min) || 1;
  const x = i => (2 + i * 296 / (pts.length - 1)).toFixed(1);
  const y = v => (36 - (v - min) * 32 / span).toFixed(1);
  const line = pts.map((p, i) => x(i) + ',' + y(p.ink)).join(' ');
  const accent = cssVar('--accent');
  svg.innerHTML = '<polygon points="2,38 ' + line + ' 298,38" fill="' + accent
    + '" opacity=".12"></polygon><polyline points="' + line + '" fill="none" stroke="'
    + accent + '" stroke-width="1.5" vector-effect="non-scaling-stroke"></polyline>';
  const delta = inks[inks.length - 1] - inks[0];
  $('#s-ink-note').textContent = (delta >= 0 ? '+' : '−') + fmt(Math.abs(delta))
    + ' over the last ' + pts.length + ' samples · peak ' + fmt(max);
}

function renderPulls(pc) {
  pc = pc || [];
  const byR = {};
  let total = 0, nw = 0;
  pc.forEach(p => { byR[p.rarity] = p; total += p.count; nw += (p.new || 0); });
  $('#pull-sub').textContent = total ? fmt(total) + ' pulls · ' + fmt(nw) + ' new' : 'No pulls yet';
  const maxc = Math.max(1, ...pc.map(p => p.count));
  $('#pull-bars').innerHTML = RARITIES.filter(r => byR[r]).map(r => {
    const c = byR[r].count;
    return '<div class="rrow"><span class="rk" style="color:' + rcol(r) + '">' + r + '</span>'
      + '<span class="track"><i style="width:' + Math.round(100 * c / maxc)
      + '%; background:' + rcol(r) + '"></i></span>'
      + '<span class="rv">' + fmt(c) + ' · ' + (total ? (100 * c / total).toFixed(1) : 0)
      + '%</span></div>';
  }).join('') || '<div class="empty">No pulls recorded.</div>';
}

function renderRecycle(rec, fails) {
  rec = rec || { by_rarity: [] };
  const rows = rec.by_rarity || [];
  $('#rec-sub').textContent = fmt(rec.total_ink) + ' ink · ' + fmt(rec.total_count) + ' cards';
  const byR = {};
  rows.forEach(x => { byR[x.rarity] = x; });
  const maxi = Math.max(1, ...rows.map(x => x.ink));
  $('#rec-bars').innerHTML = RARITIES.filter(r => byR[r]).map(r => {
    const ink = byR[r].ink;
    return '<div class="rrow"><span class="rk" style="color:' + rcol(r) + '">' + r + '</span>'
      + '<span class="track"><i style="width:' + Math.round(100 * ink / maxi)
      + '%; background:' + rcol(r) + '"></i></span>'
      + '<span class="rv">' + fmt(ink) + ' ink · ' + fmt(byR[r].count) + '</span></div>';
  }).join('') || '<div class="empty">Nothing recycled yet.</div>';

  const n = Number(fails && fails.total) || 0;
  if (!n) { $('#rec-fails').textContent = ''; return; }
  const left = fails.next_retry_at
    ? Math.max(0, Math.round(fails.next_retry_at - Date.now() / 1000)) : null;
  const when = left != null ? (left < 60 ? left + 's' : Math.floor(left / 60) + ' min') : '';
  $('#rec-fails').textContent = fmt(n) + ' card(s) refused by the API and remembered'
    + (when ? ' · retry in about ' + when : '');
}

export function renderStats(d) {
  store.lastStats = d;
  renderSpark(d.history);
  renderPulls(d.pull_counts);
  renderRecycle(d.recycle, d.recycle_failures);
}

export async function loadStats() {
  try { renderStats(await getJSON('/api/stats')); }
  catch (e) { console.error(e); }
}

let statsTimer = null;
export function scheduleStats() {
  clearTimeout(statsTimer);
  statsTimer = setTimeout(loadStats, 1500);
}
