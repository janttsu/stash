// Dashboard: tabs → category groups in columns → bookmarks. Everything is drag-and-drop sortable.
import {
  api, attempt, fill, colorRow, confirmBox, copyText, favicon, h, icon, linkAttrs, loadDash, mailBookmarks,
  openModal, promptBox, showMenu, state, toast,
} from './core.js';
import { t } from './i18n.js';
import { bookmarkDialog, invalidateTags, moveDialog } from './widgets.js';

const ACTIVE_TAB = 'stash.tab';
let root = null;
let activeTabId = null;
let flashCategory = null;
let resizeObserver = null;

const sortableOptions = { animation: 150, delay: 180, delayOnTouchOnly: true, touchStartThreshold: 4 };

export async function renderDashboard(container, focus) {
  root = container;
  root.className = 'view dashboard';
  if (focus?.tab) activeTabId = focus.tab;
  if (focus?.category) flashCategory = focus.category;
  await loadDash();
  draw();
}

export async function refresh() {
  await loadDash();
  draw();
}

/** Run an API change, then reload and redraw; errors become toasts. */
const act = (fn) => attempt(async () => { await fn(); await refresh(); });

function currentTab() {
  const { tabs } = state.dash;
  if (activeTabId == null) {
    try { activeTabId = Number(localStorage.getItem(ACTIVE_TAB)); } catch { /* ignore */ }
  }
  return tabs.find((tab) => tab.id === activeTabId) || tabs[0] || null;
}

function setTab(id) {
  activeTabId = id;
  try { localStorage.setItem(ACTIVE_TAB, String(id)); } catch { /* ignore */ }
  draw();
}

function draw() {
  if (!root.classList.contains('dashboard')) return; // the user has moved to another view meanwhile
  resizeObserver?.disconnect();
  const tab = currentTab();
  const search = h('input', {
    type: 'search', class: 'input dash__search', placeholder: t('Find categories and bookmarks…'), autocomplete: 'off',
  });
  const body = h('div', { class: 'dash__body' });
  const showColumns = () => { fill(body, tab ? columns(tab) : emptyState()); };
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    if (q) fill(body, searchResults(q));
    else showColumns();
  });

  fill(root,
    tabsBar(tab),
    h('div', { class: 'dash__tools' },
      search,
      tab && h('button', { type: 'button', class: 'btn', onclick: () => addCategory(tab) }, `+ ${t('Category')}`),
      tab && h('button', { type: 'button', class: 'btn', onclick: () => collapseAll(tab, false) }, t('Expand all')),
      tab && h('button', { type: 'button', class: 'btn', onclick: () => collapseAll(tab, true) }, t('Collapse all')),
      tab && h('button', { type: 'button', class: 'btn', onclick: (e) => tabMenu(e.currentTarget, tab) },
        t('Tab'), icon('chevron'))),
    body);
  showColumns();
  fitTabs();

  if (flashCategory) {
    const el = root.querySelector(`.cat[data-id="${flashCategory}"]`);
    flashCategory = null;
    if (el) {
      el.scrollIntoView({ block: 'center' });
      el.classList.add('flash');
      setTimeout(() => el.classList.remove('flash'), 1600);
    }
  }
}

function emptyState() {
  return h('div', { class: 'empty' },
    h('p', null, t('No tabs yet. Tabs hold category groups, and categories hold your bookmarks.')),
    h('button', { type: 'button', class: 'btn primary', onclick: addTab }, t('Add a tab')));
}

// --- tabs bar ----------------------------------------------------------------

function tabsBar(active) {
  const tabsEl = h('div', { class: `tabs size-${state.user.settings.tab_size}`, role: 'tablist' });
  for (const tab of state.dash.tabs) {
    tabsEl.append(h('button', {
      type: 'button', role: 'tab', class: `tab c-${tab.color || 'none'}${tab === active ? ' active' : ''}`,
      dataset: { id: tab.id }, 'aria-selected': String(tab === active),
      onclick: () => setTab(tab.id),
      oncontextmenu: (e) => tabMenu(e, tab),
    }, h('span', { class: 'tab__name' }, tab.name), tab.share && icon('share')));
  }
  const more = h('button', { type: 'button', class: 'btn tabs__more', hidden: true });
  more.addEventListener('click', () => showMenu(more, [...tabsEl.querySelectorAll('.tab.overflow')].map((el) => ({
    label: el.querySelector('.tab__name').textContent, action: () => setTab(Number(el.dataset.id)),
  }))));
  const bar = h('div', { class: 'tabsbar' }, tabsEl, more,
    h('button', { type: 'button', class: 'iconbtn', title: t('Add a tab'), 'aria-label': t('Add a tab'), onclick: addTab },
      icon('plus')));

  if (window.Sortable) {
    window.Sortable.create(tabsEl, {
      ...sortableOptions, draggable: '.tab', direction: 'horizontal',
      onEnd: () => act(() => api('POST', '/api/tabs/order',
        { ids: [...tabsEl.querySelectorAll('.tab')].map((el) => Number(el.dataset.id)) })),
    });
  }
  return bar;
}

