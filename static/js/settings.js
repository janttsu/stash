// Settings: appearance, add-to-Stash buttons, import/export, account security and (for admins) users.
import { api, applyTheme, attempt, fill, confirmBox, copyText, fmtDate, h, openModal, state, toast } from './core.js';
import { exportBookmarks, importDialog } from './bookmarks.js';
import { setLang, t } from './i18n.js';

let root = null;

export async function renderSettings(container) {
  root = container;
  root.className = 'view settings';
  const cards = [appearance(), behaviour(), addButtons(), importExport(), apiKeys(), changeHistory(), browsingHistory(), account()];
  fill(root, h('h1', null, t('Settings')), ...cards);
  if (state.user.is_admin) {
    const adminCard = h('section', { class: 'card' }, h('h2', null, t('Users and registration')));
    root.append(adminCard);
    await drawAdmin(adminCard);
  }
}

async function save(patch) {
  await attempt(async () => {
    state.user.settings = await api('PATCH', '/api/settings', patch);
    toast(t('Saved'));
  });
}

const selectField = (label, options, value, onchange, { disabled = false } = {}) => h('label', { class: 'field field--inline' },
  h('span', null, label),
  h('select', { class: 'input', disabled, onchange: (e) => onchange(e.target.value) },
    options.map(([v, text]) => h('option', { value: v, selected: v === value }, text))));

const checkField = (label, key, hint) => h('label', { class: 'check' },
  h('input', { type: 'checkbox', checked: state.user.settings[key], onchange: (e) => save({ [key]: e.target.checked }) }),
  h('span', null, label, hint && h('small', { class: 'muted block' }, hint)));

function appearance() {
  const s = state.user.settings;
  return h('section', { class: 'card' },
    h('h2', null, t('Appearance')),
    selectField(t('Theme'), [['auto', t('Follow the device')], ['light', t('Light')], ['dark', t('Dark')]], s.theme,
      async (theme) => { await save({ theme }); applyTheme(theme); }),
    selectField(t('Language'), [['', t('Follow the browser')], ['en', 'English'], ['fi', 'Suomi'], ['sv', 'Svenska']], s.lang,
      async (lang) => { await save({ lang }); setLang(lang); location.reload(); }),
    selectField(t('Size of tab titles'), [['s', t('Small')], ['m', t('Medium')], ['l', t('Large')]], s.tab_size,
      (tab_size) => save({ tab_size })),
    checkField(t('Show site icons'), 'favicons', t('Icons are fetched and cached by this server; your browser never contacts third parties.')),
    checkField(t('Show tags and notes in tooltips on the Dashboard'), 'tooltips'));
}

function behaviour() {
  return h('section', { class: 'card' },
    h('h2', null, t('Bookmarks')),
    checkField(t('Open links in a new browser tab'), 'new_tab'),
    checkField(t('Auto-tag when moving to the Catalog'), 'auto_tag_catalog',
      t('The tab and category names are added as tags, so the bookmark stays easy to find.')),
    checkField(t('Keep bookmarks tidy automatically'), 'auto_maintain',
      t('Stash checks now and then by itself: missing titles get the real page title, site icons are fetched again, pages that are gone get the dead-link tag, and extra copies of the same address get the duplicate tag. The tags clear on their own when no longer needed.')));
}

