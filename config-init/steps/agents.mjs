// The agent roster beyond `main`: the tool surface and thinking level of the
// Discord persona, the MeshCore radio agents, the Valheim builder and the
// optional deep sub-agent. Agents live in agents.entries.<id>.
//
// The Discord persona is created by the channel onboarding, not here; it is
// whichever agent the catch-all Discord binding routes to. The other agents
// are patcher-owned: created here, with a workspace and an AGENTS.md seed
// (written once; the seed then belongs to the agent and the operator).

import fs from 'node:fs';
import path from 'node:path';
import * as env from '../lib/env.mjs';
import { template, writeIfChanged } from '../lib/files.mjs';

const THINKING = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'adaptive', 'max'];
const PROFILES = ['minimal', 'coding', 'messaging', 'full'];
const VALHEIM_GLOB = 'valheim__*';

// Operator and desktop surfaces a Discord persona never needs. The `full`
// profile plus the bundled plugins gave the bot 113 tools on 2026.10.3, and a
// MoE picks badly from a catalog that size. `image_generate` collides with the
// local comfyui_image__generate under a shorter name (and has no key here).
// The five-tool workboard core (create/list/show/dispatch/update) stays.
const DISCORD_DENY = [
  'computer', 'mobile_ui', 'nodes', 'node_inference', 'gateway', 'plugins', 'openclaw',
  'secrets', 'terminal', 'portal', 'theme', 'dashboard', 'presence', 'progress_card',
  'talk_voice', 'file_fetch', 'file_write', 'dir_list', 'dir_fetch',
  'wiki_status', 'wiki_lint', 'wiki_apply', 'wiki_search', 'wiki_get',
  'conversations_list', 'conversations_send', 'conversations_turn',
  'create_goal', 'get_goal', 'update_goal', 'ask_user', 'skill_workshop', 'image_generate',
  'workboard_link', 'workboard_claim', 'workboard_heartbeat', 'workboard_release',
  'workboard_comment', 'workboard_proof', 'workboard_attachment_add',
  'workboard_attachment_read', 'workboard_attachment_delete', 'workboard_block',
  'workboard_unblock', 'workboard_move', 'workboard_boards', 'workboard_board_create',
  'workboard_board_archive', 'workboard_board_delete', 'workboard_stats', 'workboard_runs',
  'workboard_specify', 'workboard_decompose', 'workboard_notify_subscribe',
  'workboard_notify_list', 'workboard_notify_events', 'workboard_notify_advance',
  'workboard_notify_unsubscribe', 'workboard_promote', 'workboard_reassign',
  'workboard_reclaim', 'workboard_worker_log', 'workboard_protocol_violation',
  'workboard_sessions_board_read', 'workboard_sessions_board_update',
  'workboard_sessions_board_move',
];

// ComfyUI tool groups the Discord agent can be cut off from when ComfyUI does
// not run them (a large LLM backend leaves no room for renders). The bridge
// still advertises the tools and each schema costs prompt space
// (generate_video alone is ~5 KB); with the image tools left in, the bot kept
// calling a dead generate tool. OPENCLAW_DISCORD_MEDIA_TOOLS_OFF lists groups;
// a group left out is taken off the deny list again.
export const MEDIA_TOOL_GROUPS = {
  image: ['comfyui_image__generate', 'comfyui_image__generate_i2i', 'comfyui_image__list_workflows'],
  video: ['comfyui_image__generate_video'],
  music: ['comfyui_image__generate_music', 'comfyui_image__generate_music_repaint'],
};

const entry = (id, ...rest) => ['agents', 'entries', id, ...rest];

// Agents routed by a catch-all Discord binding (no peer / guild / team / role
// scope). A channel-scoped binding (the Valheim builder) is a different
// persona and must not inherit the bot's tool profile, deny list or thinking.
export function discordAgentIds(config) {
  return [...new Set(
    (config.get('bindings') ?? [])
      .filter((b) => b?.type === 'route' && b?.match?.channel === 'discord' && typeof b?.agentId === 'string' &&
        !b.match.peer && !b.match.guildId && !b.match.teamId && !b.match.roles)
      .map((b) => b.agentId),
  )].filter((id) => config.get(entry(id)) !== undefined);
}

