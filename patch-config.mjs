// Idempotent patch applied to the OpenClaw gateway's config (openclaw.json),
// executed by the openclaw-config-init service before every `docker compose up`.
//
// Why a patcher and not just onboarding?
// ---------------------------------------
// The interactive onboarding wizard covers the basics (picking a provider, setting
// the gateway token, creating the default agent) but does NOT populate several
// production-critical fields correctly for a self-hosted vLLM backend:
//
//   - It writes a 12-char placeholder `apiKey` into the vllm provider config
//     (leading to a "Profile vllm:default timed out" error against a real backend).
//   - It doesn't register the NVFP4 model id in the provider's models[] catalog
//     (so tool-calling can't be routed to it).
//   - It leaves memorySearch disabled (no embedding provider bundled out of the box).
//   - It ships an empty `gateway.trustedProxies`, triggering a security warning
//     behind any reverse proxy (Nginx Proxy Manager, Caddy, Traefik, Cloudflared).
//   - Its LLM idle watchdog is 120s, too tight for a 31B model + reasoning +
//     vision prefill + multi-step tool calling.
//
// This patcher makes the desired state deterministic: every `docker compose up`
// re-applies the 8 steps below in a deep-merge style. Safe to re-run — it exits
// early when the file is already in the desired state, and if openclaw.json
// doesn't exist yet (pre-onboarding), it exits 0 so the gateway can still boot.
//
// Steps:
//   1. Cleanup — strip the legacy `models.providers.vllm.capabilities` key
//      (an older OpenClaw version put this in the wrong place; the current
//      schema validator rejects it).
//   2. Ensure vllm provider core (baseUrl / api / apiKey) using VLLM_API_KEY.
//   3. Ensure the NVFP4 model entry in the provider catalog.
//   4. Ensure memorySearch: provider=openai, model=BAAI/bge-m3, remote baseUrl
//      and apiKey pointing at the sibling vllm-embedding container.
//   5. Ensure heartbeat: 30m periodic, reasoning enabled, isolated session,
//      activeHours configurable via .env (default 09:00 → 02:00).
//   6. Ensure/cleanup dreaming (memory-core plugin), gated by OPENCLAW_ENABLE_DREAMING.
//      Requires OpenClaw image >= 2026.4.15 — older gateways reject the schema.
//   7. Ensure gateway.trustedProxies: loopback + docker bridge CIDR + the
//      optional LAN CIDR from OPENCLAW_LAN_CIDR (users who access the gateway
//      directly on the LAN, bypassing the CDN/reverse proxy, must include their
//      LAN range here so X-Forwarded-For is trusted).
//   8. Ensure agents.defaults.llm.idleTimeoutSeconds = 300 (LLM idle watchdog).

import fs from 'node:fs';

const CONFIG_PATH = '/home/node/.openclaw/openclaw.json';

// LLM provider — the Gemma 4 31B IT NVFP4 vLLM service, reachable via compose DNS.
const LLM_MODEL_ID = 'nvidia/Gemma-4-31B-IT-NVFP4';
const LLM_BASE_URL = 'http://vllm-llm:8004/v1/';
const LLM_API = 'openai-completions';
const VLLM_API_KEY = process.env.VLLM_API_KEY ?? '';

// Embedding provider — the bge-m3 vLLM service, also reachable via compose DNS.
// Same VLLM_API_KEY as the LLM (both vLLM stacks share one key for simplicity).
const EMBED_MODEL = 'BAAI/bge-m3';
const EMBED_BASE_URL = 'http://vllm-embedding:8005/v1/';

// input: ['text','image'] — Gemma 4 NVFP4 natively supports vision input (NVIDIA's
// release ships the vision tower, not just the LM). The vllm-llm service also
// passes `--limit-mm-per-prompt '{"image":4,"audio":0}'`. OpenClaw uses this
// catalog entry to decide whether to forward image parts in multimodal messages.
const LLM_MODEL_ENTRY = {
  id: LLM_MODEL_ID,
  name: LLM_MODEL_ID,
  reasoning: false,
  input: ['text', 'image'],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 262144,
  maxTokens: 8192,
};

if (!fs.existsSync(CONFIG_PATH)) {
  console.log(`[patch-config] ${CONFIG_PATH} does not exist yet — onboarding has not run. Skipping patch.`);
  process.exit(0);
}

const original = fs.readFileSync(CONFIG_PATH, 'utf-8');
let config;
try {
  config = JSON.parse(original);
} catch (err) {
  console.error(`[patch-config] ${CONFIG_PATH} is not valid JSON:`, err.message);
  process.exit(1);
}

let changed = false;

// (1) Remove legacy capabilities key (old schema).
if (config?.models?.providers?.vllm?.capabilities !== undefined) {
  delete config.models.providers.vllm.capabilities;
  changed = true;
  console.log('[patch-config] removed legacy key models.providers.vllm.capabilities.');
}

