// Reusable form pieces: tag input, Dashboard/Catalog location picker, bookmark form and dialogs.
import { api, confirmBox, h, loadDash, openModal, promptBox, state, toast } from './core.js';
import { t } from './i18n.js';

let tagCache = null;
export async function allTags() {
  if (!tagCache) tagCache = (await api('GET', '/api/tags')).tags.map(([tag]) => tag);
  return tagCache;
}
export const invalidateTags = () => { tagCache = null; };

export const normTag = (s) => s.replace(/,/g, ' ').trim().replace(/\s+/g, ' ').toLowerCase().slice(0, 50);

export function tagInput(initial = [], { placeholder = t('Add tags…') } = {}) {
  let tags = [...initial];
  let options = [];
  let active = -1;
  allTags().then((all) => { options = all; }).catch(() => {});
  const chips = h('span', { class: 'chips' });
  const input = h('input', {
    type: 'text', class: 'taginput__field', placeholder, autocomplete: 'off', autocapitalize: 'none', spellcheck: false,
  });
  const list = h('div', { class: 'suggest', hidden: true });
  const el = h('div', { class: 'taginput', onclick: () => input.focus() }, chips, input, list);

  const renderChips = () => chips.replaceChildren(...tags.map((tag) => h('span', { class: 'chip' }, tag,
    h('button', {
      type: 'button', 'aria-label': t('Remove'),
      onclick: (e) => { e.stopPropagation(); tags = tags.filter((x) => x !== tag); renderChips(); },
    }, '×'))));
  const hide = () => { list.hidden = true; active = -1; };
  const add = (raw) => {
    for (const part of raw.split(',')) {
      const tag = normTag(part);
      if (tag && !tags.includes(tag)) tags.push(tag);
    }
    input.value = '';
    renderChips();
    hide();
  };
  const suggest = () => {
    const q = normTag(input.value);
    if (!q) return hide();
    const free = options.filter((o) => !tags.includes(o));
    const found = [...free.filter((o) => o.startsWith(q)), ...free.filter((o) => !o.startsWith(q) && o.includes(q))].slice(0, 8);
    if (!found.length) return hide();
    active = -1;
    list.replaceChildren(...found.map((o) => h('button', {
      type: 'button', class: 'suggest__item', tabIndex: -1,
      onmousedown: (e) => { e.preventDefault(); add(o); },
    }, o)));
    list.hidden = false;
  };

  input.addEventListener('input', () => (input.value.includes(',') ? add(input.value) : suggest()));
  input.addEventListener('keydown', (e) => {
    const items = [...list.children];
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !list.hidden) {
      e.preventDefault();
      active = e.key === 'ArrowDown' ? (active + 1) % items.length : (active <= 0 ? items.length - 1 : active - 1);
      items.forEach((item, i) => item.classList.toggle('on', i === active));
    } else if (e.key === 'Enter') {
      if (active >= 0 && !list.hidden) { e.preventDefault(); add(items[active].textContent); }
      else if (input.value.trim()) { e.preventDefault(); add(input.value); }
    } else if (e.key === 'Backspace' && !input.value && tags.length) {
      tags.pop();
      renderChips();
    } else if (e.key === 'Escape' && !list.hidden) {
      e.preventDefault();
      hide();
    }
  });
  input.addEventListener('blur', () => { if (input.value.trim()) add(input.value); hide(); });
  renderChips();
  return {
    el,
    get: () => { if (input.value.trim()) add(input.value); return [...tags]; },
    set: (next) => { tags = [...next]; renderChips(); },
  };
}

const LAST_LOCATION = 'stash.lastLocation';
function lastLocation() {
  try {
    const raw = localStorage.getItem(LAST_LOCATION);
    if (raw === 'catalog') return null;
    const id = Number(raw);
    return state.dash.categories.some((c) => c.id === id) ? id : undefined;
  } catch {
    return undefined;
  }
}
export function rememberLocation(categoryId) {
  try { localStorage.setItem(LAST_LOCATION, categoryId == null ? 'catalog' : String(categoryId)); } catch { /* ignore */ }
}