function seedOnce(file, text) {
  if (!fs.existsSync(file)) writeIfChanged(file, text);
}

// Workspace with its memory dir (the per-agent memory index reads
// workspace/memory/*.md; a separate workspace is what makes a separate
// agent's memory separate on disk) and an AGENTS.md seed.
function workspace(dir, seed) {
  fs.mkdirSync(path.join(dir, 'memory'), { recursive: true, mode: 0o755 });
  seedOnce(path.join(dir, 'AGENTS.md'), seed);
}

// The Discord persona's tool surface and thinking.
function discordAgent(config) {
  // Without an explicit profile the agent inherits `coding`, which has no
  // browser, tts or canvas ("I can't navigate the browser" on a screenshot
  // request). `full` gives it the same surface as main, minus the deny list.
  const profile = env.oneOf('OPENCLAW_DISCORD_AGENT_TOOLS_PROFILE', [...PROFILES, 'off'], 'full');
  // alsoAllow on top of the profile: on guild channels the runtime policy
  // drops `cron` from the catalog even under `full` (it shows in DMs), and the
  // model then says it cannot set reminders. Additive: operator entries stay.
  const alsoAllow = env.raw('OPENCLAW_DISCORD_AGENT_ALSO_ALLOW')?.toLowerCase() === 'off'
    ? [] : env.list('OPENCLAW_DISCORD_AGENT_ALSO_ALLOW', ['group:messaging', 'browser', 'tts', 'canvas', 'cron']);
  // `high`: at `minimal` the MoE still skipped tool calls on nuanced
  // Hungarian prompts and claimed it could not search. `off` leaves it unset.
  const thinking = env.oneOf('OPENCLAW_DISCORD_AGENT_THINKING', [...THINKING], 'high');
  // The list replaces the default; `off` skips it. Additive: operator
  // entries stay, and dropping a name here does not take it off the agent.
  const deny = env.raw('OPENCLAW_DISCORD_AGENT_TOOLS_DENY')?.toLowerCase() === 'off' ? [] : env.list('OPENCLAW_DISCORD_AGENT_TOOLS_DENY', DISCORD_DENY);
  const mediaOff = new Set(env.list('OPENCLAW_DISCORD_MEDIA_TOOLS_OFF').map((g) => g.toLowerCase()));
  const offTools = Object.entries(MEDIA_TOOL_GROUPS).filter(([g]) => mediaOff.has(g)).flatMap(([, t]) => t);
  const onTools = Object.entries(MEDIA_TOOL_GROUPS).filter(([g]) => !mediaOff.has(g)).flatMap(([, t]) => t);

  for (const id of discordAgentIds(config)) {
    if (profile !== 'off') config.set(entry(id, 'tools', 'profile'), profile);
    if (alsoAllow.length) config.addTo(entry(id, 'tools', 'alsoAllow'), alsoAllow);
    if (thinking !== 'off') config.set(entry(id, 'thinkingDefault'), thinking);
    if (deny.length) config.addTo(entry(id, 'tools', 'deny'), deny);
    config.removeFrom(entry(id, 'tools', 'deny'), onTools, 'media group back on');
    if (offTools.length) config.addTo(entry(id, 'tools', 'deny'), offTools, 'OPENCLAW_DISCORD_MEDIA_TOOLS_OFF');
  }
}

