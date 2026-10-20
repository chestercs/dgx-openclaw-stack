// Small desired-state helpers over the parsed openclaw.json. Every write
// compares first and logs one `[patch-config]` line, so a re-run on a
// patched config is silent and the log shows exactly what shifted.
//
// Paths are dotted strings or arrays. Use an array when a key contains a dot
// or a slash, e.g. ['agents', 'defaults', 'models', 'vllm/nvidia/Model-X'].

import { isDeepStrictEqual } from 'node:util';

const SECRET_KEY = /token|apikey|api_key|secret|password|authorization/i;

const split = (path) => (Array.isArray(path) ? path : path.split('.'));
const fmt = (path) => split(path).map((k) => (/^[\w-]+$/.test(k) ? k : JSON.stringify(k))).join('.');

const isSecret = (key, value) => typeof value === 'string' && (SECRET_KEY.test(key) || /token=|Bearer /.test(value));

// Masks secrets at any depth, so logging a whole object (e.g. an MCP
// server with its headers) never prints a token.
function show(path, value) {
  const key = split(path).at(-1);
  if (isSecret(key, value)) return `<secret, ${value.length} chars>`;
  const s = JSON.stringify(value, (k, v) => (isSecret(k, v) ? `<secret, ${v.length} chars>` : v));
  return s === undefined ? '(unset)' : s.length > 120 ? `${s.slice(0, 117)}...` : s;
}

export class Config {
  constructor(data) {
    this.data = data;
    this.changed = false;
  }

  log(msg) {
    console.log(`[patch-config] ${msg}`);
  }

  get(path) {
    let node = this.data;
    for (const k of split(path)) {
      if (node === null || typeof node !== 'object') return undefined;
      node = node[k];
    }
    return node;
  }

  // Creates intermediate objects; returns the parent of the last key.
  parent(path) {
    const keys = split(path);
    let node = this.data;
    for (const k of keys.slice(0, -1)) {
      if (node[k] === null || typeof node[k] !== 'object') node[k] = {};
      node = node[k];
    }
    return [node, keys.at(-1)];
  }

  set(path, value, why = '') {
    if (value === undefined) return this.remove(path, why);
    const prev = this.get(path);
    if (isDeepStrictEqual(prev, value)) return false;
    const [node, key] = this.parent(path);
    node[key] = structuredClone(value);
    this.changed = true;
    this.log(`${fmt(path)} = ${show(path, value)}${why ? ` (${why})` : ''}`);
    return true;
  }

  // Each key of `values` is set under `path`; other keys there stay as they are.
  merge(path, values, why = '') {
    let any = false;
    for (const [k, v] of Object.entries(values)) any = this.set([...split(path), k], v, why) || any;
    return any;
  }

  // Only when nothing is there yet: the operator's own value wins.
  setDefault(path, value, why = '') {
    return this.get(path) === undefined ? this.set(path, value, why) : false;
  }

  remove(path, why = '') {
    const keys = split(path);
    const parentNode = this.get(keys.slice(0, -1));
    if (!parentNode || typeof parentNode !== 'object' || !(keys.at(-1) in parentNode)) return false;
    delete parentNode[keys.at(-1)];
    this.changed = true;
    this.log(`${fmt(path)} removed${why ? ` (${why})` : ''}`);
    return true;
  }

  // Adds `items` to the array at `path` (created if missing), keeping order.
  addTo(path, items, why = '') {
    const cur = Array.isArray(this.get(path)) ? this.get(path) : [];
    const add = items.filter((i) => !cur.some((c) => isDeepStrictEqual(c, i)));
    if (!add.length && Array.isArray(this.get(path))) return false;
    return this.set(path, [...cur, ...add], why);
  }

  removeFrom(path, items, why = '') {
    const cur = this.get(path);
    if (!Array.isArray(cur)) return false;
    const kept = cur.filter((c) => !items.some((i) => isDeepStrictEqual(c, i)));
    return kept.length === cur.length ? false : this.set(path, kept, why);
  }
}
