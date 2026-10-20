// claim-guard — before a Discord answer goes out, checks that what the bot
// says it did ("elmentettem", "megnéztem", "beállítottam az emlékeztetőt")
// matches a tool call that succeeded in the same run. On a mismatch it asks
// the runner for one more model pass: do it now, or say plainly that it was
// not done.
//
// Prompt rules alone did not hold. On 2026-10-19 Nemotron said it had saved
// the ruinBot rules to memory in a turn with 9 web_fetch calls and no write,
// and invented a reason ("the messages are deleted after 30 s") for search
// results it never got. before_agent_finalize is the supported hook for this
// ("inspect the natural final answer and request one more model pass"); it
// runs before delivery and, unlike reply_payload_sending / message_sending,
// leaves Discord preview streaming on. The streamed preview may show the
// first draft until the revised answer replaces it.
//
// Every finding goes to logs/claim-guard.jsonl with structure only (time,
// session, claim kind, tools, outcome) — never message text.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';

const AGENTS = new Set((process.env.CLAIM_GUARD_AGENTS || 'discord-friend').split(',').map((s) => s.trim()).filter(Boolean));
const LOG_FILE = process.env.CLAIM_GUARD_LOG
  || path.join(process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw'), 'logs', 'claim-guard.jsonl');
const MAX_RUNS = 200;

// Word start that also works for accented letters (JS `\b` is ASCII-only, so
// `\bátnéztem` would never match).
const W = '(?<![\\p{L}\\d])';
const words = (alts) => new RegExp(alts.map((a) => W + a).join('|'), 'iu');

// Each kind: first-person past-tense claims, and the tools that make them true.
// `any` = any successful tool call counts (a look-up can use many tools).
const KINDS = [
  {
    kind: 'saved',
    re: words(['(el)?mentettem', 'megjegyeztem', 'feljegyeztem', 'rögzítettem', 'elmentve(?![\\p{L}])', 'beírtam a memóriá']),
    tools: ['write', 'edit', 'apply_patch', 'exec'],
    what: 'a write/edit/exec call that saved it',
  },
  {
    kind: 'reminder',
    re: words(['beállítottam[^.\\n]{0,60}emlékeztet', 'emlékeztetőt[^.\\n]{0,40}beállítottam']),
    tools: ['automations', 'cron'],
    what: 'an automations call that created it',
  },
  {
    kind: 'looked',
    re: words(['megnéztem', 'elolvastam', 'utánanéztem', 'ellenőriztem', 'átnéztem', 'rákerestem', 'kikerestem']),
    tools: 'any',
    what: 'any tool call that returned the information',
  },
];
// "nem mentettem el", "még nem néztem meg" — a denied claim is not a claim.
const NEGATED = new RegExp(W + '(nem|sem)(\\s+\\S+)?\\s+$', 'iu');

const runTools = new Map(); // runId → Set of tool names that succeeded
const revised = new Set(); // runIds already sent back for one more pass
const remember = (runId, name) => {
  if (!runId || !name) return;
  if (!runTools.has(runId)) {
    runTools.set(runId, new Set());
    if (runTools.size > MAX_RUNS) runTools.delete(runTools.keys().next().value);
  }
  runTools.get(runId).add(String(name));
};

// Fallback when after_tool_call data is missing: tool calls in the messages
// after the last user message of the session.
function toolsFromMessages(messages) {
  const out = new Set();
  if (!Array.isArray(messages)) return out;
  const failed = new Set();
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role === 'user') break;
    if (m.role === 'toolResult' && m.isError && m.toolCallId) failed.add(m.toolCallId);
    if (m.role === 'assistant' && Array.isArray(m.content)) {
      for (const c of m.content) {
        if ((c?.type === 'toolCall' || c?.type === 'tool_use') && c.name && !failed.has(c.id)) out.add(String(c.name));
      }
    }
  }
  return out;
}

function findClaims(text) {
  const hits = [];
  for (const k of KINDS) {
    const m = k.re.exec(text);
    if (!m) continue;
    const before = text.slice(Math.max(0, m.index - 24), m.index);
    if (NEGATED.test(before)) continue;
    hits.push({ ...k, phrase: m[0] });
  }
  return hits;
}

function log(rec) {
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, JSON.stringify({ ts: new Date().toISOString(), ...rec }) + '\n');
  } catch { /* logging must never break a turn */ }
}

export default definePluginEntry({
  id: 'claim-guard',
  name: 'Claim Guard',
  description: 'Asks for one more model pass when an answer claims a save/look-up/reminder that no tool call in the run backs.',
  register(api) {
    api.on('after_tool_call', (event, ctx) => {
      if (event?.error) return;
      remember(event?.runId ?? ctx?.runId, event?.toolName ?? ctx?.toolName);
    });

    api.on('before_agent_finalize', (event, ctx) => {
      if (!AGENTS.has(ctx?.agentId)) return;
      const text = typeof event?.lastAssistantMessage === 'string' ? event.lastAssistantMessage : '';
      if (!text) return;
      const runId = event?.runId ?? ctx?.runId;
      const used = new Set([...(runTools.get(runId) ?? []), ...toolsFromMessages(event?.messages)]);
      const unbacked = findClaims(text)
        .filter((c) => (c.tools === 'any' ? used.size === 0 : !c.tools.some((t) => used.has(t))));
      const session = ctx?.sessionKey ?? event?.sessionKey;
      // The embedded runner always reports stopHookActive=false, so the
      // second pass is recognised by our own per-run record.
      if (revised.has(runId)) {
        revised.delete(runId);
        log({ session, run: runId, kinds: unbacked.map((c) => c.kind), tools: [...used], outcome: unbacked.length ? 'unresolved' : 'resolved' });
        return;
      }
      if (!unbacked.length) return;
      revised.add(runId);
      if (revised.size > MAX_RUNS) revised.delete(revised.values().next().value);
      log({ session, run: runId, kinds: unbacked.map((c) => c.kind), tools: [...used], outcome: 'revise' });
      const lines = unbacked.map((c) => `- You wrote "${c.phrase}", but this turn has no ${c.what}.`);
      return {
        action: 'revise',
        reason: 'claim not backed by a tool call',
        retry: {
          instruction: [
            'Before sending, fix these statements about what you did:',
            ...lines,
            'Either do it now with the tool and then answer, or rewrite the answer so it says plainly that it was not done. ' +
              'Do not invent a reason. Do not mention this check.',
          ].join('\n'),
          idempotencyKey: `claim-guard:${runId}`,
          maxAttempts: 1,
        },
      };
    });
  },
});
