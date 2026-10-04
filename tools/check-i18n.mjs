// Lists UI strings (t('…') calls and API error messages) and reports the ones missing a Finnish or Swedish translation.
// Usage: node tools/check-i18n.mjs [--list]
import { readFileSync, readdirSync } from 'node:fs';

const dir = new URL('../static/js/', import.meta.url);
const found = new Set();
for (const f of readdirSync(dir)) {
  if (!f.endsWith('.js') || f.startsWith('i18n')) continue;
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
  const { SV } = await import(new URL('i18n-sv.js', dir));
  let failed = false;
  for (const [name, dict] of [['fi', FI], ['sv', SV]]) {
    const missing = [...found].filter((s) => !(s in dict));
    const unused = Object.keys(dict).filter((s) => !found.has(s));
    // a translation must keep every {placeholder} of the English text
    const broken = [...found].filter((s) => s in dict
      && (s.match(/\{\w+\}/g) || []).sort().join() !== (dict[s].match(/\{\w+\}/g) || []).sort().join());
    for (const s of missing) console.log(`${name} MISSING: ${s}`);
    for (const s of broken) console.log(`${name} PLACEHOLDERS: ${s}`);
    for (const s of unused) console.log(`${name} unused:  ${s}`);
    console.log(`${name}: ${found.size} strings, ${missing.length} missing, ${broken.length} with wrong placeholders, ${unused.length} unused`);
    failed ||= missing.length > 0 || broken.length > 0;
  }
  process.exitCode = failed ? 1 : 0;
}
