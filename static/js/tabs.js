// Open tabs: browser tabs sent here to be sorted out later, so the browser can be emptied. Each tab is open
// (still to look at), saved (now a bookmark), done or dropped. The assistant does most of the sorting over
// MCP; here you see what is left, save or drop by hand, and send more.
import { api, attempt, confirmBox, favicon, fill, fmtDate, h, openModal, safeHref, toast } from './core.js';
import { t } from './i18n.js';
import { invalidateTags, locationPicker, rememberLocation, tagInput } from './widgets.js';

let root = null;
const view = { list: null, state: 'open', q: '' };

export async function renderTabs(container, params = {}) {
  root = container;
  root.className = 'view tabs';
  if (params.list) view.list = Number(params.list);
  if (params.state !== undefined) view.state = params.state;
  await draw();
}

const STATES = ['open', 'saved', 'done', 'dropped'];
const stateLabel = (s) => ({ open: t('Still open'), saved: t('Saved'), done: t('Done'), dropped: t('Dropped') })[s];

async function draw() {
  const { lists } = await api('GET', '/api/open-tabs');
  if (view.list && !lists.some((l) => l.id === view.list)) view.list = null;
  const query = new URLSearchParams({ state: view.state, limit: 500 });
  if (view.list) query.set('list_id', view.list);
  if (view.q) query.set('q', view.q);
  const data = lists.length ? await api('GET', `/api/open-tabs/items?${query}`) : { total: 0, items: [] };
  const checks = new Map();

  const listRow = (l) => h('button', {
    type: 'button', class: view.list === l.id ? 'tabs__list active' : 'tabs__list',
    onclick: () => { view.list = view.list === l.id ? null : l.id; draw(); },
  }, h('b', null, l.name || t('Unnamed list')), h('span', { class: 'muted small' },
    ` ${fmtDate(l.created_at)} · ${t('{open} open · {saved} saved · {done} done · {dropped} dropped', l)}`));

  const chosen = () => [...checks].filter(([, box]) => box.checked).map(([id]) => id);
  const act = (fn) => attempt(async () => { await fn(); await draw(); });
  const resolve = (ids, s) => act(async () => {
    const res = await api('POST', '/api/open-tabs/resolve', { ids, state: s });
    toast(t('{n} tabs marked {state}', { n: res.updated, state: stateLabel(s).toLowerCase() }));
  });
  const save = (ids) => saveDialog(ids);

  const row = (item) => {
    const box = h('input', { type: 'checkbox' });
    checks.set(item.id, box);
    return h('li', { class: `tabs__item tabs__item--${item.state}` },
      box,
      favicon(item.url),
      h('div', { class: 'tabs__text' },
        h('a', { href: safeHref(item.url), target: '_blank', rel: 'noopener noreferrer', class: 'tabs__title' }, item.title || item.url),
        h('div', { class: 'muted small' }, [item.host, item.state !== 'open' && stateLabel(item.state), item.note].filter(Boolean).join(' · '))),
      h('div', { class: 'tabs__buttons' },
        item.state !== 'saved' && h('button', { type: 'button', class: 'btn primary', onclick: () => save([item.id]) }, t('Save')),
        item.state !== 'done' && h('button', { type: 'button', class: 'btn', onclick: () => resolve([item.id], 'done') }, t('Done')),
        item.state !== 'dropped' && h('button', { type: 'button', class: 'btn danger-quiet', onclick: () => resolve([item.id], 'dropped') }, t('Drop')),
        item.state !== 'open' && h('button', { type: 'button', class: 'btn', onclick: () => resolve([item.id], 'open') }, t('Reopen'))));
  };

  const search = h('input', { type: 'search', value: view.q, placeholder: t('Search'), oninput: (e) => { view.q = e.target.value; clearTimeout(search.timer); search.timer = setTimeout(draw, 300); } });

  fill(root, h('section', { class: 'card tabs__card' },
    h('div', { class: 'row' },
      h('h2', { class: 'grow' }, t('Open tabs')),
      h('button', { type: 'button', class: 'btn', onclick: () => pasteDialog() }, t('Paste a list…')),
      view.list && h('button', {
        type: 'button', class: 'btn danger-quiet',
        onclick: () => act(async () => {
          if (!await confirmBox(t('Delete this list? Bookmarks made from it stay.'), { okLabel: t('Delete'), danger: true })) return;
          await api('DELETE', `/api/open-tabs/${view.list}`);
          view.list = null;
        }),
      }, t('Delete list'))),
    h('p', { class: 'muted' }, t('Send a browser’s open tabs here (the extension’s menu “Send all open tabs to Stash”, or paste their addresses), close them in the browser, and sort them out later: save the useful ones, drop the rest, and go through only what was left unfinished. Your assistant can do the sorting over MCP.')),
    lists.length ? h('div', { class: 'tabs__lists' }, lists.map(listRow)) : h('p', { class: 'empty' }, t('No tabs have been sent yet.')),
    lists.length && [
      h('div', { class: 'row tabs__filters' },
        h('div', { class: 'tabs__states' }, ['open', 'saved', 'done', 'dropped', ''].map((s) => h('button', {
          type: 'button', class: view.state === s ? 'btn active' : 'btn', onclick: () => { view.state = s; draw(); },
        }, s ? stateLabel(s) : t('All')))),
        search),
      data.items.length ? [
        h('div', { class: 'row tabs__bulk' },
          h('label', { class: 'check' }, h('input', { type: 'checkbox', onchange: (e) => { for (const box of checks.values()) box.checked = e.target.checked; } }), h('span', null, t('Select all'))),
          h('span', { class: 'grow' }),
          h('button', { type: 'button', class: 'btn', onclick: () => chosen().length && save(chosen()) }, t('Save selected')),
          h('button', { type: 'button', class: 'btn', onclick: () => chosen().length && resolve(chosen(), 'done') }, t('Mark done')),
          h('button', { type: 'button', class: 'btn danger-quiet', onclick: () => chosen().length && resolve(chosen(), 'dropped') }, t('Drop selected'))),
        h('ul', { class: 'plain tabs__items' }, data.items.map(row)),
        data.total > data.items.length && h('p', { class: 'muted small' }, t('Showing {n} of {total}', { n: data.items.length, total: data.total })),
      ] : h('p', { class: 'empty' }, view.state === 'open' ? t('Nothing left to look at.') : t('No tabs here.')),
    ]));
}