function addButtons() {
  const origin = location.origin;
  const desktop = `javascript:(function(){var w=window,d=document,s=(w.getSelection?String(w.getSelection()):'').slice(0,1000);w.open('${origin}/add#url='+encodeURIComponent(location.href)+'&title='+encodeURIComponent(d.title)+'&notes='+encodeURIComponent(s),'stash','width=560,height=740,resizable=yes,scrollbars=yes')})();`;
  const mobile = `javascript:location.href='${origin}/add#back=1&url='+encodeURIComponent(location.href)+'&title='+encodeURIComponent(document.title)`;
  const button = h('a', { class: 'bookmarklet', title: t('Drag me to the bookmarks bar') }, `+ ${t('Add to Stash')}`);
  button.setAttribute('href', desktop);
  button.addEventListener('click', (e) => {
    e.preventDefault();
    toast(t('Drag this button to your bookmarks bar; it does nothing here.'));
  });
  return h('section', { class: 'card' },
    h('h2', null, t('Add to Stash button')),
    h('h3', null, t('Desktop: bookmarklet')),
    h('p', null, t('Show the bookmarks bar in your browser (Ctrl+Shift+B) and drag this button onto it. Then click it on any page to bookmark that page. Text you have selected on the page becomes the note.')),
    h('p', null, button),
    h('p', null, t('Drag this second button to the bookmarks bar as well: it opens your Stash Dashboard.')),
    h('p', null, h('a', { class: 'bookmarklet bookmarklet--plain', href: `${origin}/`, title: t('Drag me to the bookmarks bar') }, 'Stash')),
    h('h3', null, t('Phone or tablet')),
    h('p', null, t('Bookmark any page in your mobile browser, then edit that bookmark: name it “Add to Stash” and replace its address with the code below. To save a page, type “Add to Stash” in the address bar and tap the bookmark.')),
    h('div', { class: 'row' },
      h('input', { type: 'text', class: 'input mono', readOnly: true, value: mobile, onfocus: (e) => e.target.select() }),
      h('button', { type: 'button', class: 'btn', onclick: () => copyText(mobile) }, t('Copy'))),
    h('h3', null, t('Browser extension')),
    h('p', null, t('The extension adds a toolbar button, a right-click “Add link to Stash” item and “Bookmark all open tabs”. Download and unzip it, then in Chrome, Edge or Brave open the Extensions page, turn on Developer mode and choose “Load unpacked”. In Firefox use about:debugging → “Load Temporary Add-on”.')),
    h('p', null, h('a', { class: 'btn', href: '/extension.zip', download: '' }, t('Download extension'))));
}

function importExport() {
  return h('section', { class: 'card' },
    h('h2', null, t('Import and export')),
    h('p', null, t('Import the bookmarks HTML file of any browser or bookmark manager. Export gives you the same standard format, with tabs and categories as folders and your tags and notes included – a complete backup.')),
    h('div', { class: 'row' },
      h('button', { type: 'button', class: 'btn', onclick: () => importDialog() }, t('Import bookmarks…')),
      h('button', { type: 'button', class: 'btn', onclick: exportBookmarks }, t('Export bookmarks'))));
}

// --- API keys and the terminal client ------------------------------------------

