/** Entry point: view routing, the WebSocket feed, polling and control buttons. */

import { $, $$, toast } from './util.js';
import { store } from './store.js';
import { postJSON } from './api.js';
import { setStatus, setResources, tickRegen } from './status.js';
import { renderSeries, initSeries } from './series.js';
import { applyConfig, applyLogging, initSettings } from './settings.js';
import { addEntry, initFeed } from './feed.js';
import { loadStats, scheduleStats } from './stats.js';
import { loadAccounts, initAccounts, setToken } from './accounts.js';
import { loadMarket, initMarket } from './market.js';
import { initSheets } from './sheets.js';
import { initTheme } from './theme.js';
import { loadState, refreshLive } from './live.js';

const TITLES = {
  overview: 'Overview', collection: 'Collection', accounts: 'Accounts',
  market: 'Market', settings: 'Settings', log: 'Activity log',
};

function go(view) {
  $$('.view').forEach(v => v.classList.toggle('on', v.dataset.view === view));
  $$('.navitem').forEach(b => b.classList.toggle('on', b.dataset.view === view));
  $('#crumb').textContent = TITLES[view] || view;
  if (view === 'market' && !store.marketLoaded) loadMarket();
}

function initRouting() {
  document.addEventListener('click', e => {
    const nav = e.target.closest('.navitem');
    if (nav) return go(nav.dataset.view);
    const jump = e.target.closest('[data-goto]');
    if (jump) return go(jump.dataset.goto);
  });
}

function initControls() {
  $('#btn-start').onclick = () => postJSON('/api/control/start');
  $('#btn-stop').onclick = () => postJSON('/api/control/stop');
  $('#btn-next').onclick = () => postJSON('/api/control/next').then(loadAccounts);
  $('#btn-sync').onclick = () => postJSON('/api/control/sync')
    .then(d => { if (!d.ok) toast(d.error || 'Sync unavailable'); });
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onopen = () => { $('#ws-txt').textContent = 'websocket · live'; };
  ws.onclose = () => {
    $('#ws-txt').textContent = 'websocket · offline';
    setTimeout(connectWS, 2500);
  };
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.kind === 'status') setStatus(m.status);
    else if (m.kind === 'resources') setResources(m);
    else if (m.kind === 'progress') renderSeries(m.series);
    else if (m.kind === 'config') applyConfig(m);
    else if (m.kind === 'logging') applyLogging(m);
    else if (m.kind === 'token') setToken(m);
    else if (m.kind === 'action') {
      addEntry(m);
      if (['open', 'recycle', 'buy', 'mystery'].includes(m.type)) scheduleStats();
    } else if (m.kind === 'account') {
      loadAccounts();
      store.marketLoaded = false;
    }
  };
}

initSheets();
initRouting();
initControls();
initSeries();
initFeed();
initSettings();
initAccounts();
initMarket();
initTheme();

$('#host-label').textContent = location.host;

loadState();
loadAccounts();
connectWS();

setInterval(tickRegen, 1000);
setInterval(refreshLive, 5000);
setInterval(loadStats, 20000);
setInterval(loadAccounts, 5000);
