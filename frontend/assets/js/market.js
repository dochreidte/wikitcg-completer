/** Marketplace listings — mine and the open market. */

import { $, esc, rcol } from './util.js';
import { store } from './store.js';
import { getJSON } from './api.js';

function mktRow(l, mine) {
  const st = l.status || '';
  const cls = st === 'active' ? 'good' : st === 'cancelled' ? 'bad' : '';
  const rar = l.offered_rarity
    ? '<span class="sw" style="background:' + rcol(l.offered_rarity) + '"></span>' : '';
  const right = mine
    ? '<span class="chip ' + cls + '">' + esc(st) + '</span>'
    : '<span style="font-size:11px; color:var(--txt3)">' + esc(l.lister_name || '') + '</span>';
  return '<div class="mrow"><span class="mono rar" title="Offered">' + rar
    + esc(l.offered_card_type || '') + '</span><span class="ar">→</span>'
    + '<span class="mono" title="Wanted">' + esc(l.wanted_card_id || '') + '</span>'
    + right + '</div>';
}

export async function loadMarket() {
  store.marketLoaded = true;
  $('#mkt-status').textContent = 'loading…';
  try {
    const d = await getJSON('/api/marketplace');
    if (d.error) {
      $('#mkt-status').innerHTML =
        '<span style="color:var(--warn)">unavailable: ' + esc(d.error) + '</span>';
    } else {
      $('#mkt-status').textContent = (d.enabled ? 'auto on' : 'auto off') + ' · '
        + (Number(d.active_mine) || 0) + '/' + (Number(d.max_listings) || 5)
        + ' active · market ' + ((d.market || []).length);
    }
    $('#nav-mkt').textContent = (Number(d.active_mine) || 0) || '';
    $('#mkt-mine').innerHTML = (d.mine && d.mine.length)
      ? d.mine.map(l => mktRow(l, true)).join('') : '<div class="empty">No listings.</div>';
    $('#mkt-market').innerHTML = (d.market && d.market.length)
      ? d.market.map(l => mktRow(l, false)).join('') : '<div class="empty">Market empty.</div>';
  } catch (e) {
    $('#mkt-status').textContent = 'loading error';
  }
}

export function initMarket() {
  $('#mkt-refresh').onclick = loadMarket;
}