function apiKeys() {
  const card = h('section', { class: 'card' });
  const draw = async () => {
    const [data, client] = await Promise.all([attempt(() => api('GET', '/api/keys')), attempt(() => api('GET', '/api/client'))]);
    if (!data) return;
    const install = client?.wheel ? `pipx install --force ${client.wheel}` : '';
    fill(card,
      h('h2', null, t('API keys')),
      h('p', null, t('With an API key, programs on your other computers can read and change your bookmarks through the address {url}. Each key is shown only once; store it like a password.', { url: `${location.origin}/api/v1` })),
      data.keys.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'table' },
        h('thead', null, h('tr', null, [t('Name'), t('Key'), t('Access'), t('Created'), t('Last used'), ''].map((x) => h('th', null, x)))),
        h('tbody', null, data.keys.map((k) => h('tr', null,
          h('td', null, k.name),
          h('td', null, h('code', null, `${k.prefix}…`)),
          h('td', null, k.can_write ? t('Read and change') : t('Read only')),
          h('td', null, fmtDate(k.created_at)),
          h('td', null, k.last_used ? `${fmtDate(k.last_used)} (${k.last_ip})` : '–'),
          h('td', null, h('button', {
            type: 'button', class: 'btn small danger',
            onclick: async () => {
              if (!await confirmBox(t('Revoke the key {name}? Programs using it stop working.', { name: k.name }), { okLabel: t('Revoke'), danger: true })) return;
              await attempt(async () => { await api('DELETE', `/api/keys/${k.id}`); await draw(); });
            },
          }, t('Revoke'))))))))
        : h('p', { class: 'muted' }, t('No API keys yet.')),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn', onclick: () => createKey(draw) }, t('Create API key'))),
      h('h3', null, t('stashai: your bookmarks in AI assistants (MCP)')),
      h('p', null, t('stashai connects Stash to Qwen Code, Claude Code, Gemini CLI and other MCP clients. You write what you want (“move everything about company X to tag Y”, “list everything about Z”), the assistant finds the bookmarks, shows exactly what would change and changes them only when you agree. Every change can be undone below.')),
      install ? h('div', { class: 'row' },
        h('input', { type: 'text', class: 'input mono', readOnly: true, value: install, onfocus: (e) => e.target.select() }),
        h('button', { type: 'button', class: 'btn', onclick: () => copyText(install) }, t('Copy'))) : null,
      h('p', { class: 'muted' }, t('Then run “stashai login {url}”, paste a key that can read and change, and add “stashai mcp” to your MCP client: “stashai doctor” prints the settings.', { url: location.origin })),
      h('p', { class: 'muted' }, t('The same command also replaces an older version. Versions 0.1.1 and newer update themselves with “stashai update”.')));
  };
  draw();
  return card;
}

async function createKey(redraw) {
  const name = h('input', { type: 'text', class: 'input', required: true, maxLength: 100, placeholder: t('e.g. desktop') });
  const write = h('input', { type: 'checkbox', checked: true });
  const created = await openModal(t('Create API key'), h('div', { class: 'form' },
    h('label', { class: 'field' }, h('span', null, t('Name')), name),
    h('label', { class: 'check' }, write, h('span', null, t('Allow changes'),
      h('small', { class: 'muted block' }, t('Without this the key can only read. Previews of changes work with both.'))))), [
    { label: t('Cancel') },
    { label: t('Create'), kind: 'primary', action: () => api('POST', '/api/keys', { name: name.value, can_write: write.checked }) },
  ]);
  if (!created) return;
  await redraw();
  await openModal(t('New API key'), h('div', { class: 'form' },
    h('p', null, t('Copy the key now. It is shown only once.')),
    h('input', { type: 'text', class: 'input mono', readOnly: true, value: created.key, onfocus: (e) => e.target.select() })), [
    { label: t('Copy'), action: async () => { await copyText(created.key); return false; } },
    { label: t('Close'), kind: 'primary', action: () => true },
  ]);
}

const fmtWhen = (seconds) => new Date(seconds * 1000).toLocaleString(document.documentElement.lang || undefined,
  { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

function changeHistory() {
  const card = h('section', { class: 'card' });
  const counts = (c) => [
    c.created && t('{n} added', { n: c.created }),
    c.updated && t('{n} changed', { n: c.updated }),
    c.deleted && t('{n} deleted', { n: c.deleted }),
  ].filter(Boolean).join(', ') || '–';
  const undo = async (change, draw) => {
    if (!await confirmBox(t('Undo “{summary}”? The bookmarks it touched get back their earlier tags, place and details, deleted ones come back and added ones are removed.', { summary: change.summary || `#${change.id}` }), { okLabel: t('Undo') })) return;
    try {
      await api('POST', `/api/changes/${change.id}/undo`);
    } catch (e) {
      if (e.code !== 'conflict') { toast(e.message, 'error'); return; }
      if (!await confirmBox(t('Later changes touched the same bookmarks, and undoing this one overwrites them too. Undo anyway?'), { okLabel: t('Undo anyway'), danger: true })) return;
      if (!await attempt(() => api('POST', `/api/changes/${change.id}/undo?force=true`))) return;
    }
    toast(t('Undone'));
    await draw();
  };
  const draw = async () => {
    const data = await attempt(() => api('GET', '/api/changes'));
    if (!data) return;
    fill(card,
      h('h2', null, t('Changes made with API keys')),
      data.changes.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'table' },
        h('thead', null, h('tr', null, [t('When'), t('Change'), t('Bookmarks'), t('Key'), ''].map((x) => h('th', null, x)))),
        h('tbody', null, data.changes.map((c) => h('tr', null,
          h('td', null, fmtWhen(c.created_at)),
          h('td', { class: 'wrapcell' }, c.summary || '–'),
          h('td', null, counts(c.counts)),
          h('td', null, c.key),
          h('td', null, c.undone_at ? h('span', { class: 'muted' }, t('Undone'))
            : h('button', { type: 'button', class: 'btn small', onclick: () => undo(c, draw) }, t('Undo'))))))))
        : h('p', { class: 'muted' }, t('Nothing yet.')));
  };
  draw();
  return card;
}