/** Dynamic tabs bar: tabs that do not fit on one row move into a "more" menu. */
function fitTabs() {
  const tabsEl = root.querySelector('.tabs');
  const more = root.querySelector('.tabs__more');
  if (!tabsEl) return;
  const fit = () => {
    const tabs = [...tabsEl.querySelectorAll('.tab')];
    tabs.forEach((el) => el.classList.remove('overflow'));
    more.hidden = true;
    if (tabsEl.scrollWidth <= tabsEl.clientWidth + 1) return;
    more.hidden = false;
    more.textContent = '+99 ▾';
    const gap = parseFloat(getComputedStyle(tabsEl).columnGap) || 0;
    const widths = tabs.map((el) => el.offsetWidth + gap);
    const activeIndex = tabs.findIndex((el) => el.classList.contains('active'));
    // the bar shrinks once the "more" button appears, so measure after showing it
    let room = tabsEl.clientWidth - (activeIndex >= 0 ? widths[activeIndex] : 0);
    let hidden = 0;
    tabs.forEach((el, i) => {
      if (i === activeIndex) return;
      if (room - widths[i] >= 0) room -= widths[i];
      else { room = -1; el.classList.add('overflow'); hidden += 1; }
    });
    more.textContent = `+${hidden} ▾`;
  };
  fit();
  resizeObserver = new ResizeObserver(fit);
  resizeObserver.observe(root.querySelector('.tabsbar'));
}

async function addTab() {
  const name = await promptBox(t('New tab'), { label: t('Name'), okLabel: t('Add') });
  if (!name) return;
  await attempt(async () => {
    const { id } = await api('POST', '/api/tabs', { name });
    activeTabId = id;
    try { localStorage.setItem(ACTIVE_TAB, String(id)); } catch { /* ignore */ }
    await refresh();
  });
}

function tabMenu(at, tab) {
  showMenu(at, [
    { label: t('Rename tab'), action: () => rename(t('Rename tab'), tab.name, (name) => api('PATCH', `/api/tabs/${tab.id}`, { name })) },
    { heading: t('Color') },
    { el: colorRow(tab.color, (color) => act(() => api('PATCH', `/api/tabs/${tab.id}`, { color }))) },
    { heading: t('Columns') },
    {
      el: h('div', { class: 'seg seg--menu' }, [1, 2, 3, 4, 5].map((n) => h('button', {
        type: 'button', class: n === tab.columns ? 'on' : '',
        onclick: () => act(() => api('PATCH', `/api/tabs/${tab.id}`, { columns: n })),
      }, String(n)))),
    },
    { sep: true },
    { label: t('Add category'), action: () => addCategory(tab) },
    { label: t('Sort categories A–Z'), action: () => act(() => api('POST', `/api/tabs/${tab.id}/sort`)) },
    { sep: true },
    { label: tab.share ? t('Sharing settings') : t('Share tab'), action: () => shareDialog(tab) },
    tab.share && { label: t('Stop sharing tab'), action: () => act(() => api('DELETE', `/api/tabs/${tab.id}/share`)) },
    { sep: true },
    { label: t('Add a tab'), action: addTab },
    { label: t('Delete tab'), danger: true, action: () => deleteTab(tab) },
  ]);
}

async function rename(title, current, save) {
  const name = await promptBox(title, { label: t('Name'), value: current });
  if (name && name !== current) await act(() => save(name));
}

/** Confirm a delete; when bookmarks are involved, ask whether they go to the Catalog. Returns null if cancelled. */
async function confirmDelete(message, bookmarkCount) {
  if (!bookmarkCount) {
    return (await confirmBox(message, { okLabel: t('Delete'), danger: true })) ? 'delete' : null;
  }
  const keep = h('input', { type: 'radio', name: 'fate', checked: true });
  const drop = h('input', { type: 'radio', name: 'fate' });
  const ok = await openModal(t('Are you sure?'), h('div', { class: 'form' },
    h('p', null, message),
    h('label', { class: 'check' }, keep, h('span', null, t('Move its {n} bookmarks to the Catalog', { n: bookmarkCount }))),
    h('label', { class: 'check' }, drop, h('span', null, t('Delete its {n} bookmarks too', { n: bookmarkCount })))), [
    { label: t('Cancel') },
    { label: t('Delete'), kind: 'danger', action: () => true },
  ]);
  if (!ok) return null;
  return keep.checked ? 'catalog' : 'delete';
}

