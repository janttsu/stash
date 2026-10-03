// Public read-only view of a shared tab.
import { api, fill, favicon, h, safeHref } from './core.js';
import { t } from './i18n.js';

const app = document.getElementById('app');
const token = location.pathname.split('/').pop();

async function load(password = '') {
  try {
    draw(await api('POST', `/api/share/${encodeURIComponent(token)}`, { password }));
  } catch (e) {
    if (e.status === 401) askPassword(e.code === 'wrong_password' ? e.message : '');
    else {
      fill(app, h('div', { class: 'empty' },
        h('p', null, e.status === 404 ? t('This shared page does not exist or is no longer shared.') : e.message)));
    }
  }
}

function askPassword(error) {
  const input = h('input', { type: 'password', class: 'input', required: true, autocomplete: 'off', autofocus: true });
  fill(app, h('div', { class: 'auth' },
    h('form', { class: 'form card', onsubmit: (e) => { e.preventDefault(); load(input.value); } },
      h('label', { class: 'field' }, h('span', null, t('This page is password protected.')), input),
      error && h('p', { class: 'form-error' }, error),
      h('button', { type: 'submit', class: 'btn primary block' }, t('Open')))));
  input.focus();
}

function draw(data) {
  document.title = data.title;
  const byCat = new Map();
  for (const b of data.bookmarks) {
    if (!byCat.has(b.category_id)) byCat.set(b.category_id, []);
    byCat.get(b.category_id).push(b);
  }
  const cols = Array.from({ length: data.columns }, () => h('div', { class: 'col' }));
  for (const cat of data.categories) {
    cols[Math.min(cat.col, data.columns - 1)].append(h('section', { class: `cat c-${cat.color || 'none'}` },
      h('header', { class: 'cat__head cat__head--static' }, h('h3', { class: 'cat__name' }, cat.name)),
      h('ul', { class: 'bms' }, (byCat.get(cat.id) || []).map((b) => h('li', { class: `bm c-${b.color || 'none'}` },
        favicon(b.url),
        h('a', { class: 'bm__link', href: safeHref(b.url), title: b.url, target: '_blank', rel: 'noopener noreferrer' }, b.title))))));
  }
  fill(app,
    h('header', { class: 'sharehead' }, h('h1', null, data.title), data.subtitle && h('p', { class: 'muted' }, data.subtitle)),
    h('div', { class: 'view dashboard' }, h('div', { class: 'columns', dataset: { cols: data.columns } }, cols)),
    h('footer', { class: 'sharefoot muted' }, 'Stash'));
}

load();
