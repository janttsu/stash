// "My Bookmarks": list view over the Catalog and the Dashboard with tag filtering, search and bulk tools.
import {
  api, attempt, fill, confirmBox, copyText, favicon, fmtDate, h, icon, linkAttrs, loadDash, mailBookmarks,
  openModal, showMenu, state, toast,
} from './core.js';
import { t } from './i18n.js';
import { bookmarkDialog, invalidateTags, locationLabel, moveDialog, normTag, tagInput } from './widgets.js';

const PAGE = 100;
const view = {
  scope: 'all', tags: [], untagged: false, q: '', mode: 'exact', sort: 'newest',
  items: [], total: 0, tagList: [], tagFind: '', selected: new Set(), allMatching: false,
};
let root = null;
let pollTimer = null;
let loadSeq = 0;

export async function renderBookmarks(container, preset) {
  root = container;
  root.className = 'view mybm';
  if (preset?.tag) Object.assign(view, { tags: [preset.tag], scope: 'all', q: '', untagged: false });
  fill(root, h('aside', { class: 'side' }), h('section', { class: 'main' }));
  await Promise.all([loadDash(), reload()]);
  checkDeadLinkJob();
}

/** Load the list again after a change made elsewhere (another tab, device, an API key or the server). */
export async function refreshBookmarks() {
  if (!root || !mounted()) return;
  await Promise.all([loadDash(), reload()]);
}

export function leaveBookmarks() {
  clearTimeout(pollTimer);
  pollTimer = null;
  loadSeq += 1; // drop the results of requests still in flight
}

const mounted = () => root.classList.contains('mybm');

const currentFilter = () => ({ scope: view.scope, tags: view.tags, q: view.q, mode: view.mode, untagged: view.untagged });

function filterQuery() {
  const p = new URLSearchParams({ scope: view.scope, mode: view.mode });
  if (view.tags.length) p.set('tags', view.tags.join(','));
  if (view.q) p.set('q', view.q);
  if (view.untagged) p.set('untagged', 'true');
  return p;
}

async function reload() {
  const seq = ++loadSeq;
  view.selected.clear();
  view.allMatching = false;
  const query = filterQuery();
  await attempt(async () => {
    const [list, tags] = await Promise.all([
      api('GET', `/api/bookmarks?${query}&sort=${view.sort}&limit=${PAGE}`),
      api('GET', `/api/tags?${query}`),
    ]);
    if (seq !== loadSeq || !mounted()) return; // a newer search has started, or the view was left
    view.items = list.items;
    view.total = list.total;
    view.tagList = tags.tags;
    drawSide();
    drawMain();
  });
}

async function loadMore() {
  await attempt(async () => {
    const list = await api('GET', `/api/bookmarks?${filterQuery()}&sort=${view.sort}&limit=${PAGE}&offset=${view.items.length}`);
    if (!mounted()) return;
    view.items.push(...list.items);
    view.total = list.total;
    drawMain();
  });
}

/** After a change: tags and dashboard data may be stale. */
async function changed() {
  invalidateTags();
  await Promise.all([loadDash(), reload()]);
}

// --- sidebar: scope + tags ---------------------------------------------------

