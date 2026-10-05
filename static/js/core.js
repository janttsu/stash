// Shared plumbing: DOM builder, API client, dialogs, menus, toasts.
import { t } from './i18n.js';

export const state = {
  user: null,       // {id, username, is_admin, totp_on, settings}
  dash: { tabs: [], categories: [], bookmarks: [] },
  onUnauthenticated: null,
};

export const COLORS = ['', 'red', 'orange', 'yellow', 'green', 'teal', 'blue', 'indigo', 'purple', 'pink', 'gray'];

const ICONS = {
  menu: '<circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  share: '<circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="M8.2 10.8l7.6-4.4M8.2 13.2l7.6 4.4"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 7l8.5 6 8.5-6"/>',
  phone: '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 005 5L15 13l5 2v4a2 2 0 01-2 2A16 16 0 013 6a2 2 0 012-2z"/>',
  link: '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0116 0"/>',
};

export function icon(name) {
  const span = document.createElement('span');
  span.className = 'icon';
  span.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  return span;
}

/** h('div', {class: 'x', onclick: fn}, child, 'text', [more]) */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key in el) el[key] = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  el.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return el;
}

/** Replace an element's children; null/false entries are skipped, as in h(). */
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return el;
}

const ERRORS = {
  invalid_credentials: 'Wrong username or password.',
  totp_required: 'Enter the code from your authenticator app.',
  bad_totp: 'The code is not valid.',
  too_many_attempts: 'Too many attempts. Try again later.',
  username_taken: 'That username is already taken.',
  bad_username: 'Username must be 3–32 characters: letters, numbers, . _ -',
  bad_invite: 'The invitation is not valid or has already been used.',
  registration_closed: 'Registration is closed.',
  registration_fixed: 'This is set by the server configuration (STASH_REGISTRATION).',
  wrong_password: 'Wrong password.',
  bad_url: 'That address cannot be bookmarked.',
  limit_reached: 'Limit reached.',
  not_found: 'Not found.',
  last_admin: 'You are the only administrator. Delete the other accounts first.',
  already_running: 'A check is already running.',
  busy: 'The server is busy. Try again later.',
  too_large: 'The file is too large.',
  password_required: 'This page is password protected.',
  forbidden: 'Not allowed.',
  already_undone: 'This change has already been undone.',
};

export async function api(method, path, body, { raw } = {}) {
  const init = { method, headers: { 'X-Stash': '1' }, credentials: 'same-origin' };
  if (raw !== undefined) {
    init.body = raw;
    init.headers['Content-Type'] = 'text/plain; charset=utf-8';
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers['Content-Type'] = 'application/json';
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw Object.assign(new Error(t('Network error. Check your connection.')), { code: 'network', status: 0 });
  }
  const data = await response.json().catch(() => null);
  if (response.ok) return data;
  const code = typeof data?.detail === 'string' ? data.detail : 'invalid';
  const message = ERRORS[code] ? t(ERRORS[code]) : code === 'invalid' ? t('Check the values you entered.') : code;
  if (response.status === 401 && code === 'not_authenticated' && state.onUnauthenticated) state.onUnauthenticated();
  throw Object.assign(new Error(message), { code, status: response.status });
}

/** Run an action and show its error as a toast instead of an unhandled rejection. */
export async function attempt(fn) {
  try {
    return await fn();
  } catch (e) {
    if (e.code !== 'not_authenticated') toast(e.message, 'error');
    console.error(e);
    return undefined;
  }
}

export async function loadDash() {
  state.dash = await api('GET', '/api/dashboard');
  return state.dash;
}

export function toast(message, kind = '') {
  let host = document.querySelector('.toasts');
  if (!host) document.body.append((host = h('div', { class: 'toasts', role: 'status' })));
  const el = h('div', { class: `toast ${kind}` }, message);
  host.append(el);
  setTimeout(() => el.remove(), kind === 'error' ? 6000 : 3000);
}

/**
 * Modal dialog. buttons: [{label, kind, action}] – action may be async; returning false keeps the dialog open.
 * Resolves with the value returned by the action (or null when dismissed).
 */
export function openModal(title, body, buttons = [], { wide = false } = {}) {
  return new Promise((resolve) => {
    const dialog = h('dialog', { class: `modal${wide ? ' wide' : ''}` });
    let result = null;
    const close = (value) => { result = value; dialog.close(); };
    const error = h('p', { class: 'form-error', hidden: true });
    const run = async (button, el) => {
      if (!button.action) return close(null);
      el.disabled = true;
      error.hidden = true;
      try {
        const value = await button.action();
        if (value !== false) close(value === undefined ? true : value);
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
      } finally {
        el.disabled = false;
      }
    };
    const primary = buttons.find((b) => b.kind === 'primary' || b.kind === 'danger');
    const buttonEls = buttons.map((b) => {
      const el = h('button', { type: b === primary ? 'submit' : 'button', class: `btn ${b.kind || ''}` }, b.label);
      if (b !== primary) el.addEventListener('click', () => run(b, el));
      return el;
    });
    const form = h('form', {
      class: 'modal__form',
      onsubmit: (e) => {
        e.preventDefault();
        if (primary) run(primary, buttonEls[buttons.indexOf(primary)]);
      },
    },
      h('header', { class: 'modal__head' },
        h('h2', null, title),
        h('button', { type: 'button', class: 'iconbtn', 'aria-label': t('Close'), onclick: () => close(null) }, icon('close'))),
      h('div', { class: 'modal__body' }, body, error),
      buttons.length ? h('footer', { class: 'modal__foot' }, buttonEls) : null);
    dialog.append(form);
    dialog.addEventListener('close', () => { dialog.remove(); resolve(result); });
    // a press that starts on the backdrop closes; a text selection dragged out of the dialog must not
    dialog.addEventListener('mousedown', (e) => { if (e.target === dialog) close(null); });
    document.body.append(dialog);
    dialog.showModal();
    const focus = dialog.querySelector('[autofocus], .modal__body input, .modal__body select, .modal__body textarea');
    if (focus) { focus.focus(); if (focus.select && focus.type === 'text') focus.select(); }
  });
}

