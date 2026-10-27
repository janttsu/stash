// Playlists: Spotify and YouTube playlists copied into Stash, edited here (by hand or by the assistant over
// MCP) as pending changes, and pushed back to the service when you say so. Also the guide for connecting.
import { api, attempt, confirmBox, copyText, fill, fmtDate, h, openModal, safeHref, toast } from './core.js';
import { t } from './i18n.js';

let root = null;
// problem: a youtube_api_disabled:<project> code to explain; progress: a running fetch; result: the last one's
const view = { playlist: null, problem: '', progress: null, result: null };

export async function renderPlaylists(container, params = {}) {
  root = container;
  root.className = 'view playlists';
  if (params.playlist) view.playlist = Number(params.playlist);
  if (params.connected) toast(t('{service} connected', { service: serviceName(params.connected) }));
  if (params.error) toast(t('Connecting failed: {error}', { error: params.error }), 'error');
  if (params.problem) view.problem = params.problem;
  if (params.connected || params.error || params.problem) history.replaceState(null, '', '#/playlists');
  await draw();
  // right after connecting, the first fetch starts by itself
  if (params.fetch && params.connected) await fetchAll(params.connected);
}

/** Both ways: push every playlist's pending changes (after asking, naming them), then fetch everything. */
async function syncAll(service, playlists) {
  const pending = playlists.filter((p) => p.service === service && p.pending && p.editable && !p.gone);
  if (pending.length && !await confirmBox(t('Push the pending changes of {n} playlists to {service} and then fetch everything back? {names}',
    { n: pending.length, service: serviceName(service), names: pending.map((p) => p.name).join(', ') }), { okLabel: t('Synchronise') })) return;
  await fetchAll(service, { both: true });
}

/** Fetch every playlist of a service, with a progress bar while it runs and a summary when it is done. */
async function fetchAll(service, { both = false } = {}) {
  if (view.progress) return;
  view.problem = '';
  view.result = null;
  view.progress = { service, phase: both ? 'push' : 'fetch', done: 0, total: 0, name: '', items: 0 };
  await draw();
  let polling = true;
  const poll = async () => {
    while (polling) {
      await new Promise((r) => setTimeout(r, 700));
      if (!polling) break;
      try {
        const p = await api('GET', `/api/playlists/accounts/${service}/progress`);
        if (p.running && polling) { view.progress = { service, ...p }; drawProgress(); }
      } catch { /* the fetch itself reports errors */ }
    }
  };
  poll();
  try {
    const r = await api('POST', `/api/playlists/accounts/${service}/${both ? 'sync-all' : 'sync'}`);
    view.result = { service, ...r };
  } catch (e) {
    if (String(e.code).startsWith('youtube_api_disabled')) view.problem = e.code;
    else view.result = { service, error: e.message };
  } finally {
    polling = false;
    view.progress = null;
  }
  await draw();
}

function progressBox() {
  const p = view.progress;
  const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
  const pushing = p.phase === 'push';
  return h('div', { class: 'pl__progress', id: 'pl-progress', role: 'status' },
    h('div', { class: 'row' },
      h('b', null, pushing ? t('Pushing changes to {service}…', { service: serviceName(p.service) })
        : t('Fetching playlists from {service}…', { service: serviceName(p.service) })),
      h('span', { class: 'muted small grow' }, pushing
        ? t('{done} of {total} playlists', { done: p.done, total: p.total })
        : p.total ? t('{done} of {total} playlists · {items} items', { done: p.done, total: p.total, items: p.items })
          : t('Listing playlists…'))),
    h('progress', { max: p.total || 1, value: p.total ? p.done : undefined, 'aria-label': `${pct}%` }),
    p.name ? h('div', { class: 'row pl__progress-list' },
      h('span', { class: 'small grow' }, p.name),
      !pushing && p.list_total ? h('span', { class: 'muted small' },
        t('{read} of {total} videos from this list', { read: p.list_read || 0, total: p.list_total })) : null) : null,
    p.name && !pushing && p.list_total ? h('progress', { class: 'pl__progress-sub', max: p.list_total, value: p.list_read || 0 }) : null);
}

function drawProgress() {
  const el = document.getElementById('pl-progress');
  if (el && view.progress) el.replaceWith(progressBox());
}