/** Dashboard (tab + category) or Catalog. initial: category id, null = Catalog, undefined = last used. */
export function locationPicker(initial) {
  if (initial === undefined) initial = lastLocation();
  if (initial === undefined) initial = state.dash.categories[0]?.id ?? null;
  const name = `loc-${Math.random().toString(36).slice(2)}`;
  const onDash = h('input', { type: 'radio', name, checked: initial !== null });
  const onCatalog = h('input', { type: 'radio', name, checked: initial === null });
  const tabSel = h('select', { class: 'input', 'aria-label': t('Tab') });
  const catSel = h('select', { class: 'input', 'aria-label': t('Category') });
  const find = h('input', { type: 'search', class: 'input', placeholder: t('Find category…'), autocomplete: 'off' });
  const found = h('div', { class: 'suggest suggest--static', hidden: true });

  const fillTabs = (tabId) => {
    tabSel.replaceChildren(...state.dash.tabs.map((tab) => h('option', { value: tab.id, selected: tab.id === tabId }, tab.name)));
  };
  const fillCats = (catId) => {
    const tabId = Number(tabSel.value);
    const cats = state.dash.categories.filter((c) => c.tab_id === tabId)
      .sort((a, b) => a.name.localeCompare(b.name));
    catSel.replaceChildren(...cats.map((c) => h('option', { value: c.id, selected: c.id === catId }, c.name)));
  };
  const select = (catId) => {
    const cat = state.dash.categories.find((c) => c.id === catId);
    fillTabs(cat ? cat.tab_id : Number(tabSel.value) || state.dash.tabs[0]?.id);
    fillCats(catId);
  };
  select(initial);
  tabSel.addEventListener('change', () => fillCats());

  find.addEventListener('input', () => {
    const q = find.value.trim().toLowerCase();
    const hits = q ? state.dash.categories.filter((c) => c.name.toLowerCase().includes(q)).slice(0, 8) : [];
    found.hidden = !hits.length;
    found.replaceChildren(...hits.map((c) => h('button', {
      type: 'button', class: 'suggest__item',
      onclick: () => { select(c.id); find.value = ''; found.hidden = true; },
    }, `${state.dash.tabs.find((tab) => tab.id === c.tab_id)?.name} › ${c.name}`)));
  });

  const addTab = async () => {
    const tabName = await promptBox(t('New tab'), { label: t('Name'), okLabel: t('Add') });
    if (!tabName) return;
    const { id } = await api('POST', '/api/tabs', { name: tabName });
    await loadDash();
    fillTabs(id);
    fillCats();
  };
  const addCat = async () => {
    if (!tabSel.value) return toast(t('Add a tab first.'), 'error');
    const catName = await promptBox(t('New category'), { label: t('Name'), okLabel: t('Add') });
    if (!catName) return;
    const { id } = await api('POST', '/api/categories', { tab_id: Number(tabSel.value), name: catName });
    await loadDash();
    fillCats(id);
  };
  const guarded = (fn) => () => fn().catch((e) => toast(e.message, 'error'));

  const dashBox = h('div', { class: 'loc__dash' },
    h('div', { class: 'loc__find' }, find, found),
    h('div', { class: 'row' }, tabSel,
      h('button', { type: 'button', class: 'btn', title: t('New tab'), onclick: guarded(addTab) }, '+')),
    h('div', { class: 'row' }, catSel,
      h('button', { type: 'button', class: 'btn', title: t('New category'), onclick: guarded(addCat) }, '+')));
  const sync = () => { dashBox.hidden = onCatalog.checked; };
  onDash.addEventListener('change', sync);
  onCatalog.addEventListener('change', sync);
  sync();

  return {
    el: h('div', { class: 'loc' },
      h('div', { class: 'seg' },
        h('label', null, onDash, h('span', null, t('Dashboard'))),
        h('label', null, onCatalog, h('span', null, t('Catalog')))),
      dashBox),
    /** category id, null for the Catalog; throws when the Dashboard is chosen without a category */
    get: () => {
      if (onCatalog.checked) return null;
      if (!catSel.value) throw new Error(t('Choose a category (or create one with +).'));
      return Number(catSel.value);
    },
  };
}

export function locationLabel(bookmark) {
  if (bookmark.category_id == null) return t('Catalog');
  const cat = state.dash.categories.find((c) => c.id === bookmark.category_id);
  const tab = cat && state.dash.tabs.find((x) => x.id === cat.tab_id);
  return cat ? `${tab?.name} › ${cat.name}` : t('Dashboard');
}

