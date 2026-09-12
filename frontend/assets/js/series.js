/** Series progress: the collection list, the overview shortlist and the detail sheet. */

import { $, fmt, esc, rcol, cssVar, isValidColor } from './util.js';
import { store } from './store.js';
import { getJSON } from './api.js';
import { openSide } from './sheets.js';
import { renderRun } from './status.js';

/** Pressroom drops the per-series API colours for flat black, red when badly incomplete. */
export function seriesBar(s) {
  const pct = Number(s.pct) || 0;
  if (cssVar('--series-bars') === 'mono') return pct < 50 ? cssVar('--accent') : cssVar('--txt');
  return isValidColor(s.accent_color) ? s.accent_color
    : isValidColor(s.primary_color) ? s.primary_color : cssVar('--accent');
}

function seriesRow(s) {
  const pct = Number(s.pct) || 0, missing = Number(s.missing) || 0;
  const sub = missing === 0
    ? 'Complete · ' + fmt(s.pulls) + ' pulls'
    : fmt(missing) + ' missing · ' + fmt(s.pulls) + ' pulls';
  return '<button class="srow" data-series="' + esc(s.series_id) + '">'
    + '<div style="min-width:0"><div class="nm"'
    + (missing === 0 ? ' style="color:var(--txt3)"' : '') + '>' + esc(s.name) + '</div>'
    + '<div class="sb">' + esc(sub) + '</div></div>'
    + '<div class="track" style="width:100%"><i style="width:' + pct + '%; background:'
    + seriesBar(s) + '"></i></div>'
    + '<div><div class="num">' + fmt(s.owned) + '<span style="color:var(--txt3)">/'
    + fmt(s.total) + '</span></div><div class="num pc">' + pct + '%</div></div></button>';
}

export function seriesOptions(selected) {
  const menu = !selected || store.seriesMenu.some(s => s.series_id === selected)
    ? store.seriesMenu : [...store.seriesMenu, { series_id: selected, name: selected }];
  return '<option value="">Series focus: auto</option>' + menu.map(s =>
    '<option value="' + esc(s.series_id) + '"' + (s.series_id === selected ? ' selected' : '')
    + '>' + esc(s.name) + '</option>').join('');
}

export function renderSeries(list) {
  if (!list) return;
  store.seriesList = list;
  const sorted = [...list].sort((a, b) =>
    (a.missing === 0) - (b.missing === 0) || (Number(a.pct) || 0) - (Number(b.pct) || 0));

  const menu = sorted.filter(s => s.series_id !== 'mystery')
    .sort((a, b) => String(a.name).localeCompare(String(b.name)));
  const key = menu.map(s => s.series_id + '|' + s.name).join(',');
  // Rewriting the options closes an open menu, so only touch it when the series change.
  if (key !== store.seriesKey) {
    store.seriesKey = key;
    store.seriesMenu = menu;
    const sel = $('#new-acct-series');
    sel.innerHTML = seriesOptions(sel.value);
  }

  const total = sorted.reduce((a, s) => a + (Number(s.total) || 0), 0);
  const owned = sorted.reduce((a, s) => a + (Number(s.owned) || 0), 0);
  const open = sorted.filter(s => Number(s.missing) > 0).length;
  const pct = total ? Math.round(1000 * owned / total) / 10 : 0;
  const note = fmt(owned) + ' / ' + fmt(total) + ' cards · ' + open + ' series still open';

  $('#nav-open').textContent = open || '';
  $('#s-pct').textContent = pct;
  $('#s-pctbar').style.width = pct + '%';
  $('#s-pct-note').textContent = note;
  $('#c-pct').textContent = pct;
  $('#c-pctbar').style.width = pct + '%';
  $('#c-note').textContent = note;
  $('#c-missing').textContent = fmt(total - owned);

  $('#series-list').innerHTML = sorted.length
    ? sorted.map(seriesRow).join('') : '<div class="empty">No series yet — run a sync.</div>';
  const near = sorted.filter(s => Number(s.missing) > 0).reverse().slice(0, 5);
  $('#near-list').innerHTML = near.length
    ? near.map(seriesRow).join('') : '<div class="empty">Every known series is complete.</div>';
  renderRun();
}

export async function openSeries(sid) {
  openSide('<div class="empty">Loading…</div>');
  try {
    const d = await getJSON('/api/series/' + encodeURIComponent(sid));
    const s = d.series || {};
    const pct = Number(s.pct) || 0, missing = Number(s.missing) || 0;

    let chips = '';
    if (d.rarity && d.rarity.length) {
      chips = '<div><div class="caps" style="margin-bottom:8px">Owned by rarity</div>'
        + '<div class="chipsr">' + d.rarity.map(r => {
          const owned = Number(r.owned) || 0, total = Number(r.total) || 0;
          return '<span class="rchip' + (owned >= total ? ' full' : '') + '">'
            + '<span class="sw" style="background:' + rcol(r.rarity) + '"></span>'
            + esc(r.rarity) + ' <b>' + owned + '</b>/' + total + '</span>';
        }).join('') + '</div></div>';
    } else if (!d.catalog_loaded) {
      chips = '<div style="font-size:12px; color:var(--txt3)">Catalogue not loaded yet — run a sync.</div>';
    }

    let miss = '';
    if (d.missing && d.missing.length) {
      miss = '<div><div class="caps" style="margin-bottom:6px">' + fmt(d.missing.length)
        + ' cards still missing</div><div class="misslist">'
        + d.missing.map(c => '<div class="mrowx"><span>' + esc(c.title || c.card_id) + '</span>'
          + '<span class="mono rar" style="font-size:11px; color:' + rcol(c.rarity) + '">'
          + '<span class="sw" style="background:' + rcol(c.rarity) + '"></span>'
          + esc(c.rarity || '') + '</span></div>').join('') + '</div></div>';
    } else if (d.catalog_loaded) {
      miss = '<div class="caps">Series complete</div>';
    }

    openSide('<div style="display:flex; align-items:flex-start; gap:14px">'
      + '<div style="flex-grow:1"><div class="sheetttl">' + esc(s.name || sid) + '</div>'
      + '<div style="font-size:12px; color:var(--txt2); margin-top:5px">'
      + fmt(s.owned) + ' / ' + fmt(s.total) + ' owned · ' + pct + '% · ' + fmt(missing)
      + ' missing</div></div><button class="btn sm" data-close="1">Close</button></div>'
      + '<div class="track tall"><i style="width:' + pct + '%; background:' + seriesBar(s)
      + '"></i></div>' + chips + miss
      + '<div style="font-size:11.5px; color:var(--txt3); border-top:var(--bw) solid var(--rule);'
      + ' padding-top:11px; line-height:1.5">' + fmt(d.duplicates)
      + ' duplicate(s) of this series held in reserve.</div>');
  } catch (e) {
    openSide('<div class="empty">Could not load this series.</div>');
  }
}

export function initSeries() {
  document.addEventListener('click', e => {
    const row = e.target.closest('[data-series]');
    if (row) openSeries(row.dataset.series);
  });
}
