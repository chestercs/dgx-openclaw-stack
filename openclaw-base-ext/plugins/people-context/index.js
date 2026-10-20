// people-context — tells the discord-friend agent who is talking and who is
// tagged, next to each Discord turn.
//
// 1. Mentions. The Discord plugin turns `<@123…>` into `@Name` before any hook
//    or the model sees the message, so the model cannot tag that person back
//    and nothing downstream knows who was tagged. message_received carries
//    the message ID, so for a message with an `@` the plugin reads its
//    mention list from the Discord API (bot token, one GET) and the turn gets
//    a line like "Tagged: @Name = <@ID>".
// 2. People memory. people/MEMORY.md is in the system prompt already, but the
//    model rarely links "mit csinálok most?" to the sender's section there.
//    before_prompt_build adds the sender's lines and those of tagged people.
// A before_tool_call hook refuses direct edits of the file (see below). No
// reply hooks: `reply_payload_sending` / `message_sending` would turn off
// Discord preview streaming for every turn.
import fs from 'node:fs';
import path from 'node:path';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';

const AGENT_ID = process.env.PEOPLE_CONTEXT_AGENT || 'discord-friend';
const PEOPLE_FILE = 'people/MEMORY.md';
const MAX_TAGGED = 4;
const API = 'https://discord.com/api/v10';
const MENTION_WAIT_MS = 3000;

const discordId = (v) => String(v ?? '').match(/\d{15,22}/)?.[0] ?? '';
const lastId = (v) => [...String(v ?? '').matchAll(/\d{15,22}/g)].at(-1)?.[0] ?? '';

// The bot token as the runtime resolved it (a plain string); the env value
// when the config holds a secret reference instead.
function botToken(cfg) {
  const t = cfg?.channels?.discord?.token;
  if (typeof t === 'string' && t && !t.startsWith('${')) return t;
  return process.env.DISCORD_BOT_TOKEN || null;
}

// The mentions of one message: [{ id, label }], label as the reader saw it.
async function fetchMentions(token, channelId, messageId) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), MENTION_WAIT_MS);
  try {
    const res = await fetch(`${API}/channels/${channelId}/messages/${messageId}`, {
      headers: { Authorization: `Bot ${token}` }, signal: ac.signal,
    });
    if (!res.ok) return [];
    const msg = await res.json();
    return (msg.mentions ?? []).map((u) => ({ id: u.id, label: u.member?.nick || u.global_name || u.username }));
  } catch {
    return [];
  } finally {
    clearTimeout(timer);
  }
}

// Map of Discord ID → { head, lines } for every `## Name (<@ID>)` section.
function readSections(file) {
  let text;
  try { text = fs.readFileSync(file, 'utf8'); } catch { return null; }
  const out = new Map();
  let cur = null;
  for (const line of text.split('\n')) {
    if (line.startsWith('## ')) {
      const id = line.match(/<@!?(\d+)>/)?.[1];
      cur = id ? { head: line.replace(/^##\s+/, '').trim(), lines: [] } : null;
      if (id) out.set(id, cur);
    } else if (cur && line.trim().startsWith('- ')) {
      cur.lines.push(line.trim());
    }
  }
  return out;
}

export default definePluginEntry({
  id: 'people-context',
  name: 'People Context',
  description: 'Prepends the Discord sender\'s people/MEMORY.md section to each turn.',
  register(api) {
    const token = botToken(api.config);
    // sessionKey → promise of the latest message's mentions. A turn waits for
    // it (at most MENTION_WAIT_MS); a later message replaces it.
    const pending = new Map();

    api.on('message_received', (event) => {
      const key = event?.sessionKey;
      if (!token || !key || !String(key).includes(`agent:${AGENT_ID}:discord:`)) return;
      if (!String(event.content ?? '').includes('@')) { pending.delete(key); return; }
      const channelId = lastId(event.metadata?.originatingTo) || lastId(key);
      const messageId = discordId(event.messageId);
      if (!channelId || !messageId) return;
      pending.set(key, fetchMentions(token, channelId, messageId));
    });

    api.on('before_prompt_build', async (event, ctx) => {
      if (ctx?.agentId !== AGENT_ID) return;
      const sender = discordId(ctx.senderId);
      const mentions = ((await pending.get(ctx.sessionKey)) ?? []).filter((m) => m.id !== sender);
      pending.delete(ctx.sessionKey);
      const parts = [];
      if (mentions.length) {
        parts.push(`[Tagged in this message — to tag someone back, write the <@ID> form] ${mentions.map((m) => `@${m.label} = <@${m.id}>`).join(', ')}`);
      }
      const sections = sender && ctx.workspaceDir ? readSections(path.join(ctx.workspaceDir, PEOPLE_FILE)) : null;
      if (!sections) return parts.length ? { prependContext: parts.join('\n') } : undefined;

      const own = sections.get(sender);
      parts.push('[People memory — the relevant lines of people/MEMORY.md]');
      if (own && own.lines.length) {
        parts.push(`Sender: ${own.head}`, ...own.lines);
        parts.push('If the sender asks about THEMSELVES (what they are doing, where they are, what you know about them), answer from these lines; ' +
          'the "ideiglenes" line is their current state. The question is about them, not about earlier tasks in the channel.');
      } else {
        parts.push(`No facts saved about the sender (<@${sender}>) yet — if they ask about themselves, say you know nothing about them yet.`);
      }
      const tagged = mentions.map((m) => m.id)
        .filter((id) => sections.get(id)?.lines.length)
        .slice(0, MAX_TAGGED);
      for (const id of tagged) parts.push(`Mentioned: ${sections.get(id).head}`, ...sections.get(id).lines);
      return { prependContext: parts.join('\n') };
    });

    // Gemma's own edit/write on the shared file once replaced every section,
    // and it still reached for `edit` with the router saying otherwise. The
    // guarded writer is /opt/people-digest.mjs note|forget (exec), so direct
    // file tools on people/MEMORY.md are refused with the right command.
    // The nightly job writes people/MEMORY.next.md, which stays allowed.
    api.on('before_tool_call', (event, ctx) => {
      if (ctx?.agentId !== AGENT_ID) return;
      const p = event?.params ?? {};
      const targets = [p.path, p.file_path, p.filePath, ...(Array.isArray(event?.derivedPaths) ? event.derivedPaths : [])];
      if (event?.toolName === 'apply_patch' && typeof p.input === 'string' && p.input.includes(PEOPLE_FILE)) targets.push(PEOPLE_FILE);
      if (!targets.some((t) => typeof t === 'string' && t.replace(/\\/g, '/').endsWith(PEOPLE_FILE))) return;
      return {
        block: true,
        blockReason: 'people/MEMORY.md cannot be edited directly. Save: exec `node /opt/people-digest.mjs note --id <ID> --name "<name>" --text "<fact>"`; ' +
          'correct: the same with `--replace "<old fragment>"`; delete: `node /opt/people-digest.mjs forget --id <ID> --match "<fragment>"`.',
      };
    }, { matcher: ['edit', 'write', 'apply_patch'] });
  },
});
