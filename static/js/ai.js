// The AI section: everything about assistants in one place. Connecting an assistant (keys, rules), the
// handovers assistants save through MCP, and the changes made with API keys, which can be undone here.
import { fill, h } from './core.js';
import { handoverCard } from './handover.js';
import { t } from './i18n.js';
import { assistantCard, changeHistoryCard } from './settings.js';

export async function renderAI(container, params = {}) {
  container.className = 'view settings ai';
  fill(container,
    h('h1', null, t('AI')),
    h('p', { class: 'muted' }, t('An assistant such as Claude Code works on your bookmarks, open tabs and playlists through Stash’s MCP server, in your own words. Here you connect one, keep the rules it follows, read the handovers it saves, and undo its changes.')),
    params.project ? handoverCard(params) : null,
    assistantCard(),
    params.project ? null : handoverCard(params),
    changeHistoryCard());
}
