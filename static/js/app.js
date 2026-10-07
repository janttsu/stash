// Entry point of the main app: session check, header, hash routing.
import { renderAuth } from './auth.js';
import { leaveBookmarks, refreshBookmarks, renderBookmarks } from './bookmarks.js';
import { api, applyTheme, attempt, fill, h, icon, showMenu, state } from './core.js';
import { refresh as refreshDashboard, renderDashboard } from './dashboard.js';
import { setLang, t } from './i18n.js';
import { renderSettings } from './settings.js';
import { renderTrash } from './trash.js';
import { bookmarkDialog } from './widgets.js';

const app = document.getElementById('app');

async function boot() {
  try {
    state.user = await api('GET', '/api/me');
  } catch (e) {
    if (e.status === 401) return renderAuth(app, () => { history.replaceState(null, '', '/'); boot(); });
    fill(app, h('p', { class: 'empty' }, e.message));
    return undefined;
  }
  state.onUnauthenticated = () => location.reload();
  setLang(state.user.settings.lang);
  applyTheme(state.user.settings.theme);
  shell();
  window.addEventListener('hashchange', route);
  live();
  return route();
}

// --- live updates: the page redraws itself when the bookmarks change anywhere -----------------
// The server sends the user's change counter whenever it moves. A redraw waits while the person is busy
// (a dialog or menu is open, something is being dragged, a field has focus) or the page is not visible.
let liveTimer = null;

function busy() {
  if (document.hidden || document.querySelector('dialog[open], .menu, .sortable-chosen, .sortable-drag')) return true;
  const el = document.activeElement;
  return Boolean(el && el.closest('#view') && el.matches('input, textarea, select, [contenteditable]'));
}

function scheduleRedraw() {
  clearTimeout(liveTimer);
  liveTimer = setTimeout(async function redraw() {
    if (busy()) {
      liveTimer = setTimeout(redraw, 1500);
      return;
    }
    const y = window.scrollY;
    if (current === 'dashboard') await refreshDashboard();
    else if (current === 'bookmarks') await refreshBookmarks();
    else await route();
    window.scrollTo(0, y);
  }, 400);
}

function live() {
  if (!('EventSource' in window)) return;
  let rev = null;
  const seen = (e) => {
    if (rev !== null && e.data !== rev) scheduleRedraw();
    rev = e.data;
  };
  const source = new EventSource('/api/events');
  source.addEventListener('hello', seen);  // also after a reconnect: changes made meanwhile are caught here
  source.onmessage = seen;
}

function shell() {
  const nav = (hash, label) => h('a', { href: hash, class: 'top__link', dataset: { route: hash } }, label);
  fill(app,
    h('header', { class: 'top' },
      h('a', { class: 'brand', href: '#/' }, h('img', { src: '/static/icon.svg', alt: '', width: 24, height: 24 }), 'Stash'),
      h('nav', { class: 'top__nav' }, nav('#/', t('Dashboard')), nav('#/bookmarks', t('My Bookmarks'))),
      h('span', { class: 'grow' }),
      h('button', {
        type: 'button', class: 'btn primary',
        onclick: async () => { if (await bookmarkDialog(null)) route(); },
      }, `+ ${t('Add')}`),
      h('button', {
        type: 'button', class: 'btn top__user', 'aria-label': t('Account'),
        onclick: (e) => showMenu(e.currentTarget, [
          { heading: state.user.username },
          { label: t('Trash'), action: () => { location.hash = '#/trash'; } },
          { label: t('Settings'), action: () => { location.hash = '#/settings'; } },
          { label: t('Sign out'), action: () => attempt(async () => { await api('POST', '/api/logout'); location.href = '/'; }) },
        ]),
      }, icon('user'), h('span', { class: 'top__username' }, state.user.username))),
    h('main', { id: 'view' }));
}

let current = null;
async function route() {
  const [path, query] = location.hash.replace(/^#/, '').split('?');
  const params = Object.fromEntries(new URLSearchParams(query || ''));
  const view = document.getElementById('view');
  const name = path === '/bookmarks' ? 'bookmarks' : path === '/settings' ? 'settings'
    : path === '/trash' ? 'trash' : 'dashboard';
  if (current === 'bookmarks' && name !== 'bookmarks') leaveBookmarks();
  current = name;
  for (const link of document.querySelectorAll('.top__link')) {
    link.classList.toggle('active', link.dataset.route === (name === 'bookmarks' ? '#/bookmarks' : name === 'dashboard' ? '#/' : ''));
  }
  await attempt(async () => {
    if (name === 'bookmarks') await renderBookmarks(view, params);
    else if (name === 'settings') await renderSettings(view);
    else if (name === 'trash') await renderTrash(view);
    else await renderDashboard(view, params.tab ? { tab: Number(params.tab) } : null);
  });
}

boot();
