// Lists UI strings (t('…') calls and API error messages) and reports the ones missing a Finnish translation.
// Usage: node tools/check-i18n.mjs [--list]
import { readFileSync, readdirSync } from 'node:fs';

const dir = new URL('../static/js/', import.meta.url);
const found = new Set();
for (const f of readdirSync(dir)) {
  if (!f.endsWith('.js') || f === 'i18n.js') continue;
  const src = readFileSync(new URL(f, dir), 'utf8');
  for (const m of src.matchAll(/\bt\(\s*'((?:[^'\\]|\\.)*)'/g)) found.add(m[1].replace(/\\'/g, "'"));
  if (f === 'core.js') {
    const start = src.indexOf('const ERRORS');
    for (const m of src.slice(start, src.indexOf('};', start)).matchAll(/: '((?:[^'\\]|\\.)*)'/g)) found.add(m[1]);
    for (const m of src.match(/const COLORS = \[(.*?)\]/)[1].matchAll(/'(\w+)'/g)) found.add(m[1]);
  }
}

if (process.argv.includes('--list')) {
  for (const s of found) console.log(s);
} else {
  const { FI } = await import(new URL('i18n.js', dir));
  const missing = [...found].filter((s) => !(s in FI));
  const unused = Object.keys(FI).filter((s) => !found.has(s));
  for (const s of missing) console.log(`MISSING: ${s}`);
  for (const s of unused) console.log(`unused:  ${s}`);
  console.log(`${found.size} strings, ${missing.length} missing, ${unused.length} unused`);
  process.exitCode = missing.length ? 1 : 0;
}
