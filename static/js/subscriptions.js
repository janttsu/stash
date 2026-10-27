// Subscriptions: YouTube channels and Spotify artists and followed playlists, copied into Stash. Tags and notes
// are Stash's own; subscribing and unsubscribing wait as pending until "Synchronise" sends them. YouTube
// playlists saved from others cannot be read through YouTube's API, so they can be kept here by address only.
import { api, attempt, confirmBox, fill, fmtDate, h, openModal, promptBox, safeHref, toast } from './core.js';
import { t } from './i18n.js';

let root = null;
const view = {
  service: 'youtube', kind: '', q: '', tag: '', state: '', inactive: 0, sort: 'title',
  progress: null, result: null, problem: '',
};
const SERVICES = { youtube: 'YouTube', spotify: 'Spotify' };
const KIND = () => ({ channel: t('Channel'), artist: t('Artist'), playlist: t('Playlist') });

export async function renderSubscriptions(container, params = {}) {
  root = container;
  root.className = 'view subscriptions';
  if (params.service && SERVICES[params.service]) view.service = params.service;
  await draw();
}

const act = (fn) => attempt(async () => { await fn(); await draw(); });

async function edit(ops) {
  const r = await api('POST', `/api/subscriptions/${view.service}/edit`, { ops });
  const notes = [];
  if (r.unknown.length) notes.push(t('Not recognised: {items}', { items: r.unknown.join(', ') }));
  if (r.not_found.length) notes.push(t('Not found on {service}: {items}', { service: SERVICES[view.service], items: r.not_found.join(', ') }));
  if (r.already) notes.push(t('{n} already subscribed', { n: r.already }));
  if (notes.length) toast(notes.join(' · '), 'error');
  return r;
}

/** Fetch, push+fetch or the activity check, with the progress bar and a summary. */
async function run(kind) {
  if (view.progress) return;
  const service = view.service;
  if (kind === 'push') {
    const p = await api('GET', `/api/subscriptions/${service}/pending`);
    const n = p.subscribe.length + p.unsubscribe.length;
    if (n) {
      const lines = [
        p.subscribe.length && t('Subscribe: {names}', { names: p.subscribe.map((x) => x.title).join(', ') }),
        p.unsubscribe.length && t('Unsubscribe: {names}', { names: p.unsubscribe.map((x) => x.title).join(', ') }),
        p.quota_units && t('This uses about {units} of YouTube’s 10 000 daily quota units.', { units: p.quota_units }),
      ].filter(Boolean).join('\n');
      if (!await confirmBox(`${t('Send {n} changes to {service} and then fetch everything back?', { n, service: SERVICES[service] })}\n\n${lines}`,
        { okLabel: t('Synchronise') })) return;
    }
  }
  view.problem = '';
  view.result = null;
  view.progress = { service, phase: kind === 'activity' ? 'activity' : kind, done: 0, total: 0, name: '' };
  await draw();
  let polling = true;
  (async () => {
    while (polling) {
      await new Promise((r) => setTimeout(r, 700));
      if (!polling) break;
      try {
        const p = await api('GET', `/api/subscriptions/${service}/progress`);
        if (p.running && polling) { view.progress = { service, ...p }; redrawProgress(); }
      } catch { /* the call itself reports errors */ }
    }
  })();
  try {
    const r = kind === 'activity'
      ? await api('POST', `/api/subscriptions/${service}/activity`, { ids: [] })
      : await api('POST', `/api/subscriptions/${service}/sync${kind === 'push' ? '?push=true' : ''}`);
    view.result = { kind, service, ...r };
  } catch (e) {
    if (/^(youtube_api_disabled|reconnect_needed)/.test(String(e.code))) view.problem = e.code;
    else view.result = { kind, service, error: e.message };
  } finally {
    polling = false;
    view.progress = null;
  }
  await draw();
}

function progressBox() {
  const p = view.progress;
  const label = { push: t('Sending changes to {service}…'), fetch: t('Fetching subscriptions from {service}…'),
    activity: t('Checking the newest upload of each channel…') }[p.phase] || '';
  return h('div', { class: 'pl__progress', id: 'sub-progress', role: 'status' },
    h('div', { class: 'row' },
      h('b', null, label.replace('{service}', SERVICES[p.service])),
      h('span', { class: 'muted small grow' }, p.total ? t('{done} of {total}', { done: p.done, total: p.total }) : '')),
    h('progress', { max: p.total || 1, value: p.total ? p.done : undefined }),
    p.name ? h('div', { class: 'muted small' }, p.name) : null);
}

function redrawProgress() {
  const el = document.getElementById('sub-progress');
  if (el && view.progress) el.replaceWith(progressBox());
}