async function deleteTab(tab) {
  const catIds = new Set(state.dash.categories.filter((c) => c.tab_id === tab.id).map((c) => c.id));
  const count = state.dash.bookmarks.filter((b) => catIds.has(b.category_id)).length;
  const fate = await confirmDelete(t('Delete the tab “{name}” and its categories?', { name: tab.name }), count);
  if (!fate) return;
  activeTabId = null;
  await act(() => api('DELETE', `/api/tabs/${tab.id}?bookmarks=${fate}`));
  invalidateTags();
}

async function shareDialog(tab) {
  const share = tab.share;
  const title = h('input', { type: 'text', class: 'input', maxLength: 200, value: share?.title || '', placeholder: tab.name });
  const subtitle = h('input', { type: 'text', class: 'input', maxLength: 500, value: share?.subtitle || '' });
  const password = h('input', {
    type: 'password', class: 'input', maxLength: 200, autocomplete: 'new-password',
    placeholder: share?.has_password ? t('Unchanged') : t('No password'),
  });
  const removePw = h('input', { type: 'checkbox' });
  const body = h('div', { class: 'form' },
    h('p', { class: 'muted' }, t('Anyone with the link can view this tab (read-only). Notes and tags are not shown.')),
    h('label', { class: 'field' }, h('span', null, t('Title')), title),
    h('label', { class: 'field' }, h('span', null, t('Subtitle')), subtitle),
    h('label', { class: 'field' }, h('span', null, t('Password (optional)')), password),
    share?.has_password && h('label', { class: 'check' }, removePw, h('span', null, t('Remove password protection'))));
  const saved = await openModal(share ? t('Sharing settings') : t('Share tab'), body, [
    { label: t('Cancel') },
    {
      label: share ? t('Save') : t('Share tab'), kind: 'primary',
      action: () => api('PUT', `/api/tabs/${tab.id}/share`, {
        title: title.value, subtitle: subtitle.value,
        password: removePw.checked ? '' : (password.value || null),
      }),
    },
  ]);
  if (!saved) return;
  await refresh();
  const url = `${location.origin}/share/${saved.token}`;
  const link = h('input', { type: 'text', class: 'input', readOnly: true, value: url, onfocus: (e) => e.target.select() });
  await openModal(t('Tab is shared'), h('div', { class: 'form' },
    h('label', { class: 'field' }, h('span', null, t('Link')), link),
    h('div', { class: 'row' },
      h('button', { type: 'button', class: 'btn', onclick: () => copyText(url) }, t('Copy link')),
      h('a', { class: 'btn', href: `mailto:?subject=${encodeURIComponent(title.value || tab.name)}&body=${encodeURIComponent(url)}` }, t('Send by email')),
      h('a', { class: 'btn', href: url, target: '_blank', rel: 'noopener' }, t('Open')))), [
    { label: t('Close'), kind: 'primary', action: () => true },
  ]);
}

// --- categories --------------------------------------------------------------

async function addCategory(tab, col) {
  const name = await promptBox(t('New category'), { label: t('Name'), okLabel: t('Add') });
  if (name) await act(() => api('POST', '/api/categories', { tab_id: tab.id, name, col }));
}

const collapseAll = (tab, collapsed) => act(() => api('POST', `/api/tabs/${tab.id}/collapse`, { collapsed }));

