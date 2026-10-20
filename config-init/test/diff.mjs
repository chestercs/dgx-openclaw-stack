// usage: node diff.mjs <dirA> <dirB> — config paths + workspace files that differ; secrets masked
import fs from 'node:fs'; import path from 'node:path'; import crypto from 'node:crypto';
const [A, B] = process.argv.slice(2);
const SECRET = /token|apikey|api_key|secret|password|authorization|bearer/i;
const mask = (k, v) => (typeof v === 'string' && (SECRET.test(k) || /token=|Bearer /.test(v))) ? `<secret len=${v.length}>` : JSON.stringify(v)?.slice(0, 160);
function walk(a, b, p, out) {
  if (JSON.stringify(a) === JSON.stringify(b)) return;
  const obj = (x) => x && typeof x === 'object' && !Array.isArray(x);
  if (obj(a) && obj(b)) { for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) walk(a[k], b[k], p ? `${p}.${k}` : k, out); return; }
  const k = p.split('.').pop();
  out.push(`${p}\n    A: ${a === undefined ? '(absent)' : mask(k, a)}\n    B: ${b === undefined ? '(absent)' : mask(k, b)}`);
}
const ja = JSON.parse(fs.readFileSync(path.join(A, 'state/openclaw.json'), 'utf8'));
const jb = JSON.parse(fs.readFileSync(path.join(B, 'state/openclaw.json'), 'utf8'));
const out = []; walk(ja, jb, '', out);
console.log(`== openclaw.json: ${out.length} differing paths`); for (const l of out) console.log(l);
const files = (root) => { const r = {}; const go = (d) => { for (const e of fs.readdirSync(d, { withFileTypes: true })) { const f = path.join(d, e.name); if (e.isDirectory()) go(f); else if (!/\/memory\/|people\/MEMORY|\.sqlite|openclaw\.json/.test(f)) r[path.relative(root, f)] = crypto.createHash('sha1').update(fs.readFileSync(f)).digest('hex'); } }; go(root); return r; };
const fa = files(path.join(A, 'state')), fb = files(path.join(B, 'state'));
const ch = [...new Set([...Object.keys(fa), ...Object.keys(fb)])].filter((f) => fa[f] !== fb[f]).sort();
console.log(`== files: ${ch.length} differ`); for (const f of ch) console.log(`  ${!fa[f] ? 'B-only' : !fb[f] ? 'A-only' : 'changed'}  ${f}`);