async function saveDialog(ids) {
  const location_ = locationPicker();
  const tags = tagInput([]);
  const saved = await openModal(t('Save as bookmarks'), h('div', { class: 'form' },
    h('p', { class: 'muted' }, t('{n} tabs', { n: ids.length })),
    h('div', { class: 'field' }, h('span', null, t('Location')), location_.el),
    h('div', { class: 'field' }, h('span', null, t('Tags')), tags.el)), [
    { label: t('Cancel') },
    {
      label: t('Save'), kind: 'primary',
      action: async () => {
        const category_id = location_.get();
        const res = await api('POST', '/api/open-tabs/save', { ids, category_id, tags: tags.get() });
        rememberLocation(category_id);
        invalidateTags();
        toast(t('{n} bookmarks added', { n: res.bookmarks_made }));
        return true;
      },
    },
  ]);
  if (saved) await draw();
}

async function pasteDialog() {
  const name = h('input', { type: 'text', maxlength: 100, placeholder: t('e.g. laptop, Firefox') });
  const text = h('textarea', { rows: 10, placeholder: 'https://…\nhttps://…' });
  const sent = await openModal(t('Paste a list of tabs'), h('div', { class: 'form' },
    h('p', { class: 'muted' }, t('One address per line, or any text with addresses in it. The easiest way is the extension’s menu “Send all open tabs to Stash”. Without it, in Firefox: press Ctrl+Shift+D to bookmark all tabs into a folder, open the Library (Ctrl+Shift+O), select that folder’s bookmarks (Ctrl+A), copy (Ctrl+C) and paste them here.')),
    h('label', { class: 'field' }, h('span', null, t('Name of this list')), name),
    h('label', { class: 'field' }, h('span', null, t('Addresses')), text)), [
    { label: t('Cancel') },
    {
      label: t('Send to Stash'), kind: 'primary',
      action: async () => {
        const res = await api('POST', '/api/open-tabs/import', { name: name.value, text: text.value });
        toast(t('{n} tabs sent to Stash. You can close them in the browser now.', { n: res.tabs }));
        view.list = res.list_id;
        view.state = 'open';
        return true;
      },
    },
  ], { wide: true });
  if (sent) await draw();
}
