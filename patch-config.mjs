// Idempotent patch applied to the OpenClaw gateway's config (openclaw.json),
// run by the openclaw-config-init service before every `docker compose up`.
//
// Why a patcher, not just onboarding? The interactive wizard picks a provider,
// sets the gateway token, and creates the default agent — but it leaves several
// production-critical fields wrong for a self-hosted vLLM backend: placeholder
// `apiKey`, no NVFP4 model id in the catalog, memorySearch disabled, empty
// `gateway.trustedProxies`, and a 120s LLM idle watchdog that's too tight for
// 31B + reasoning + vision prefill + multi-step tool calling.
//
// This script makes the desired state deterministic — every `docker compose up`
// re-applies the 14 steps below in a deep-merge style. Safe to re-run; exits
// early when nothing changes, and exits 0 when openclaw.json doesn't exist yet
// (pre-onboarding fresh install) so the gateway container can still boot.
//
// Step index:
//   1. Strip the legacy `models.providers.vllm.capabilities` key.
//   2. Ensure vllm provider core (baseUrl / api / apiKey).
//   3. Ensure the NVFP4 model entry in the provider catalog.
//   4. Ensure memorySearch points at the bge-m3 embedding service.
//   5. Ensure heartbeat (30m periodic, reasoning, isolated, activeHours).
//   6. Ensure/cleanup dreaming — env-gated by OPENCLAW_ENABLE_DREAMING.
//   7. Ensure gateway.trustedProxies — loopback + bridge + optional LAN CIDR.
//   8. Ensure agents.defaults.llm.idleTimeoutSeconds = 300.
//   9. Ensure memorySearch hybrid (BM25 + vector) retrieval with MMR re-rank.
//  10. Ensure webSearch provider = searxng + enable the bundled searxng plugin.
//  11. Ensure messages.tts wiring — env-gated by OPENCLAW_TTS_ROUTER_API_KEY.
//  12. Mirror gateway.auth.token into gateway.remote.token so the loopback CLI
//      can WS-connect without hitting "gateway token mismatch" and silently
//      falling back to an embedded runner.
//  13. Sync the per-agent auth-profiles.json `vllm:default.key` with the
//      current VLLM_API_KEY from .env. The agent runner prefers this
//      credential store over the config-file apiKey; drift here produces
//      HTTP 401 from vLLM even when providers.vllm.apiKey is correct.
//  14. Ensure tools.media.audio wires the Whisper STT backend — env-gated by
//      STT_API_TOKEN. Feeds voice-note upload, Discord voice-channel
//      transcription, VoiceCall CLI, and Talk / Voicewake nodes. The Control
//      UI realtime mic button is unaffected — that path uses the browser's
//      native Web Speech API and bypasses this pipeline.
//
// Each step's inline comment below explains *why* (constraint, benchmark, or
// schema gotcha). When adding a step, follow the same deep-merge pattern and
// log a `[patch-config]` line for every field you change.

import fs from 'node:fs';

const CONFIG_PATH = '/home/node/.openclaw/openclaw.json';

// LLM provider — Gemma 4 31B IT NVFP4 on the in-compose `vllm-llm` service.
// Override LLM_BASE_URL in .env to point at any OpenAI-compatible chat endpoint
// (remote vLLM, Bedrock proxy, OpenRouter, …). See .env.example and
// docs/CUSTOMIZATION.md → "Run with a remote vLLM backend".
const LLM_MODEL_ID = 'nvidia/Gemma-4-31B-IT-NVFP4';
const LLM_BASE_URL = process.env.LLM_BASE_URL || 'http://vllm-llm:8004/v1/';
const LLM_API = 'openai-completions';
const VLLM_API_KEY = process.env.VLLM_API_KEY ?? '';

// Embedding provider — bge-m3 on the in-compose `vllm-embedding` service.
// Shares VLLM_API_KEY with the LLM by convention; override EMBED_BASE_URL to
// host embeddings on a different machine.
const EMBED_MODEL = 'BAAI/bge-m3';
const EMBED_BASE_URL = process.env.EMBED_BASE_URL || 'http://vllm-embedding:8005/v1/';

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