// MeshCore radio agents for the openclaw-meshcore-bridge service. No channel
// plugin exists for MeshCore, so the patcher registers them. Two agents
// because a session key does not isolate memory (memory and workspace are
// per agent): the private DM rail, and an optional shared-channel agent whose
// own memory keeps DM topics out of a room several people read. The mesh is a
// public radio and the bridge allowlist is its only gate, hence the narrow
// default tools (minimal + web_search).
function meshcore(ctx) {
  const { config } = ctx;
  if (!env.bool('MESHCORE_ENABLED', false)) return;
  const profile = env.oneOf('MESHCORE_AGENT_TOOLS_PROFILE', PROFILES, 'minimal');
  const alsoAllow = env.list('MESHCORE_AGENT_ALSO_ALLOW', ['web_search']);
  // `off` keeps radio latency low (users wait on ~130-byte packets); the
  // reference host runs `high`.
  const thinking = env.oneOf('MESHCORE_AGENT_THINKING', THINKING, 'off');
  const radio = template('agents/meshcore-radio.md');

  const ensure = (id, seedFile, label) => {
    if (config.get(entry(id)) === undefined) config.set(entry(id), { name: id }, label);
    config.setDefault(entry(id, 'workspace'), ctx.ws(id));
    config.set(entry(id, 'tools', 'profile'), profile);
    config.addTo(entry(id, 'tools', 'alsoAllow'), alsoAllow);
    config.set(entry(id, 'thinkingDefault'), thinking);
    workspace(ctx.ws(id), template(seedFile, { RADIO_CONTRACT: radio }));
  };
  const dmId = env.str('MESHCORE_AGENT_ID', 'meshcore');
  ensure(dmId, 'agents/meshcore-dm.md', 'MeshCore private DM rail');
  const chanId = env.str('MESHCORE_CHANNEL_AGENT_ID');
  if (chanId && chanId !== dmId) ensure(chanId, 'agents/meshcore-channel.md', 'MeshCore shared channel');
}

// Valheim builder. DevBridge drives the owner's running Valheim client
// through a BepInEx plugin; its MCP server runs on the gaming PC and is
// reached over the LAN. Its ~58 `valheim__*` tools (several with in-game
// writes) go to a separate agent that answers only in the listed Discord
// channels (peer bindings win over the catch-all Discord binding), and are
// denied on every other agent so nobody else can drive the game.
function valheim(ctx) {
  const { config } = ctx;
  const id = env.str('VALHEIM_AGENT_ID', 'valheim');
  const isOurs = (b) => b?.type === 'route' && b?.agentId === id && b?.match?.channel === 'discord' && b?.match?.peer;
  const bindings = config.get('bindings') ?? [];
  const agents = Object.keys(config.get('agents.entries') ?? {});

  // `off` removes the server, our bindings and the deny glob; the agent entry
  // and its workspace stay (they hold the operator's build notes).
  if (env.raw('VALHEIM_MCP')?.toLowerCase() === 'off') {
    config.remove('mcp.servers.valheim', 'VALHEIM_MCP=off');
    if (bindings.some(isOurs)) config.set('bindings', bindings.filter((b) => !isOurs(b)), 'VALHEIM_MCP=off');
    for (const a of agents) config.removeFrom(entry(a, 'tools', 'deny'), [VALHEIM_GLOB], 'VALHEIM_MCP=off');
    return;
  }
  const url = env.str('VALHEIM_MCP_URL');
  const token = env.str('VALHEIM_MCP_TOKEN');
  if (!url || !token) return;

  // One call can wait up to 10 min for the owner to confirm a write in the
  // game, and calls queue behind each other, so the timeout sits above that.
  config.merge('mcp.servers.valheim', {
    transport: 'streamable-http',
    url,
    connectionTimeoutMs: 10000,
    requestTimeoutMs: env.int('VALHEIM_MCP_REQUEST_TIMEOUT_MS', 660000),
  });
  config.set('mcp.servers.valheim.headers.Authorization', `Bearer ${token}`);

  const extraDeny = env.str('VALHEIM_AGENT_TOOLS_DENY', 'valheim__csharp_run').toLowerCase() === 'none'
    ? [] : env.list('VALHEIM_AGENT_TOOLS_DENY', ['valheim__csharp_run']);
  if (config.get(entry(id)) === undefined) {
    config.set(entry(id), {
      name: id,
      workspace: ctx.ws(id),
      tools: { profile: 'minimal', alsoAllow: [VALHEIM_GLOB], deny: extraDeny },
      thinkingDefault: 'high',
    }, 'Valheim builder');
  }
  config.addTo(entry(id, 'tools', 'deny'), extraDeny);
  // With all ~58 DevBridge schemas the builder's prompt was ~81k tokens and
  // the local models narrated tool calls instead of making them. The listed
  // tools replace alsoAllow; the sub-agent tools let a design be built stage
  // by stage in small sessions. Unset = every valheim__ tool.
  const tools = env.list('VALHEIM_AGENT_TOOLS');
  if (tools.length) {
    const wanted = tools.map((t) => (t.startsWith('valheim__') ? t : `valheim__${t}`));
    config.set(entry(id, 'tools', 'alsoAllow'), [...new Set([...wanted, 'sessions_spawn', 'sessions_yield', 'subagents'])]);
  }
  // Full thinking spent ~57k reasoning tokens and a 40-minute turn on one dry
  // run; the dry-run verdicts already give the builder feedback. Unset =
  // leave the level as created.
  const thinking = env.oneOf('VALHEIM_AGENT_THINKING', THINKING);
  if (thinking) config.set(entry(id, 'thinkingDefault'), thinking);
  workspace(ctx.ws(id), template('agents/valheim.md'));

  const channels = env.ids('VALHEIM_AGENT_CHANNELS');
  const kept = bindings.filter((b) => !isOurs(b) || channels.includes(String(b.match.peer.id)));
  const added = channels
    .filter((c) => !kept.some((b) => isOurs(b) && String(b.match.peer.id) === c))
    .map((c) => ({ type: 'route', agentId: id, match: { channel: 'discord', peer: { kind: 'channel', id: c } } }));
  if (added.length || kept.length !== bindings.length) config.set('bindings', [...kept, ...added], 'VALHEIM_AGENT_CHANNELS');

  for (const a of agents) if (a !== id) config.addTo(entry(a, 'tools', 'deny'), [VALHEIM_GLOB], 'Valheim tools only for the builder');
}

