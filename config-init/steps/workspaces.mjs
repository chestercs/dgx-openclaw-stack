// Prompt text in the agent workspaces: marker blocks in AGENTS.md (shared
// with the operator) and skill files (wholly patcher-owned). The text itself
// lives in config-init/templates/; this module decides what goes where.
//
// AGENTS.md is injected into every session, so it holds only what must be
// seen at decision time (policy, persona, the skill router). Tool recipes are
// skills: one line in the prompt, the body read on demand. That kept the
// Discord bootstrap at ~29 KB instead of ~53 KB.
//
// Media recipes (image / video / music) belong to steps/media.mjs.

import fs from 'node:fs';
import path from 'node:path';
import * as env from '../lib/env.mjs';
import { template, upsertBlock, skill, removeSkill, writeIfChanged } from '../lib/files.mjs';
import { mediaOn } from './media.mjs';

const DESC = {
  'browser-automation': 'Open a web page, take a screenshot, read an article, click/fill forms with the browser tool — open/snapshot/screenshot/act actions and the mandatory parameter shapes (fill = fields array!).',
  'cron-reminders': 'Reminders / scheduled messages with the automations tool (old name cron) — for "emlékeztess", "X múlva szólj", "minden reggel küldj" requests. NOT for looking up future info (weather, results).',
  'media-downloads': 'Media download and processing — YouTube/audio download with yt-dlp, transcription ("írd le / feliratozd"), uploading a file as a Discord attachment, finding and saving an image.',
  'weather-forecast': 'Weather forecast via the open-meteo API ("milyen idő lesz", "hány fok lesz X napon") — an immediate lookup, NEVER a cron job/reminder.',
  'coding-projects': 'Coding and projects — creating scripts/apps in the sandbox, web-app build + hosting (Angular/React), git_push to GitHub, using the exec shell. Triggers: "írj scriptet/programot/appot", "pushold", "hostold".',
  'long-task-workboard': 'Tracking multi-hour / day-long work on a workboard card — create, dispatch, list, read; worker runs + status checks. Triggers: a long task, "dolgozz rajta egy napig", "kövessük a státuszt".',
  'people-memory': 'Nightly persona update: lasting personal facts from the guild\'s messages into people/MEMORY.md (run by the people-memory-nightly job).',
  'community-summary': 'Community summary of a few days of the guilds\' messages into memory/community/ (searchable; run by the community-summary job or a backfill).',
};
const put = (ws, name, vars) => skill(ws, name, DESC[name], template(`skills/${name}.md`, vars));
const block = (file, id, vars) => upsertBlock(file, id, template(`discord/AGENTS.d/${id}.md`, vars));

// Names in the Discord prompt come from .env so the public repo carries no
// member names or IDs. The examples need four names; missing ones are filled
// with neutral placeholders.
function peopleVars() {
  const members = env.list('OPENCLAW_DISCORD_MEMBER_NAMES');
  const adminName = env.str('OPENCLAW_DISCORD_ADMIN_NAME');
  const admin = [adminName, env.str('OPENCLAW_DISCORD_ADMIN_ID')].filter(Boolean);
  const [a, b, c, d] = [...members, 'Anna', 'Bálint', 'Csaba', 'Dani'].slice(0, 4);
  return {
    EVERYONE: members.length ? `${members.join(', ')} and anyone else get` : 'Everyone gets',
    ADMIN_REF: admin.length ? ` (${admin.join(' / ')})` : '',
    ADMIN_NAME: adminName ?? 'Admin',
    NAME_A: a, NAME_B: b, NAME_C: c, NAME_D: d,
    // The example quotes a Hungarian chat line ("why don't you answer X"),
    // which puts the name in the dative.
    NAME_D_DATIVE: `${d.toLowerCase()}nak`,
  };
}

// The router lists only the media tools that run; an "off" line tells the
// bot to say so instead of reaching for another tool.
function routerVars() {
  const vars = {};
  for (const g of ['image', 'video', 'music']) {
    const on = mediaOn(g);
    vars[`${g.toUpperCase()}_TOOLS`] = on ? template(`discord/router/${g}-tools.md`) : '';
    vars[`${g.toUpperCase()}_ROUTE`] = template(`discord/router/${g}-${on ? 'on' : 'off'}.md`);
  }
  return vars;
}

