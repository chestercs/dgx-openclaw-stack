// Plugins and internal hooks. Runs last because it follows what the other
// areas switched on (Discord, the Valheim server).
//
// The local plugins ship in the openclaw-base-ext image under
// /opt/openclaw-plugins. None registers a reply hook: any
// `reply_payload_sending` / `message_sending` hook turns off Discord preview
// streaming for every Discord turn.

import fs from 'node:fs';
import path from 'node:path';
import * as env from '../lib/env.mjs';
import { discordAgentIds } from './agents.mjs';
import { ownerIds, ownerOnlyTools } from './discord.mjs';

const PLUGIN_ROOT = '/opt/openclaw-plugins';
const PEOPLE_MEMORY = 'people/MEMORY.md';

export default function plugins({ config, ws }) {
  const discord = config.get('channels.discord') !== undefined;

  // on: true = load and enable, false = remove load path and entry,
  // undefined = leave as it is. A plugin missing from the image is removed:
  // a load path that does not exist fails the gateway's plugin scan.
  // Non-bundled plugins need hooks.allowConversationAccess for the
  // conversation hooks (before_prompt_build, before_agent_finalize, ...);
  // before_dispatch, message_received and the tool hooks need no grant.
  const ensureLocalPlugin = (dir, id, { on, config: pluginConfig, conversationAccess = false } = {}) => {
    const path = `${PLUGIN_ROOT}/${dir}`;
    const p = ['plugins', 'entries', id];
    if (on === true && !fs.existsSync(path)) on = false;
    if (on === false) {
      config.removeFrom('plugins.load.paths', [path], `${id} off`);
      config.remove(p, `${id} off`);
      return;
    }
    if (on !== true) return;
    config.addTo('plugins.load.paths', [path]);
    config.set([...p, 'enabled'], true);
    if (conversationAccess) config.set([...p, 'hooks', 'allowConversationAccess'], true);
    for (const [k, v] of Object.entries(pluginConfig ?? {})) {
      if (Array.isArray(v)) config.addTo([...p, 'config', k], v); else config.set([...p, 'config', k], v);
    }
  };
  // Discord-only plugins: on/off from their knob, but only wired while the
  // Discord channel is configured.
  const discordGate = (knob) => (!env.bool(knob, true) ? false : discord ? true : undefined);
  // The agents a Discord plugin serves: the ones the catch-all Discord
  // binding routes to, so no agent id is hard-coded in a plugin.
  const servesDiscordAgents = (id) => {
    if (config.get(['plugins', 'entries', id, 'enabled'])) config.set(['plugins', 'entries', id, 'config', 'agents'], discordAgentIds(config));
  };

  // Discord people memory (people/MEMORY.md, seeded by the workspaces step).
  // OpenClaw drops the root MEMORY.md from group/channel sessions, so in a
  // guild the bot had no long-term memory. bootstrap-extra-files injects after
  // that filter, which only matches the root path, so the file reaches
  // channel, group and DM sessions. `paths` wins over the hook's
  // patterns/files, so we append to an operator list instead of replacing it.
  const peopleOn = discordGate('OPENCLAW_DISCORD_PEOPLE_MEMORY');
  const hook = ['hooks', 'internal', 'entries', 'bootstrap-extra-files'];
  if (peopleOn === false) {
    config.removeFrom([...hook, 'paths'], [PEOPLE_MEMORY], 'OPENCLAW_DISCORD_PEOPLE_MEMORY=off');
  } else if (peopleOn) {
    config.set([...hook, 'enabled'], true);
    config.addTo([...hook, 'paths'], [PEOPLE_MEMORY]);
  }

  // session-memory (on after onboarding) dumps the last messages of a
  // session into memory/<date>.md on every /new or reset. The dump keeps the
  // bot's own wrong conclusions, and memory search and Active Memory then
  // bring them back as facts ("ruinbot posts are deleted after 30 s", which
  // the bot repeated a day later). Memory comes from deliberate writes only:
  // people-digest note for people, memory/<topic>.md for what was asked.
  config.set(['hooks', 'internal', 'entries', 'session-memory', 'enabled'], env.bool('OPENCLAW_SESSION_MEMORY', false));

  // people-context: the file sits in the system prompt, yet the model answered
  // "what am I doing now?" from the channel's recent history instead of the
  // sender's section. before_prompt_build prepends the sender's own lines (and
  // those of people tagged, read from message_received) to each turn.
  ensureLocalPlugin('people-context', 'people-context', { on: peopleOn, conversationAccess: true });
  servesDiscordAgents('people-context');

  // claim-guard: prompt rules did not stop "I saved it" in turns with no write
  // call. before_agent_finalize has the host model list the actions the answer
  // claims, checks them against the run's successful tool calls and asks for
  // one more pass on a mismatch. Audit: logs/claim-guard.jsonl.
  ensureLocalPlugin('claim-guard', 'claim-guard', { on: discordGate('OPENCLAW_DISCORD_CLAIM_GUARD'), conversationAccess: true });
  servesDiscordAgents('claim-guard');
  // The source check (facts against the tool results) is off by default: on
  // the eval it cost +15 s median and +100 s p90 for no gain in passes.
  if (config.get('plugins.entries.claim-guard.enabled')) {
    if (env.bool('OPENCLAW_CLAIM_GUARD_SOURCE_CHECK', false)) config.set('plugins.entries.claim-guard.config.sourceCheck', true);
    else config.remove('plugins.entries.claim-guard.config.sourceCheck', 'OPENCLAW_CLAIM_GUARD_SOURCE_CHECK=off');
  }

  // harness-guard: policy checks outside the model. Owner-only tools in DMs,
  // where Discord has no per-sender tool policy (guilds: steps/discord.mjs);
  // a source line for every new fact in memory/*.md; a "Done when:" line in
  // every sub-agent task. Without owners nobody is an owner, so no tool is
  // owner-only.
  const owners = ownerIds();
  ensureLocalPlugin('harness-guard', 'harness-guard', { on: discordGate('OPENCLAW_HARNESS_GUARD') });
  if (config.get('plugins.entries.harness-guard.enabled')) {
    config.set('plugins.entries.harness-guard.config', {
      agents: discordAgentIds(config), owners, ownerOnlyTools: owners.length ? ownerOnlyTools() : [],
    });
  }

  // dm-guild-gate: DMs are open (dmPolicy=open) so members need no pairing,
  // and dmPolicy has no "members of my servers" mode. before_dispatch asks
  // Discord whether the sender shares a guild with the bot and ends a
  // stranger's turn silently. Its config (guilds, allow) is the operator's.
  ensureLocalPlugin('dm-guild-gate', 'dm-guild-gate', { on: discordGate('OPENCLAW_DISCORD_DM_GUILD_GATE') });

  // mcp-image-saver: DevBridge screenshots come back as image blocks that the
  // model sees but Discord cannot attach. This tool-result middleware saves
  // them into the canvas and adds a `[saved: <path>]` line for a MEDIA reply.
  // Follows the Valheim server (steps/agents.mjs).
  const valheimOff = env.raw('VALHEIM_MCP')?.toLowerCase() === 'off';
  const valheimOn = !valheimOff && Boolean(env.str('VALHEIM_MCP_URL') && env.str('VALHEIM_MCP_TOKEN'));
  ensureLocalPlugin('mcp-image-saver', 'mcp-image-saver', {
    on: valheimOff ? false : valheimOn ? true : undefined,
    config: { prefixes: ['valheim__'] },
  });

  // Workboard: card board for multi-hour work (/workboard create|list|show|
  // dispatch). `off` removes the entry, unset leaves it alone. Its ~40 tools
  // beyond a five-tool core are denied on the Discord agent (steps/agents.mjs).
  const workboard = env.bool('OPENCLAW_WORKBOARD');
  if (workboard === true) config.set('plugins.entries.workboard.enabled', true, '/workboard slash surface');
  else if (workboard === false) config.remove('plugins.entries.workboard', 'OPENCLAW_WORKBOARD=off');

  discordSkills(config, ws);
}

// The Discord agents see only the skills written into their workspace (the
// ones the router points at) plus OPENCLAW_DISCORD_AGENT_SKILLS_EXTRA. Bundled
// skills (notion, obsidian, node debugging, ...) are no use on Discord and
// were 7 KB of every prompt; one of them, `weather`, duplicated
// weather-forecast. Runs here, last, because earlier steps write the skills.
// `off` lifts the filter.
function discordSkills(config, ws) {
  const extra = env.raw('OPENCLAW_DISCORD_AGENT_SKILLS_EXTRA');
  const dir = path.join(ws('discord'), 'skills');
  for (const id of discordAgentIds(config)) {
    const p = ['agents', 'entries', id, 'skills'];
    if (extra?.toLowerCase() === 'off' || !fs.existsSync(dir)) { config.remove(p, 'skill filter off'); continue; }
    const own = fs.readdirSync(dir).filter((n) => fs.existsSync(path.join(dir, n, 'SKILL.md'))).sort();
    config.set(p, [...new Set([...own, ...env.list('OPENCLAW_DISCORD_AGENT_SKILLS_EXTRA')])]);
  }
}
