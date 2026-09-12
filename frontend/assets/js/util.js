/** DOM, formatting and theme-colour helpers shared by every module. */

export const $ = s => document.querySelector(s);
export const $$ = s => Array.from(document.querySelectorAll(s));

export const RARITIES = ['LR', 'UR', 'SSR', 'SR', 'R', 'UC', 'C'];

export const fmt = n => (Number(n) || 0).toLocaleString('en-US');

export const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

export const isValidColor = c => /^#[0-9a-f]{3,8}$/i.test(c || '');

/** Read a theme token off <body>, so callers follow the active theme automatically. */
export const cssVar = n => getComputedStyle(document.body).getPropertyValue(n).trim();
export const rcol = r => cssVar('--r-' + r) || cssVar('--accent');

let toastTimer = null;
export function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 1800);
}

/** Mark the button carrying data-v="value" inside a segmented control as active. */
export function setSeg(id, value) {
  $$('#' + id + ' button').forEach(b => b.classList.toggle('on', b.dataset.v === value));
}

/** Delegate clicks on a segmented control to a handler receiving the chosen data-v. */
export function onSeg(id, handler) {
  $('#' + id).onclick = e => {
    const b = e.target.closest('[data-v]');
    if (b) handler(b.dataset.v);
  };
}
