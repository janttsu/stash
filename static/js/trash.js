// The trash: deleted bookmarks wait here for a while and can be put back with one click.
import { api, attempt, confirmBox, favicon, fill, fmtDate, h, safeHref, toast } from './core.js';
import { t } from './i18n.js';

let root = null;

export async function renderTrash(container) {
  root = container;
  root.className = 'view trash';
  await draw();
}

async function draw() {
  const data = await api('GET', '/api/trash');
  const restore = (ids, all = false) => attempt(async () => {
    const res = await api('POST', '/api/trash/restore', all ? { all: true } : { ids });
    toast(res.restored === 1 ? t('Restored') : t('{n} bookmarks restored', { n: res.restored }));
    await draw();
  });
  const forget = async (body, question) => {
    if (!await confirmBox(question, { okLabel: t('Delete for good'), danger: true })) return;
    await attempt(async () => { await api('POST', '/api/trash/delete', body); await draw(); });
  };
  const row = (item) => h('li', { class: 'trash__item' },
    favicon(item.url),
    h('div', { class: 'trash__text' },
      h('a', { href: safeHref(item.url), target: '_blank', rel: 'noopener noreferrer', class: 'trash__title' }, item.title),
      h('div', { class: 'muted small' },
        [t('Deleted {date}', { date: fmtDate(item.deleted_at) }), item.place && t('from {place}', { place: item.place })]
          .filter(Boolean).join(' · '))),
    h('div', { class: 'trash__buttons' },
      h('button', { type: 'button', class: 'btn primary', onclick: () => restore([item.id]) }, t('Restore')),
      h('button', {
        type: 'button', class: 'btn danger-quiet',
        onclick: () => forget({ ids: [item.id] }, t('Delete “{name}” for good?', { name: item.title })),
      }, t('Delete for good'))));

  fill(root, h('section', { class: 'card trash__card' },
    h('h2', null, t('Trash')),
    h('p', { class: 'muted' }, t('Deleted bookmarks stay here for {n} days. Until then you can put them back where they were.', { n: data.days })),
    data.items.length
      ? [
        h('div', { class: 'row' },
          h('button', { type: 'button', class: 'btn', onclick: () => restore([], true) }, t('Restore all')),
          h('button', {
            type: 'button', class: 'btn danger-quiet',
            onclick: () => forget({ all: true }, t('Empty the trash? These bookmarks cannot be brought back.')),
          }, t('Empty the trash'))),
        h('ul', { class: 'plain trash__list' }, data.items.map(row)),
      ]
      : h('p', { class: 'empty' }, t('The trash is empty.'))));
}
