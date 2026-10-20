// Long-term memory: the embedding backend behind memory_search, nightly
// dreaming (consolidation into MEMORY.md), and Active Memory (recall that runs
// before a reply, so the Discord bot remembers other channels and DMs without
// having to decide to search).

import fs from 'node:fs';
import path from 'node:path';
import * as env from '../lib/env.mjs';

// Removes `path` and then every parent object it leaves empty, so switching a
// feature off leaves no `plugins.entries: {}` husk behind.
function removePruned(config, keys, why) {
  if (!config.remove(keys, why)) return;
  for (let i = keys.length - 1; i > 0; i--) {
    const parent = config.get(keys.slice(0, i));
    if (parent && typeof parent === 'object' && !Object.keys(parent).length) config.remove(keys.slice(0, i));
    else break;
  }
}

function search(config) {
  // memory_search embeds with the local bge-m3 service. It shares the vLLM
  // key by convention; without one the embedding call would 401, so leave
  // whatever onboarding wrote.
  const key = env.str('VLLM_API_KEY');
  if (!key) {
    console.warn('[patch-config] VLLM_API_KEY is not set; memory.search left as is.');
    return;
  }
  config.merge('memory.search', { enabled: true, provider: 'openai', model: 'BAAI/bge-m3' });
  config.merge('memory.search.remote', {
    baseUrl: env.str('EMBED_BASE_URL', 'http://vllm-embedding:8005/v1/'),
    apiKey: key,
  });
}

// The Dream Diary grows without bound (~50 KB per workspace after a few
// months). Past the threshold it moves to memory/dreams-archive/, which
// memory_search still indexes; dreaming starts a fresh DREAMS.md next night.
function rotateDreams(ctx) {
  const limit = env.int('OPENCLAW_DREAMS_ROTATE_BYTES', 32768);
  if (limit <= 0) return;
  for (const root of [ctx.ws(), ctx.ws('discord')]) {
    const file = path.join(root, 'DREAMS.md');
    let size;
    try { size = fs.statSync(file).size; } catch { continue; }
    if (size <= limit) continue;
    try {
      const dir = path.join(root, 'memory', 'dreams-archive');
      fs.mkdirSync(dir, { recursive: true, mode: 0o755 });
      const stamp = new Date().toISOString().slice(0, 10);
      let target = path.join(dir, `DREAMS-${stamp}.md`);
      for (let n = 1; fs.existsSync(target); n++) target = path.join(dir, `DREAMS-${stamp}-${n}.md`);
      fs.renameSync(file, target);
      console.log(`[patch-config] rotated ${file} (${size} bytes > ${limit}) -> ${target}`);
    } catch (err) {
      console.warn(`[patch-config] DREAMS.md rotation failed in ${root}: ${err.message}`);
    }
  }
}

function dreaming(ctx) {
  const { config } = ctx;
  const p = ['plugins', 'entries', 'memory-core'];
  if (!env.bool('OPENCLAW_ENABLE_DREAMING', true)) {
    removePruned(config, p, 'OPENCLAW_ENABLE_DREAMING=off');
    return;
  }
  config.set([...p, 'enabled'], true);
  const d = [...p, 'config', 'dreaming'];
  config.merge(d, {
    enabled: true,
    // Nightly at 03:00, after the default heartbeat window (09:00-02:00) ends.
    frequency: '0 3 * * *',
    timezone: env.str('OPENCLAW_HEARTBEAT_TZ', 'UTC'),
    verboseLogging: false,
  });
  // `separate`: inline storage writes Light/REM Sleep blocks into the daily
  // notes, and on a group-chat bot those were mostly session metadata
  // ("Session Key: ...") that then ranked in memory_search next to real notes.
  config.set([...d, 'storage', 'mode'], 'separate');
  config.merge([...d, 'phases', 'light'], { enabled: true, lookbackDays: 3, limit: 20, dedupeSimilarity: 0.92 });
  config.merge([...d, 'phases', 'deep'], {
    enabled: true, limit: 10, minScore: 0.75, minRecallCount: 2, minUniqueQueries: 2,
    recencyHalfLifeDays: 14, maxAgeDays: 90,
    // MEMORY.md goes into every session's bootstrap; unbounded promotions grew
    // it to ~12 KB. 80 tokens per promotion halves the upstream growth rate.
    maxPromotedSnippetTokens: 80,
  });
  config.merge([...d, 'phases', 'rem'], { enabled: true, lookbackDays: 14, limit: 5, minPatternStrength: 0.6 });
  rotateDreams(ctx);
}

function activeMemory(config) {
  const p = ['plugins', 'entries', 'active-memory'];
  if (!env.bool('OPENCLAW_ACTIVE_MEMORY', true)) {
    removePruned(config, p, 'OPENCLAW_ACTIVE_MEMORY=off');
    return;
  }
  config.set([...p, 'enabled'], true);
  config.merge([...p, 'config'], {
    enabled: true,
    // `always`: recall runs on every eligible turn. The default `escalate`
    // only runs on recall intent, and its intent detector has no Hungarian
    // patterns, so a Hungarian guild never escalated.
    mode: 'always',
    // `agents` is the narrowest documented scope: only the Discord-routed
    // agent recalls; the operator agent and the MeshCore agents never pay
    // the extra latency.
    agents: ['discord-friend'],
    allowedChatTypes: ['direct', 'channel', 'group'],
    queryMode: 'recent',
    promptStyle: 'recall-heavy',
    // Thinking on the recall sub-agent pushed it past its timeout on every
    // turn; 30 s leaves room for a vision-capable model's prefill.
    thinking: 'off',
    timeoutMs: 30000,
    maxSummaryChars: 600,
    // Less recent context in, faster recall out.
    recentUserTurns: 1,
    recentAssistantTurns: 1,
    recentUserChars: 300,
    recentAssistantChars: 200,
  });
}

export default function memory(ctx) {
  search(ctx.config);
  dreaming(ctx);
  activeMemory(ctx.config);
}
