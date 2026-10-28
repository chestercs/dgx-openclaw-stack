// Gateway and run-level defaults: who the gateway trusts, how big a prompt
// and how long a run may get, heartbeats, sub-agents, message queueing, and
// the device / node pairing surface.

import * as env from '../lib/env.mjs';
import { adopt } from '../lib/secrets.mjs';

const QUEUE_MODES = ['steer', 'followup', 'collect', 'interrupt'];

// Removes `path` and then every parent object it leaves empty.
function removePruned(config, keys, why) {
  if (!config.remove(keys, why)) return;
  for (let i = keys.length - 1; i > 0; i--) {
    const parent = config.get(keys.slice(0, i));
    if (parent && typeof parent === 'object' && !Object.keys(parent).length) config.remove(keys.slice(0, i));
    else break;
  }
}

// `off` removes the key, a value writes it; anything else leaves it alone.
const offOr = (name, def) => (env.raw(name)?.toLowerCase() === 'off' ? 'off' : env.str(name, def));

function network(config) {
  // Loopback + the Docker bridge range, so X-Forwarded-For from the reverse
  // proxy is honoured. LAN clients that hit the gateway directly need their
  // range here too, or they show up as the proxy's address.
  const lan = env.str('OPENCLAW_LAN_CIDR');
  config.set('gateway.trustedProxies', ['127.0.0.1', '::1', '172.16.0.0/12', ...(lan ? [lan] : [])]);

  // The shared token lives in .env (OPENCLAW_GATEWAY_TOKEN, passed to the
  // gateway, the CLI and the MeshCore bridge). Onboarding writes
  // gateway.auth.token but not gateway.remote.token, which is what the local
  // CLI presents; on a mismatch `openclaw agent` falls back to an embedded
  // runner that dials vLLM from the CLI's own env.
  // Password and trusted-proxy auth refuse a token next to them.
  if ((config.get('gateway.auth.mode') ?? 'token') === 'token') adopt(config, 'gateway.auth.token', 'OPENCLAW_GATEWAY_TOKEN');
  const token = config.get('gateway.auth.token');
  if (token) config.set('gateway.remote.token', token, 'mirrors gateway.auth.token');

  // `openclaw qr` advertises the container's bridge IP otherwise, and a
  // plaintext ws:// URL downgrades setup codes to "Access: limited". Point it
  // at the TLS reverse proxy (it must forward WebSocket upgrades).
  const pairUrl = offOr('OPENCLAW_DEVICE_PAIR_PUBLIC_URL');
  const pairPath = ['plugins', 'entries', 'device-pair', 'config', 'publicUrl'];
  if (pairUrl === 'off') removePruned(config, pairPath, 'OPENCLAW_DEVICE_PAIR_PUBLIC_URL=off');
  else if (pairUrl) {
    if (!/^wss:\/\//i.test(pairUrl)) console.warn(`[patch-config] OPENCLAW_DEVICE_PAIR_PUBLIC_URL is not wss://; setup codes stay "Access: limited".`);
    config.set(pairPath, pairUrl);
  }

  // The gateway admits only a per-platform default command set from a node.
  // On Windows that leaves out the Companion's canvas.*, tts.* and stt.*
  // commands. Exact names, no wildcards; the node re-reconciles on connect.
  const allowPath = ['gateway', 'nodes', 'commands', 'allow'];
  if (offOr('OPENCLAW_GATEWAY_NODE_COMMANDS_ALLOW') === 'off') removePruned(config, allowPath, 'OPENCLAW_GATEWAY_NODE_COMMANDS_ALLOW=off');
  else {
    const allow = [...new Set(env.list('OPENCLAW_GATEWAY_NODE_COMMANDS_ALLOW'))];
    if (allow.length) config.set(allowPath, allow);
  }
}

function heartbeat(config) {
  const hb = ['agents', 'defaults', 'heartbeat'];
  config.merge(hb, { every: '30m', isolatedSession: true });
  config.merge([...hb, 'activeHours'], {
    start: env.str('OPENCLAW_HEARTBEAT_ACTIVE_START', '09:00'),
    end: env.str('OPENCLAW_HEARTBEAT_ACTIVE_END', '02:00'),
    timezone: env.str('OPENCLAW_HEARTBEAT_TZ', 'UTC'),
  });

  // The default target "owner" finds no DM on Discord: the plugin reads the
  // bare owner snowflake as a channel, and every poll skips with
  // reason=no-route. An explicit target plus `user:<id>` reaches the DM.
  // It sits on the defaults because a per-agent heartbeat block switches the
  // heartbeat off for every other agent.
  // OPENCLAW_HEARTBEAT_TO: a user ID; empty = first ID in
  // OPENCLAW_DISCORD_COMMAND_OWNERS; off = leave delivery to OpenClaw.
  const to = env.str('OPENCLAW_HEARTBEAT_TO');
  if (to?.toLowerCase() === 'off') {
    if (config.get([...hb, 'target']) === 'discord') {
      config.remove([...hb, 'target'], 'OPENCLAW_HEARTBEAT_TO=off');
      config.remove([...hb, 'to'], 'OPENCLAW_HEARTBEAT_TO=off');
      config.remove([...hb, 'directPolicy'], 'OPENCLAW_HEARTBEAT_TO=off');
    }
    return;
  }
  const owner = to ?? env.ids('OPENCLAW_DISCORD_COMMAND_OWNERS')[0];
  if (/^\d+$/.test(owner ?? '') && config.get('channels.discord.enabled') === true) {
    // directPolicy "allow" is the default; stated, because Doctor warns on
    // every start about a DM target with the policy unset.
    config.merge(hb, { target: 'discord', to: `user:${owner}`, directPolicy: 'allow' }, 'owner DM');
  }
}

function runs(config) {
  const d = ['agents', 'defaults'];

  // Per-file and total caps on injected workspace files. Past the cap the
  // file is cut silently and the agent never sees its tail; the Discord
  // AGENTS.md is ~29 KB, and the upstream 12000 / 60000 cut it.
  config.set([...d, 'bootstrapMaxChars'], env.int('OPENCLAW_BOOTSTRAP_MAX_CHARS', 60000));
  config.set([...d, 'bootstrapTotalMaxChars'], env.int('OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS', 80000));

  // Per-run timeout, aligned with the provider's LLM_REQUEST_TIMEOUT_SECONDS:
  // a cold 128K prefill alone is ~2 minutes. OpenClaw 2026.10's own default is
  // 48 h; the schema takes a positive integer (days-long autonomous work on
  // Strata: compose/strata-llm.yml).
  const timeout = offOr('OPENCLAW_AGENT_TIMEOUT_SECONDS', '1800');
  if (timeout === 'off') config.remove([...d, 'timeoutSeconds'], 'OPENCLAW_AGENT_TIMEOUT_SECONDS=off');
  else config.set([...d, 'timeoutSeconds'], env.int('OPENCLAW_AGENT_TIMEOUT_SECONDS', 1800));

  // Trims old tool results from the in-memory context before each model call
  // (the transcript on disk is untouched). On long coding runs exec/read
  // output dominates context growth; this keeps prefill bounded.
  const prune = env.oneOf('OPENCLAW_AGENT_CONTEXT_PRUNING_MODE', ['cache-ttl', 'off'], 'cache-ttl');
  if (prune === 'off') config.remove([...d, 'contextPruning'], 'OPENCLAW_AGENT_CONTEXT_PRUNING_MODE=off');
  else config.merge([...d, 'contextPruning'], { mode: 'cache-ttl', ttl: env.str('OPENCLAW_AGENT_CONTEXT_PRUNING_TTL', '5m') });

  // Sub-agent bounds. Depth 2 lets a child spawn its own workers. A child
  // runs at most 30 minutes by default, so a stuck one ends instead of
  // holding a slot; "work on this for a day" passes runTimeoutSeconds 0 in
  // its own sessions_spawn call (the per-call value wins, 0 = no limit).
  // The values stay inside the documented ranges: an out-of-range one is
  // rejected as "agents.defaults: Invalid input" and the gateway crash-loops.
  if (env.bool('OPENCLAW_SUBAGENTS_BOUNDS', true)) {
    config.merge([...d, 'subagents'], {
      maxSpawnDepth: 2,
      maxChildrenPerAgent: 5,
      maxConcurrent: 8,
      runTimeoutSeconds: Math.max(0, env.int('OPENCLAW_SUBAGENTS_RUN_TIMEOUT', 1800)),
      announceTimeoutMs: 120000,
      archiveAfterMinutes: 60,
      delegationMode: 'suggest',
    });
  } else config.remove([...d, 'subagents'], 'OPENCLAW_SUBAGENTS_BOUNDS=off');

  // `sessions_spawn {thread:true}` may mint a new thread-bound session, and
  // the child starts from a clean transcript rather than a fork of the parent.
  if (env.bool('OPENCLAW_SESSION_THREAD_SPAWN', true)) {
    config.merge('session.threadBindings', { spawnSessions: true, defaultSpawnContext: 'isolated' });
  } else removePruned(config, ['session', 'threadBindings'], 'OPENCLAW_SESSION_THREAD_SPAWN=off');

  // Owner of Custodian / system-agent consults. The Discord persona has no
  // gateway tools and answered "approve node ..." with "noted"; the operator
  // agent `main` does. Only written when that agent exists.
  const sys = env.str('OPENCLAW_SYSTEM_AGENT', 'main');
  if (sys.toLowerCase() !== 'off') {
    if (config.get(['agents', 'entries', sys]) === undefined) console.warn(`[patch-config] OPENCLAW_SYSTEM_AGENT=${sys} is not a configured agent; agents.defaults.systemAgent left as is.`);
    else config.set([...d, 'systemAgent', 'agentId'], sys);
  }
}

function messages(config) {
  // Run-state emoji on the user's message (queued, thinking, tool, done). With
  // a slow local model it is the first sign the bot is working. Driven by the
  // run lifecycle, not the inbound-ack queue whose replays cycled reactions.
  if (env.bool('OPENCLAW_STATUS_REACTIONS', true)) config.set('messages.statusReactions.enabled', true);
  else removePruned(config, ['messages', 'statusReactions'], 'OPENCLAW_STATUS_REACTIONS=off');

  // Pinned because the upstream docs disagree on the default. `steer` folds a
  // mid-run message into the running turn at the next model boundary, so a
  // correction lands now instead of after a multi-hour run.
  config.set('messages.queue.mode', env.oneOf('OPENCLAW_MESSAGES_QUEUE_MODE', QUEUE_MODES, 'steer'));

  // A guild channel is one shared session, so `steer` merged a second user's
  // request into the run still working on the first: of an image request and
  // a screenshot request 6 s apart, only the screenshot was posted.
  // `followup` gives every message its own run, in order.
  if (config.get('channels.discord') !== undefined) {
    config.set('messages.queue.byChannel.discord', env.oneOf('OPENCLAW_DISCORD_QUEUE_MODE', QUEUE_MODES, 'followup'));
  }
}

// A guild channel is one shared session that never resets by itself: it kept
// growing, and the model copied its own older replies, wrong ones included.
// After OPENCLAW_DISCORD_SESSION_IDLE_MINUTES (default 240) without a message
// the next one starts a fresh session; people memory and Active Memory carry
// what should last. `off` removes the policy. Guild channels count as "group".
function sessionReset(config) {
  const p = ['session', 'resetByType', 'group'];
  if (env.raw('OPENCLAW_DISCORD_SESSION_IDLE_MINUTES')?.toLowerCase() === 'off') {
    removePruned(config, p, 'OPENCLAW_DISCORD_SESSION_IDLE_MINUTES=off');
    return;
  }
  config.merge(p, { mode: 'idle', idleMinutes: env.int('OPENCLAW_DISCORD_SESSION_IDLE_MINUTES', 240) });
}

export default function gateway({ config }) {
  sessionReset(config);
  network(config);
  heartbeat(config);
  runs(config);
  messages(config);
}
