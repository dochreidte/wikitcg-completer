/** The two overlays: a centred modal and a right-hand side sheet. */

import { $ } from './util.js';

export function closeSheets() {
  $('#ov-series').classList.remove('show');
  $('#ov-modal').classList.remove('show');
}

function wireCloseButtons(root) {
  root.querySelectorAll('[data-close]').forEach(b => { b.onclick = closeSheets; });
}

export function openModal(html) {
  const sheet = $('#modal-sheet');
  sheet.innerHTML = html;
  wireCloseButtons(sheet);
  $('#ov-modal').classList.add('show');
  return sheet;
}

export function openSide(html) {
  const sheet = $('#series-sheet');
  sheet.innerHTML = html;
  wireCloseButtons(sheet);
  $('#ov-series').classList.add('show');
  return sheet;
}

export function initSheets() {
  $('#ov-series').onclick = e => { if (e.target.id === 'ov-series') closeSheets(); };
  $('#ov-modal').onclick = e => { if (e.target.id === 'ov-modal') closeSheets(); };
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheets(); });
}
