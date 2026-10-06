// Reports raw and gzipped size of the JS and CSS each built page loads (including shared chunks
// reached through static imports).

import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, relative, posix } from 'node:path';
import { gzipSync } from 'node:zlib';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../dist/', import.meta.url));
if (!existsSync(root)) {
  console.error('size: dist/ not found; run `npm run build` first.');
  process.exit(2);
}

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out); else out.push(p);
  }
  return out;
}

const sizes = new Map();
function size(urlPath) {
  if (!sizes.has(urlPath)) {
    const buf = readFileSync(join(root, urlPath));
    sizes.set(urlPath, { raw: buf.length, gz: gzipSync(buf, { level: 9 }).length, text: buf.toString('utf8') });
  }
  return sizes.get(urlPath);
}

function jsClosure(entry, seen = new Set()) {
  if (seen.has(entry)) return seen;
  seen.add(entry);
  const { text } = size(entry);
  for (const m of text.matchAll(/(?:import|from)\s*["'](\.{1,2}\/[^"']+\.js)["']/g)) {
    jsClosure(posix.join(posix.dirname(entry), m[1]), seen);
  }
  return seen;
}

const kb = (n) => `${(n / 1024).toFixed(1)} KiB`;
const rows = [];
for (const file of walk(root).filter((f) => f.endsWith('.html')).sort()) {
  const html = readFileSync(file, 'utf8');
  const page = '/' + relative(root, file).replace(/\\/g, '/').replace(/index\.html$/, '');
  const js = new Set();
  for (const m of html.matchAll(/<script\b[^>]*\bsrc="\/([^"]+)"/g)) jsClosure(m[1], js);
  const css = [...html.matchAll(/<link\b[^>]*rel="stylesheet"[^>]*href="\/([^"]+)"/g)].map((m) => m[1]);
  const sum = (list) => list.reduce((a, p) => ({ raw: a.raw + size(p).raw, gz: a.gz + size(p).gz }), { raw: 0, gz: 0 });
  const j = sum([...js]), c = sum(css);
  rows.push([page, js.size, kb(j.raw), kb(j.gz), kb(c.gz)]);
}

const all = walk(join(root, '_astro')).filter((f) => f.endsWith('.js'));
const total = all.reduce((a, f) => {
  const b = readFileSync(f);
  return { raw: a.raw + b.length, gz: a.gz + gzipSync(b, { level: 9 }).length };
}, { raw: 0, gz: 0 });

const head = ['page', 'js files', 'js raw', 'js gzip', 'css gzip'];
const widths = head.map((h, i) => Math.max(h.length, ...rows.map((r) => String(r[i]).length)));
const line = (r) => r.map((v, i) => String(v).padEnd(widths[i])).join('  ');
console.log(line(head));
for (const r of rows) console.log(line(r));
console.log(`\nall dist/_astro/*.js: ${all.length} files, ${kb(total.raw)} raw, ${kb(total.gz)} gzip (sum of individually gzipped files)`);