function discordAgentsMd(config, ws) {
  const md = path.join(ws, 'AGENTS.md');
  if (!fs.existsSync(md)) return; // no Discord-routed agent onboarded yet

  const people = peopleVars();
  block(md, 'discord-format-rules', people);
  block(md, 'discord-deep-agentic');
  block(md, 'discord-honesty');
  block(md, 'discord-sender-identity', people);
  // The delegation recipe names the full-thinking deep agent only when the
  // agents step creates it.
  const deepId = env.bool('OPENCLAW_DISCORD_DEEP_AGENT', false) ? env.str('OPENCLAW_DISCORD_DEEP_AGENT_ID', 'deep') : undefined;
  upsertBlock(md, 'discord-subagent-delegation',
    template('discord/AGENTS.d/discord-subagent-delegation.md') +
    (deepId ? template('discord/AGENTS.d/discord-subagent-delegation.deep.md', { DEEP_AGENT_ID: deepId }) : ''));
  block(md, 'discord-tool-policy-core');
  block(md, 'discord-thread-tasks');
  block(md, 'discord-skills-discoverability', routerVars());

  for (const name of ['cron-reminders', 'browser-automation', 'media-downloads', 'weather-forecast']) put(ws, name);
  // Where web apps built in the sandbox are reachable (the sandbox's preview
  // port, usually behind a reverse proxy).
  put(ws, 'coding-projects', { SANDBOX_PUBLIC_URL: env.str('OPENCLAW_SANDBOX_PUBLIC_URL', 'http://localhost:8095').replace(/\/$/, '') });
  // Points at workboard tools that exist only with the plugin enabled.
  if (env.bool('OPENCLAW_WORKBOARD', false)) put(ws, 'long-task-workboard');
  else removeSkill(ws, 'long-task-workboard');

  // Past bootstrapMaxChars the runtime silently truncates the file, and the
  // rules at its end vanish without an error.
  const cap = config.get('agents.defaults.bootstrapMaxChars') ?? 20000;
  const size = Buffer.byteLength(fs.readFileSync(md, 'utf8'));
  if (size > cap) console.warn(`[patch-config] WARNING: ${md} is ${size} bytes, over agents.defaults.bootstrapMaxChars=${cap}; the end of it is cut off.`);
}

// Durable facts about guild members in people/MEMORY.md, which the
// bootstrap-extra-files hook loads into every Discord session (the root
// MEMORY.md never reaches channel sessions). The file is bot-owned: seeded
// once, then written only through /opt/people-digest.mjs.
function peopleMemory(config, ws) {
  if (!config.get('channels.discord') || !fs.existsSync(ws)) return;
  if (!env.bool('OPENCLAW_DISCORD_PEOPLE_MEMORY', true)) {
    removeSkill(ws, 'people-memory');
    return;
  }
  const file = path.join(ws, 'people', 'MEMORY.md');
  if (!fs.existsSync(file)) writeIfChanged(file, template('discord/people-MEMORY.seed.md'));

  // NSFW channels are off by default: the file reaches every channel's
  // prompt, so whatever is learnt there surfaces everywhere.
  const exclude = env.list('OPENCLAW_PEOPLE_DIGEST_EXCLUDE').filter((s) => /^[\p{L}\p{N}_-]+$/u.test(s));
  const flags = (env.bool('OPENCLAW_PEOPLE_DIGEST_NSFW', false) ? ' --nsfw' : '') +
    (exclude.length ? ` --exclude ${exclude.join(',')}` : '');
  // Recipes for the two cron jobs; the job messages only point at them.
  put(ws, 'people-memory', { DIGEST_FLAGS: flags });
  if (env.bool('OPENCLAW_COMMUNITY_SUMMARY', true)) put(ws, 'community-summary', { DIGEST_FLAGS: flags });
  else removeSkill(ws, 'community-summary');
}

export default function workspaces({ config, state, ws }) {
  // The gateway serves this directory under /__openclaw__/canvas/; the
  // ComfyUI bridge and the sandbox drop files there, and fail on a missing
  // dir. Not fatal: a bind-mount permission problem surfaces in the bridge.
  const canvas = path.join(state, 'canvas');
  try {
    if (!fs.existsSync(canvas)) fs.mkdirSync(canvas, { recursive: true, mode: 0o755 });
  } catch (err) {
    console.warn(`[patch-config] could not create ${canvas}: ${err.message}`);
  }

  // Soft layer only: credentialed browser profiles are opt-in. A determined
  // prompt injection can talk past it.
  const mainMd = path.join(ws(), 'AGENTS.md');
  if (fs.existsSync(mainMd)) upsertBlock(mainMd, 'browser-policy', template('main/AGENTS.d/browser-policy.md'));
  if (fs.existsSync(ws())) put(ws(), 'browser-automation');

  discordAgentsMd(config, ws('discord'));
  peopleMemory(config, ws('discord'));
}