export function confirmBox(message, { okLabel = t('OK'), danger = false, title = t('Are you sure?') } = {}) {
  return openModal(title, h('p', null, message), [
    { label: t('Cancel') },
    { label: okLabel, kind: danger ? 'danger' : 'primary', action: () => true },
  ]);
}

export function promptBox(title, { label = '', value = '', okLabel = t('Save'), type = 'text', hint = '' } = {}) {
  const input = h('input', { type, value, class: 'input', required: true, maxLength: 200, autocomplete: 'off' });
  return openModal(title, h('label', { class: 'field' }, label && h('span', null, label), input,
    hint && h('small', { class: 'muted' }, hint)), [
    { label: t('Cancel') },
    { label: okLabel, kind: 'primary', action: () => input.value.trim() || false },
  ]);
}

let openMenuEl = null;
export function closeMenu() {
  if (openMenuEl) { openMenuEl.remove(); openMenuEl = null; }
}

/** Popup menu at a mouse event or below an element. items: {label, action, danger} | {sep} | {el} | {heading} */
export function showMenu(at, items) {
  closeMenu();
  const menu = h('div', { class: 'menu', role: 'menu' });
  for (const item of items.filter(Boolean)) {
    if (item.sep) menu.append(h('hr'));
    else if (item.heading) menu.append(h('div', { class: 'menu__heading' }, item.heading));
    else if (item.el) menu.append(h('div', { class: 'menu__custom' }, item.el));
    else {
      menu.append(h('button', {
        type: 'button', role: 'menuitem', class: `menu__item${item.danger ? ' danger' : ''}`,
        onclick: () => { closeMenu(); item.action(); },
      }, item.label));
    }
  }
  // inside an open <dialog> the menu has to live in the dialog's top layer to be visible
  (document.querySelector('dialog[open]') || document.body).append(menu);
  openMenuEl = menu;
  let x, y;
  if (at instanceof Event) {
    at.preventDefault();
    x = at.clientX; y = at.clientY;
  } else {
    const r = at.getBoundingClientRect();
    x = r.left; y = r.bottom + 4;
  }
  const { offsetWidth: w, offsetHeight: hh } = menu;
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - w - 8))}px`;
  menu.style.top = `${Math.max(8, y + hh > innerHeight - 8 ? Math.max(8, innerHeight - hh - 8) : y)}px`;
  menu.querySelector('.menu__item')?.focus({ preventScroll: true });
  return menu;
}
document.addEventListener('pointerdown', (e) => { if (openMenuEl && !openMenuEl.contains(e.target)) closeMenu(); }, true);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && openMenuEl) { e.preventDefault(); closeMenu(); } });
window.addEventListener('resize', closeMenu);
window.addEventListener('scroll', closeMenu, true);

export function colorRow(current, onPick) {
  return h('div', { class: 'swatches' }, COLORS.map((c) => h('button', {
    type: 'button', class: `swatch c-${c || 'none'}${c === (current || '') ? ' on' : ''}`,
    title: c ? t(c) : t('No color'), 'aria-label': c ? t(c) : t('No color'),
    onclick: () => { closeMenu(); onPick(c); },
  })));
}

export function hostOf(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.hostname : '';
  } catch {
    return '';
  }
}

const SAFE_SCHEMES = ['http:', 'https:', 'ftp:', 'mailto:', 'tel:'];
export function safeHref(url) {
  try {
    return SAFE_SCHEMES.includes(new URL(url).protocol) ? url : '#';
  } catch {
    return '#';
  }
}

export function favicon(url, enabled = true) {
  if (url.startsWith('mailto:')) return h('span', { class: 'fav fav--glyph' }, icon('mail'));
  if (url.startsWith('tel:')) return h('span', { class: 'fav fav--glyph' }, icon('phone'));
  const host = hostOf(url);
  const letter = () => h('span', { class: 'fav fav--letter' }, (host.replace(/^www\./, '')[0] || '?').toUpperCase());
  if (!host || !enabled) return letter();
  // the version changes when icons are fetched again, so browsers do not keep showing a cached old one
  const rev = state.user?.favicon_rev ? `?v=${encodeURIComponent(state.user.favicon_rev)}` : '';
  const img = h('img', { class: 'fav', alt: '', loading: 'lazy', width: 16, height: 16, src: `/favicon/${encodeURIComponent(host)}${rev}` });
  img.addEventListener('error', () => img.replaceWith(letter()), { once: true });
  return img;
}

export function linkAttrs(url) {
  const settings = state.user?.settings;
  return { href: safeHref(url), rel: 'noopener noreferrer', target: !settings || settings.new_tab ? '_blank' : null };
}

export function mailBookmarks(bookmarks) {
  const subject = bookmarks.length === 1 ? bookmarks[0].title : t('Bookmarks');
  const body = bookmarks.map((b) => `${b.title}\n${b.url}`).join('\n\n');
  location.href = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body.slice(0, 1500))}`;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast(t('Copied to clipboard'));
  } catch {
    toast(text);
  }
}

export function fmtDate(seconds) {
  return new Date(seconds * 1000).toLocaleDateString(document.documentElement.lang || undefined,
    { year: 'numeric', month: 'short', day: 'numeric' });
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme || 'auto';
  try { localStorage.setItem('stash.theme', theme || 'auto'); } catch { /* private mode */ }
}
