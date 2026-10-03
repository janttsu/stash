// Entry point of the main app: session check, header, hash routing.
import { renderAuth } from './auth.js';
import { leaveBookmarks, renderBookmarks } from './bookmarks.js';
import { api, applyTheme, attempt, fill, h, icon, showMenu, state } from './core.js';
import { renderDashboard } from './dashboard.js';
import { setLang, t } from './i18n.js';
import { renderSettings } from './settings.js';
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
  return route();
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
  const name = path === '/bookmarks' ? 'bookmarks' : path === '/settings' ? 'settings' : 'dashboard';
  if (current === 'bookmarks' && name !== 'bookmarks') leaveBookmarks();
  current = name;
  for (const link of document.querySelectorAll('.top__link')) {
    link.classList.toggle('active', link.dataset.route === (name === 'bookmarks' ? '#/bookmarks' : name === 'dashboard' ? '#/' : ''));
  }
  await attempt(async () => {
    if (name === 'bookmarks') await renderBookmarks(view, params);
    else if (name === 'settings') await renderSettings(view);
    else await renderDashboard(view, params.tab ? { tab: Number(params.tab) } : null);
  });
}

boot();