function resultBox() {
  const r = view.result;
  const close = h('button', { type: 'button', class: 'btn small', onclick: () => { view.result = null; draw(); } }, t('Close'));
  if (r.error) {
    return h('div', { class: 'pl__result pl__result--bad', role: 'status' },
      h('div', { class: 'row' }, h('b', { class: 'grow' }, t('{service} failed', { service: SERVICES[r.service] })), close),
      h('div', { class: 'small' }, r.error));
  }
  if (r.kind === 'activity') {
    return h('div', { class: `pl__result ${r.failed.length ? 'pl__result--bad' : 'pl__result--ok'}`, role: 'status' },
      h('div', { class: 'row' }, h('b', { class: 'grow' }, t('Newest upload checked for {checked} of {total} channels', r)), close),
      r.failed.length ? h('ul', { class: 'small' }, r.failed.map((f) => h('li', null, `${f.name}: ${f.error}`))) : null);
  }
  const pushFailed = r.push_failed || [];
  const ok = !pushFailed.length;
  const kinds = Object.entries(r.by_kind || {}).map(([k, n]) => `${n} ${(KIND()[k] || k).toLowerCase()}`).join(', ');
  return h('div', { class: `pl__result ${ok ? 'pl__result--ok' : 'pl__result--bad'}`, role: 'status' },
    h('div', { class: 'row' },
      h('b', { class: 'grow' }, t('{total} subscriptions fetched from {service} ({kinds}): {new} new, {gone} gone',
        { total: r.total, service: SERVICES[r.service], kinds: kinds || '0', new: r.new, gone: r.gone })),
      close),
    r.kind === 'push' ? h('div', { class: 'small' }, r.pushed.length
      ? t('Sent {n} of {total} changes: {names}', { n: r.pushed.length, total: r.pushed.length + pushFailed.length,
        names: r.pushed.map((x) => `${x.action === 'subscribe' ? '+' : '−'}${x.name}`).join(', ') })
      : pushFailed.length ? null : t('There were no pending changes to send.')) : null,
    pushFailed.length ? [
      h('div', { class: 'small' }, t('{n} could not be sent (they stay pending):', { n: pushFailed.length })),
      h('ul', { class: 'small' }, pushFailed.map((f) => h('li', null, `${f.action === 'subscribe' ? '+' : '−'}${f.name}: ${f.error}`))),
    ] : null);
}

function problemBox() {
  const [code, arg] = view.problem.split(':');
  if (code === 'reconnect_needed') {
    return h('div', { class: 'pl__result pl__result--bad', role: 'alert' },
      h('b', null, t('Connect {service} again', { service: SERVICES[arg] || arg })),
      h('p', { class: 'small' }, t('The connection was made before Stash asked for permission to read and change what you follow. Open Playlists, press “Connect again…” for this service, and fetch again.')),
      h('div', { class: 'row' }, h('a', { class: 'btn primary', href: '#/playlists' }, t('Open Playlists'))));
  }
  const link = `https://console.cloud.google.com/apis/library/youtube.googleapis.com${arg ? `?project=${arg}` : ''}`;
  return h('div', { class: 'pl__result pl__result--bad', role: 'alert' },
    h('b', null, t('YouTube is connected, but the YouTube Data API is not enabled')),
    h('ol', { class: 'small' },
      h('li', null, t('Open the API page and press “Enable”:'), ' ', h('a', { href: link, target: '_blank', rel: 'noopener noreferrer' }, t('YouTube Data API v3 in your project'))),
      h('li', null, t('Wait a minute: Google takes a moment to pass the change on.')),
      h('li', null, t('Then fetch again.'))));
}

