// Project handovers: the records AI assistants save through MCP, so a project can be picked up later.
// A card on the AI page; read-only here, saving happens through the MCP tools with an API key.
import { api, attempt, fill, fmtDate, h } from './core.js';
import { t } from './i18n.js';

export function handoverCard(params = {}) {
  const card = h('section', { class: 'card handover__card' });
  const draw = () => attempt(() => (params.project
    ? drawProject(card, params.project, params.version ? Number(params.version) : null)
    : drawList(card)));
  draw();
  return card;
}

function who(r) {
  return [r.ai, r.machine, r.session].filter(Boolean).join(' · ');
}

async function drawList(card) {
  const data = await api('GET', '/api/handovers');
  const row = (p) => h('li', { class: 'handover__item' },
    h('div', { class: 'handover__text' },
      h('a', { href: `#/ai?project=${encodeURIComponent(p.name)}`, class: 'handover__title' }, p.name),
      h('div', { class: 'muted small' },
        [t('Version {n}', { n: p.latest_version }), fmtDate(p.updated_at), who(p)].filter(Boolean).join(' · ')),
      p.title && h('div', { class: 'small' }, p.title)));

  fill(card,
    h('h2', null, t('Handovers')),
    h('p', { class: 'muted' }, t('Written handovers of your projects, saved by an AI assistant through the MCP tools. Every save is a new version; earlier ones are kept.')),
    data.projects.length
      ? h('ul', { class: 'plain handover__list' }, data.projects.map(row))
      : h('p', { class: 'empty' }, t('No handovers yet. Ask your assistant to “save the handover for this project to Stash”.')));
}

async function drawProject(card, project, version) {
  const data = await api('GET', `/api/handovers/${encodeURIComponent(project)}`);
  const latest = data.versions.length ? data.versions[0].version : 0;
  const current = version || latest;
  const item = current ? await api('GET', `/api/handovers/${encodeURIComponent(project)}/${current}`) : null;
  const link = (v) => h('a', {
    href: `#/ai?project=${encodeURIComponent(project)}&version=${v.version}`,
    class: v.version === current ? 'handover__version active' : 'handover__version',
  }, t('Version {n}', { n: v.version }), ' ', h('span', { class: 'muted small' }, fmtDate(v.created_at)));

  fill(card,
    h('p', null, h('a', { href: '#/ai' }, `← ${t('Handovers')}`)),
    h('h2', null, data.project),
    h('div', { class: 'handover__versions' }, data.versions.map(link)),
    item && [
      h('div', { class: 'muted small' },
        [who(item), item.git_commit && t('commit {hash}', { hash: item.git_commit })].filter(Boolean).join(' · ')),
      item.title && h('h3', null, item.title),
      h('pre', { class: 'handover__body' }, item.body),
      h('button', {
        type: 'button', class: 'btn',
        onclick: () => attempt(async () => { await navigator.clipboard.writeText(item.body); }),
      }, t('Copy text')),
    ]);
}
