// Fails if the static build contains anything the production CSP would block:
//   default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;
//   connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'
// Checks every dist/**/*.html for: <script> without src, external script/style/img URLs,
// <style> blocks, style="" attributes, inline on*= handlers, javascript: URLs, <base>.
// Also scans dist/_astro/*.js for setAttribute('style' / innerHTML assignments as a guard.

import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../dist/', import.meta.url));
if (!existsSync(root)) {
  console.error('check:csp: dist/ not found; run `npm run build` first.');
  process.exit(2);
}

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}

const files = walk(root);
const problems = [];
const add = (file, msg, snippet) => problems.push(`${relative(root, file)}: ${msg}${snippet ? `\n    ${snippet.slice(0, 160)}` : ''}`);

const isExternal = (url) => /^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(url) && !/^data:/i.test(url);

for (const file of files.filter((f) => f.endsWith('.html'))) {
  const html = readFileSync(file, 'utf8');

  for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
    const attrs = m[1];
    const src = /\bsrc\s*=\s*["']?([^"'\s>]+)/i.exec(attrs)?.[1];
    if (!src) {
      // JSON data blocks are not executed and are allowed by CSP.
      if (/type\s*=\s*["']?application\/(?:ld\+)?json/i.test(attrs)) continue;
      add(file, 'inline <script> without src', m[0]);
    } else if (isExternal(src)) add(file, `external script ${src}`);
    if (m[2].trim() && src) add(file, '<script src> with inline body', m[0]);
  }
  for (const m of html.matchAll(/<style\b[^>]*>/gi)) add(file, 'inline <style> block', m[0]);
  for (const m of html.matchAll(/<[a-z][^>]*\sstyle\s*=\s*["'][^"']*["'][^>]*>/gi)) add(file, 'style="" attribute', m[0]);
  for (const m of html.matchAll(/<[a-z][^>]*\son[a-z]+\s*=\s*[^>]*>/gi)) add(file, 'inline event handler attribute', m[0]);
  for (const m of html.matchAll(/(?:href|src|action)\s*=\s*["']?\s*javascript:/gi)) add(file, 'javascript: URL', m[0]);
  for (const m of html.matchAll(/<base\b[^>]*>/gi)) add(file, '<base> element (base-uri none)', m[0]);
  for (const m of html.matchAll(/<link\b[^>]*>/gi)) {
    const href = /\bhref\s*=\s*["']?([^"'\s>]+)/i.exec(m[0])?.[1];
    if (href && isExternal(href)) add(file, `external <link> ${href}`, m[0]);
  }
  for (const m of html.matchAll(/<(?:img|iframe|object|embed|source)\b[^>]*\bsrc\s*=\s*["']?([^"'\s>]+)/gi)) {
    if (isExternal(m[1])) add(file, `external resource ${m[1]}`, m[0]);
  }
  for (const m of html.matchAll(/<form\b[^>]*\baction\s*=\s*["']?([^"'\s>]+)/gi)) {
    if (isExternal(m[1])) add(file, `form action to another origin ${m[1]}`, m[0]);
  }
  for (const m of html.matchAll(/<(?:iframe|frame)\b/gi)) add(file, 'frame element', m[0]);
}

for (const file of files.filter((f) => f.endsWith('.css'))) {
  const css = readFileSync(file, 'utf8');
  for (const m of css.matchAll(/(?:@import|url\()\s*["']?((?:https?:)?\/\/[^"')\s]+)/gi)) add(file, `external CSS resource ${m[1]}`);
}

for (const file of files.filter((f) => f.endsWith('.js'))) {
  const js = readFileSync(file, 'utf8');
  if (/setAttribute\(\s*["'`]style["'`]/.test(js)) add(file, "setAttribute('style', ...) (blocked by style-src 'self')");
  if (/\.(?:innerHTML|outerHTML)\s*=|insertAdjacentHTML\s*\(/.test(js)) add(file, 'HTML string injection API used (innerHTML/outerHTML/insertAdjacentHTML)');
  if (/\beval\s*\(|new\s+Function\s*\(/.test(js)) add(file, "eval/new Function (blocked: no 'unsafe-eval')");
}

const htmlCount = files.filter((f) => f.endsWith('.html')).length;
if (problems.length) {
  console.error(`check:csp FAILED: ${problems.length} problem(s) in ${htmlCount} HTML file(s):`);
  for (const p of problems) console.error(`  - ${p}`);
  process.exit(1);
}
console.log(`check:csp OK: ${htmlCount} HTML file(s) and ${files.length - htmlCount} asset(s) contain no inline scripts, inline styles, handlers or external origins.`);