function browsingHistory() {
  const card = h('section', { class: 'card' });
  const script = `${location.origin}/dl/stash-history-sync.py`;
  const commands = [
    `curl -o ~/stash-history-sync.py ${script}`,
    'python3 ~/stash-history-sync.py --setup',
    'python3 ~/stash-history-sync.py --schedule',
  ].join('\n');
  const draw = async () => {
    const data = await attempt(() => api('GET', '/api/history/sources'));
    if (!data) return;
    fill(card,
      h('h2', null, t('Browsing history')),
      h('p', null, t('Your computers can send their Firefox history here. stashai then sees which bookmarks you really use: it can bring the most used ones to the Dashboard, put them first, and find often visited pages that are not bookmarked yet. The history stays on this server, is never part of the export and can be deleted here at any time.')),
      data.sources.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'table' },
        h('thead', null, h('tr', null, [t('Computer'), t('Browser'), t('Addresses'), t('Last sync'), ''].map((x) => h('th', null, x)))),
        h('tbody', null, data.sources.map((s) => h('tr', null,
          h('td', null, s.source),
          h('td', null, s.browser || '–'),
          h('td', null, String(s.items)),
          h('td', null, s.synced_at ? fmtWhen(s.synced_at) : '–'),
          h('td', null, h('button', {
            type: 'button', class: 'btn small danger',
            onclick: async () => {
              if (!await confirmBox(t('Delete the history sent by {name}? A scheduled sync sends it again unless you stop it on that computer.', { name: s.source }), { okLabel: t('Delete'), danger: true })) return;
              await attempt(async () => { await api('DELETE', `/api/history/${encodeURIComponent(s.source)}`); await draw(); });
            },
          }, t('Delete'))))))))
        : h('p', { class: 'muted' }, t('No history yet.')),
      h('h3', null, t('Sending the history (macOS or Linux)')),
      h('p', null, t('Run these in a terminal on the computer where you use Firefox. The script needs only the Python 3 of the system and an API key that can change. It sends every few hours; --dry-run shows what would be sent, --forget deletes it from here.')),
      h('div', { class: 'row' },
        h('textarea', { class: 'input mono', rows: 3, readOnly: true, value: commands, onfocus: (e) => e.target.select() }),
        h('button', { type: 'button', class: 'btn', onclick: () => copyText(commands) }, t('Copy'))));
  };
  draw();
  return card;
}

// --- account -----------------------------------------------------------------

