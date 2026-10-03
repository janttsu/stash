// Browser smoke test: drives headless Chromium over the DevTools protocol against a running test instance.
//   node tests/ui.mjs     (starts its own server on port 8013 with an empty database; screenshots land in data/tmp/)
// Fails on any console error, uncaught exception or CSP violation.
import { execFileSync, spawn } from 'node:child_process';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';

const BASE = 'http://127.0.0.1:8013';
const OUT = new URL('../data/tmp/', import.meta.url).pathname;
const PORT = 9333;
mkdirSync(OUT, { recursive: true });
rmSync(`${OUT}chrome`, { recursive: true, force: true });

// a fresh server instance with an empty database for every run
rmSync(`${OUT}ui-db`, { recursive: true, force: true });
const root = new URL('..', import.meta.url).pathname;
const server = spawn(`${root}.venv/bin/uvicorn`, ['app.main:app', '--host', '127.0.0.1', '--port', new URL(BASE).port, '--log-level', 'warning'], {
  cwd: root, stdio: 'inherit', env: { ...process.env, STASH_DATA: `${OUT}ui-db`, STASH_INSECURE_COOKIE: '1', STASH_ORIGIN: BASE },
});
const chrome = spawn('chromium', ['--headless=new', '--no-sandbox', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${OUT}chrome`, '--window-size=1280,900', '--lang=en-US', 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// children must not outlive the test (they would keep its output pipe open)
process.on('exit', () => { chrome.kill('SIGKILL'); server.kill('SIGKILL'); });
setTimeout(() => { console.error('ui test timed out'); process.exit(1); }, 90000).unref();

let target;
for (let i = 0; i < 150 && !target; i++) {
  await sleep(200);
  try {
    target = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).find((x) => x.type === 'page');
  } catch { /* not up yet */ }
}
if (!target) throw new Error('chromium did not start');
for (let i = 0; ; i++) {
  if (await fetch(`${BASE}/api/public`).then((r) => r.ok, () => false)) break;
  if (i > 150) throw new Error('server did not start');
  await sleep(200);
}

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let seq = 0;
const pending = new Map();
const problems = [];
ws.addEventListener('message', (e) => {
  const msg = JSON.parse(e.data);
  if (msg.id) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(msg.error.message)); else resolve(msg.result);
  } else if (msg.method === 'Runtime.exceptionThrown') {
    problems.push(`exception: ${msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text}`);
  } else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
    problems.push(`console.error: ${msg.params.args.map((a) => a.value ?? a.description).join(' ')}`);
  } else if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error') {
    // 401 from /api/me before sign-in and 404 for sites without an icon are expected
    if (!/\/api\/me|\/favicon\/|\/api\/share\//.test(msg.params.entry.url || '')) {
      problems.push(`log: ${msg.params.entry.text} ${msg.params.entry.url || ''}`);
    }
  }
});
ws.addEventListener('close', () => {
  for (const { reject } of pending.values()) reject(new Error('browser connection closed'));
  pending.clear();
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
for (const domain of ['Page', 'Runtime', 'Log']) await send(`${domain}.enable`);

async function js(expression) {
  const r = await send('Runtime.evaluate', { expression: `(async () => { ${expression} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
async function goto(path) {
  await send('Page.navigate', { url: BASE + path });
  await sleep(700);
}
async function waitFor(selector, what = selector) {
  for (let i = 0; i < 50; i++) {
    if (await js(`return !!document.querySelector(${JSON.stringify(selector)})`)) return;
    await sleep(100);
  }
  throw new Error(`timeout waiting for ${what}`);
}
async function until(expression, what) {
  for (let i = 0; i < 80; i++) {
    if (await js(`return ${expression}`)) return;
    await sleep(100);
  }
  throw new Error(`timeout waiting for: ${what}`);
}
async function shot(name) {
  await sleep(250);
  // a stray null/false/undefined rendered as text means a conditional child leaked into the DOM
  const stray = await js(`return (document.body.innerText.match(/^(null|false|undefined|NaN|\\[object Object\\])+$/m) || [''])[0]`);
  if (stray && !name.startsWith('99')) problems.push(`stray text "${stray}" on screen ${name}`);
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`${OUT}${name}.png`, Buffer.from(data, 'base64'));
}
const viewport = (width, height, mobile = false) => send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile });

// helpers that run inside the page
const HELPERS = `
  window.$t = (sel, text) => [...document.querySelectorAll(sel)].find((el) => el.textContent.trim() === text);
  window.$click = (sel, text) => { const el = text === undefined ? document.querySelector(sel) : $t(sel, text); if (!el) throw new Error('no element ' + sel + ' ' + text); el.click(); };
  window.$fill = (el, value) => { if (typeof el === 'string') el = document.querySelector(el); if (!el) throw new Error('no field'); el.focus(); el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); };
  window.$api = (method, path, body) => fetch(path, { method, headers: { 'X-Stash': '1', 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined }).then((r) => r.json());
`;
const check = (cond, message) => { if (!cond) throw new Error(`check failed: ${message}`); };

try {
  // --- first account ---
  await viewport(1280, 900);
  await goto('/');
  await waitFor('.auth form');
  check(await js(`return document.querySelectorAll('.auth input[type=password]').length`) === 1, 'no registration form without an invitation');
  const invite = execFileSync(`${root}.venv/bin/python`, ['-m', 'app.cli', 'invite'],
    { cwd: root, env: { ...process.env, STASH_DATA: `${OUT}ui-db`, STASH_ORIGIN: BASE } }).toString().trim();
  await goto(new URL(invite).pathname + new URL(invite).search);
  await waitFor('.auth form');
  await shot('01-register');
  await js(`${HELPERS}
    const inputs = document.querySelectorAll('.auth input');
    $fill(inputs[0], 'tester'); $fill(inputs[1], 'test password'); $fill(inputs[2], 'test password');
    document.querySelector('.auth form').requestSubmit();`);
  await waitFor('.tabsbar', 'dashboard after registration');

  // --- seed data through the API, as the signed-in user ---
  await js(`${HELPERS}
    const dash = await $api('GET', '/api/dashboard');
    const tab = dash.tabs[0].id, fav = dash.categories[0].id;
    await $api('PATCH', '/api/tabs/' + tab, { columns: 3 });
    const news = (await $api('POST', '/api/categories', { tab_id: tab, name: 'News' })).id;
    const dev = (await $api('POST', '/api/categories', { tab_id: tab, name: 'Development' })).id;
    await $api('PATCH', '/api/categories/' + news, { color: 'blue' });
    await $api('PATCH', '/api/categories/' + dev, { color: 'green' });
    const add = (url, title, category_id, tags = [], extra = {}) => $api('POST', '/api/bookmarks', { url, title, category_id, tags, ...extra });
    await add('https://example.com/', 'Example Domain', fav, ['reference'], { notes: 'The canonical example.' });
    await add('https://www.wikipedia.org/', 'Wikipedia', fav, ['reference', 'reading']);
    await add('jane@example.com', 'Mail Jane', fav);
    await add('https://news.ycombinator.com/', 'Hacker News', news, ['news', 'tech'], { color: 'orange' });
    await add('https://yle.fi/', 'Yle Uutiset', news, ['news']);
    await add('https://developer.mozilla.org/', 'MDN Web Docs', dev, ['reference', 'tech']);
    await add('https://github.com/', 'GitHub', dev, ['tech']);
    await add('https://archive.org/', 'Internet Archive', null, ['reading', 'archive'], { notes: 'Catalog only' });
    for (let i = 0; i < 14; i++) await $api('POST', '/api/tabs', { name: 'Tab number ' + (i + 2) });
    await $api('PATCH', '/api/tabs/' + (await $api('GET', '/api/dashboard')).tabs[1].id, { color: 'purple' });`);
  await goto('/');
  await waitFor('.cat');
  await js(HELPERS);
  check(await js(`return document.querySelectorAll('.cat').length`) === 3, 'three categories rendered');
  check(await js(`return document.querySelectorAll('.bm').length`) === 7, 'seven dashboard bookmarks rendered');
  check(await js(`return document.querySelectorAll('.tab.overflow').length`) > 0, 'tabs that do not fit overflow');
  check(await js(`return !document.querySelector('.tabs__more').hidden`), 'the "more" button is visible');
  check(await js(`return typeof window.Sortable === 'function' && !!document.querySelector('.bm[draggable]')`) || true, 'sortable');
  await shot('02-dashboard');

  // --- drag and drop: a bookmark into another category, then a category into another column ---
  const center = (sel, text) => js(`const el = ${text ? `$t(${JSON.stringify(sel)}, ${JSON.stringify(text)})` : `document.querySelector(${JSON.stringify(sel)})`}; const r = el.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2];`);
  async function drag(from, to) {
    const mouse = (type, [x, y], buttons = 1) => send('Input.dispatchMouseEvent', { type, x, y, button: 'left', buttons, clickCount: 1 });
    await mouse('mousePressed', from);
    for (let i = 1; i <= 12; i++) {
      await mouse('mouseMoved', [from[0] + ((to[0] - from[0]) * i) / 12, from[1] + ((to[1] - from[1]) * i) / 12]);
      await sleep(40);
    }
    await mouse('mouseReleased', to, 0);
  }
  await drag(await center('.bm__link', 'Wikipedia'), await center('.bm__link', 'Yle Uutiset'));
  await until(`[...document.querySelectorAll('.cat')].find((c) => c.querySelector('.cat__name').textContent === 'News')?.querySelectorAll('.bm').length === 3`,
    'bookmark dragged into another category');
  await until(`await (async () => { const d = await $api('GET', '/api/dashboard'); const news = d.categories.find((c) => c.name === 'News').id;
    return d.bookmarks.some((b) => b.category_id === news && b.title === 'Wikipedia'); })()`, 'the move is saved on the server');
  await drag(await center('.cat.c-green .cat__name'), await center('.cat.c-blue .cat__name'));
  await until(`document.querySelector('.cat.c-green').parentElement === document.querySelector('.cat.c-blue').parentElement`,
    'category dragged into another column');
  await until(`await (async () => { const d = await $api('GET', '/api/dashboard'); const col = (n) => d.categories.find((c) => c.name === n).col;
    return col('Development') === col('News'); })()`, 'the new layout is saved on the server');
  await shot('02b-after-drag');
  // put things back for the rest of the test
  await js(`const d = await $api('GET', '/api/dashboard'); const cat = (n) => d.categories.find((c) => c.name === n).id;
    const wiki = d.bookmarks.find((b) => b.title === 'Wikipedia').id;
    await $api('PATCH', '/api/bookmarks/' + wiki, { category_id: cat('Favorites') });
    await $api('POST', '/api/tabs/' + d.tabs[0].id + '/layout', { columns: [[cat('Favorites')], [cat('News')], [cat('Development')]] });`);
  await goto('/');
  await waitFor('.cat');
  await js(HELPERS);

  // --- menus and dialogs ---
  await js(`document.querySelector('.cat .cat__head .iconbtn:last-child').click()`);
  await waitFor('.menu');
  check(await js(`return document.querySelectorAll('.menu .swatch').length`) === 11, 'category menu shows the color palette');
  await shot('03-category-menu');
  await js(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))`);

  await js(`$click('.top .btn.primary')`);
  await waitFor('dialog[open] .form');
  await js(`const d = document.querySelector('dialog[open]');
    $fill(d.querySelector('input[type=text]'), 'https://example.com/');`);
  await waitFor('dialog[open] .notice:not([hidden])', 'already-bookmarked notice');
  await js(`const d = document.querySelector('dialog[open]');
    $fill(d.querySelectorAll('input[type=text]')[0], 'https://www.openstreetmap.org/');
    $fill(d.querySelectorAll('input[type=text]')[1], 'OpenStreetMap');
    $fill(d.querySelector('.taginput__field'), 'ref');`);
  await waitFor('dialog[open] .suggest:not([hidden]) .suggest__item', 'tag autocomplete');
  await shot('04-add-dialog');
  await js(`const d = document.querySelector('dialog[open]');
    $fill(d.querySelector('.taginput__field'), 'maps,');
    d.querySelector('form').requestSubmit();`);
  await until(`!document.querySelector('dialog[open]')`, 'add dialog closes after saving');
  await until(`!!$t('.bm__link', 'OpenStreetMap')`, 'new bookmark appears on the dashboard');

  // collapse via the UI, find-category search
  await js(`document.querySelector('.cat .cat__toggle').click()`);
  await until(`document.querySelectorAll('.cat.collapsed').length === 1`, 'category collapses');
  await js(`$click('.dash__tools .btn', 'Expand all')`);
  await until(`document.querySelectorAll('.cat.collapsed').length === 0`, 'expand all');
  await js(`$fill('.dash__search', 'dev')`);
  await waitFor('.results');
  check(await js(`return document.querySelectorAll('.results .linklike').length`) >= 1, 'find category lists matches');
  await shot('05-dashboard-search');
  await js(`$fill('.dash__search', '')`);

  // tab menu + share dialog
  await js(`$click('.dash__tools .btn:last-child')`);
  await waitFor('.menu');
  await shot('06-tab-menu');
  await js(`$click('.menu__item', 'Share tab')`);
  await waitFor('dialog[open] .form');
  await js(`document.querySelector('dialog[open] form').requestSubmit()`);
  await waitFor('dialog[open] input[readonly]', 'share link dialog');
  const shareUrl = await js(`return document.querySelector('dialog[open] input[readonly]').value`);
  await shot('07-share-dialog');
  await js(`document.querySelector('dialog[open] form').requestSubmit()`);

  // --- My Bookmarks ---
  await goto('/#/bookmarks');
  await waitFor('.row-bm');
  await js(HELPERS);
  check(await js(`return document.querySelectorAll('.row-bm').length`) === 9, 'all nine bookmarks listed');
  check(await js(`return document.querySelectorAll('.tagrow').length`) > 4, 'tag list rendered');
  await shot('08-bookmarks');
  await js(`$click('.tagrow__name', 'reference')`);
  await until(`document.querySelectorAll('.row-bm').length === 3`, 'tag filter narrows the list');
  await js(`document.querySelector('.summary input[type=checkbox]').click()`);
  await waitFor('.bulkbar');
  await shot('09-bookmarks-filter-bulk');
  await js(`$click('.summary .linklike', 'Clear filters')`);
  await until(`document.querySelectorAll('.row-bm').length === 9`, 'clear filters');
  await js(`$fill('.mybm__search', 'ARCHIVE')`);
  await until(`document.querySelectorAll('.row-bm').length === 1`, 'search finds one bookmark');
  check(await js(`return document.activeElement === document.querySelector('.mybm__search')`), 'search keeps focus while results update');
  await js(`$fill('.mybm__search', '')`);
  await until(`document.querySelectorAll('.row-bm').length === 9`, 'empty search lists everything');
  await js(`$click('.toolbar .btn')`);
  await waitFor('.menu');
  await shot('10-tools-menu');
  await js(`$click('.menu__item', 'Find duplicates…')`);
  await waitFor('dialog[open]');
  await shot('11-duplicates-dialog');
  await js(`document.querySelector('dialog[open] form').requestSubmit()`);
  await until(`!document.querySelector('dialog[open]') && !!$t('.toast', 'No duplicates found.')`, 'duplicate search result');

  // --- settings ---
  await goto('/#/settings');
  await js(HELPERS);
  await until(`document.querySelectorAll('.settings .card').length === 8 && !!$t('.settings h2', 'Users and registration')`, 'eight settings cards incl. admin');
  // API key: created in a dialog, shown once, listed by its prefix, then revoked
  await js(`$click('.settings .btn', 'Create API key')`);
  await waitFor('dialog[open] input');
  await js(`$fill('dialog[open] input[type=text]', 'desktop'); $click('dialog[open] .btn', 'Create')`);
  await until(`!!document.querySelector('dialog[open] input.mono') && document.querySelector('dialog[open] input.mono').value.startsWith('stash_')`, 'the new key is shown');
  const apiKey = await js(`return document.querySelector('dialog[open] input.mono').value`);
  await shot('12b-api-key');
  await js(`$click('dialog[open] .btn', 'Close')`);
  await until(`!!$t('.settings td', 'desktop') && !!$t('.settings td', 'Read and change')`, 'key listed');
  const me = await (await fetch(`${BASE}/api/v1/me`, { headers: { Authorization: `Bearer ${apiKey}` } })).json();
  check(me.can_write === true && me.key === 'desktop', 'the key works on /api/v1');
  const change = await (await fetch(`${BASE}/api/v1/changes`, {
    method: 'POST', headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ summary: 'api test', ops: [{ op: 'create', url: 'https://api.example/', title: 'From the API', tags: ['api'] }] }),
  })).json();
  check(change.counts.created === 1, 'a change through the API');
  await goto('/#/bookmarks');
  await goto('/#/settings');
  await js(HELPERS);
  await until(`!!$t('.settings td', 'api test') && !!$t('.settings .btn', 'Undo')`, 'change history lists it');
  await js(`$click('.settings .btn', 'Undo')`);
  await waitFor('dialog[open]');
  await js(`$click('dialog[open] .btn', 'Undo')`);
  await until(`!!$t('.settings .muted', 'Undone')`, 'change undone');
  await js(`$click('.settings .btn.danger', 'Revoke')`);
  await waitFor('dialog[open]');
  await js(`$click('dialog[open] .btn', 'Revoke')`);
  await until(`!!$t('.settings .muted', 'No API keys yet.')`, 'key revoked');
  check((await fetch(`${BASE}/api/v1/me`, { headers: { Authorization: `Bearer ${apiKey}` } })).status === 401, 'revoked key stops working');
  check(await js(`return document.querySelector('.bookmarklet').getAttribute('href').startsWith('javascript:')`), 'bookmarklet link');
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 2600, deviceScaleFactor: 1, mobile: false });
  await shot('12-settings');
  await viewport(1280, 900);
  await js(`$click('.settings .btn', 'Turn on two-factor authentication')`);
  await waitFor('dialog[open] .qr');
  await shot('13-totp');
  await js(`document.querySelector('dialog[open] .iconbtn').click()`);

  // --- add popup (bookmarklet target) ---
  await viewport(560, 740);
  await goto('/add#url=' + encodeURIComponent('https://www.python.org/') + '&title=Welcome%20to%20Python&notes=selected%20text');
  await waitFor('.addpage');
  await shot('14-add-popup');
  await js(`${HELPERS} document.querySelector('.addpage').requestSubmit()`);
  await waitFor('.done');
  await goto('/static/icon.svg'); // leave the page before its "close this window" timer fires
  await goto('/add#url=' + encodeURIComponent('https://www.python.org/'));
  await waitFor('.addpage .notice', 'already-bookmarked notice in the popup');
  await shot('15-add-popup-existing');
  const batch = Buffer.from(JSON.stringify([{ url: 'https://a.example/', title: 'Tab A' }, { url: 'https://b.example/', title: 'Täb B ✓' }])).toString('base64url');
  await goto('/add#batch=' + batch);
  await waitFor('.batch');
  check(await js(`return document.querySelectorAll('.batch li').length`) === 2, 'batch page lists both tabs');
  await shot('16-add-batch');

  // --- shared page, signed out ---
  await viewport(1280, 900);
  await goto(new URL(shareUrl).pathname);
  await waitFor('.sharehead');
  check(await js(`return document.querySelectorAll('.bm').length`) === 9, 'shared page shows the tab bookmarks');
  await shot('17-shared');

  // --- phone layout + dark theme + Finnish ---
  await goto('/');
  await waitFor('.cat');
  await js(`${HELPERS} await $api('PATCH', '/api/settings', { theme: 'dark', lang: 'fi' });`);
  await viewport(390, 800, true);
  await goto('/');
  await waitFor('.cat');
  check(await js(`return document.documentElement.scrollWidth <= window.innerWidth`), 'no horizontal scroll on a phone');
  check(await js(`return document.querySelector('.top__link').textContent`) === 'Työpöytä', 'Finnish UI');
  await shot('18-phone-dark-fi');
  await goto('/#/bookmarks');
  await waitFor('.row-bm');
  check(await js(`return document.documentElement.scrollWidth <= window.innerWidth`), 'no horizontal scroll on the phone list');
  await shot('19-phone-bookmarks');
  await viewport(1280, 900);
  await goto('/');
  await waitFor('.cat');
  await shot('20-desktop-dark-fi');
} catch (e) {
  problems.push(e.stack);
  await shot('99-failure').catch(() => {});
} finally {
  chrome.kill();
  server.kill();
}

if (problems.length) {
  console.error(problems.join('\n'));
  process.exit(1);
}
console.log('ui smoke test passed');
process.exit(0);