function columns(tab) {
  const cats = state.dash.categories.filter((c) => c.tab_id === tab.id);
  const byCat = new Map();
  for (const b of state.dash.bookmarks) {
    if (!byCat.has(b.category_id)) byCat.set(b.category_id, []);
    byCat.get(b.category_id).push(b);
  }
  const grid = h('div', { class: 'columns', dataset: { cols: tab.columns } });
  const cols = Array.from({ length: tab.columns }, () => h('div', { class: 'col' }));
  // categories saved for a wider layout fall into the last column
  for (const cat of cats) cols[Math.min(cat.col, tab.columns - 1)].append(categoryEl(cat, byCat.get(cat.id) || []));
  grid.append(...cols);
  if (!cats.length) {
    cols[0].append(h('div', { class: 'empty' },
      h('p', null, t('This tab is empty.')),
      h('button', { type: 'button', class: 'btn primary', onclick: () => addCategory(tab) }, t('Add category'))));
  }

  if (window.Sortable) {
    for (const col of cols) {
      window.Sortable.create(col, {
        ...sortableOptions, group: 'cats', draggable: '.cat', handle: '.cat__head', filter: 'button', preventOnFilter: false,
        onEnd: () => act(() => api('POST', `/api/tabs/${tab.id}/layout`, {
          columns: cols.map((c) => [...c.querySelectorAll(':scope > .cat')].map((el) => Number(el.dataset.id))),
        })),
      });
    }
    for (const list of grid.querySelectorAll('.bms')) {
      window.Sortable.create(list, {
        ...sortableOptions, group: 'bms', draggable: '.bm',
        onEnd: (e) => act(() => api('POST', `/api/categories/${e.to.dataset.cat}/order`, {
          ids: [...e.to.querySelectorAll(':scope > .bm')].map((el) => Number(el.dataset.id)),
        })),
      });
    }
  }
  return grid;
}

function categoryEl(cat, bookmarks) {
  const toggle = () => act(() => api('PATCH', `/api/categories/${cat.id}`, { collapsed: !cat.collapsed }));
  return h('section', { class: `cat c-${cat.color || 'none'}${cat.collapsed ? ' collapsed' : ''}`, dataset: { id: cat.id } },
    h('header', { class: 'cat__head', oncontextmenu: (e) => categoryMenu(e, cat, bookmarks) },
      h('button', {
        type: 'button', class: 'cat__toggle', 'aria-expanded': String(!cat.collapsed),
        title: cat.collapsed ? t('Expand') : t('Collapse'), onclick: toggle,
      }, icon('chevron')),
      h('h3', { class: 'cat__name', ondblclick: toggle }, cat.name),
      h('span', { class: 'cat__count' }, String(bookmarks.length)),
      h('button', {
        type: 'button', class: 'iconbtn', title: t('Add bookmark'), 'aria-label': t('Add bookmark'),
        onclick: () => addBookmark(cat.id),
      }, icon('plus')),
      h('button', {
        type: 'button', class: 'iconbtn', title: t('Menu'), 'aria-label': t('Menu'),
        onclick: (e) => categoryMenu(e.currentTarget, cat, bookmarks),
      }, icon('menu'))),
    h('ul', { class: 'bms', dataset: { cat: cat.id, empty: t('Drop bookmarks here') } }, bookmarks.map(bookmarkEl)));
}

function categoryMenu(at, cat, bookmarks) {
  const otherTabs = state.dash.tabs.filter((tab) => tab.id !== cat.tab_id);
  showMenu(at, [
    { label: t('Add bookmark'), action: () => addBookmark(cat.id) },
    { label: t('Open all links'), action: () => openAll(bookmarks) },
    { label: t('Sort bookmarks A–Z'), action: () => act(() => api('POST', `/api/categories/${cat.id}/sort`)) },
    { sep: true },
    { label: t('Rename category'), action: () => rename(t('Rename category'), cat.name, (name) => api('PATCH', `/api/categories/${cat.id}`, { name })) },
    { heading: t('Color') },
    { el: colorRow(cat.color, (color) => act(() => api('PATCH', `/api/categories/${cat.id}`, { color }))) },
    otherTabs.length && { label: t('Move to another tab…'), action: () => moveCategory(cat, otherTabs) },
    { sep: true },
    { label: t('Delete category'), danger: true, action: () => deleteCategory(cat, bookmarks.length) },
  ]);
}

async function moveCategory(cat, tabs) {
  const select = h('select', { class: 'input' }, tabs.map((tab) => h('option', { value: tab.id }, tab.name)));
  const ok = await openModal(t('Move “{name}” to another tab', { name: cat.name }),
    h('label', { class: 'field' }, h('span', null, t('Tab')), select), [
      { label: t('Cancel') },
      { label: t('Move'), kind: 'primary', action: () => api('PATCH', `/api/categories/${cat.id}`, { tab_id: Number(select.value) }) },
    ]);
  if (ok) await refresh();
}

async function deleteCategory(cat, count) {
  const fate = await confirmDelete(t('Delete the category “{name}”?', { name: cat.name }), count);
  if (!fate) return;
  await act(() => api('DELETE', `/api/categories/${cat.id}?bookmarks=${fate}`));
  invalidateTags();
}

