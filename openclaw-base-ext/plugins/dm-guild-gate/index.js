// dm-guild-gate — the bot answers a Discord DM only when the sender shares a
// server (guild) with it. Strangers who find the bot get no reply at all.
//
// The Discord DM gate (channels.discord.dmPolicy) has four modes: pairing,
// allowlist, open, disabled. None of them means "members of my servers":
// `open` lets anyone in (and step 28 opens it so guild members can DM the
// bot without an approval round), `allowlist` needs every member's ID
// written into the config by hand. So this plugin decides per message.
//
// before_dispatch is the supported hook for it: it runs before model
// dispatch, and returning { handled: true } without text ends the turn with
// nothing sent and no model call. It is not a reply hook, so Discord preview
// streaming stays on (registering message_sending / reply_payload_sending
// would turn it off for every turn).
//
// Membership is checked with the bot's own token on the Discord REST API:
// GET /guilds/{guild}/members/{user} is 200 for a member and 404 "Unknown
// Member" (code 10007) otherwise (checked live 2026-10-20); it needs no
// privileged intent. Any other answer counts as "unclear". Results are
// cached (members for 6 h, non-members for 10 min, so someone who just
// joined gets through soon). If Discord cannot be asked (no token, network
// error, rate limit), the message is let through and the error is logged:
// a transient outage should not silence the bot for its own members.
//
// Each decision gets one line in logs/dm-guild-gate.jsonl: time, sender ID,
// outcome, how it was decided. Never message text.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';

const API = 'https://discord.com/api/v10';
const LOG_FILE = process.env.DM_GUILD_GATE_LOG
  || path.join(process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw'), 'logs', 'dm-guild-gate.jsonl');
const MEMBER_TTL_MS = 6 * 3600e3;
const STRANGER_TTL_MS = 10 * 60e3;
const GUILDS_TTL_MS = 30 * 60e3;
const FETCH_TIMEOUT_MS = 8000;

const discordId = (v) => String(v ?? '').match(/\d{15,22}/)?.[0] ?? '';
const idList = (v) => (Array.isArray(v) ? v : String(v ?? '').split(','))
  .map(discordId).filter(Boolean);

function log(rec) {
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, JSON.stringify({ ts: new Date().toISOString(), ...rec }) + '\n');
  } catch { /* logging must never break a turn */ }
}

// A Discord DM: the inbound hook context says not-a-group. The session key
// is the fallback signal (`…:discord:direct:<id>` with dmScope per-peer).
const isDiscordDm = (event) => event?.channel === 'discord'
  && (event.isGroup === false || (event.isGroup === undefined && /:discord:(direct|dm):/.test(String(event.sessionKey ?? ''))));

export function createGate({ token, guildIds = [], allow = [], block = [], fetchImpl = fetch, now = () => Date.now() }) {
  const cache = new Map(); // userId → { member, until }
  let guilds = { ids: [], until: 0 };

  async function get(url) {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), FETCH_TIMEOUT_MS);
    try {
      return await fetchImpl(url, { headers: { Authorization: `Bot ${token}` }, signal: ac.signal });
    } finally {
      clearTimeout(t);
    }
  }

  async function botGuilds() {
    if (guildIds.length) return guildIds;
    if (guilds.until > now()) return guilds.ids;
    const res = await get(`${API}/users/@me/guilds?limit=200`);
    if (!res.ok) throw new Error(`guild list HTTP ${res.status}`);
    const ids = (await res.json()).map((g) => g.id);
    guilds = { ids, until: now() + GUILDS_TTL_MS };
    return ids;
  }

  // → { member: true|false, via } or throws when Discord gave no clear answer.
  async function isMember(userId) {
    if (block.includes(userId)) return { member: false, via: 'block' };
    if (allow.includes(userId)) return { member: true, via: 'allow' };
    const hit = cache.get(userId);
    if (hit && hit.until > now()) return { member: hit.member, via: 'cache' };
    if (!token) throw new Error('no bot token');
    let unclear = null;
    for (const g of await botGuilds()) {
      const res = await get(`${API}/guilds/${g}/members/${userId}`);
      if (res.ok) {
        cache.set(userId, { member: true, until: now() + MEMBER_TTL_MS });
        return { member: true, via: 'discord' };
      }
      const body = await res.json().catch(() => ({}));
      // 10007 Unknown Member: not in this guild. 10013 Unknown User: no such account.
      if (res.status === 404 && (body?.code === 10007 || body?.code === 10013)) continue;
      unclear = `guild ${g}: HTTP ${res.status}${body?.code ? ` code ${body.code}` : ''}`;
    }
    if (unclear) throw new Error(unclear);
    cache.set(userId, { member: false, until: now() + STRANGER_TTL_MS });
    return { member: false, via: 'discord' };
  }

  // The before_dispatch handler: undefined lets the message through.
  return async function check(event) {
    if (!isDiscordDm(event)) return undefined;
    const sender = discordId(event.senderId);
    if (!sender) return undefined;
    try {
      const { member, via } = await isMember(sender);
      log({ sender, outcome: member ? 'allow' : 'block', via });
      return member ? undefined : { handled: true };
    } catch (err) {
      log({ sender, outcome: 'error_allow', error: String(err?.message ?? err).slice(0, 120) });
      return undefined;
    }
  };
}

// The bot token as the runtime resolved it (a plain string); the env value
// when the config holds a secret reference instead. With neither, every DM
// is let through (logged).
function botToken(cfg) {
  const t = cfg?.channels?.discord?.token;
  if (typeof t === 'string' && t && !t.startsWith('${')) return t;
  return process.env.DISCORD_BOT_TOKEN || null;
}

export default definePluginEntry({
  id: 'dm-guild-gate',
  name: 'DM Guild Gate',
  description: 'Answers a Discord DM only when the sender shares a server with the bot.',
  register(api) {
    const pc = api.pluginConfig ?? {};
    const check = createGate({
      token: botToken(api.config),
      guildIds: idList(pc.guilds),
      allow: idList(pc.allow),
      block: idList(pc.block),
    });
    api.on('before_dispatch', (event) => check(event));
  },
});
