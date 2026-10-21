// harness-guard — policy checks outside the model for the Discord agent:
// the model proposes a tool call, this hook decides whether it may run. The
// block reason says how to fix the call, so the next attempt can pass.
//
// Owner-only tools in DMs. Guild channels get the same rule from the
// official channels.discord.guilds["*"].toolsBySender policy (config-init),
// which also takes the tools out of the prompt. Discord has no per-sender
// tool policy for DMs, and the agent-level toolsBySender would also hit
// scheduled runs (they have no sender and match "*"), which need exec. So in
// a DM the peer in the session key (…:discord:direct:<id>) must be an owner
// for these tools to run; runs without a sender pass.
//
// Memory with a source. A new fact in memory/<topic>.md needs a "Forrás:"
// line among the added lines. The bot once saved its own wrong conclusion
// ("RuinBot messages are deleted after 30 s") and repeated it as a fact the
// next day; the prompt rule alone did not hold. The file is copied to
// memory-backups/ before every change, outside the workspace so memory
// search does not index the copies. memory/server-map.md is generated and
// not writable. people/MEMORY.md has its own gate in people-context.
//
// Sub-agent tasks with an end. A sessions_spawn task needs a "Done when:"
// line: the task is all the child knows, and one without an end condition
// either stops early or never reports.
//
// Actions that reach other people. For anyone but an owner, `message` sends
// and `automations` deliveries go only to the conversation the request came
// from (its channel or thread, or the sender's own DM): a member's reminder
// once went to a channel of another server. No DM to someone else, no
// broadcast, no @everyone/@here or mass mention. The sender comes from
// before_prompt_build (runs of one session are serialised, so the latest
// sender of the session is the current run's); runs without a sender
// (scheduled jobs, the CLI) pass.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';

const ids = (v) => (Array.isArray(v) ? v : []).map((x) => String(x).match(/\d{15,22}/)?.[0]).filter(Boolean);
const STATE_DIR = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw');
const BACKUPS = path.join(STATE_DIR, 'memory-backups');
const KEEP_BACKUPS = 20;

