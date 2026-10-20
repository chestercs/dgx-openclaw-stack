// Typed readers for the .env knobs. One vocabulary for every switch:
// on/true/1/yes and off/false/0/no. An empty value counts as unset, so a
// knob written as `FOO=` in .env behaves like a missing one (its default).

const ON = new Set(['on', 'true', '1', 'yes']);
const OFF = new Set(['off', 'false', '0', 'no']);

export const raw = (name) => {
  const v = process.env[name];
  return v === undefined || v.trim() === '' ? undefined : v.trim();
};

export const str = (name, def) => raw(name) ?? def;

export function bool(name, def) {
  const v = raw(name)?.toLowerCase();
  if (v === undefined) return def;
  if (ON.has(v)) return true;
  if (OFF.has(v)) return false;
  throw new Error(`${name}="${v}" is not on/off`);
}

export function int(name, def) {
  const v = raw(name);
  if (v === undefined) return def;
  const n = Number(v);
  if (!Number.isInteger(n)) throw new Error(`${name}="${v}" is not an integer`);
  return n;
}

export function num(name, def) {
  const v = raw(name);
  if (v === undefined) return def;
  const n = Number(v);
  if (!Number.isFinite(n)) throw new Error(`${name}="${v}" is not a number`);
  return n;
}

// Comma-separated list; entries are trimmed, empty ones dropped.
export const list = (name, def = []) => {
  const v = raw(name);
  return v === undefined ? def : v.split(',').map((s) => s.trim()).filter(Boolean);
};

export function oneOf(name, choices, def) {
  const v = raw(name)?.toLowerCase();
  if (v === undefined) return def;
  if (!choices.includes(v)) throw new Error(`${name}="${v}" must be one of ${choices.join(', ')}`);
  return v;
}

// Discord snowflakes as strings (19-digit IDs lose precision as numbers).
export const ids = (name) => list(name).filter((s) => /^\d{15,22}$/.test(s));

// Whether an optional compose service runs: its profile is in
// COMPOSE_PROFILES (config-init gets .env through env_file). Unset counts as
// yes, so a host that starts services with --profile flags keeps its wiring.
export function profile(name) {
  const v = raw('COMPOSE_PROFILES');
  if (v === undefined) return true;
  const set = v.split(',').map((s) => s.trim());
  return set.includes(name) || set.includes('*');
}