function drawSide() {
  const side = root.querySelector('.side');
  const scopes = [['all', t('All')], ['dashboard', t('Dashboard')], ['catalog', t('Catalog')]];
  const find = h('input', {
    type: 'search', class: 'input', placeholder: t('Find tags…'), value: view.tagFind, autocomplete: 'off',
  });
  const list = h('ul', { class: 'taglist' });
  const drawTags = () => {
    const q = view.tagFind.trim().toLowerCase();
    const tags = view.tagList.filter(([tag]) => !view.tags.includes(tag) && (!q || tag.includes(q)));
    list.replaceChildren(...tags.slice(0, 500).map(([tag, n]) => h('li', null, h('button', {
      type: 'button', class: 'tagrow',
      onclick: () => { view.tags = [...view.tags, tag]; view.untagged = false; view.tagFind = ''; reload(); },
      oncontextmenu: (e) => tagMenu(e, tag),
    }, h('span', { class: 'tagrow__name' }, tag), h('span', { class: 'tagrow__n' }, String(n))))));
    if (!tags.length) list.append(h('li', { class: 'muted pad' }, q ? t('No matching tags') : t('No tags')));
    else if (tags.length > 500) list.append(h('li', { class: 'muted pad' }, t('Type to find more tags…')));
  };
  find.addEventListener('input', () => { view.tagFind = find.value; drawTags(); });
  drawTags();

  const order = state.user.settings.tag_order;
  fill(side,
    h('div', { class: 'seg seg--full' }, scopes.map(([value, label]) => h('button', {
      type: 'button', class: view.scope === value ? 'on' : '',
      onclick: () => { view.scope = value; reload(); },
    }, label))),
    h('div', { class: 'side__head' },
      h('h3', null, t('Tags')),
      h('button', {
        type: 'button', class: 'linklike small',
        title: t('Change the order of the tag list'),
        onclick: () => attempt(async () => {
          state.user.settings = await api('PATCH', '/api/settings', { tag_order: order === 'count' ? 'alpha' : 'count' });
          await reload();
        }),
      }, order === 'count' ? t('Most used first') : t('A–Z'))),
    find,
    h('button', {
      type: 'button', class: `tagrow${view.untagged ? ' on' : ''}`,
      onclick: () => { view.untagged = !view.untagged; view.tags = []; reload(); },
    }, h('span', { class: 'tagrow__name muted' }, t('Untagged'))),
    list);
}

function tagMenu(at, tag) {
  showMenu(at, [
    { label: t('Rename tag'), action: () => renameTagDialog(tag) },
    { label: t('Delete tag'), danger: true, action: () => deleteTag(tag) },
  ]);
}

async function renameTagDialog(preset = '') {
  const from = h('input', { type: 'text', class: 'input', value: preset, required: true, autocomplete: 'off' });
  const to = h('input', { type: 'text', class: 'input', required: true, autocomplete: 'off' });
  const done = await openModal(t('Rename tag'), h('div', { class: 'form' },
    h('p', { class: 'muted' }, t('The tag is renamed on every bookmark that has it.')),
    h('label', { class: 'field' }, h('span', null, t('Tag')), from),
    h('label', { class: 'field' }, h('span', null, t('New name')), to)), [
    { label: t('Cancel') },
    { label: t('Rename'), kind: 'primary', action: () => api('POST', '/api/tags/rename', { old: from.value, new: to.value }) },
  ]);
  if (!done) return;
  toast(t('{n} bookmarks updated', { n: done.count }));
  const renamed = normTag(from.value);
  view.tags = view.tags.map((tag) => (tag === renamed ? normTag(to.value) : tag));
  await changed();
}

async function deleteTag(tag) {
  if (!await confirmBox(t('Remove the tag “{name}” from all bookmarks? The bookmarks are kept.', { name: tag }),
    { okLabel: t('Delete tag'), danger: true })) return;
  await attempt(async () => {
    await api('POST', '/api/tags/rename', { old: tag, new: '' });
    view.tags = view.tags.filter((x) => x !== tag);
    await changed();
  });
}

// --- main: toolbar, list -----------------------------------------------------