function resultBox() {
  const r = view.result;
  const close = h('button', { type: 'button', class: 'btn small', onclick: () => { view.result = null; draw(); } }, t('Close'));
  if (r.error) {
    return h('div', { class: 'pl__result pl__result--bad', role: 'status' },
      h('div', { class: 'row' }, h('b', { class: 'grow' }, t('Fetching from {service} failed', { service: serviceName(r.service) })), close),
      h('div', { class: 'small' }, r.error));
  }
  const pushFailed = r.push_failed || [];
  const ok = r.failed.length === 0 && pushFailed.length === 0;
  return h('div', { class: `pl__result ${ok ? 'pl__result--ok' : 'pl__result--bad'}`, role: 'status' },
    h('div', { class: 'row' },
      h('b', { class: 'grow' }, t('{fetched} of {total} playlists fetched from {service} ({items} items)',
        { fetched: r.fetched, total: r.total, service: serviceName(r.service), items: r.items })),
      close),
    r.pushed ? h('div', { class: 'small' }, r.pushed.length
      ? t('Changes pushed for {n} of {total} playlists: {names}', { n: r.pushed.length, total: r.pushed.length + pushFailed.length, names: r.pushed.join(', ') })
      : pushFailed.length ? null : t('There were no pending changes to push.')) : null,
    pushFailed.length ? [
      h('div', { class: 'small' }, t('Pushing failed for {n} (their changes are still pending):', { n: pushFailed.length })),
      h('ul', { class: 'small' }, pushFailed.map((f) => h('li', null, `${f.name}: ${f.error}`))),
    ] : null,
    r.skipped_with_local_changes.length ? h('div', { class: 'small' },
      t('Left alone because of pending changes: {names}', { names: r.skipped_with_local_changes.join(', ') })) : null,
    r.failed.length ? [
      h('div', { class: 'small' }, t('{n} failed:', { n: r.failed.length })),
      h('ul', { class: 'small' }, r.failed.map((f) => h('li', null, `${f.name}: ${f.error}`))),
    ] : null);
}