const MEMORY_FILE = /(^|\/)memory\/[^/]+\.md$/;
const GENERATED = /(^|\/)memory\/server-map\.md$/;
const SOURCE_LINE = /(^|[\s*(\-[])(Forrás|Source)\**\s*:/i;
const DONE_WHEN = /(^|\n)\s*[-*]?\s*\**(Done when|Kész,? ha)\**\s*:/i;

// message actions that put something in front of people.
const DELIVERING = new Set(['send', 'broadcast', 'poll', 'reply', 'thread-create', 'thread-reply', 'sticker']);
const MAX_MENTIONS = 5;
const lastId = (v) => [...String(v ?? '').matchAll(/\d{15,22}/g)].at(-1)?.[0] ?? '';
const isUserTarget = (t) => /(^|[^a-z])user:|<@!?\d/i.test(String(t));

const lines = (s) => String(s ?? '').split('\n').map((l) => l.trim()).filter(Boolean);
// Lines in `next` that `prev` does not have: what the call adds.
const added = (prev, next) => { const old = new Set(lines(prev)); return lines(next).filter((l) => !old.has(l)); };
const isFact = (l) => !/^(#|<!--|-->|```)/.test(l) && !SOURCE_LINE.test(l) && /\p{L}/u.test(l);

export default definePluginEntry({
  id: 'harness-guard',
  name: 'Harness Guard',
  description: 'Policy checks outside the model: owner-only tools in Discord DMs, sourced memory writes, sub-agent tasks with an end condition, members\' messages and reminders only where they asked.',
  register(api) {
    const pc = api.pluginConfig ?? {};
    const agents = new Set(pc.agents ?? []);
    const owners = new Set(ids(pc.owners));
    const ownerOnly = new Set(pc.ownerOnlyTools ?? []);
    const memoryGate = pc.memorySources !== false;
    const spawnGate = pc.spawnDoneWhen !== false;
    const othersGateOn = pc.memberReach !== false;

    const workspaceOf = (ctx) => {
      const w = ctx?.workspaceDir ?? api.config?.agents?.entries?.[ctx?.agentId]?.workspace;
      return typeof w === 'string' ? w.replace(/^~(?=\/)/, os.homedir()) : null;
    };
    const resolve = (p, ctx) => {
      if (path.isAbsolute(p)) return p;
      const ws = workspaceOf(ctx);
      return ws ? path.join(ws, p) : null;
    };
    function backup(file, agentId) {
      try {
        if (!fs.existsSync(file)) return;
        const dir = path.join(BACKUPS, agentId ?? 'agent');
        fs.mkdirSync(dir, { recursive: true });
        const base = path.basename(file);
        fs.copyFileSync(file, path.join(dir, `${base}.${new Date().toISOString().replace(/[:.]/g, '-')}`));
        const old = fs.readdirSync(dir).filter((f) => f.startsWith(`${base}.`)).sort();
        for (const f of old.slice(0, Math.max(0, old.length - KEEP_BACKUPS))) fs.rmSync(path.join(dir, f), { force: true });
      } catch { /* a failed backup must not block the write */ }
    }

    // → block result, or null. What the call adds to a memory/*.md file.
    function memoryWrite(event, ctx) {
      const p = event?.params ?? {};
      let target = [p.path, p.file_path, p.filePath].find((t) => typeof t === 'string');
      let add = [];
      if (event?.toolName === 'apply_patch') {
        const patch = String(p.input ?? p.patch ?? '');
        target = patch.match(/^\*\*\* (?:Add|Update) File: (.+)$/m)?.[1]?.trim();
        if (target && MEMORY_FILE.test(target.replace(/\\/g, '/'))) {
          add = patch.split('\n').filter((l) => l.startsWith('+') && !l.startsWith('+++')).map((l) => l.slice(1).trim()).filter(Boolean);
        }
      }
      if (!target) return null;
      const norm = target.replace(/\\/g, '/');
      if (!MEMORY_FILE.test(norm)) return null;
      if (GENERATED.test(norm)) {
        return { block: true, blockReason: 'memory/server-map.md is generated every night from Discord; it cannot be edited. Read it, or use discord_history.' };
      }
      const file = resolve(target, ctx);
      if (event?.toolName === 'write') {
        let prev = '';
        try { prev = file ? fs.readFileSync(file, 'utf8') : ''; } catch { /* new file */ }
        add = added(prev, p.content);
      } else if (event?.toolName === 'edit') {
        const edits = Array.isArray(p.edits) ? p.edits : [p];
        add = edits.flatMap((e) => added(e?.oldText ?? e?.old_string, e?.newText ?? e?.new_string));
      }
      if (add.some(isFact) && !add.some((l) => SOURCE_LINE.test(l))) {
        return {
          block: true,
          blockReason: `Each new fact in ${norm.replace(/^.*?(?=memory\/)/, '')} needs its source on a line like ` +
            '"Forrás: <url> | <#channel> <date> | <@user> mondta". Save only what a user said or a tool showed: ' +
            'never your own conclusion, and never that something was not found. Add the source line and call again.',
        };
      }
      if (file) backup(file, ctx?.agentId);
      return null;
    }

    // sessionKey → the Discord id of the current run's sender.
    const senders = new Map();
    api.on('before_prompt_build', (event, ctx) => {
      if (!agents.has(ctx?.agentId) || !ctx?.sessionKey) return;
      const id = String(ctx.senderId ?? '').match(/\d{15,22}/)?.[0];
      senders.delete(ctx.sessionKey);
      if (id) senders.set(ctx.sessionKey, id);
      if (senders.size > 500) senders.delete(senders.keys().next().value);
    });

    // → block result, or null. Targets of a non-owner's message or delivery.
    function reach(targets, text, ctx, sender, what) {
      const here = new Set(String(ctx?.sessionKey ?? '').match(/\d{15,22}/g) ?? []);
      for (const t of targets.filter(Boolean).map(String)) {
        const id = lastId(t);
        if (!id) continue;
        if (isUserTarget(t) ? id === sender : here.has(id)) continue;
        return {
          block: true,
          blockReason: isUserTarget(t)
            ? `Only the bot's owner can have ${what} go to someone else's DM. Do not retry: say so, and offer to post it here instead.`
            : `${what[0].toUpperCase()}${what.slice(1)} for a member goes back to where it was asked, not to another channel. ` +
              'Leave out the target (for automations: "sessionTarget":"current" and no delivery) and call again, or tell the user that only the owner can send elsewhere.',
        };
      }
      const mentions = (String(text ?? '').match(/<@!?\d{15,22}>/g) ?? []).length;
      if (/@(everyone|here)\b/.test(String(text ?? '')) || mentions > MAX_MENTIONS) {
        return { block: true, blockReason: `No @everyone, @here or more than ${MAX_MENTIONS} mentions in one message for a member. Write it without them.` };
      }
      return null;
    }

    function othersGate(tool, p, ctx) {
      const sender = senders.get(ctx?.sessionKey);
      if (!sender || owners.has(sender)) return null;
      if (tool === 'message') {
        const action = String(p.action ?? '');
        if (!DELIVERING.has(action)) return null;
        if (action === 'broadcast') return { block: true, blockReason: 'Broadcasts are for the bot\'s owner only. Do not retry; answer here instead.' };
        return reach([p.target, ...(Array.isArray(p.targets) ? p.targets : [])], p.message ?? p.text ?? p.content, ctx, sender, 'a message');
      }
      if (tool === 'automations' || tool === 'cron') {
        if (!['add', 'update', 'edit'].includes(String(p.action ?? ''))) return null;
        const job = p.job ?? p.patch ?? {};
        return reach([job?.delivery?.to], job?.payload?.message ?? job?.payload?.text, ctx, sender, 'a scheduled message');
      }
      return null;
    }

    api.on('before_tool_call', (event, ctx) => {
      if (!agents.has(ctx?.agentId)) return;
      const tool = event?.toolName;
      if (ownerOnly.has(tool)) {
        const peer = String(ctx?.sessionKey ?? '').match(/:discord:direct:(\d{15,22})/)?.[1];
        if (peer && !owners.has(peer)) {
          return {
            block: true,
            blockReason: `${tool} runs only for the bot's owner. Do not retry: tell the user plainly that this is not available to them, and offer what you can do instead.`,
          };
        }
      }
      if (memoryGate && (tool === 'write' || tool === 'edit' || tool === 'apply_patch')) {
        const r = memoryWrite(event, ctx);
        if (r) return r;
      }
      if (othersGateOn) {
        const r = othersGate(tool, event?.params ?? {}, ctx);
        if (r) return r;
      }
      if (spawnGate && tool === 'sessions_spawn' && !DONE_WHEN.test(String(event?.params?.task ?? ''))) {
        return {
          block: true,
          blockReason: 'The task needs a line "Done when: <what the finished result is>" (next to the goal, inputs, limits and the reply language). ' +
            'Add it and call sessions_spawn again.',
        };
      }
    });
  },
});