/** The form shared by the Add/Edit dialog and the bookmarklet popup. */
export function bookmarkForm(bookmark, defaults = {}) {
  const src = bookmark || defaults;
  const url = h('input', {
    type: 'text', class: 'input', required: true, maxLength: 2000, value: src.url || '', inputMode: 'url',
    autocomplete: 'off', autocapitalize: 'none', spellcheck: false, placeholder: 'https://… / name@example.com / +358…',
  });
  const title = h('input', { type: 'text', class: 'input', maxLength: 500, value: src.title || '', autocomplete: 'off' });
  const notes = h('textarea', { class: 'input', rows: 3, maxLength: 5000, value: src.notes || '' });
  const tags = tagInput(src.tags || []);
  const location = locationPicker(bookmark ? bookmark.category_id : defaults.category_id);
  const notice = h('div', { class: 'notice', hidden: true });

  let titleTouched = Boolean(src.title);
  title.addEventListener('input', () => { titleTouched = true; });
  url.addEventListener('change', async () => {
    const value = url.value.trim();
    if (!value) return;
    if (!bookmark) checkExisting(value);
    if (titleTouched || /^(mailto:|tel:)/i.test(value)) return;
    try {
      const { title: fetched } = await api('POST', '/api/fetch-title', { url: value });
      if (fetched && !titleTouched) title.value = fetched;
    } catch { /* the title stays empty; the server falls back to the URL */ }
  });

  let onExisting = null;
  async function checkExisting(value) {
    try {
      const { bookmark: existing } = await api('POST', '/api/bookmarks/lookup', { url: value });
      notice.hidden = !existing;
      if (existing) {
        notice.replaceChildren(
          h('span', null, `${t('Already bookmarked')}: ${locationLabel(existing)}`),
          h('button', { type: 'button', class: 'btn small', onclick: () => onExisting?.(existing) }, t('Edit it')));
      }
      return existing;
    } catch {
      return null;
    }
  }

  return {
    el: h('div', { class: 'form' },
      notice,
      h('label', { class: 'field' }, h('span', null, t('Address')), url),
      h('label', { class: 'field' }, h('span', null, t('Title')), title),
      h('div', { class: 'field' }, h('span', null, t('Location')), location.el),
      h('div', { class: 'field' }, h('span', null, t('Tags')), tags.el),
      h('label', { class: 'field' }, h('span', null, t('Notes')), notes)),
    value: () => ({
      url: url.value.trim(), title: title.value.trim(), notes: notes.value, tags: tags.get(), category_id: location.get(),
    }),
    checkExisting,
    setOnExisting: (fn) => { onExisting = fn; },
  };
}

export async function saveBookmark(bookmark, value) {
  if (bookmark) await api('PATCH', `/api/bookmarks/${bookmark.id}`, value);
  else await api('POST', '/api/bookmarks', value);
  rememberLocation(value.category_id);
  invalidateTags();
}

/** Add (bookmark = null) or edit dialog. Resolves truthy when something changed. */
export async function bookmarkDialog(bookmark, defaults = {}) {
  let switchTo = null;
  const form = bookmarkForm(bookmark, defaults);
  const buttons = [
    { label: t('Cancel') },
    { label: bookmark ? t('Save') : t('Add'), kind: 'primary', action: () => saveBookmark(bookmark, form.value()) },
  ];
  if (bookmark) {
    buttons.unshift({
      label: t('Delete'), kind: 'danger-quiet',
      action: async () => {
        if (!await confirmBox(t('Delete this bookmark?'), { okLabel: t('Delete'), danger: true })) return false;
        await api('DELETE', `/api/bookmarks/${bookmark.id}`);
        return true;
      },
    });
  }
  form.setOnExisting((existing) => {
    switchTo = existing;
    document.querySelector('dialog[open]:last-of-type')?.close();
  });
  const result = await openModal(bookmark ? t('Edit bookmark') : t('Add bookmark'), form.el, buttons);
  if (switchTo) return bookmarkDialog(switchTo);
  return result;
}

/** Ask for a destination and move bookmarks there. target: {ids} or {filter}. */
export async function moveDialog(target, count) {
  const location = locationPicker();
  return openModal(t('Move {n} bookmarks', { n: count }), location.el, [
    { label: t('Cancel') },
    {
      label: t('Move'), kind: 'primary',
      action: async () => {
        const category_id = location.get();
        await api('POST', '/api/bookmarks/bulk', { action: 'move', ...target, category_id });
        rememberLocation(category_id);
        invalidateTags();
      },
    },
  ]);
}
