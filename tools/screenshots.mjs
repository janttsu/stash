// README screenshots from a throwaway instance filled with demo bookmarks (no real data is ever shown).
//   node tools/screenshots.mjs      → docs/screenshots/*.png
// Starts its own server on port 8014 with an empty database and drives headless Chromium.
import { execFileSync, spawn } from 'node:child_process';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';

const BASE = 'http://127.0.0.1:8014';
const PORT = 9334;
const root = new URL('..', import.meta.url).pathname;
const TMP = `${root}data/tmp/shots/`;
const OUT = `${root}docs/screenshots/`;
rmSync(TMP, { recursive: true, force: true });
mkdirSync(TMP, { recursive: true });
mkdirSync(OUT, { recursive: true });

const env = { ...process.env, STASH_DATA: `${TMP}db`, STASH_INSECURE_COOKIE: '1', STASH_ORIGIN: BASE };
const server = spawn(`${root}.venv/bin/uvicorn`, ['app.main:app', '--host', '127.0.0.1', '--port', '8014', '--log-level', 'warning'],
  { cwd: root, stdio: 'inherit', env });
const chrome = spawn('chromium', ['--headless=new', '--no-sandbox', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${TMP}chrome`, '--hide-scrollbars', '--lang=en-US', 'about:blank'], { stdio: 'ignore' });
process.on('exit', () => { chrome.kill('SIGKILL'); server.kill('SIGKILL'); });
setTimeout(() => { console.error('timed out'); process.exit(1); }, 180000).unref();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let target;
for (let i = 0; i < 150 && !target; i++) {
  await sleep(200);
  try { target = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).find((x) => x.type === 'page'); } catch { /* starting */ }
}
for (let i = 0; !(await fetch(`${BASE}/api/public`).then((r) => r.ok, () => false)); i++) {
  if (i > 150) throw new Error('server did not start');
  await sleep(200);
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let seq = 0;
const pending = new Map();
ws.addEventListener('message', (e) => {
  const msg = JSON.parse(e.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(msg.error.message)); else resolve(msg.result);
  }
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
await send('Page.enable');
await send('Runtime.enable');
async function js(expression) {
  const r = await send('Runtime.evaluate', { expression: `(async () => { ${expression} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
async function goto(path) {
  await send('Page.navigate', { url: BASE + path });
  await sleep(800);
}
async function until(expression, what) {
  for (let i = 0; i < 150; i++) {
    if (await js(`return ${expression}`)) return;
    await sleep(100);
  }
  throw new Error(`timeout: ${what}`);
}
const viewport = (width, height) => send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 2, mobile: false });
async function shot(name) {
  // every site icon loaded (or given up), then a moment for the layout to settle
  // icons on screen loaded (lazy ones below the fold never load), at most a few seconds
  for (let i = 0; i < 60; i++) {
    if (await js(`return [...document.images].filter((i) => i.getBoundingClientRect().top < innerHeight).every((i) => i.complete)`)) break;
    await sleep(100);
  }
  await sleep(600);
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`${OUT}${name}.png`, Buffer.from(data, 'base64'));
  console.log(`docs/screenshots/${name}.png`);
}
const API = `
  window.$api = (method, path, body) => fetch(path, { method, headers: { 'X-Stash': '1', 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined }).then((r) => r.json());`;

// --- a demo account ---
await viewport(1280, 800);
const invite = execFileSync(`${root}.venv/bin/python`, ['-m', 'app.cli', 'invite'], { cwd: root, env }).toString().trim();
await goto(new URL(invite).pathname + new URL(invite).search);
await until(`!!document.querySelector('.auth form')`, 'registration form');
await js(`
  const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); };
  const inputs = document.querySelectorAll('.auth input');
  set(inputs[0], 'alex'); set(inputs[1], 'demo password'); set(inputs[2], 'demo password');
  document.querySelector('.auth form').requestSubmit();`);
await until(`!!document.querySelector('.tabsbar')`, 'dashboard');

// --- demo bookmarks: well-known public sites only ---
await js(`${API}
  await $api('PATCH', '/api/settings', { lang: 'en', theme: 'light' });
  const dash = await $api('GET', '/api/dashboard');
  const home = dash.tabs[0].id;
  await $api('PATCH', '/api/tabs/' + home, { name: 'Home', columns: 3 });
  const first = dash.categories[0].id;
  await $api('PATCH', '/api/categories/' + first, { name: 'Daily', color: 'blue' });
  const cat = async (tab, name, color = '') => {
    const id = (await $api('POST', '/api/categories', { tab_id: tab, name })).id;
    if (color) await $api('PATCH', '/api/categories/' + id, { color });
    return id;
  };
  const add = (category_id, url, title, tags = [], extra = {}) =>
    $api('POST', '/api/bookmarks', { url, title, category_id, tags, ...extra });
  const dev = await cat(home, 'Development', 'green');
  const news = await cat(home, 'News', 'orange');
  const reading = await cat(home, 'Reading list');
  const tools = await cat(home, 'Tools', 'purple');
  const media = await cat(home, 'Music & video', 'pink');
  for (const [u, t, g] of [
    ['https://mail.proton.me/', 'Proton Mail', ['email']],
    ['https://calendar.google.com/', 'Calendar', ['planning']],
    ['https://www.openstreetmap.org/', 'OpenStreetMap', ['maps']],
    ['https://www.yr.no/', 'Weather – Yr', ['weather']],
    ['https://www.wikipedia.org/', 'Wikipedia', ['reference']],
  ]) await add(first, u, t, g);
  for (const [u, t, g] of [
    ['https://github.com/', 'GitHub', ['code', 'git']],
    ['https://docs.python.org/3/', 'Python 3 documentation', ['python', 'docs']],
    ['https://developer.mozilla.org/', 'MDN Web Docs', ['web', 'docs']],
    ['https://fastapi.tiangolo.com/', 'FastAPI', ['python', 'web']],
    ['https://sqlite.org/docs.html', 'SQLite documentation', ['database', 'docs']],
    ['https://stackoverflow.com/', 'Stack Overflow', ['code']],
  ]) await add(dev, u, t, g);
  for (const [u, t, g] of [
    ['https://news.ycombinator.com/', 'Hacker News', ['tech', 'news']],
    ['https://www.bbc.com/news', 'BBC News', ['news']],
    ['https://arstechnica.com/', 'Ars Technica', ['tech', 'news']],
    ['https://lwn.net/', 'LWN.net', ['linux', 'news']],
  ]) await add(news, u, t, g);
  for (const [u, t, g, n] of [
    ['https://www.gutenberg.org/', 'Project Gutenberg', ['books', 'reading'], 'Free classics'],
    ['https://longform.org/', 'Longform', ['reading'], ''],
    ['https://archive.org/', 'Internet Archive', ['archive', 'reading'], ''],
  ]) await add(reading, u, t, g, { notes: n });
  for (const [u, t, g] of [
    ['https://excalidraw.com/', 'Excalidraw', ['drawing']],
    ['https://regex101.com/', 'regex101', ['code', 'tools']],
    ['https://www.photopea.com/', 'Photopea', ['images']],
    ['https://squoosh.app/', 'Squoosh', ['images', 'tools']],
  ]) await add(tools, u, t, g);
  for (const [u, t, g] of [
    ['https://www.youtube.com/', 'YouTube', ['video']],
    ['https://bandcamp.com/', 'Bandcamp', ['music']],
    ['https://www.nts.live/', 'NTS Radio', ['music', 'radio']],
  ]) await add(media, u, t, g);
  await $api('PATCH', '/api/bookmarks/' + (await $api('GET', '/api/bookmarks?q=hacker')).items[0].id, { color: 'orange' });
  for (const [name, color] of [['Work', ''], ['Projects', ''], ['Travel', ''], ['Recipes', '']]) {
    const tab = (await $api('POST', '/api/tabs', { name })).id;
    if (color) await $api('PATCH', '/api/tabs/' + tab, { color });
    const c = await cat(tab, 'General');
    await add(c, 'https://example.com/' + name.toLowerCase(), name + ' notes');
  }
  // the Catalog: tagged bookmarks that are not on the Dashboard
  for (const [u, t, g, n] of [
    ['https://www.debian.org/doc/', 'Debian documentation', ['linux', 'docs'], ''],
    ['https://wiki.archlinux.org/', 'ArchWiki', ['linux', 'docs'], 'The best Linux wiki there is'],
    ['https://ollama.com/', 'Ollama', ['ai', 'tools'], 'Local language models'],
    ['https://huggingface.co/', 'Hugging Face', ['ai'], ''],
    ['https://restic.net/', 'restic', ['backup', 'tools'], ''],
    ['https://www.seriouseats.com/', 'Serious Eats', ['cooking', 'recipes'], ''],
    ['https://www.budgetbytes.com/', 'Budget Bytes', ['cooking', 'recipes'], ''],
    ['https://www.rome2rio.com/', 'Rome2Rio', ['travel'], 'Routes between any two places'],
    ['https://www.seat61.com/', 'The Man in Seat 61', ['travel', 'trains'], ''],
    ['https://caddyserver.com/docs/', 'Caddy documentation', ['web', 'docs', 'selfhosted'], ''],
    ['https://www.home-assistant.io/', 'Home Assistant', ['selfhosted', 'home'], ''],
    ['https://jellyfin.org/', 'Jellyfin', ['selfhosted', 'video'], ''],
  ]) await add(null, u, t, g, { notes: n });`);

// 1. the Dashboard
await viewport(1280, 640);
await goto('/');
await until(`document.querySelectorAll('.bm').length > 20`, 'dashboard bookmarks');
await shot('dashboard');

// 2. the Catalog with the tag list
await viewport(1280, 760);
await goto('/#/bookmarks');
await until(`document.querySelectorAll('.row-bm').length > 10`, 'bookmark list');
await shot('bookmarks');

ws.close();
process.exit(0);
