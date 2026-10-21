// people-context — tells the Discord agent who is talking and who is tagged,
// next to each Discord turn. config.agents names the agents it serves
// (config-init writes the Discord-routed ones).
//
// 1. Mentions. The Discord plugin turns `<@123…>` into `@Name` before any hook
//    or the model sees the message, so the model cannot tag that person back
//    and nothing downstream knows who was tagged. message_received carries
//    the message ID, so for a message with an `@` the plugin reads its
//    mention list from the Discord API (bot token, one GET) and the turn gets
//    a line like "Tagged: @Name = <@ID>". The sender's own <@ID> is added
//    too: untagged, the model made one up when asked to tag them back.
// 2. People memory. people/MEMORY.md is in the system prompt already, but the
//    model rarely links "mit csinálok most?" to the sender's section there.
//    before_prompt_build adds the sender's lines and those of tagged people.
// 3. Tools. people_note, people_forget and discord_history wrap
//    /opt/people-digest.mjs (the guarded people-file writer and the channel
//    reader) as tools with their own parameters and error text, so the agent
//    needs no shell for them and exec can stay owner-only. A note or forget
//    about someone other than the sender is refused unless the owner asks:
//    it changes what the bot says about another person.
// A before_tool_call hook refuses direct edits of the file (see below). No
// reply hooks: `reply_payload_sending` / `message_sending` would turn off
// Discord preview streaming for every turn.
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';
import { textResult } from 'openclaw/plugin-sdk/tool-results';

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

const DIGEST = '/opt/people-digest.mjs';
const TOOL_NAMES = ['discord_history', 'people_note', 'people_forget'];

function digest(args, timeoutMs = 90_000) {
  return new Promise((resolve) => {
    execFile(process.execPath, [DIGEST, ...args], { timeout: timeoutMs, maxBuffer: 4 << 20 }, (err, stdout, stderr) => {
      const out = String(stdout || '').trim();
      if (!err) return resolve(textResult(out || 'ok', { ok: true }));
      const why = String(stderr || err.message).trim().split('\n').slice(-2).join(' ');
      resolve(textResult(`FAILED: ${why}${out ? `\n${out}` : ''}`, { ok: false }));
    });
  });
}

const refuse = (text) => Promise.resolve(textResult(`REFUSED: ${text}`, { ok: false }));

function peopleTools(ctx) {
  const sender = discordId(ctx?.requesterSenderId);
  // Only the person themself, or the owner, may change what is stored about
  // someone. No sender (CLI, scheduled job) is the operator.
  const otherPerson = (id) => Boolean(sender) && ctx?.senderIsOwner !== true && id !== sender;
  return [
    {
      name: 'discord_history',
      label: 'Discord channel history',
      description: 'Read a Discord channel\'s history, newest first: one line per message (time, author, text including embeds, and who ran a slash command such as [/ruin — Name]), with a header that says which range was scanned. ' +
        'Search the channel where the thing happened, not necessarily the one you are asked in: a game or log bot posts in its own channel. `grep` matches the author\'s name and the text. ' +
        'On 0 matches, say the scanned range from the header, or page back with `before`.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['channel'],
        properties: {
          channel: { type: 'string', description: 'Channel ID (digits).' },
          author: { type: 'string', description: 'Only messages by this name or user ID.' },
          grep: { type: 'string', description: 'Only messages whose author or text contains this.' },
          limit: { type: 'integer', minimum: 1, maximum: 100, description: 'Matches to return (default 20).' },
          days: { type: 'integer', minimum: 1, maximum: 365, description: 'Stop after this many days back.' },
          before: { type: 'string', description: 'Message ID to continue before (from the header of an earlier call).' },
        },
      },
      execute: async (_id, p = {}) => {
        const channel = discordId(p.channel);
        if (!channel) return refuse('channel must be a channel ID (digits).');
        const args = ['history', '--channel', channel];
        for (const k of ['author', 'grep', 'limit', 'days', 'before']) if (p[k] !== undefined && p[k] !== '') args.push(`--${k}`, String(p[k]));
        return digest(args);
      },
    },
    {
      name: 'people_note',
      label: 'Save a fact about a person',
      description: 'Save ONE fact about a person to people/MEMORY.md — the only way to write it. One call = one fact, one short Hungarian sentence. ' +
        'Only what the person said or a tool showed; never your own guess. A temporary state ("épp vonaton ülök") is kind=temp; ' +
        'to correct an old fact pass a fragment of it as `replace`. Facts about someone other than the sender are refused unless the owner asks.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'name', 'text'],
        properties: {
          id: { type: 'string', description: 'The person\'s Discord user ID (digits).' },
          name: { type: 'string', description: 'The person\'s display name.' },
          text: { type: 'string', description: 'The fact, one short sentence in Hungarian.' },
          kind: { type: 'string', enum: ['durable', 'temp'], description: 'temp = a current state that replaces the previous one.' },
          replace: { type: 'string', description: 'A fragment of the old fact this one corrects.' },
        },
      },
      execute: async (_id, p = {}) => {
        const id = discordId(p.id);
        if (!id) return refuse('id must be the person\'s Discord user ID (digits).');
        if (otherPerson(id)) return refuse('only the person themself or the owner can change what is stored about them. Tell the user so.');
        const args = ['note', '--id', id, '--name', String(p.name ?? ''), '--text', String(p.text ?? '')];
        if (p.kind === 'temp') args.push('--temp');
        if (p.kind === 'durable') args.push('--durable');
        if (p.replace) args.push('--replace', String(p.replace));
        return digest(args);
      },
    },
    {
      name: 'people_forget',
      label: 'Forget a fact about a person',
      description: 'Remove facts about a person from people/MEMORY.md: the lines containing `match`, or all of them with all=true. ' +
        'Refused for someone other than the sender unless the owner asks.',
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['id'],
        properties: {
          id: { type: 'string', description: 'The person\'s Discord user ID (digits).' },
          match: { type: 'string', description: 'A fragment of the line(s) to remove.' },
          all: { type: 'boolean', description: 'Remove everything stored about the person.' },
        },
      },
      execute: async (_id, p = {}) => {
        const id = discordId(p.id);
        if (!id) return refuse('id must be the person\'s Discord user ID (digits).');
        if (otherPerson(id)) return refuse('only the person themself or the owner can change what is stored about them. Tell the user so.');
        if (!p.match && p.all !== true) return refuse('give `match` (a fragment of the line) or all=true.');
        return digest(['forget', '--id', id, ...(p.all === true ? ['--all'] : ['--match', String(p.match)])]);
      },
    },
  ];
}

