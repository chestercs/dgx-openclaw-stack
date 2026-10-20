// config-init: brings openclaw.json and the agent workspaces to the state this
// stack needs, from .env. It runs in the openclaw-config-init container before
// the gateway on every `docker compose up`, and a re-run on an already patched
// config changes nothing.
//
// Contract:
//   - No openclaw.json yet (fresh install, before onboarding): exit 0.
//     Onboarding chooses the gateway token and pairs the UI; see SETUP.md.
//   - Writes the current OpenClaw schema (2026.10: keyed agents.entries,
//     memory.search, tts, tools.media.models, exec.mode).
//   - Never touches what the operator owns: their own agents, channel
//     credentials, text outside the marker blocks in AGENTS.md.
//   - Logs one `[patch-config]` line per change.
//
// Each area lives in steps/<area>.mjs and exports `apply(ctx)`. The order
// below matters only where a later area reads what an earlier one wrote
// (agents need the model id; plugins come last because they list what the
// other areas enabled).

import fs from 'node:fs';
import path from 'node:path';
import { Config } from './lib/config.mjs';
import models from './steps/models.mjs';
import memory from './steps/memory.mjs';
import gateway from './steps/gateway.mjs';
import tools from './steps/tools.mjs';
import discord from './steps/discord.mjs';
import agents from './steps/agents.mjs';
import workspaces from './steps/workspaces.mjs';
import media from './steps/media.mjs';
import plugins from './steps/plugins.mjs';

const STATE = process.env.OPENCLAW_STATE_DIR || '/home/node/.openclaw';
const CONFIG_PATH = path.join(STATE, 'openclaw.json');

if (!fs.existsSync(CONFIG_PATH)) {
  console.log(`[patch-config] ${CONFIG_PATH} does not exist yet (onboarding has not run); nothing to do.`);
  process.exit(0);
}

const config = new Config(JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8')));
if (config.get('agents.list') !== undefined) {
  throw new Error('openclaw.json still has the pre-2026.10 agents.list; run `openclaw doctor --fix` first.');
}

const ctx = { config, state: STATE, ws: (name = '') => path.join(STATE, name ? `workspace-${name}` : 'workspace') };
for (const step of [models, memory, gateway, tools, discord, agents, workspaces, media, plugins]) step(ctx);

if (config.changed) {
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(config.data, null, 2) + '\n');
  console.log('[patch-config] openclaw.json updated.');
} else {
  console.log('[patch-config] openclaw.json already in the desired state.');
}
