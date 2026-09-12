/** Visual direction switching. Rarity and series colours are theme tokens read at
    render time, so a swap has to repaint everything that painted with them. */

import { $, $$ } from './util.js';
import { store } from './store.js';
import { renderSeries } from './series.js';
import { renderFeed } from './feed.js';
import { renderStats } from './stats.js';
import { renderAccounts } from './accounts.js';

const THEMES = ['archive', 'console', 'pressroom'];
const KEY = 'wikitcg-theme';

export function applyTheme(t) {
  document.body.className = 't-' + t;
  $$('#theme-switch button').forEach(b => b.classList.toggle('on', b.dataset.theme === t));
  try { localStorage.setItem(KEY, t); } catch (e) { /* private mode */ }

  renderSeries(store.seriesList);
  renderFeed();
  if (store.lastStats) renderStats(store.lastStats);
  store.accountsHtml = '';
  renderAccounts();
}

export function initTheme() {
  $('#theme-switch').onclick = e => {
    const b = e.target.closest('[data-theme]');
    if (b) applyTheme(b.dataset.theme);
  };
  try {
    const saved = localStorage.getItem(KEY);
    if (saved && THEMES.includes(saved)) applyTheme(saved);
  } catch (e) { /* private mode */ }
}