// Deep agent: the Discord agent's sub-agent with full thinking. Nemotron's
// template has off / medium (the server default) / full thinking, and
// OpenClaw's /think is on/off only, so full thinking needs its own agent
// (per-agent chat_template_kwargs) that sessions_spawn can target. On two
// sets of 10 checkable reasoning questions full scored 40/40 against medium's
// 39/40 at 2-2.5x the time, so it is off by default. When on it shares the
// Discord workspace and mirrors the Discord agent's tools on every run.
function deep(ctx) {
  const { config } = ctx;
  const id = env.str('OPENCLAW_DISCORD_DEEP_AGENT_ID', 'deep');
  const entries = config.get('agents.entries') ?? {};
  const parent = Object.keys(entries).find((a) => entries[a]?.workspace === ctx.ws('discord'))
    ?? (entries['discord-friend'] ? 'discord-friend' : undefined);
  if (!parent || parent === id) return;
  const allow = entry(parent, 'subagents', 'allowAgents');

  if (!env.bool('OPENCLAW_DISCORD_DEEP_AGENT', false)) {
    // Off: nothing is written unless an earlier `on` left the agent behind.
    config.remove(entry(id), 'OPENCLAW_DISCORD_DEEP_AGENT=off');
    config.removeFrom(allow, [id], 'OPENCLAW_DISCORD_DEEP_AGENT=off');
    if (config.get(allow)?.length === 0) config.remove(allow);
    const sub = config.get(entry(parent, 'subagents'));
    if (sub && Object.keys(sub).length === 0) config.remove(entry(parent, 'subagents'));
    return;
  }
  const p = entries[parent];
  config.set(entry(id), {
    name: 'Deep',
    workspace: p.workspace,
    thinkingDefault: 'high',
    params: { chat_template_kwargs: { enable_thinking: true, medium_effort: false } },
    ...(p.tools ? { tools: p.tools } : {}),
  }, `full thinking, tools mirrored from ${parent}`);
  config.addTo(allow, [id]);
}

export default function agents(ctx) {
  discordAgent(ctx.config);
  meshcore(ctx);
  valheim(ctx);
  deep(ctx);
}
