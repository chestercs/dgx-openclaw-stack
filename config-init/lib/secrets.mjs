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

// The value for a config field that accepts a SecretRef. `name` must be in
// x-openclaw-secrets in docker-compose.yml.
export function secret(name, value) {
  if (!value) return undefined;
  return env.bool('OPENCLAW_SECRET_REFS', true) ? { source: 'env', provider: 'default', id: name } : value;
}

// For a credential onboarding wrote into openclaw.json as a plain value
// (the gateway token, the Discord bot token): swaps it for the reference when
// .env holds the same value. An absent key is not added, and a different
// value stays as it is: swapping it
// would change the credential that paired clients and the bot log in with.
export function adopt(config, path, name) {
  const value = env.str(name);
  const current = config.get(path);
  if (!value || current === undefined) return;
  if (typeof current === 'string' && current !== value) {
    config.log(`${path} differs from ${name} in .env, left as a plain value`);
    return;
  }
  config.set(path, secret(name, value), `${name} from .env`);
}