/** Google answered that the YouTube Data API is not enabled in the project of the user's client. */
function problemBox() {
  const project = view.problem.split(':')[1] || '';
  const link = `https://console.cloud.google.com/apis/library/youtube.googleapis.com${project ? `?project=${project}` : ''}`;
  return h('div', { class: 'pl__result pl__result--bad', role: 'alert' },
    h('b', null, t('YouTube is connected, but the YouTube Data API is not enabled')),
    h('p', { class: 'small' }, t('Google allows the connection but refuses to give playlists until the YouTube Data API v3 is enabled in the same Google Cloud project as your client{project}.',
      { project: project ? ` (${project})` : '' })),
    h('ol', { class: 'small' },
      h('li', null, t('Open the API page and press “Enable”:'), ' ', h('a', { href: link, target: '_blank', rel: 'noopener noreferrer' }, t('YouTube Data API v3 in your project'))),
      h('li', null, t('Wait a minute: Google takes a moment to pass the change on.')),
      h('li', null, t('Press “Fetch playlists” below. There is no need to connect again.'))),
    h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn primary', onclick: () => fetchAll('youtube') }, t('Fetch playlists'))));
}

const serviceName = (s) => ({ spotify: 'Spotify', youtube: 'YouTube' })[s] || s;
const duration = (ms) => {
  const s = Math.floor((ms || 0) / 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return s >= 3600 ? `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}` : `${Math.floor(s / 60)}:${pad(s % 60)}`;
};
const act = (fn) => attempt(async () => { await fn(); await draw(); });

async function draw() {
  const data = await api('GET', '/api/playlists');
  if (view.playlist && !data.playlists.some((p) => p.id === view.playlist)) view.playlist = null;
  const detail = view.playlist ? await api('GET', `/api/playlists/lists/${view.playlist}?limit=5000`) : null;

  const accountCard = (a) => h('div', { class: 'pl__account' },
    h('div', { class: 'row' },
      h('b', null, serviceName(a.service)),
      h('span', { class: 'muted small grow' }, a.connected
        ? [t('Connected as {name}', { name: a.display_name || '…' }), a.synced_at && t('synced {date}', { date: fmtDate(a.synced_at) })].filter(Boolean).join(' · ')
        : t('Not connected')),
      a.connected && h('button', {
        type: 'button', class: 'btn primary', disabled: Boolean(view.progress), onclick: () => syncAll(a.service, data.playlists),
        title: t('Push the pending changes of every playlist, then fetch everything back'),
      }, t('Synchronise')),
      a.connected && h('button', {
        type: 'button', class: 'btn', disabled: Boolean(view.progress), onclick: () => fetchAll(a.service),
        title: t('Only fetch: playlists with pending changes are left alone'),
      }, t('Fetch playlists')),
      a.connected && h('button', { type: 'button', class: 'btn', onclick: () => createDialog(a.service) }, t('New playlist…')),
      h('button', { type: 'button', class: 'btn', onclick: () => connectDialog(a) }, a.connected ? t('Connect again…') : t('Connect…')),
      a.connected && h('button', { type: 'button', class: 'btn danger-quiet', onclick: () => act(async () => {
        if (!await confirmBox(t('Disconnect {service}? The playlists fetched so far stay in Stash.', { service: serviceName(a.service) }))) return;
        await api('DELETE', `/api/playlists/accounts/${a.service}`);
      }) }, t('Disconnect'))));

  const listRow = (p) => h('button', {
    type: 'button', class: view.playlist === p.id ? 'pl__list active' : 'pl__list',
    onclick: () => { view.playlist = p.id; draw(); },
  }, h('span', { class: `pl__badge pl__badge--${p.service}` }, serviceName(p.service)), ' ',
  h('b', null, p.name), h('span', { class: 'muted small' }, ` · ${t('{n} items', { n: p.count })}`),
  p.pending ? h('span', { class: 'pl__pending' }, ` ${t('{n} pending', { n: p.pending })}`) : null,
  p.editable ? null : h('span', { class: 'muted small' }, ` · ${t('someone else’s')}`),
  p.gone ? h('span', { class: 'muted small' }, ` · ${t('gone from the service')}`) : null);

  fill(root, h('section', { class: 'card pl__card' },
    h('div', { class: 'row' },
      h('h2', { class: 'grow' }, t('Playlists')),
      h('a', { class: 'btn', href: '/api/playlists/export', download: '' }, t('Export all (JSON)'))),
    h('p', { class: 'muted' }, t('Your Spotify and YouTube playlists, copied into Stash. Edit them here or ask your assistant (“move every live track to a new playlist”); the changes wait as pending until you push them to the service. The copy is also your backup: it stays even when a playlist disappears from the service.')),
    h('div', { class: 'pl__accounts' }, data.accounts.map(accountCard)),
    view.progress ? progressBox() : null,
    view.problem ? problemBox() : null,
    view.result && !view.progress ? resultBox() : null,
    h('div', { class: 'pl__columns' },
      h('div', { class: 'pl__lists' }, data.playlists.length ? data.playlists.map(listRow) : h('p', { class: 'empty' }, t('No playlists fetched yet. Connect a service and press “Fetch playlists”.'))),
      detail && playlistDetail(detail))));
}

function playlistDetail(d) {
  const p = d.playlist;
  const edit = (ops) => act(async () => {
    const r = await api('POST', `/api/playlists/lists/${p.id}/edit`, { ops });
    if (r.unknown.length) toast(t('Not recognised as {service} items: {items}', { service: serviceName(p.service), items: r.unknown.join(', ') }), 'error');
  });
  const itemRow = (x) => h('li', { class: `pl__item pl__item--${x.state}${x.moved ? ' pl__item--moved' : ''}`, dataset: { id: x.id } },
    h('span', { class: 'pl__handle', title: t('Drag to reorder') }, '⋮⋮'),
    h('span', { class: 'muted small pl__pos' }, x.position + 1),
    h('div', { class: 'pl__text' },
      h('a', { href: safeHref(x.url), target: '_blank', rel: 'noopener noreferrer', class: 'pl__title' }, x.title || x.item_id),
      h('div', { class: 'muted small' }, [x.artist, x.duration_ms && duration(x.duration_ms),
        x.state === 'added' && t('added'), x.state === 'removed' && t('removed'), x.moved && t('moved')].filter(Boolean).join(' · '))),
    x.state === 'removed'
      ? h('button', { type: 'button', class: 'btn', onclick: () => edit([{ op: 'order', ids: [] }]).then(() => restore(x)) }, t('Keep'))
      : h('button', { type: 'button', class: 'btn danger-quiet', onclick: () => edit([{ op: 'remove', ids: [x.id] }]) }, t('Remove')));
  const restore = async () => { await act(async () => { await api('POST', `/api/playlists/lists/${p.id}/discard`); }); };

  const list = h('ul', { class: 'plain pl__items' }, d.items.map(itemRow));
  if (window.Sortable && p.editable) {
    window.Sortable.create(list, {
      handle: '.pl__handle', animation: 120,
      onEnd: () => edit([{ op: 'order', ids: [...list.children].map((li) => Number(li.dataset.id)) }]),
    });
  }
  const pend = d.pending;
  const pendingText = [pend.added && `+${pend.added}`, pend.removed && `−${pend.removed}`, pend.moved && t('{n} moved', { n: pend.moved })].filter(Boolean).join(' ');

  return h('div', { class: 'pl__detail' },
    h('div', { class: 'row' },
      h('h3', { class: 'grow' }, p.name, ' ', h('a', { href: safeHref(p.url), target: '_blank', rel: 'noopener noreferrer', class: 'small' }, t('open on {service}', { service: serviceName(p.service) }))),
      p.editable && h('button', { type: 'button', class: 'btn', onclick: () => renameDialog(p, edit) }, t('Rename…')),
      p.editable && h('button', { type: 'button', class: 'btn', onclick: () => addDialog(p, edit) }, t('Add by link…'))),
    p.description && h('p', { class: 'muted small' }, p.description),
    h('div', { class: 'row pl__actions' },
      h('span', { class: 'muted small grow' }, p.dirty ? t('Pending changes: {text}', { text: pendingText || t('name or description') }) : t('No pending changes')),
      p.dirty && h('button', { type: 'button', class: 'btn primary', onclick: () => act(async () => {
        if (!await confirmBox(t('Push these changes to {service}?', { service: serviceName(p.service) }), { okLabel: t('Push') })) return;
        const r = await api('POST', `/api/playlists/lists/${p.id}/push`);
        toast(t('Pushed: {added} added, {removed} removed', r.pushed));
      }) }, t('Push to {service}', { service: serviceName(p.service) })),
      p.dirty && h('button', { type: 'button', class: 'btn', onclick: () => act(async () => {
        if (!await confirmBox(t('Drop the pending changes?'), { danger: true, okLabel: t('Drop') })) return;
        await api('POST', `/api/playlists/lists/${p.id}/discard`);
      }) }, t('Discard changes')),
      !p.dirty && !p.gone && h('button', { type: 'button', class: 'btn', onclick: () => act(() => api('POST', `/api/playlists/lists/${p.id}/sync`)) }, t('Fetch again')),
      h('button', { type: 'button', class: 'btn danger-quiet', onclick: () => act(async () => {
        if (!await confirmBox(t('Forget the copy in Stash? The playlist on the service is not touched.'), { danger: true, okLabel: t('Forget') })) return;
        await api('DELETE', `/api/playlists/lists/${p.id}`);
        view.playlist = null;
      }) }, t('Forget copy'))),
    d.items.length ? list : h('p', { class: 'empty' }, t('This playlist is empty.')));
}

async function addDialog(p, edit) {
  const text = h('textarea', { rows: 6, placeholder: p.service === 'youtube' ? 'https://www.youtube.com/watch?v=…' : 'https://open.spotify.com/track/…' });
  const top = h('input', { type: 'checkbox' });
  const ok = await openModal(t('Add by link'), h('div', { class: 'form' },
    h('p', { class: 'muted' }, t('One address or id per line. The titles are fetched from the service.')),
    h('label', { class: 'field' }, h('span', null, t('Addresses')), text),
    h('label', { class: 'check' }, top, h('span', null, t('Add at the beginning')))), [
    { label: t('Cancel') },
    { label: t('Add'), kind: 'primary', action: () => text.value.trim() !== '' },
  ]);
  if (!ok) return;
  const op = { op: 'add', items: text.value.split(/\s+/).filter(Boolean) };
  if (top.checked) op.position = 0;
  await edit([op]);
}

async function renameDialog(p, edit) {
  const name = h('input', { type: 'text', maxlength: 150, value: p.name, required: true });
  const description = h('textarea', { rows: 3, maxlength: 1000 }, p.description);
  const pub = h('input', { type: 'checkbox', checked: p.public });
  const ok = await openModal(t('Rename playlist'), h('div', { class: 'form' },
    h('label', { class: 'field' }, h('span', null, t('Name')), name),
    h('label', { class: 'field' }, h('span', null, t('Description')), description),
    h('label', { class: 'check' }, pub, h('span', null, t('Public')))), [
    { label: t('Cancel') },
    { label: t('Save'), kind: 'primary', action: () => name.value.trim() !== '' },
  ]);
  if (!ok) return;
  await edit([{ op: 'rename', name: name.value.trim(), description: description.value, public: pub.checked }]);
}

async function createDialog(service) {
  const name = h('input', { type: 'text', maxlength: 150, required: true });
  const description = h('textarea', { rows: 2, maxlength: 1000 });
  const pub = h('input', { type: 'checkbox' });
  const ok = await openModal(t('New playlist on {service}', { service: serviceName(service) }), h('div', { class: 'form' },
    h('label', { class: 'field' }, h('span', null, t('Name')), name),
    h('label', { class: 'field' }, h('span', null, t('Description')), description),
    h('label', { class: 'check' }, pub, h('span', null, t('Public')))), [
    { label: t('Cancel') },
    { label: t('Create'), kind: 'primary', action: () => name.value.trim() !== '' },
  ]);
  if (!ok) return;
  await act(async () => {
    const r = await api('POST', `/api/playlists/accounts/${service}/create`, { name: name.value.trim(), description: description.value, public: pub.checked });
    view.playlist = r.id;
  });
}

function guide(a) {
  const uri = h('code', { class: 'pl__uri' }, a.redirect_uri);
  const copy = h('button', { type: 'button', class: 'btn small', onclick: () => copyText(a.redirect_uri) }, t('Copy'));
  const steps = a.service === 'spotify'
    ? [
      t('Open developer.spotify.com/dashboard and sign in with your Spotify account. Press “Create app”.'),
      t('Give the app any name and description. In “Redirect URIs” paste the address below. Under “Which API/SDKs are you planning to use?” tick “Web API”. Save.'),
      t('Open the app’s Settings: copy the Client ID, then press “View client secret” and copy it. Paste both here.'),
      t('Press Connect: Spotify asks you to allow this app to see and change your playlists. In “Development mode” the app works for your own account (and up to 25 accounts you add in User Management).'),
    ]
    : [
      t('Open console.cloud.google.com and create a project (any name). Do every step below in this same project.'),
      t('“APIs & Services → Library”: find “YouTube Data API v3” and press Enable. Without it Google lets you connect but fetching fails with “YouTube Data API v3 has not been used in project … or it is disabled”; then enable it, wait a minute and press “Fetch playlists”.'),
      t('“Google Auth Platform” (or “OAuth consent screen”) → Get started: app name “Stash”, your e-mail, audience External. Google lets you create the client only after this.'),
      t('Audience → Test users → Add users: add the Google account you sign in to YouTube with, and leave the app in “Testing”. Without this Google says the app “has not completed the Google verification process” and only approved testers have access.'),
      t('Clients → Create client: type Web application (not Desktop app: a file with “installed” and “http://localhost” in it is the wrong kind); in “Authorized redirect URIs” paste the address below. Copy the Client ID and Client secret here.'),
      t('Press Connect, sign in with the test-user account and continue past “Google hasn’t verified this app”. In “Testing” Google ends the connection after 7 days: then press “Connect again…”.'),
      t('YouTube gives 10 000 quota units a day: fetching is cheap, but every added, removed or moved item costs about 50, so large reorders are better done in parts or left to Spotify.'),
    ];
  return h('div', { class: 'pl__guide' },
    h('ol', null, steps.map((s) => h('li', null, s))),
    h('div', { class: 'row' }, h('span', { class: 'small muted' }, t('Redirect URI')), uri, copy));
}

async function connectDialog(a) {
  const clientId = h('input', { type: 'text', maxlength: 200, value: a.client_id || '', autocomplete: 'off', required: true });
  const secret = h('input', { type: 'password', maxlength: 200, autocomplete: 'off', required: true });
  const ok = await openModal(t('Connect {service}', { service: serviceName(a.service) }), h('div', { class: 'form' },
    guide(a),
    h('label', { class: 'field' }, h('span', null, t('Client ID')), clientId),
    h('label', { class: 'field' }, h('span', null, t('Client secret')), secret)), [
    { label: t('Cancel') },
    { label: t('Connect'), kind: 'primary', action: () => clientId.value.trim() !== '' && secret.value.trim() !== '' },
  ], { wide: true });
  if (!ok) return;
  await attempt(async () => {
    const r = await api('POST', `/api/playlists/accounts/${a.service}/connect`, { client_id: clientId.value.trim(), client_secret: secret.value.trim() });
    location.href = r.url;
  });
}