function account() {
  const card = h('section', { class: 'card' });
  const draw = () => fill(card,
    h('h2', null, t('Account')),
    h('p', null, t('Signed in as'), ' ', h('strong', null, state.user.username)),
    h('div', { class: 'row wrap' },
      h('button', { type: 'button', class: 'btn', onclick: changePassword }, t('Change password')),
      state.user.totp_on
        ? h('button', { type: 'button', class: 'btn', onclick: () => disableTotp(draw) }, t('Turn off two-factor authentication'))
        : h('button', { type: 'button', class: 'btn', onclick: () => enableTotp(draw) }, t('Turn on two-factor authentication')),
      h('button', {
        type: 'button', class: 'btn',
        onclick: () => attempt(async () => { await api('POST', '/api/account/logout-all'); toast(t('Other devices were signed out.')); }),
      }, t('Sign out other devices')),
      h('button', { type: 'button', class: 'btn danger', onclick: deleteAccount }, t('Delete account'))),
    h('p', { class: 'muted' }, state.user.totp_on
      ? t('Two-factor authentication is on: signing in needs a code from your authenticator app.')
      : t('Two-factor authentication adds a one-time code from an authenticator app to your sign-in.')));
  draw();
  return card;
}

const passwordInput = (autocomplete) => h('input', {
  type: 'password', class: 'input', required: true, minLength: autocomplete === 'new-password' ? 8 : 1, maxLength: 200, autocomplete,
});

async function changePassword() {
  const current = passwordInput('current-password');
  const next = passwordInput('new-password');
  const again = passwordInput('new-password');
  const ok = await openModal(t('Change password'), h('div', { class: 'form' },
    h('label', { class: 'field' }, h('span', null, t('Current password')), current),
    h('label', { class: 'field' }, h('span', null, t('New password (at least 8 characters)')), next),
    h('label', { class: 'field' }, h('span', null, t('New password again')), again)), [
    { label: t('Cancel') },
    {
      label: t('Change password'), kind: 'primary',
      action: async () => {
        if (next.value !== again.value) throw new Error(t('The passwords do not match.'));
        await api('POST', '/api/account/password', { current: current.value, new: next.value });
      },
    },
  ]);
  if (ok) toast(t('Password changed. Other devices were signed out.'));
}

async function enableTotp(redraw) {
  const setup = await attempt(() => api('POST', '/api/account/totp/setup'));
  if (!setup) return;
  const code = h('input', {
    type: 'text', class: 'input', required: true, inputMode: 'numeric', autocomplete: 'one-time-code', maxLength: 10, placeholder: '123456',
  });
  const ok = await openModal(t('Two-factor authentication'), h('div', { class: 'form' },
    h('p', null, t('Scan the code with an authenticator app (Aegis, Google Authenticator, 1Password…), then enter the 6-digit code it shows.')),
    h('img', { class: 'qr', src: setup.qr, alt: t('QR code'), width: 200, height: 200 }),
    h('p', { class: 'muted' }, t('Or enter this key by hand:'), ' ', h('code', null, setup.secret)),
    h('label', { class: 'field' }, h('span', null, t('Code')), code)), [
    { label: t('Cancel') },
    { label: t('Turn on'), kind: 'primary', action: () => api('POST', '/api/account/totp/enable', { code: code.value }) },
  ]);
  if (!ok) return;
  state.user.totp_on = true;
  toast(t('Two-factor authentication is on.'));
  redraw();
}

async function disableTotp(redraw) {
  const password = passwordInput('current-password');
  const ok = await openModal(t('Turn off two-factor authentication'),
    h('label', { class: 'field' }, h('span', null, t('Confirm with your password')), password), [
      { label: t('Cancel') },
      { label: t('Turn off'), kind: 'danger', action: () => api('POST', '/api/account/totp/disable', { password: password.value }) },
    ]);
  if (!ok) return;
  state.user.totp_on = false;
  redraw();
}

async function deleteAccount() {
  const password = passwordInput('current-password');
  const ok = await openModal(t('Delete account'), h('div', { class: 'form' },
    h('p', null, t('This permanently deletes your account and all your bookmarks. Export them first if you want a copy.')),
    h('label', { class: 'field' }, h('span', null, t('Confirm with your password')), password)), [
    { label: t('Cancel') },
    { label: t('Delete account'), kind: 'danger', action: () => api('POST', '/api/account/delete', { password: password.value }) },
  ]);
  if (ok) location.reload();
}