// The bot's own user ID: tagging the bot is how people talk to it, so its
// own mention is left out of the "Tagged" line.
let selfId;
async function botUserId(token) {
  if (selfId !== undefined) return selfId;
  try {
    const res = await fetch(`${API}/users/@me`, { headers: { Authorization: `Bot ${token}` } });
    selfId = res.ok ? (await res.json()).id : null;
  } catch {
    selfId = null;
  }
  return selfId;
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
    const self = await botUserId(token);
    return (msg.mentions ?? []).filter((u) => u.id !== self)
      .map((u) => ({ id: u.id, label: u.member?.nick || u.global_name || u.username }));
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
  description: 'Prepends the Discord sender\'s people/MEMORY.md section to each turn; people_note, people_forget and discord_history tools.',
  register(api) {
    const token = botToken(api.config);
    const agents = new Set(api.pluginConfig?.agents ?? []);
    // Only the agents it serves get the tools; others see nothing extra.
    api.registerTool((ctx) => (agents.has(ctx?.agentId) ? peopleTools(ctx) : null), { names: TOOL_NAMES });
    // sessionKey → promise of the latest message's mentions. A turn waits for
    // it (at most MENTION_WAIT_MS); a later message replaces it.
    const pending = new Map();

    api.on('message_received', (event) => {
      const key = event?.sessionKey;
      const agent = String(key ?? '').match(/^agent:([^:]+):discord:/)?.[1];
      if (!token || !agents.has(agent)) return;
      if (!String(event.content ?? '').includes('@')) { pending.delete(key); return; }
      const channelId = lastId(event.metadata?.originatingTo) || lastId(key);
      const messageId = discordId(event.messageId);
      if (!channelId || !messageId) return;
      pending.set(key, fetchMentions(token, channelId, messageId));
    });

    api.on('before_prompt_build', async (event, ctx) => {
      if (!agents.has(ctx?.agentId)) return;
      const sender = discordId(ctx.senderId);
      const mentions = ((await pending.get(ctx.sessionKey)) ?? []).filter((m) => m.id !== sender);
      pending.delete(ctx.sessionKey);
      const parts = [];
      if (sender) parts.push(`[Sender of this message — to tag them, write] <@${sender}>`);
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
      if (!agents.has(ctx?.agentId)) return;
      const p = event?.params ?? {};
      const targets = [p.path, p.file_path, p.filePath, ...(Array.isArray(event?.derivedPaths) ? event.derivedPaths : [])];
      if (event?.toolName === 'apply_patch' && typeof p.input === 'string' && p.input.includes(PEOPLE_FILE)) targets.push(PEOPLE_FILE);
      if (!targets.some((t) => typeof t === 'string' && t.replace(/\\/g, '/').endsWith(PEOPLE_FILE))) return;
      return {
        block: true,
        blockReason: 'people/MEMORY.md cannot be edited directly. Save one fact with the people_note tool ' +
          '(correct an old one with its `replace`); remove with people_forget.',
      };
    }, { matcher: ['edit', 'write', 'apply_patch'] });
  },
});