function drawMain() {
  const main = root.querySelector('.main');
  let toolbar = main.querySelector('.toolbar');
  let content = main.querySelector('.main__content');
  if (!toolbar) {
    main.append(
      (toolbar = h('div', { class: 'toolbar' })),
      h('div', { class: 'jobbar', hidden: true }),
      (content = h('div', { class: 'main__content' })));
  }
  // the toolbar is left alone while the search field has focus, so typing is never interrupted by a redraw
  if (!toolbar.contains(document.activeElement)) toolbar.replaceChildren(...toolbarItems());

  const filtered = view.tags.length || view.untagged || view.q || view.scope !== 'all';
  fill(content,
    h('div', { class: 'summary' },
      h('label', { class: 'check' },
        h('input', {
          type: 'checkbox', 'aria-label': t('Select all'),
          checked: view.items.length > 0 && view.selected.size === view.items.length,
          onchange: (e) => {
            view.selected = new Set(e.target.checked ? view.items.map((b) => b.id) : []);
            view.allMatching = false;
            drawMain();
          },
        }),
        h('strong', null, t('{n} bookmarks', { n: view.total }))),
      view.tags.map((tag) => h('button', {
        type: 'button', class: 'chip chip--filter', title: t('Remove filter'),
        onclick: () => { view.tags = view.tags.filter((x) => x !== tag); reload(); },
      }, tag, ' ×')),
      view.untagged && h('span', { class: 'chip' }, t('Untagged')),
      filtered && h('button', {
        type: 'button', class: 'linklike small',
        onclick: () => { Object.assign(view, { tags: [], untagged: false, q: '', scope: 'all' }); reload(); },
      }, t('Clear filters'))),
    bulkBar(),
    h('ul', { class: 'list' }, view.items.map(rowEl)),
    !view.items.length && h('p', { class: 'empty muted' },
      filtered ? t('No bookmarks match.') : t('No bookmarks yet. Add one with the + Add button or import your browser bookmarks from Tools.')),
    view.items.length < view.total && h('button', { type: 'button', class: 'btn more', onclick: loadMore },
      t('Show more ({n} left)', { n: view.total - view.items.length })));
  drawJob();
}

function toolbarItems() {
  const search = h('input', {
    type: 'search', class: 'input mybm__search', placeholder: t('Search titles, addresses, tags and notes…'),
    value: view.q, autocomplete: 'off',
  });
  let timer = null;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => { view.q = search.value; reload(); }, 250);
  });
  const select = (options, value, onchange, label) => h('select', {
    class: 'input', 'aria-label': label, onchange: (e) => onchange(e.target.value),
  }, options.map(([v, text]) => h('option', { value: v, selected: v === value }, text)));
  return [
    search,
    select([['exact', t('Exact phrase')], ['all', t('All of the words')], ['any', t('Any of the words')]],
      view.mode, (v) => { view.mode = v; if (view.q) reload(); }, t('Search mode')),
    select([['newest', t('Newest first')], ['oldest', t('Oldest first')], ['title', t('Title A–Z')],
      ['title_desc', t('Title Z–A')], ['domain', t('By site')]],
      view.sort, (v) => { view.sort = v; reload(); }, t('Order')),
    h('button', { type: 'button', class: 'btn', onclick: (e) => toolsMenu(e.currentTarget) }, t('Tools'), icon('chevron')),
  ];
}

function rowEl(b) {
  const checked = view.selected.has(b.id);
  return h('li', { class: `row-bm${checked ? ' selected' : ''}`, oncontextmenu: (e) => rowMenu(e, b) },
    h('input', {
      type: 'checkbox', checked, 'aria-label': t('Select'),
      onchange: (e) => {
        if (e.target.checked) view.selected.add(b.id); else view.selected.delete(b.id);
        view.allMatching = false;
        drawMain();
      },
    }),
    favicon(b.url, state.user.settings.favicons),
    h('div', { class: 'row-bm__main' },
      h('a', { class: `row-bm__title c-${b.color || 'none'}`, ...linkAttrs(b.url) }, b.title),
      h('div', { class: 'row-bm__url' }, b.url),
      b.notes && h('div', { class: 'row-bm__notes' }, b.notes),
      h('div', { class: 'row-bm__meta' },
        b.tags.map((tag) => h('button', {
          type: 'button', class: 'chip chip--tag',
          onclick: () => { if (!view.tags.includes(tag)) { view.tags = [...view.tags, tag]; view.untagged = false; reload(); } },
        }, tag)),
        h('span', { class: 'muted' }, locationLabel(b)),
        h('span', { class: 'muted' }, fmtDate(b.created_at)))),
    h('button', {
      type: 'button', class: 'iconbtn', title: t('Menu'), 'aria-label': t('Menu'),
      onclick: (e) => rowMenu(e.currentTarget, b),
    }, icon('menu')));
}

