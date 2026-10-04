// The "Add to Stash" window opened by the bookmarklet and the browser extension.
import { renderAuth } from './auth.js';
import { api, applyTheme, fill, confirmBox, h, loadDash, state } from './core.js';
import { setLang, t } from './i18n.js';
import { bookmarkForm, invalidateTags, locationPicker, rememberLocation, saveBookmark, tagInput } from './widgets.js';

const app = document.getElementById('app');
// The bookmarklet and the extension pass the page in the fragment, which is never sent to the server (or its logs).
const params = new URLSearchParams(location.hash.length > 1 ? location.hash.slice(1) : location.search);
// the popup window is reused: a new fragment means a new page to bookmark
window.addEventListener('hashchange', () => location.reload());

/** The extension passes all open tabs as #batch=<base64url JSON [{url, title}]>. */
function readBatch() {
  const raw = params.get('batch');
  if (!raw) return null;
  try {
    const bytes = Uint8Array.from(atob(raw.replace(/-/g, '+').replace(/_/g, '/')), (ch) => ch.charCodeAt(0));
    const list = JSON.parse(new TextDecoder().decode(bytes));
    return Array.isArray(list) ? list.filter((x) => x && typeof x.url === 'string').slice(0, 500) : null;
  } catch {
    return null;
  }
}

function finish(message) {
  fill(app, h('div', { class: 'addpage done' },
    h('p', { class: 'done__mark' }, '✓'),
    h('p', null, message),
    h('p', null, h('a', { class: 'btn', href: '/' }, t('Open Stash')))));
  setTimeout(() => {
    if (params.get('back')) history.go(-(history.length > 1 ? 1 : 0));
    else window.close(); // only works for windows opened by script, i.e. the popup
  }, 700);
}

async function boot() {
  try {
    state.user = await api('GET', '/api/me');
  } catch (e) {
    if (e.status === 401) return renderAuth(app, boot);
    fill(app, h('p', { class: 'empty' }, e.message));
    return undefined;
  }
  setLang(state.user.settings.lang);
  document.title = t('Add to Stash');
  applyTheme(state.user.settings.theme);
  await loadDash();
  const batch = readBatch();
  return batch ? batchPage(batch) : singlePage();
}

async function singlePage(existing) {
  const defaults = { url: params.get('url') || '', title: params.get('title') || '', notes: params.get('notes') || '' };
  let bookmark = existing || null;
  if (!bookmark && defaults.url) {
    try {
      bookmark = (await api('POST', '/api/bookmarks/lookup', { url: defaults.url })).bookmark;
    } catch { /* treated as new */ }
  }
  const form = bookmarkForm(bookmark, defaults);
  form.setOnExisting((found) => singlePage(found));
  const error = h('p', { class: 'form-error', hidden: true });
  const submit = h('button', { type: 'submit', class: 'btn primary' }, bookmark ? t('Save') : t('Add'));
  const guard = async (fn) => {
    error.hidden = true;
    submit.disabled = true;
    try {
      await fn();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
    }
  };

  fill(app, h('form', {
    class: 'addpage',
    onsubmit: (e) => {
      e.preventDefault();
      guard(async () => {
        await saveBookmark(bookmark, form.value());
        finish(bookmark ? t('Bookmark updated') : t('Bookmark added'));
      });
    },
  },
    h('h1', null, bookmark ? t('Edit bookmark') : t('Add bookmark')),
    bookmark && h('div', { class: 'notice' }, t('This page is already bookmarked – you are editing the existing bookmark.')),
    form.el,
    error,
    h('div', { class: 'row end' },
      bookmark && h('button', {
        type: 'button', class: 'btn danger-quiet',
        onclick: () => guard(async () => {
          if (!await confirmBox(t('Delete this bookmark?'), { okLabel: t('Delete'), danger: true })) return;
          await api('DELETE', `/api/bookmarks/${bookmark.id}`);
          finish(t('Bookmark deleted'));
        }),
      }, t('Delete')),
      h('span', { class: 'grow' }),
      h('button', { type: 'button', class: 'btn', onclick: () => finish(t('Nothing was saved')) }, t('Cancel')),
      submit)));
}

function batchPage(batch) {
  const rows = batch.map((item) => ({ ...item, box: h('input', { type: 'checkbox', checked: true }) }));
  const location_ = locationPicker();
  const tags = tagInput([]);
  const error = h('p', { class: 'form-error', hidden: true });
  const submit = h('button', { type: 'submit', class: 'btn primary' }, t('Add'));

  fill(app, h('form', {
    class: 'addpage',
    onsubmit: async (e) => {
      e.preventDefault();
      error.hidden = true;
      submit.disabled = true;
      try {
        const category_id = location_.get();
        const chosen = rows.filter((r) => r.box.checked);
        const tagList = tags.get();
        let failed = 0;
        for (const r of chosen) {
          try {
            await api('POST', '/api/bookmarks', { url: r.url, title: r.title || '', tags: tagList, category_id });
          } catch {
            failed += 1; // browser-internal pages (about:, chrome://) cannot be bookmarked
          }
        }
        rememberLocation(category_id);
        invalidateTags();
        finish(t('{n} bookmarks added', { n: chosen.length - failed }));
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
        submit.disabled = false;
      }
    },
  },
    h('h1', null, t('Bookmark all open tabs')),
    h('ul', { class: 'plain batch' }, rows.map((r) => h('li', null, h('label', { class: 'check' }, r.box,
      h('span', null, r.title || r.url, h('small', { class: 'muted block' }, r.url)))))),
    h('div', { class: 'form' },
      h('div', { class: 'field' }, h('span', null, t('Location')), location_.el),
      h('div', { class: 'field' }, h('span', null, t('Tags')), tags.el)),
    error,
    h('div', { class: 'row end' },
      h('button', { type: 'button', class: 'btn', onclick: () => finish(t('Nothing was saved')) }, t('Cancel')),
      submit)));
}

boot();