// (2) Ensure vllm provider core fields.
const vllm = config?.models?.providers?.vllm;
if (vllm) {
  const desiredCore = {
    baseUrl: LLM_BASE_URL,
    api: LLM_API,
  };
  if (VLLM_API_KEY) desiredCore.apiKey = VLLM_API_KEY;
  for (const [k, v] of Object.entries(desiredCore)) {
    if (vllm[k] !== v) {
      const prev = vllm[k];
      vllm[k] = v;
      changed = true;
      const shown = k === 'apiKey' ? `${String(v).slice(0, 4)}...(len=${String(v).length})` : v;
      const prevShown = k === 'apiKey' && typeof prev === 'string' ? `(len=${prev.length})` : prev;
      console.log(`[patch-config] models.providers.vllm.${k}: ${prevShown} -> ${shown}`);
    }
  }
  if (!VLLM_API_KEY) {
    console.warn('[patch-config] VLLM_API_KEY is not set — skipped provider apiKey patch.');
  }
}

// (3) Ensure the NVFP4 model entry is in the provider's models[] catalog.
if (vllm) {
  vllm.models ??= [];
  const existing = vllm.models.find((m) => m?.id === LLM_MODEL_ID);
  if (!existing) {
    vllm.models.push(LLM_MODEL_ENTRY);
    changed = true;
    console.log(`[patch-config] added provider model entry: ${LLM_MODEL_ID} (contextWindow=262144).`);
  } else {
    const before = JSON.stringify(existing);
    for (const [k, v] of Object.entries(LLM_MODEL_ENTRY)) {
      if (JSON.stringify(existing[k]) !== JSON.stringify(v)) {
        existing[k] = v;
      }
    }
    if (JSON.stringify(existing) !== before) {
      changed = true;
      console.log(`[patch-config] updated provider model entry: ${LLM_MODEL_ID}.`);
    }
  }
}

// (4) Ensure memorySearch points at the local bge-m3 embedding service.
if (!VLLM_API_KEY) {
  console.warn('[patch-config] VLLM_API_KEY is not set — skipped memorySearch patch.');
} else {
  config.agents ??= {};
  config.agents.defaults ??= {};
  config.agents.defaults.memorySearch ??= {};
  const ms = config.agents.defaults.memorySearch;

  const desired = {
    enabled: true,
    provider: 'openai',
    model: EMBED_MODEL,
  };
  for (const [k, v] of Object.entries(desired)) {
    if (ms[k] !== v) {
      ms[k] = v;
      changed = true;
    }
  }

  ms.remote ??= {};
  const desiredRemote = {
    baseUrl: EMBED_BASE_URL,
    apiKey: VLLM_API_KEY,
  };
  for (const [k, v] of Object.entries(desiredRemote)) {
    if (ms.remote[k] !== v) {
      ms.remote[k] = v;
      changed = true;
    }
  }

  if (changed) {
    console.log(`[patch-config] memorySearch: provider=openai, model=${EMBED_MODEL}, baseUrl=${EMBED_BASE_URL}`);
  }
}

// (5) Ensure heartbeat — 30m periodic agent check-in with reasoning, isolated session.
//     Active hours come from .env (OPENCLAW_HEARTBEAT_ACTIVE_START/END/TZ).
//     Default: 09:00 → 02:00 UTC (you almost certainly want to set a local timezone).
config.agents ??= {};
config.agents.defaults ??= {};
config.agents.defaults.heartbeat ??= {};
const hb = config.agents.defaults.heartbeat;

const desiredHb = {
  every: '30m',
  includeReasoning: true,
  isolatedSession: true,
};
for (const [k, v] of Object.entries(desiredHb)) {
  if (hb[k] !== v) {
    hb[k] = v;
    changed = true;
    console.log(`[patch-config] agents.defaults.heartbeat.${k} = ${v}`);
  }
}

hb.activeHours ??= {};
const desiredHours = {
  start: process.env.OPENCLAW_HEARTBEAT_ACTIVE_START || '09:00',
  end: process.env.OPENCLAW_HEARTBEAT_ACTIVE_END || '02:00',
  timezone: process.env.OPENCLAW_HEARTBEAT_TZ || 'UTC',
};
for (const [k, v] of Object.entries(desiredHours)) {
  if (hb.activeHours[k] !== v) {
    hb.activeHours[k] = v;
    changed = true;
    console.log(`[patch-config] agents.defaults.heartbeat.activeHours.${k} = ${v}`);
  }
}

// (6) Dreaming (REM-style memory consolidation). Env-gated: OPENCLAW_ENABLE_DREAMING=1
//     turns it on; anything else cleans up a previously-set entry (older gateway
//     images reject the memory-core plugin schema with "additional properties").
const dreamingEnabled = process.env.OPENCLAW_ENABLE_DREAMING === '1';