async function draw() {
  const data = await api('GET', '/api/subscriptions');
  const acc = data.accounts.find((a) => a.service === view.service) || {};
  const query = new URLSearchParams({ sort: view.sort });
  for (const k of ['kind', 'q', 'tag', 'state']) if (view[k]) query.set(k, view[k]);
  if (view.inactive) query.set('inactive_days', view.inactive);
  const list = acc.connected || data.stats[view.service] ? await api('GET', `/api/subscriptions/${view.service}?${query}`) : { total: 0, items: [] };
  const stats = data.stats[view.service] || { by_kind: {}, pending: 0, local: 0, gone: 0 };
  const checks = new Map();
  const chosen = () => [...checks].filter(([, b]) => b.checked).map(([id]) => id);
  const busy = Boolean(view.progress);

  const tabs = h('div', { class: 'tabs__states' }, Object.entries(SERVICES).map(([s, name]) => h('button', {
    type: 'button', class: view.service === s ? 'btn active' : 'btn',
    onclick: () => { view.service = s; view.kind = ''; view.tag = ''; view.result = null; view.problem = ''; draw(); },
  }, name)));

  const sel = (key, options, onchange) => h('select', {
    class: 'input', onchange: (e) => { view[key] = onchange ? onchange(e.target.value) : e.target.value; draw(); },
  }, options.map(([v, text]) => h('option', { value: v, selected: String(view[key]) === String(v) }, text)));

  const kinds = view.service === 'youtube' ? ['channel', 'playlist'] : ['artist', 'playlist'];
  const filters = h('div', { class: 'row sub__filters' },
    sel('kind', [['', t('All kinds')], ...kinds.map((k) => [k, KIND()[k]])]),
    sel('state', [['', t('Any state')], ['synced', t('Subscribed')], ['added', t('To subscribe')], ['removed', t('To unsubscribe')],
      ['local', t('Only in Stash')], ['gone', t('Gone from the service')]]),
    Object.keys(data.tags).length ? sel('tag', [['', t('Any tag')], ...Object.entries(data.tags).map(([tag, n]) => [tag, `${tag} (${n})`])]) : null,
    view.service === 'youtube' ? sel('inactive', [[0, t('Any activity')], [180, t('No upload in 6 months')],
      [365, t('No upload in a year')], [730, t('No upload in 2 years')]], Number) : null,
    sel('sort', [['title', t('By name')], ['subscribed', t('Newest subscriptions first')], ...(view.service === 'youtube'
      ? [['activity', t('Quietest first')]] : []), ['size', t('Largest first')]]),
    h('input', {
      type: 'search', class: 'input grow', value: view.q, placeholder: t('Search'),
      oninput: (e) => { view.q = e.target.value; clearTimeout(view.timer); view.timer = setTimeout(draw, 300); },
    }));

  const row = (x) => {
    const box = h('input', { type: 'checkbox' });
    checks.set(x.id, box);
    const last = x.last_activity ? t('last upload {date}', { date: fmtDate(x.last_activity) })
      : x.activity_checked ? t('no uploads') : '';
    const stateLabel = { added: t('to subscribe'), removed: t('to unsubscribe'), local: t('only in Stash') }[x.state];
    return h('li', { class: `sub__item sub__item--${x.state}${x.gone ? ' sub__item--gone' : ''}` },
      box,
      h('div', { class: 'sub__text' },
        h('a', { href: safeHref(x.url), target: '_blank', rel: 'noopener noreferrer', class: 'sub__title' }, x.title || x.external_id),
        h('div', { class: 'muted small' }, [KIND()[x.kind], x.owner && t('by {name}', { name: x.owner }),
          x.item_count != null && (x.kind === 'artist' ? t('{n} followers', { n: x.item_count }) : t('{n} videos or tracks', { n: x.item_count })),
          last, x.subscribed_at && t('since {date}', { date: fmtDate(x.subscribed_at) })].filter(Boolean).join(' · ')),
        (x.tags.length || stateLabel || x.gone || x.note) ? h('div', { class: 'sub__meta' },
          stateLabel ? h('span', { class: `sub__badge sub__badge--${x.state}` }, stateLabel) : null,
          x.gone ? h('span', { class: 'sub__badge' }, t('gone from the service')) : null,
          x.tags.map((tag) => h('button', { type: 'button', class: 'chip', onclick: () => { view.tag = tag; draw(); } }, tag)),
          x.note ? h('span', { class: 'small' }, x.note) : null) : null),
      h('div', { class: 'tabs__buttons' },
        h('button', { type: 'button', class: 'btn small', onclick: () => tagDialog([x.id], x.tags) }, t('Tags…')),
        h('button', { type: 'button', class: 'btn small', onclick: () => noteDialog(x) }, t('Note…')),
        x.state === 'removed'
          ? h('button', { type: 'button', class: 'btn small', onclick: () => act(() => edit([{ op: 'keep', ids: [x.id] }])) }, t('Keep'))
          : x.gone ? h('button', { type: 'button', class: 'btn small', onclick: () => act(() => edit([{ op: 'forget', ids: [x.id] }])) }, t('Forget'))
            : h('button', { type: 'button', class: 'btn small danger-quiet', onclick: () => act(() => edit([{ op: 'unsubscribe', ids: [x.id] }])) },
              x.state === 'local' || x.state === 'added' ? t('Remove') : t('Unsubscribe'))));
  };

  const counts = Object.entries(stats.by_kind).map(([k, n]) => `${n} ${(KIND()[k] || k).toLowerCase()}`).join(' · ');
  fill(root, h('section', { class: 'card pl__card' },
    h('div', { class: 'row' },
      h('h2', { class: 'grow' }, t('Subscriptions')),
      h('a', { class: 'btn', href: '/api/subscriptions/export', download: '' }, t('Export all (JSON)'))),
    h('p', { class: 'muted' }, t('Your YouTube channel subscriptions and the artists and playlists you follow on Spotify, copied into Stash: a backup you can tag, annotate and sort. Unsubscribing and subscribing wait as pending until you press “Synchronise”. Ask your assistant, for example “which channels have not uploaded in a year?”.')),
    tabs,
    h('div', { class: 'pl__account' },
      h('div', { class: 'row' },
        h('span', { class: 'muted small grow' }, acc.connected
          ? [t('Connected as {name}', { name: acc.display_name || '…' }), counts, stats.pending && t('{n} pending', { n: stats.pending }),
            stats.local && t('{n} only in Stash', { n: stats.local })].filter(Boolean).join(' · ')
          : t('Not connected: connect {service} on the Playlists page first.', { service: SERVICES[view.service] })),
        acc.connected ? [
          h('button', { type: 'button', class: 'btn primary', disabled: busy, onclick: () => run('push'),
            title: t('Send the pending subscribes and unsubscribes, then fetch everything back') }, t('Synchronise')),
          h('button', { type: 'button', class: 'btn', disabled: busy, onclick: () => run('fetch') }, t('Fetch')),
          view.service === 'youtube' && h('button', { type: 'button', class: 'btn', disabled: busy, onclick: () => run('activity'),
            title: t('1 quota unit per channel') }, t('Check activity')),
          h('button', { type: 'button', class: 'btn', onclick: () => addDialog() }, t('Add by link…')),
        ] : h('a', { class: 'btn', href: '#/playlists' }, t('Open Playlists')))),
    view.service === 'youtube' ? h('p', { class: 'muted small' }, t('YouTube’s API cannot list or save playlists you have saved from others. Add them by link to keep them here: Stash reads their title and size, but they stay only in Stash.')) : null,
    view.progress ? progressBox() : null,
    view.problem ? problemBox() : null,
    view.result && !view.progress ? resultBox() : null,
    filters,
    list.items.length ? [
      h('div', { class: 'row tabs__bulk' },
        h('label', { class: 'check' }, h('input', { type: 'checkbox', onchange: (e) => { for (const b of checks.values()) b.checked = e.target.checked; } }),
          h('span', null, t('Select all'))),
        h('span', { class: 'muted small grow' }, t('{n} shown', { n: list.total })),
        h('button', { type: 'button', class: 'btn', onclick: () => chosen().length && tagDialog(chosen(), []) }, t('Tag selected…')),
        h('button', { type: 'button', class: 'btn danger-quiet', onclick: () => chosen().length && act(() => edit([{ op: 'unsubscribe', ids: chosen() }])) },
          t('Unsubscribe selected'))),
      h('ul', { class: 'plain tabs__items' }, list.items.map(row)),
    ] : h('p', { class: 'empty' }, acc.connected ? t('Nothing here yet. Press “Fetch” to copy your subscriptions into Stash.') : '')));
}