function rowMenu(at, b) {
  showMenu(at, [
    { label: t('Edit'), action: async () => { if (await bookmarkDialog(b)) await changed(); } },
    { label: t('Move…'), action: async () => { if (await moveDialog({ ids: [b.id] }, 1)) await changed(); } },
    b.category_id != null && { label: t('Move to Catalog'), action: () => bulk({ action: 'move', category_id: null }, { ids: [b.id] }) },
    { label: t('Copy link'), action: () => copyText(b.url) },
    { label: t('Send by email'), action: () => mailBookmarks([b]) },
    { sep: true },
    {
      label: t('Delete'), danger: true,
      action: async () => {
        if (await confirmBox(t('Delete “{name}”?', { name: b.title }), { okLabel: t('Delete'), danger: true })) {
          await bulk({ action: 'delete' }, { ids: [b.id] });
        }
      },
    },
  ]);
}

// --- bulk operations ---------------------------------------------------------

const selectionTarget = () => (view.allMatching ? { filter: currentFilter() } : { ids: [...view.selected] });
const selectionCount = () => (view.allMatching ? view.total : view.selected.size);

async function bulk(body, target = selectionTarget()) {
  await attempt(async () => {
    const { count } = await api('POST', '/api/bookmarks/bulk', { ...body, ...target });
    toast(t('{n} bookmarks updated', { n: count }));
    await changed();
  });
}

function bulkBar() {
  if (!view.selected.size) return null;
  const n = selectionCount();
  const allLoadedSelected = view.selected.size === view.items.length && view.total > view.items.length;
  const button = (label, onclick, kind = '') => h('button', { type: 'button', class: `btn small ${kind}`, onclick }, label);
  return h('div', { class: 'bulkbar' },
    h('strong', null, t('{n} selected', { n })),
    allLoadedSelected && !view.allMatching && h('button', {
      type: 'button', class: 'linklike small', onclick: () => { view.allMatching = true; drawMain(); },
    }, t('Select all {n} matching', { n: view.total })),
    button(t('Move…'), async () => { if (await moveDialog(selectionTarget(), n)) await changed(); }),
    button(t('Move to Catalog'), () => bulk({ action: 'move', category_id: null })),
    button(t('Add tags'), () => tagsDialog('add_tags')),
    button(t('Remove tags'), () => tagsDialog('remove_tags')),
    button(t('Replace tag'), replaceTagDialog),
    !view.allMatching && button(t('Send by email'), () => mailBookmarks(view.items.filter((b) => view.selected.has(b.id)))),
    button(t('Delete'), async () => {
      if (await confirmBox(t('Delete {n} bookmarks? This cannot be undone.', { n }), { okLabel: t('Delete'), danger: true })) {
        await bulk({ action: 'delete' });
      }
    }, 'danger'),
    button(t('Clear selection'), () => { view.selected.clear(); view.allMatching = false; drawMain(); }));
}

async function tagsDialog(action) {
  const input = tagInput([]);
  const target = selectionTarget();
  const n = selectionCount();
  const title = action === 'add_tags' ? t('Add tags to {n} bookmarks', { n }) : t('Remove tags from {n} bookmarks', { n });
  const tags = await openModal(title, h('div', { class: 'field' }, h('span', null, t('Tags')), input.el), [
    { label: t('Cancel') },
    { label: action === 'add_tags' ? t('Add tags') : t('Remove tags'), kind: 'primary', action: () => (input.get().length ? input.get() : false) },
  ]);
  if (tags) await bulk({ action, tags }, target);
}

