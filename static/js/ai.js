// The AI section: everything about assistants and the tools that work with Stash from outside, in one place.
// Connecting an assistant (keys, rules), what it can do, the handovers it saves, the changes made with API
// keys (undoable here), and the Tools: the link check, the browsing history sync, the API, the extension and
// bookmarklet, import and export.
import { copyText, fill, h } from './core.js';
import { handoverCard } from './handover.js';
import { t } from './i18n.js';
import { assistantCard, browsingHistory, changeHistoryCard, linkCheck } from './settings.js';

export async function renderAI(container, params = {}) {
  container.className = 'view settings ai';
  const tools = h('div', { id: 'ai-tools', class: 'settings-group' },
    h('h2', { class: 'settings-group__title' }, t('Tools')),
    toolsCard(), linkCheck(), browsingHistory(), apiCard());
  fill(container,
    h('h1', null, t('AI')),
    h('p', { class: 'muted' }, t('An assistant such as Claude Code works on your bookmarks, open tabs, playlists and subscriptions through Stash’s MCP server, in your own words. Here you connect one, keep the rules it follows, read the handovers it saves, undo its changes, and set up the other tools that work with Stash.')),
    h('nav', { class: 'settings-nav' },
      [['ai-assistant', t('Assistant (MCP)')], ['ai-handovers', t('Handovers')], ['ai-changes', t('Changes made with API keys')], ['ai-tools', t('Tools')]]
        .map(([id, label]) => h('button', { type: 'button', class: 'linklike', onclick: () => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, label))),
    params.project ? handoverCard(params) : null,
    withId('ai-assistant', assistantCard()),
    canDoCard(),
    params.project ? null : withId('ai-handovers', handoverCard(params)),
    withId('ai-changes', changeHistoryCard()),
    tools);
  if (params.section === 'tools') tools.scrollIntoView({ block: 'start' });
}

function withId(id, el) {
  el.id = id;
  return el;
}

/** What a connected assistant can do in Stash: one line per area, with an example to ask. */
function canDoCard() {
  const rows = [
    [t('Bookmarks'), t('search, tag, move, rename, find duplicates and dead links; every change is previewed and can be undone'), t('“Which bookmarks about Python have no tags? Suggest some.”')],
    [t('Open tabs'), t('go through the tabs you sent, save the useful ones as bookmarks and drop the rest'), t('“Sort out the tabs I sent yesterday.”')],
    [t('Playlists'), t('read and reorganise your Spotify and YouTube playlists; changes wait until you agree to push them'), t('“Move every live recording to a new playlist Live.”')],
    [t('Subscriptions'), t('tag channels and artists, find the quiet ones, and prepare unsubscribing'), t('“Which channels have not uploaded in a year?”')],
    [t('Handovers'), t('save a written record of a project so another session can continue it'), t('“Save the handover for this project to Stash.”')],
  ];
  return h('section', { class: 'card' },
    h('h2', null, t('What an assistant can do')),
    h('ul', { class: 'ai__cando' }, rows.map(([area, what, ask]) => h('li', null,
      h('b', null, area), ' — ', what, h('div', { class: 'muted small' }, ask)))),
    h('p', { class: 'muted small' }, t('Nothing changes on a music service, and no bookmark is deleted, before you have seen the change and agreed.')));
}

/** Every tool that works with Stash from outside, with where it is set up. */
function toolsCard() {
  const tool = (name, what, link, label) => h('li', null,
    h('b', null, name), ' — ', what, ' ',
    link ? h('a', { href: link, class: 'small' }, label) : null);
  return h('section', { class: 'card' },
    h('h2', null, t('Tools that work with Stash')),
    h('ul', { class: 'ai__tools' },
      tool(t('AI assistants (MCP)'), t('Claude Code, Crush, Qwen Code, Gemini CLI and other MCP clients connect with a key.'), '#/ai', t('Connect an assistant')),
      tool(t('Link check with a local model'), t('a script on your computer finds bookmarks whose content is gone and tags the rest.'), null, ''),
      tool(t('Browsing history sync'), t('a script sends Firefox’s visit counts, so the assistant knows which bookmarks you use.'), null, ''),
      tool(t('API'), t('your own programs read and change bookmarks at /api/v1 with the same keys.'), null, ''),
      tool(t('Browser extension and bookmarklet'), t('add the page, a link, or send all open tabs to Stash.'), '#/settings?section=add', t('Set up in Settings')),
      tool(t('Import and export'), t('browser bookmark files in and out; playlists and subscriptions as JSON on their pages.'), '#/settings?section=import', t('Open in Settings')),
      tool(t('Music services'), t('Spotify and YouTube connect on the Playlists page; Subscriptions uses the same connection.'), '#/playlists', t('Open Playlists'))),
    h('p', { class: 'muted small' }, t('The link check, the history sync and the API are explained in the cards below.')));
}

/** The /api/v1 interface for the user's own programs. */
function apiCard() {
  const base = `${location.origin}/api/v1`;
  const example = `curl -H "Authorization: Bearer stash_…" ${base}/bookmarks?q=python`;
  const rows = [
    ['GET /me', t('who the key belongs to and what it may do')],
    ['GET /snapshot', t('all bookmarks, tabs, categories and tags at once')],
    ['GET /bookmarks?q=&tags=&host=', t('search, up to 1000 at a time')],
    ['POST /changes', t('changes in one transaction; "dry_run": true previews them')],
    ['GET /changes, POST /changes/{id}/undo', t('the change history and undoing a change')],
    ['POST /history/{device}', t('browsing history sent by a device')],
  ];
  return h('section', { class: 'card' },
    h('h2', null, t('API for your own programs')),
    h('p', null, t('Programs send a key in the Authorization header. Make one under Keys above (“Create a key for a program”); a key can only read unless it is allowed to make changes, and every change can be undone in “Changes made with API keys”.')),
    h('div', { class: 'row' },
      h('input', { type: 'text', class: 'input mono', readOnly: true, value: base, onfocus: (e) => e.target.select() }),
      h('button', { type: 'button', class: 'btn', onclick: () => copyText(base) }, t('Copy'))),
    h('div', { class: 'tablewrap' }, h('table', { class: 'table' },
      h('tbody', null, rows.map(([call, what]) => h('tr', null, h('td', null, h('code', null, call)), h('td', null, what)))))),
    h('p', { class: 'muted small' }, t('For example:')),
    h('pre', { class: 'mono small ai__example' }, example),
    h('p', { class: 'muted small' }, t('The full list of calls and change operations is in the README of the Stash repository.')));
}