async function openAll(bookmarks) {
  if (!bookmarks.length) return;
  if (bookmarks.length > 8 && !await confirmBox(t('Open {n} links in new tabs?', { n: bookmarks.length }), { okLabel: t('Open') })) return;
  let blocked = 0;
  for (const b of bookmarks) {
    // opened without the "noopener" feature so that a blocked pop-up is detectable (null)
    const win = window.open(linkAttrs(b.url).href, '_blank');
    if (win) win.opener = null;
    else blocked += 1;
  }
  if (blocked) toast(t('Your browser blocked some pop-ups. Allow pop-ups for this site.'), 'error');
}

// --- bookmarks ---------------------------------------------------------------

function tooltip(b) {
  if (!state.user.settings.tooltips) return b.url;
  return [b.url, b.tags.length ? `${t('Tags')}: ${b.tags.join(', ')}` : '', b.notes].filter(Boolean).join('\n');
}

function bookmarkEl(b) {
  return h('li', { class: `bm c-${b.color || 'none'}`, dataset: { id: b.id }, oncontextmenu: (e) => bookmarkMenu(e, b) },
    favicon(b.url, state.user.settings.favicons),
    h('a', { class: 'bm__link', title: tooltip(b), draggable: false, ...linkAttrs(b.url) }, b.title),
    h('button', {
      type: 'button', class: 'iconbtn bm__menu', title: t('Menu'), 'aria-label': t('Menu'),
      onclick: (e) => bookmarkMenu(e.currentTarget, b),
    }, icon('menu')));
}

async function addBookmark(categoryId) {
  if (await bookmarkDialog(null, { category_id: categoryId })) await refresh();
}

function bookmarkMenu(at, b) {
  showMenu(at, [
    { label: t('Edit'), action: async () => { if (await bookmarkDialog(b)) await refresh(); } },
    { label: t('Open in new tab'), action: () => window.open(linkAttrs(b.url).href, '_blank', 'noopener') },
    { label: t('Copy link'), action: () => copyText(b.url) },
    { label: t('Send by email'), action: () => mailBookmarks([b]) },
    { heading: t('Color') },
    { el: colorRow(b.color, (color) => act(() => api('PATCH', `/api/bookmarks/${b.id}`, { color }))) },
    { label: t('Move…'), action: async () => { if (await moveDialog({ ids: [b.id] }, 1)) await refresh(); } },
    {
      label: t('Move to Catalog'),
      action: async () => {
        await act(() => api('PATCH', `/api/bookmarks/${b.id}`, { category_id: null }));
        invalidateTags();
        toast(t('Moved to the Catalog'));
      },
    },
    { sep: true },
    {
      label: t('Delete'), danger: true,
      action: async () => {
        if (await confirmBox(t('Delete “{name}”?', { name: b.title }), { okLabel: t('Delete'), danger: true })) {
          await act(() => api('DELETE', `/api/bookmarks/${b.id}`));
        }
      },
    },
  ]);
}

// --- find categories / search bookmarks --------------------------------------

function searchResults(q) {
  const { tabs, categories, bookmarks } = state.dash;
  const tabName = (id) => tabs.find((tab) => tab.id === id)?.name || '';
  const catById = new Map(categories.map((c) => [c.id, c]));
  const jump = (cat) => { flashCategory = cat.id; setTab(cat.tab_id); };
  const cats = categories.filter((c) => c.name.toLowerCase().includes(q));
  const hits = bookmarks.filter((b) => `${b.title}\n${b.url}\n${b.tags.join(' ')}\n${b.notes}`.toLowerCase().includes(q));

  return h('div', { class: 'results' },
    h('h3', null, `${t('Categories')} (${cats.length})`),
    h('ul', { class: 'results__list' }, cats.slice(0, 50).map((c) => h('li', null,
      h('button', { type: 'button', class: 'linklike', onclick: () => jump(c) }, `${tabName(c.tab_id)} › ${c.name}`)))),
    h('h3', null, `${t('Bookmarks')} (${hits.length})`),
    h('ul', { class: 'results__list' }, hits.slice(0, 200).map((b) => {
      const cat = catById.get(b.category_id);
      return h('li', { class: 'bm', oncontextmenu: (e) => bookmarkMenu(e, b) },
        favicon(b.url, state.user.settings.favicons),
        h('a', { class: 'bm__link', title: tooltip(b), ...linkAttrs(b.url) }, b.title),
        cat && h('button', { type: 'button', class: 'linklike muted', onclick: () => jump(cat) },
          `${tabName(cat.tab_id)} › ${cat.name}`));
    })),
    !cats.length && !hits.length && h('p', { class: 'muted' },
      t('Nothing on the Dashboard matches. Catalog bookmarks are searched on the My Bookmarks page.')));
}