// (9) Ensure memorySearch hybrid (BM25 + vector) + MMR diversity rerank.
//     - vectorWeight 0.7 / textWeight 0.3: vector dominates (semantic retrieval
//       is the main use case on multilingual content with bge-m3); BM25
//       supplements on exact-keyword / ID / proper-noun matches where cosine
//       similarity tends to underperform.
//     - candidateMultiplier 3: first-stage fetch brings 3× topK candidates
//       (e.g. topK=5 → 15), which MMR re-ranks back down to topK for diversity.
//     - mmr.lambda 0.7: relevance-weighted (1.0 = pure relevance, 0.0 = pure
//       diversity). 0.7 nudges away from returning near-duplicate chunks.
//     The default SQLite FTS5 tokenizer (`unicode61`) handles accents well but
//     does not stem morphology; for heavily inflected languages where BM25
//     lexical recall matters, consider a trigram tokenizer override.
config.agents ??= {};
config.agents.defaults ??= {};
config.agents.defaults.memorySearch ??= {};
config.agents.defaults.memorySearch.query ??= {};
config.agents.defaults.memorySearch.query.hybrid ??= {};
const hybrid = config.agents.defaults.memorySearch.query.hybrid;

const desiredHybrid = {
  enabled: true,
  vectorWeight: 0.7,
  textWeight: 0.3,
  candidateMultiplier: 3,
};
for (const [k, v] of Object.entries(desiredHybrid)) {
  if (hybrid[k] !== v) {
    hybrid[k] = v;
    changed = true;
    console.log(`[patch-config] agents.defaults.memorySearch.query.hybrid.${k} = ${v}`);
  }
}

hybrid.mmr ??= {};
const desiredMmr = { enabled: true, lambda: 0.7 };
for (const [k, v] of Object.entries(desiredMmr)) {
  if (hybrid.mmr[k] !== v) {
    hybrid.mmr[k] = v;
    changed = true;
    console.log(`[patch-config] agents.defaults.memorySearch.query.hybrid.mmr.${k} = ${v}`);
  }
}

// (10) Ensure webSearch provider = searxng and the bundled searxng plugin is
//      enabled. The plugin ships bundled-but-default-disabled; without the
//      explicit enable the gateway keeps it in "bundled (disabled by default)"
//      state and the webSearch tool never lights up in the agent runtime.
//      The SearxNG service runs as a sibling container on the compose default
//      bridge, reachable by DNS at http://searxng:8080. Privacy posture lives
//      in searxng/settings/settings.yml — here we only wire baseUrl + language.
//      We deliberately do NOT pin `categories`: the gateway forwards a static
//      string here as a Python-list literal in the SearxNG POST form, which
//      SearxNG rejects with a validation warning. Letting the agent pass
//      categories per query (or relying on SearxNG's default) keeps the log
//      clean and the search functional.
config.tools ??= {};
config.tools.web ??= {};
config.tools.web.search ??= {};
if (config.tools.web.search.provider !== 'searxng') {
  const prev = config.tools.web.search.provider;
  config.tools.web.search.provider = 'searxng';
  changed = true;
  console.log(`[patch-config] tools.web.search.provider: ${prev ?? '(unset)'} -> searxng`);
}

config.plugins ??= {};
config.plugins.entries ??= {};
config.plugins.entries.searxng ??= {};
if (config.plugins.entries.searxng.enabled !== true) {
  config.plugins.entries.searxng.enabled = true;
  changed = true;
  console.log('[patch-config] plugins.entries.searxng.enabled = true (bundled plugin enable)');
}
config.plugins.entries.searxng.config ??= {};
config.plugins.entries.searxng.config.webSearch ??= {};
const ws = config.plugins.entries.searxng.config.webSearch;

const desiredWebSearch = {
  baseUrl: 'http://searxng:8080',
  language: '',
};
for (const [k, v] of Object.entries(desiredWebSearch)) {
  if (ws[k] !== v) {
    ws[k] = v;
    changed = true;
    console.log(`[patch-config] plugins.entries.searxng.config.webSearch.${k} = ${JSON.stringify(v)}`);
  }
}