if (dreamingEnabled) {
  config.plugins ??= {};
  config.plugins.entries ??= {};
  config.plugins.entries['memory-core'] ??= {};
  const mc = config.plugins.entries['memory-core'];

  if (mc.enabled !== true) {
    mc.enabled = true;
    changed = true;
    console.log('[patch-config] plugins.entries.memory-core.enabled = true');
  }

  mc.config ??= {};
  mc.config.dreaming ??= {};
  const dreaming = mc.config.dreaming;

  const desiredDreaming = {
    enabled: true,
    // CronPattern: nightly at 03:00 in the configured timezone. Falls after the
    // default heartbeat activeHours window (09:00 → 02:00) so nothing overlaps.
    frequency: '0 3 * * *',
    timezone: process.env.OPENCLAW_HEARTBEAT_TZ || 'UTC',
    verboseLogging: false,
  };
  for (const [k, v] of Object.entries(desiredDreaming)) {
    if (dreaming[k] !== v) {
      dreaming[k] = v;
      changed = true;
      console.log(`[patch-config] plugins.entries.memory-core.config.dreaming.${k} = ${v}`);
    }
  }

  dreaming.storage ??= {};
  if (dreaming.storage.mode !== 'both') {
    dreaming.storage.mode = 'both';
    changed = true;
    console.log('[patch-config] dreaming.storage.mode = both');
  }

  dreaming.phases ??= {};
  const desiredPhases = {
    light: { enabled: true, lookbackDays: 3, limit: 20, dedupeSimilarity: 0.92 },
    deep: { enabled: true, limit: 10, minScore: 0.75, minRecallCount: 2, minUniqueQueries: 2, recencyHalfLifeDays: 14, maxAgeDays: 90 },
    rem: { enabled: true, lookbackDays: 14, limit: 5, minPatternStrength: 0.6 },
  };
  for (const [phaseName, phaseCfg] of Object.entries(desiredPhases)) {
    dreaming.phases[phaseName] ??= {};
    for (const [k, v] of Object.entries(phaseCfg)) {
      if (dreaming.phases[phaseName][k] !== v) {
        dreaming.phases[phaseName][k] = v;
        changed = true;
        console.log(`[patch-config] dreaming.phases.${phaseName}.${k} = ${v}`);
      }
    }
  }
} else {
  if (config.plugins?.entries?.['memory-core'] !== undefined) {
    delete config.plugins.entries['memory-core'];
    if (Object.keys(config.plugins.entries).length === 0) delete config.plugins.entries;
    if (config.plugins && Object.keys(config.plugins).length === 0) delete config.plugins;
    changed = true;
    console.log('[patch-config] OPENCLAW_ENABLE_DREAMING != 1 — removed plugins.entries.memory-core.');
  }
}

// (7) Ensure gateway.trustedProxies — silences the "trustedProxies is empty" security
//     warning and lets the gateway correctly parse X-Forwarded-For headers.
//     Baseline: loopback + the entire RFC1918 docker bridge range (172.16.0.0/12).
//     Optional LAN CIDR from OPENCLAW_LAN_CIDR lets LAN clients that bypass the
//     reverse proxy (and hit the gateway directly) be trusted as well.
config.gateway ??= {};
const baselineTrusted = ['127.0.0.1', '::1', '172.16.0.0/12'];
const lanCidr = process.env.OPENCLAW_LAN_CIDR?.trim();
const desiredTrustedProxies = lanCidr ? [...baselineTrusted, lanCidr] : baselineTrusted;
const currentTrustedProxies = Array.isArray(config.gateway.trustedProxies)
  ? config.gateway.trustedProxies
  : [];
const needsProxyUpdate =
  currentTrustedProxies.length !== desiredTrustedProxies.length ||
  desiredTrustedProxies.some((ip, i) => currentTrustedProxies[i] !== ip);
if (needsProxyUpdate) {
  config.gateway.trustedProxies = desiredTrustedProxies;
  changed = true;
  console.log(`[patch-config] gateway.trustedProxies = ${JSON.stringify(desiredTrustedProxies)}`);
}

// (8) Ensure agents.defaults.llm.idleTimeoutSeconds = 300.
//     The schema default is 120s — too tight for 31B + reasoning + vision prefill
//     + multi-step tool calling. 300s is a comfortable margin that still catches
//     real hangs (OOM, CUDA stuck) in reasonable time.
const desiredIdleTimeoutSeconds = 300;
config.agents ??= {};
config.agents.defaults ??= {};
config.agents.defaults.llm ??= {};
if (config.agents.defaults.llm.idleTimeoutSeconds !== desiredIdleTimeoutSeconds) {
  const prev = config.agents.defaults.llm.idleTimeoutSeconds;
  config.agents.defaults.llm.idleTimeoutSeconds = desiredIdleTimeoutSeconds;
  changed = true;
  console.log(`[patch-config] agents.defaults.llm.idleTimeoutSeconds: ${prev ?? '(unset)'} -> ${desiredIdleTimeoutSeconds}`);
}

if (!changed) {
  console.log('[patch-config] no-op (openclaw.json already in the desired state).');
  process.exit(0);
}

fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + '\n');
console.log('[patch-config] openclaw.json updated.');
