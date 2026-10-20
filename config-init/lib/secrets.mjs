// Credentials go into OpenClaw's secret store and openclaw.json only holds a
// reference ({ source: "store", provider: "default", id }). The gateway
// resolves references into memory at start, the value stays out of the
// config file, logs and the agent's view of the config, and `openclaw
// secrets audit` stops reporting it as plaintext.
//
// The store is write-only (no command reads a secret back), so a value is
// written only when its hash differs from the one recorded on the last
// write. OPENCLAW_SECRET_STORE=off writes the plain values instead.

import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import * as env from './env.mjs';

const STATE = process.env.OPENCLAW_STATE_DIR || '/home/node/.openclaw';
const HASHES = path.join(STATE, 'config-init', 'secret-hashes.json');

const useStore = () => env.bool('OPENCLAW_SECRET_STORE', true);
const sha = (v) => crypto.createHash('sha256').update(v).digest('hex');

function readHashes() {
  try { return JSON.parse(fs.readFileSync(HASHES, 'utf8')); } catch { return {}; }
}

// The value to put into a config field that accepts a SecretRef: a store
// reference for `name` (writing `value` into the store when it changed), or
// the plain value when the store is off.
export function secret(name, value) {
  if (!value) return undefined;
  if (!useStore()) return value;
  const hashes = readHashes();
  if (hashes[name] !== sha(value)) {
    const r = spawnSync('openclaw', ['secrets', 'store', 'set', name, '--kind', 'secret', '--value-file', '-'], {
      input: value, encoding: 'utf8', env: { ...process.env, OPENCLAW_STATE_DIR: STATE },
    });
    if (r.status !== 0) throw new Error(`openclaw secrets store set ${name} failed: ${(r.stderr || r.stdout || '').trim().slice(-300)}`);
    hashes[name] = sha(value);
    fs.mkdirSync(path.dirname(HASHES), { recursive: true });
    fs.writeFileSync(HASHES, JSON.stringify(hashes, null, 2) + '\n', { mode: 0o600 });
    console.log(`[patch-config] secret store: ${name} written`);
  }
  return { source: 'store', provider: 'default', id: name };
}
