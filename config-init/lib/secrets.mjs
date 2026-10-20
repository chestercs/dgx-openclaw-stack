// Credentials stay in .env and reach openclaw.json only as a reference,
// { source: "env", provider: "default", id: NAME }: the gateway resolves it
// from its own environment (compose passes the variable) into memory at
// start, the value stays out of the config file and its backups, and
// `openclaw secrets audit` stops reporting it as plaintext.
//
// Why env and not the secret store: only a process in the gateway's PID
// namespace may write the store while the gateway runs, and config-init runs
// in its own container while the old gateway is still up during a recreate.
// OPENCLAW_SECRET_REFS=off writes the plain values instead.

import * as env from './env.mjs';

// The value for a config field that accepts a SecretRef. The gateway service
// in docker-compose.yml must pass `name` in its environment.
export function secret(name, value) {
  if (!value) return undefined;
  return env.bool('OPENCLAW_SECRET_REFS', true) ? { source: 'env', provider: 'default', id: name } : value;
}
