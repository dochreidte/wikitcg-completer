/** Engine/marketplace settings and logging controls. */

import { $, toast, setSeg, onSeg } from './util.js';
import { postJSON } from './api.js';
import { loadMarket } from './market.js';

const HELP = {
  recmode: {
    surplus: 'Continuously, whenever duplicates pile up past the spares you keep.',
    on_demand: 'Only when ink is needed for a restock.',
  },
  resmode: {
    fixed: 'A set number per rarity.',
    missing: 'As many spares as you have cards missing in that rarity.',
  },
  recprio: {
    common_first: 'Clears the low-value backlog first; rares stay as trade stock.',
    rare_first: 'Cashes in the valuable duplicates first for ink.',
  },
};

export function applyConfig(c) {
  if (!c) return;
  $('#c-buy').checked = !!c.buy_packs_with_ink;
  $('#c-recycle').checked = !!c.auto_recycle;
  $('#c-mystery').checked = !!c.mystery_pack;
  $('#c-mkt').checked = !!c.enabled;
  $('#c-autostart').checked = !!c.autostart;
  $('#c-fulfill').checked = !!c.fulfill_others;
  // Writing to a field the user is editing would fight their typing.
  if (c.min_ink_reserve != null && document.activeElement !== $('#c-reserve'))
    $('#c-reserve').value = c.min_ink_reserve;
  if (c.max_listings != null && document.activeElement !== $('#c-maxlist'))
    $('#c-maxlist').value = c.max_listings;
  if (c.near_completion_max_missing != null && document.activeElement !== $('#c-near'))
    $('#c-near').value = c.near_completion_max_missing;

  const rm = c.recycle_mode || 'surplus';
  const sm = c.recycle_reserve_mode || 'fixed';
  const rp = c.recycle_priority || 'common_first';
  setSeg('c-recmode', rm);
  setSeg('c-resmode', sm);
  setSeg('c-recprio', rp);
  $('#h-recmode').textContent = HELP.recmode[rm] || '';
  $('#h-resmode').textContent = HELP.resmode[sm] || '';
  $('#h-recprio').textContent = HELP.recprio[rp] || '';
}

async function postConfig(patch) {
  try {
    const d = await postJSON('/api/config', patch);
    applyConfig(d.config);
    toast('Setting saved');
  } catch (e) { toast('Failed to save'); }
}

export function applyLogging(lg) {
  if (!lg) return;
  setSeg('c-loglevel', lg.level || 'INFO');
  $('#c-logreq').checked = !!lg.log_requests;
}

async function postLogging(patch) {
  try {
    const d = await postJSON('/api/logging', patch);
    applyLogging(d.logging);
    toast('Logging updated');
  } catch (e) { toast('Failed to set logging'); }
}

export function initSettings() {
  $('#c-buy').onchange = e => postConfig({ buy_packs_with_ink: e.target.checked });
  $('#c-recycle').onchange = e => postConfig({ auto_recycle: e.target.checked });
  $('#c-mystery').onchange = e => postConfig({ mystery_pack: e.target.checked });
  $('#c-autostart').onchange = e => postConfig({ autostart: e.target.checked });
  $('#c-reserve').onchange = e => postConfig({ min_ink_reserve: Number(e.target.value) || 0 });
  $('#c-maxlist').onchange = e => postConfig({ max_listings: Number(e.target.value) || 5 });
  $('#c-near').onchange = e =>
    postConfig({ near_completion_max_missing: Math.max(0, Number(e.target.value) || 0) });

  onSeg('c-recmode', v => postConfig({ recycle_mode: v }));
  onSeg('c-resmode', v => postConfig({ recycle_reserve_mode: v }));
  onSeg('c-recprio', v => postConfig({ recycle_priority: v }));
  onSeg('c-loglevel', v => postLogging({ level: v }));
  $('#c-logreq').onchange = e => postLogging({ log_requests: e.target.checked });

  // Both marketplace switches commit real cards, so they confirm before turning on.
  $('#c-mkt').onchange = async e => {
    if (e.target.checked && !confirm(
      'Enable auto-listings? This publishes real listings that commit your cards to other players.')) {
      e.target.checked = false;
      return;
    }
    await postConfig({ enabled: e.target.checked });
    loadMarket();
  };
  $('#c-fulfill').onchange = async e => {
    if (e.target.checked && !confirm(
      "Fulfill other players' offers? Each trade GIVES one of your cards (irreversible).")) {
      e.target.checked = false;
      return;
    }
    await postConfig({ fulfill_others: e.target.checked });
  };

  $('#c-reset').onclick = async () => {
    try {
      const d = await postJSON('/api/config/reset');
      applyConfig(d.config);
      toast('Settings reset to defaults');
    } catch (e) { toast('Reset failed'); }
  };
}