async function replaceTagDialog() {
  const target = selectionTarget();
  const from = h('input', { type: 'text', class: 'input', required: true, autocomplete: 'off' });
  const to = h('input', { type: 'text', class: 'input', required: true, autocomplete: 'off' });
  const ok = await openModal(t('Replace tag'), h('div', { class: 'form' },
    h('p', { class: 'muted' }, t('Only the selected bookmarks are changed.')),
    h('label', { class: 'field' }, h('span', null, t('Tag')), from),
    h('label', { class: 'field' }, h('span', null, t('Replace with')), to)), [
    { label: t('Cancel') },
    { label: t('Replace'), kind: 'primary', action: () => true },
  ]);
  if (ok) await bulk({ action: 'replace_tag', old: from.value, new: to.value }, target);
}

// --- tools -------------------------------------------------------------------

function toolsMenu(at) {
  showMenu(at, [
    { label: t('Find duplicates…'), action: duplicatesDialog },
    { label: t('Find dead links…'), action: deadLinksDialog },
    { sep: true },
    { label: t('Rename tag…'), action: () => renameTagDialog() },
    { sep: true },
    { label: t('Import bookmarks…'), action: async () => { if (await importDialog()) await changed(); } },
    { label: t('Export bookmarks'), action: exportBookmarks },
  ]);
}

export function exportBookmarks() {
  location.href = '/api/export';
}

async function duplicatesDialog() {
  const strict = h('input', { type: 'checkbox', checked: true });
  const tag = h('input', { type: 'text', class: 'input', value: 'duplicate', required: true });
  const onlyDupes = h('input', { type: 'radio', name: 'mark', checked: true });
  const both = h('input', { type: 'radio', name: 'mark' });
  const result = await openModal(t('Find duplicates'), h('div', { class: 'form' },
    h('p', { class: 'muted' }, t('Bookmarks with the same address get a tag, so you can review and delete them.')),
    h('label', { class: 'check' }, strict, h('span', null, t('Strict match (otherwise http/https and www. are ignored)'))),
    h('label', { class: 'field' }, h('span', null, t('Tag')), tag),
    h('label', { class: 'check' }, onlyDupes, h('span', null, t('Tag only the duplicates (the oldest copy stays untagged)'))),
    h('label', { class: 'check' }, both, h('span', null, t('Tag both originals and duplicates')))), [
    { label: t('Cancel') },
    {
      label: t('Find duplicates'), kind: 'primary',
      action: () => api('POST', '/api/tools/duplicates', { strict: strict.checked, tag: tag.value, mark: both.checked ? 'both' : 'duplicates' }),
    },
  ]);
  if (!result) return;
  if (!result.duplicates) {
    toast(t('No duplicates found.'));
    await changed();
    return;
  }
  toast(t('{n} duplicates found', { n: result.duplicates }));
  Object.assign(view, { scope: 'all', tags: [result.tag], untagged: false, q: '', sort: 'domain' });
  await changed();
}

async function deadLinksDialog() {
  const tag = h('input', { type: 'text', class: 'input', value: 'dead-link', required: true });
  const job = await openModal(t('Find dead links'), h('div', { class: 'form' },
    h('p', { class: 'muted' }, t('Every bookmark is checked in the background. Links that answer “404 not found” or whose site no longer exists get a tag. You can keep working or close the page meanwhile.')),
    h('label', { class: 'field' }, h('span', null, t('Tag')), tag)), [
    { label: t('Cancel') },
    { label: t('Start'), kind: 'primary', action: () => api('POST', '/api/tools/deadlinks', { tag: tag.value }) },
  ]);
  if (!job) return;
  view.job = job;
  drawJob();
  pollJob();
}

async function checkDeadLinkJob() {
  try {
    view.job = await api('GET', '/api/tools/deadlinks');
  } catch {
    return;
  }
  drawJob();
  if (view.job.status === 'running') pollJob();
}

function pollJob() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!mounted()) return;
    try {
      view.job = await api('GET', '/api/tools/deadlinks');
    } catch {
      return;
    }
    drawJob();
    if (view.job.status === 'running') pollJob();
    else invalidateTags();
  }, 2000);
}

const jobSeenKey = (job) => `stash.job.${job.started}`;