async function addDialog() {
  const text = h('textarea', { rows: 6, placeholder: view.service === 'youtube'
    ? 'https://www.youtube.com/@name\nhttps://www.youtube.com/playlist?list=…' : 'https://open.spotify.com/artist/…\nhttps://open.spotify.com/playlist/…' });
  const ok = await openModal(t('Add by link'), h('div', { class: 'form' },
    h('p', { class: 'muted' }, view.service === 'youtube'
      ? t('Channel addresses (…/@name, …/channel/UC…) become pending subscriptions. Playlist addresses are kept only in Stash.')
      : t('Artist and playlist addresses become pending follows.')),
    h('label', { class: 'field' }, h('span', null, t('Addresses')), text)), [
    { label: t('Cancel') },
    { label: t('Add'), kind: 'primary', action: () => text.value.trim() !== '' },
  ], { wide: true });
  if (!ok) return;
  await act(() => edit([{ op: 'subscribe', items: text.value.split(/\s+/).filter(Boolean) }]));
}

async function tagDialog(ids, current) {
  const value = await promptBox(t('Tags'), { label: t('Comma-separated; a minus sign removes, e.g. “music, -old”'), value: current.join(', '), okLabel: t('Save') });
  if (value == null) return;
  const parts = value.split(',').map((s) => s.trim()).filter(Boolean);
  const add = parts.filter((p) => !p.startsWith('-'));
  const remove = parts.filter((p) => p.startsWith('-')).map((p) => p.slice(1));
  const op = ids.length === 1 && current.length ? { op: 'set_tags', ids, tags: add } : { op: 'tag', ids, add, remove };
  await act(() => edit([op]));
}

async function noteDialog(x) {
  const text = h('textarea', { class: 'input', rows: 4, maxLength: 2000 }, x.note);
  const ok = await openModal(t('Note for {name}', { name: x.title }), h('label', { class: 'field' }, text), [
    { label: t('Cancel') },
    { label: t('Save'), kind: 'primary', action: () => true },
  ]);
  if (!ok) return;
  await act(() => edit([{ op: 'note', id: x.id, note: text.value }]));
  toast(t('Saved'));
}