// (11) Ensure messages.tts.providers.openai points at the openclaw-tts-router.
//      Env-gated: when OPENCLAW_TTS_ROUTER_API_KEY is unset, leave the openai
//      TTS provider untouched. This lets users opt out of TTS by simply not
//      setting the var (and parking the openclaw-tts-en / openclaw-tts-router
//      services with `profiles: ["never"]` in the compose file).
//
//      The router exposes the OpenAI Audio API shape on
//      `${OPENCLAW_TTS_ROUTER_URL}/audio/speech` and accepts the same model /
//      voice fields. The gateway sends `model` opaquely (the router ignores
//      it), and the `voiceId` we set is what the TTS surface picks when an
//      agent doesn't override. voiceAliases give the agent (and human users)
//      friendly names like `english`, `narrator`, `male` instead of Kokoro's
//      `af_heart` / `bf_emma` / `am_michael`.
//
//      Hungarian aliases (`magyar`, `hungarian`) are written unconditionally
//      so the alias surface stays stable; the router itself decides whether
//      `default_hu` is a live voice (it is only when F5HUN_URL +
//      F5HUN_API_TOKEN are set on the router service). Without HU wired, the
//      router's HU autodetect is a no-op and `default_hu` returns 404 — that
//      is the user's signal that they need to bring an F5-TTS HU backend.
const ttsRouterKey = process.env.OPENCLAW_TTS_ROUTER_API_KEY?.trim();
if (ttsRouterKey) {
  config.messages ??= {};
  config.messages.tts ??= {};

  // Top-level switches. OpenClaw gateway builds observed in 2026-04 require
  // these to actually invoke the configured TTS provider. Without them, the
  // providers.openai block below is silently ignored on Discord / voice-skill
  // paths (the web chat UI is hard-wired to the browser's speechSynthesis
  // and bypasses this pipeline, so it isn't affected either way).
  // Enum values required by the OpenClaw v0.4.x config schema:
  //   auto: "off" | "always" | "inbound" | "tagged"   (NOT a boolean)
  //   mode: "final" | "all"                            (NOT "auto")
  // An earlier draft wrote `auto: true` / `mode: 'auto'`, which the gateway
  // rejects with `Invalid option` and crash-loops on startup. `always` +
  // `final` reproduces the intended "speak every final agent message" posture.
  const desiredTopLevel = {
    enabled: true,
    auto: 'always',
    mode: 'final',
  };
  for (const [k, v] of Object.entries(desiredTopLevel)) {
    if (config.messages.tts[k] !== v) {
      config.messages.tts[k] = v;
      changed = true;
      console.log(`[patch-config] messages.tts.${k} = ${JSON.stringify(v)}`);
    }
  }

  config.messages.tts.providers ??= {};
  config.messages.tts.providers.openai ??= {};
  const tts = config.messages.tts.providers.openai;

  const desiredTts = {
    baseUrl: process.env.OPENCLAW_TTS_ROUTER_URL || 'http://openclaw-tts-router:8080/v1',
    apiKey: ttsRouterKey,
    model: 'openclaw-tts',
    voiceId: process.env.OPENCLAW_TTS_DEFAULT_VOICE || 'af_heart',
  };
  for (const [k, v] of Object.entries(desiredTts)) {
    if (tts[k] !== v) {
      const shown = k === 'apiKey' ? `${String(v).slice(0, 4)}...(len=${String(v).length})` : JSON.stringify(v);
      tts[k] = v;
      changed = true;
      console.log(`[patch-config] messages.tts.providers.openai.${k} = ${shown}`);
    }
  }

  tts.voiceAliases ??= {};
  const desiredAliases = {
    english:   'af_heart',
    narrator:  'bf_emma',
    male:      'am_michael',
    female:    'af_bella',
    magyar:    'default_hu',
    hungarian: 'default_hu',
  };
  for (const [k, v] of Object.entries(desiredAliases)) {
    if (tts.voiceAliases[k] !== v) {
      tts.voiceAliases[k] = v;
      changed = true;
      console.log(`[patch-config] messages.tts.providers.openai.voiceAliases.${k} = ${JSON.stringify(v)}`);
    }
  }
} else {
  console.log('[patch-config] OPENCLAW_TTS_ROUTER_API_KEY not set — skipping messages.tts.* (TTS opt-out).');
}