function drawJob() {
  const bar = mounted() && root.querySelector('.jobbar');
  const job = view.job;
  if (!bar) return;
  let seen = false;
  try { seen = job && sessionStorage.getItem(jobSeenKey(job)); } catch { /* ignore */ }
  if (!job || job.status === 'idle' || seen) { bar.hidden = true; return; }
  bar.hidden = false;
  if (job.status === 'running') {
    bar.replaceChildren(
      h('progress', { max: job.total || 1, value: job.checked }),
      h('span', null, t('Checking links… {a} / {b}, {n} dead so far', { a: job.checked, b: job.total, n: job.dead })));
    return;
  }
  const dismiss = () => { try { sessionStorage.setItem(jobSeenKey(job), '1'); } catch { /* ignore */ } bar.hidden = true; };
  bar.replaceChildren(
    h('span', null, job.status === 'done'
      ? t('Link check finished: {b} checked, {n} dead links found.', { b: job.checked, n: job.dead })
      : t('The link check stopped unexpectedly.')),
    job.dead > 0 && h('button', {
      type: 'button', class: 'btn small',
      onclick: () => { dismiss(); Object.assign(view, { scope: 'all', tags: [job.tag], untagged: false, q: '' }); reload(); },
    }, t('Show them')),
    h('button', { type: 'button', class: 'linklike small', onclick: dismiss }, t('Dismiss')));
}

export async function importDialog() {
  const file = h('input', { type: 'file', accept: '.html,.htm,text/html', required: true, class: 'input' });
  const radio = (value, checked) => h('input', { type: 'radio', name: 'import-mode', value, checked });
  const toCatalog = radio('catalog', true);
  const toTab = radio('tab');
  const toStructure = radio('structure');
  const folderTags = h('input', { type: 'checkbox', checked: true });
  const tabName = h('input', { type: 'text', class: 'input', value: t('Imported'), maxLength: 100 });
  const skip = h('input', { type: 'checkbox', checked: true });
  const body = h('div', { class: 'form' },
    h('p', { class: 'muted' }, t('Choose the bookmarks HTML file exported from your browser or another bookmark manager.')),
    file,
    h('label', { class: 'check' }, toCatalog, h('span', null, t('Into the Catalog'))),
    h('label', { class: 'check indent' }, folderTags, h('span', null, t('Turn folder names into tags'))),
    h('label', { class: 'check' }, toTab, h('span', null, t('Into one new Dashboard tab (each folder becomes a category)'))),
    h('label', { class: 'field indent' }, h('span', null, t('Tab name')), tabName),
    h('label', { class: 'check' }, toStructure, h('span', null, t('Rebuild the structure: top-level folders become tabs, their subfolders categories'))),
    h('hr'),
    h('label', { class: 'check' }, skip, h('span', null, t('Skip addresses I already have'))));
  const stats = await openModal(t('Import bookmarks'), body, [
    { label: t('Cancel') },
    {
      label: t('Import'), kind: 'primary',
      action: async () => {
        if (!file.files[0]) return false;
        const mode = toTab.checked ? 'tab' : toStructure.checked ? 'structure' : 'catalog';
        const query = new URLSearchParams({
          mode, tab_name: tabName.value.trim() || 'Imported', folder_tags: folderTags.checked, skip_duplicates: skip.checked,
        });
        return api('POST', `/api/import?${query}`, undefined, { raw: await file.files[0].text() });
      },
    },
  ]);
  if (!stats) return false;
  await openModal(t('Import finished'), h('ul', { class: 'plain' },
    h('li', null, t('{n} bookmarks imported', { n: stats.imported })),
    stats.duplicates > 0 && h('li', null, t('{n} skipped because you already had them', { n: stats.duplicates })),
    stats.skipped > 0 && h('li', null, t('{n} skipped (not web addresses, e.g. browser-internal links)', { n: stats.skipped }))), [
    { label: t('OK'), kind: 'primary', action: () => true },
  ]);
  invalidateTags();
  return true;
}