// --- admin -------------------------------------------------------------------

async function drawAdmin(card) {
  const data = await attempt(() => api('GET', '/api/admin'));
  if (!data) return;
  const redraw = () => drawAdmin(card);
  const inviteUrl = (code) => `${location.origin}/?invite=${code}`;
  const open = data.invites.filter((i) => !i.used_at);

  fill(card,
    h('h2', null, t('Users and registration')),
    selectField(t('Who can create an account'), [
      ['invite', t('Only people with an invitation link')],
      ['open', t('Anyone')],
      ['closed', t('Nobody')],
    ], data.registration, (registration) => attempt(async () => {
      await api('PUT', '/api/admin/config', { registration });
      toast(t('Saved'));
    }), { disabled: data.registration_fixed }),
    data.registration_fixed && h('p', { class: 'muted small' },
      t('This is set by the server configuration (STASH_REGISTRATION).')),
    h('h3', null, t('Invitations')),
    h('p', { class: 'muted' }, t('Each link lets one person create an account.')),
    h('ul', { class: 'plain' }, open.map((i) => h('li', { class: 'row' },
      h('input', { type: 'text', class: 'input mono', readOnly: true, value: inviteUrl(i.code), onfocus: (e) => e.target.select() }),
      h('button', { type: 'button', class: 'btn', onclick: () => copyText(inviteUrl(i.code)) }, t('Copy')),
      h('button', {
        type: 'button', class: 'btn',
        onclick: () => attempt(async () => { await api('DELETE', `/api/admin/invites/${i.code}`); await redraw(); }),
      }, t('Revoke'))))),
    h('button', {
      type: 'button', class: 'btn',
      onclick: () => attempt(async () => { await api('POST', '/api/admin/invites'); await redraw(); }),
    }, t('Create invitation link')),
    h('h3', null, t('Users')),
    h('div', { class: 'tablewrap' }, h('table', { class: 'table' },
      h('thead', null, h('tr', null, [t('User'), t('Bookmarks'), t('Joined'), t('Last active'), ''].map((x) => h('th', null, x)))),
      h('tbody', null, data.users.map((u) => h('tr', null,
        h('td', null, u.username, u.is_admin ? ` (${t('admin')})` : ''),
        h('td', null, String(u.bookmarks)),
        h('td', null, fmtDate(u.created_at)),
        h('td', null, u.last_seen ? fmtDate(u.last_seen) : '–'),
        h('td', null, u.id !== state.user.id && h('div', { class: 'row' },
          h('button', { type: 'button', class: 'btn small', onclick: () => resetPassword(u) }, t('Reset password')),
          h('button', { type: 'button', class: 'btn small danger', onclick: () => deleteUser(u, redraw) }, t('Delete'))))))))));
}

async function resetPassword(user) {
  if (!await confirmBox(t('Set a new temporary password for {name}? Their two-factor authentication is turned off and they are signed out everywhere.', { name: user.username }),
    { okLabel: t('Reset password') })) return;
  const result = await attempt(() => api('POST', `/api/admin/users/${user.id}/reset-password`));
  if (!result) return;
  await openModal(t('Temporary password'), h('div', { class: 'form' },
    h('p', null, t('Give this password to {name}. It is shown only once.', { name: user.username })),
    h('input', { type: 'text', class: 'input mono', readOnly: true, value: result.password, onfocus: (e) => e.target.select() })), [
    { label: t('Copy'), action: async () => { await copyText(result.password); return false; } },
    { label: t('Close'), kind: 'primary', action: () => true },
  ]);
}

async function deleteUser(user, redraw) {
  if (!await confirmBox(t('Delete the account {name} and all its bookmarks? This cannot be undone.', { name: user.username }),
    { okLabel: t('Delete'), danger: true })) return;
  await attempt(async () => { await api('DELETE', `/api/admin/users/${user.id}`); await redraw(); });
}