// (12) Mirror gateway.auth.token into gateway.remote.token.
//      The OpenClaw onboarding wizard writes gateway.auth.token (what the
//      gateway accepts) but leaves gateway.remote.token (what the local CLI
//      presents on WS connect) unset or stale. On a mismatch the CLI gets
//      `unauthorized: gateway token mismatch` and falls back to an embedded
//      runner, which dials vLLM from the CLI's own env — a separate and
//      error-prone path. Keeping them in sync makes `openclaw agent` take the
//      gateway route, which is the one actually exercised by production
//      clients (Chrome extension, remote CLI, reverse proxy).
const authToken = config?.gateway?.auth?.token;
if (typeof authToken === 'string' && authToken) {
  config.gateway.remote ??= {};
  if (config.gateway.remote.token !== authToken) {
    const prev = config.gateway.remote.token;
    config.gateway.remote.token = authToken;
    changed = true;
    const prevShown = typeof prev === 'string' ? `(len=${prev.length})` : prev === undefined ? '(unset)' : prev;
    console.log(`[patch-config] gateway.remote.token: ${prevShown} -> (len=${authToken.length}) [mirrored from gateway.auth.token]`);
  }
} else {
  console.log('[patch-config] gateway.auth.token missing — skipped gateway.remote.token mirror (pre-onboarding).');
}

// (13) Sync per-agent auth-profiles.json with the current VLLM_API_KEY.
//      OpenClaw stores provider credentials per-agent in
//      `~/.openclaw/agents/<agent>/agent/auth-profiles.json`. The agent
//      runner reads the key from there, NOT from models.providers.vllm.apiKey
//      (which we keep patched in step 2). The wizard seeds this file during
//      onboarding; after a secret rotation in .env, the per-agent profile
//      goes stale and every LLM call gets HTTP 401 from vLLM.
//
//      We rewrite the file atomically: preserve unrelated profiles,
//      update/create the `vllm:default` entry, keep version=1. Skipped
//      entirely when VLLM_API_KEY is empty (opt-out) or the agent dir
//      doesn't exist yet (pre-onboarding — the wizard will create it).
if (VLLM_API_KEY) {
  const agentsDir = '/home/node/.openclaw/agents';
  if (fs.existsSync(agentsDir)) {
    for (const agentId of fs.readdirSync(agentsDir)) {
      const profilePath = `${agentsDir}/${agentId}/agent/auth-profiles.json`;
      if (!fs.existsSync(profilePath)) continue;
      let profile;
      try {
        profile = JSON.parse(fs.readFileSync(profilePath, 'utf8'));
      } catch (e) {
        console.warn(`[patch-config] failed to parse ${profilePath}: ${e.message} — skipping.`);
        continue;
      }
      profile.version ??= 1;
      profile.profiles ??= {};
      profile.profiles['vllm:default'] ??= { type: 'api_key', provider: 'vllm' };
      const cur = profile.profiles['vllm:default'];
      const desired = { type: 'api_key', provider: 'vllm', key: VLLM_API_KEY };
      let agentChanged = false;
      for (const [k, v] of Object.entries(desired)) {
        if (cur[k] !== v) {
          const prev = cur[k];
          cur[k] = v;
          agentChanged = true;
          const shown = k === 'key' ? `(len=${String(v).length})` : JSON.stringify(v);
          const prevShown = k === 'key' && typeof prev === 'string' ? `(len=${prev.length})` : prev === undefined ? '(unset)' : JSON.stringify(prev);
          console.log(`[patch-config] agents/${agentId}/auth-profiles.json vllm:default.${k}: ${prevShown} -> ${shown}`);
        }
      }
      if (agentChanged) {
        fs.writeFileSync(profilePath, JSON.stringify(profile, null, 2) + '\n');
        changed = true;
      }
    }
  } else {
    console.log('[patch-config] agents dir missing — skipped auth-profiles sync (pre-onboarding).');
  }
} else {
  console.log('[patch-config] VLLM_API_KEY not set — skipped auth-profiles sync.');
}

// (14) Ensure tools.media.audio wires the Whisper STT service.
//      OpenClaw's audio pipeline — voice-note upload in the Control UI chat,
//      Discord voice-channel transcription, VoiceCall CLI, Talk / Voicewake
//      nodes — picks the first matching entry in tools.media.audio.models[]
//      and POSTs to its baseUrl. Per-entry baseUrl overrides the global
//      models.providers.openai.baseUrl per the audio-node docs, so there is
//      no need for a separate provider-level openai-stt namespace.
//
//      Auth isolation: the schema normally routes provider auth through the
//      standard chain (auth profiles, env vars, models.providers.openai.apiKey).
//      To keep the Whisper Bearer token separated from any cloud OpenAI
//      account the user might also have configured, we write the Bearer
//      explicitly into headers.Authorization. speaches accepts that form,
//      and this keeps the STT token orthogonal to the global openai apiKey.
//
//      Env-gated: when STT_API_TOKEN is unset, skip cleanly so users can opt
//      out by clearing the env var (and parking openclaw-stt-whisper with
//      `profiles: ["never"]` in the compose file). Upsert-by-baseUrl
//      preserves any unrelated user-added entries (a cloud Deepgram/Groq
//      fallback, a whisper-cpp CLI entry, …).
const sttToken = process.env.STT_API_TOKEN?.trim();
if (sttToken) {
  config.tools ??= {};
  config.tools.media ??= {};
  config.tools.media.audio ??= {};

  if (config.tools.media.audio.enabled !== true) {
    config.tools.media.audio.enabled = true;
    changed = true;
    console.log('[patch-config] tools.media.audio.enabled = true');
  }

  config.tools.media.audio.models ??= [];

  // OpenClaw convention: trailing slash on baseUrl (same as vllm /
  // memorySearch). Append one if the env var omits it, so either form works.
  const sttBaseUrl = (process.env.OPENCLAW_STT_BASE_URL || 'http://openclaw-stt-whisper:8080/v1')
    .replace(/\/?$/, '/');
  const sttModel = process.env.OPENCLAW_STT_MODEL || 'Systran/faster-whisper-large-v3';
  const sttLanguage = process.env.OPENCLAW_STT_LANGUAGE?.trim();

  const desiredEntry = {
    provider: 'openai',
    model: sttModel,
    baseUrl: sttBaseUrl,
    headers: {
      Authorization: `Bearer ${sttToken}`,
    },
  };
  if (sttLanguage) desiredEntry.language = sttLanguage;

  const idx = config.tools.media.audio.models.findIndex(
    (m) => m?.provider === 'openai' && m?.baseUrl === sttBaseUrl,
  );
  const existing = idx >= 0 ? config.tools.media.audio.models[idx] : {};

  // Deep-merge headers so the user can layer extra headers onto ours without
  // losing the Authorization on re-run.
  const mergedHeaders = { ...(existing.headers || {}), ...desiredEntry.headers };
  const merged = { ...existing, ...desiredEntry, headers: mergedHeaders };

  const entryDiffers = idx < 0 || JSON.stringify(existing) !== JSON.stringify(merged);
  if (entryDiffers) {
    if (idx < 0) {
      config.tools.media.audio.models.unshift(merged);
    } else {
      config.tools.media.audio.models[idx] = merged;
    }
    changed = true;
    const shownKey = `${sttToken.slice(0, 4)}...(len=${sttToken.length})`;
    const langFrag = sttLanguage ? `, language:${sttLanguage}` : '';
    console.log(
      `[patch-config] tools.media.audio.models[${idx < 0 ? 'unshift' : idx}] = ` +
        `{provider:openai, model:${sttModel}, baseUrl:${sttBaseUrl}, ` +
        `Authorization:Bearer ${shownKey}${langFrag}}`,
    );
  }
} else {
  console.log('[patch-config] STT_API_TOKEN not set — skipping tools.media.audio (STT opt-out).');
}

if (!changed) {
  console.log('[patch-config] no-op (openclaw.json already in the desired state).');
  process.exit(0);
}

fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + '\n');
console.log('[patch-config] openclaw.json updated.');
