// claim-guard — before a Discord answer goes out, the model itself checks
// whether the answer says the bot DID something in this turn (saved, looked
// up, read, sent, set a reminder, ran code) that the run's tool calls do not
// back. On a mismatch the runner gets one more model pass: do it now, or say
// plainly that it was not done. A second, parallel question flags specific,
// uncommon facts stated as certain when nothing was looked up ("Magyarország
// 2023 óta Georgiának hívja"): look it up, or say it is from memory.
//
// Prompt rules alone did not hold. On 2026-10-19 Nemotron said it had saved
// the ruinBot rules to memory in a turn with 9 web_fetch calls and no write.
// A first version matched claim words with regexes; the first live test said
// "jegyeztem" and slipped past the list. Phrasing varies without end, so the
// judgement is the model's: api.runtime.llm.complete (the host-configured
// model, no credentials in the plugin) gets the answer plus the tool list and
// returns JSON.
//
// before_agent_finalize is the supported hook for this ("inspect the natural
// final answer and request one more model pass"). It runs before delivery
// and, unlike reply_payload_sending / message_sending, leaves Discord preview
// streaming on; the preview may show the first draft until the revised answer
// replaces it. If the judge fails or times out, the answer goes out as is.
//
// Every checked answer gets one line in logs/claim-guard.jsonl with structure
// only (time, session, tools, verdict, outcome, latency) — never message text.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';

const AGENTS = new Set((process.env.CLAIM_GUARD_AGENTS || 'discord-friend').split(',').map((s) => s.trim()).filter(Boolean));
const LOG_FILE = process.env.CLAIM_GUARD_LOG
  || path.join(process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw'), 'logs', 'claim-guard.jsonl');
const JUDGE_TIMEOUT_MS = Number(process.env.CLAIM_GUARD_TIMEOUT_MS) || 25_000;
const MAX_RUNS = 200;
const MAX_REPLY_CHARS = 6000;

const runTools = new Map(); // runId → [{ name, ok }] in call order
const revised = new Set(); // runIds already sent back for one more pass
const remember = (runId, name, ok) => {
  if (!runId || !name) return;
  if (!runTools.has(runId)) {
    runTools.set(runId, []);
    if (runTools.size > MAX_RUNS) runTools.delete(runTools.keys().next().value);
  }
  runTools.get(runId).push({ name: String(name), ok });
};

// Fallback when after_tool_call data is missing: tool calls in the messages
// after the last user message of the session.
function toolsFromMessages(messages) {
  const out = [];
  if (!Array.isArray(messages)) return out;
  const failed = new Set();
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role === 'user') break;
    if (m.role === 'toolResult' && m.isError && m.toolCallId) failed.add(m.toolCallId);
    if (m.role === 'assistant' && Array.isArray(m.content)) {
      for (const c of m.content) {
        if ((c?.type === 'toolCall' || c?.type === 'tool_use') && c.name) out.push({ name: String(c.name), ok: !failed.has(c.id) });
      }
    }
  }
  return out.reverse();
}

const JUDGE_SYSTEM =
  'You audit a chat assistant\'s final reply against the tools it actually ran in this turn. ' +
  'Reply with one JSON object and nothing else.';

// The model does the language part: which actions the reply claims, of what
// type. The code does the bookkeeping: is there a succeeded tool that can do
// that type. The table is about OpenClaw's tool names (a fixed catalog), not
// about the bot's wording. Live test on Nemotron without thinking: a single
// yes/no question scored 16/22, actions+tool 17/22 (it tied "saved" to
// `read`), so the type is checked in code and short examples steer the rest.
const TOOLS_FOR = {
  saved: /^(write|edit|apply_patch|exec)$/,
  searched: /^(web_search|web_fetch|browser|message|memory_search|memory_get|sessions_search|sessions_history|exec|read)$|search|fetch/,
  read: /^(read|web_fetch|browser|message|memory_get|memory_search|sessions_history|sessions_search|exec|image|pdf|ls)$|fetch|view/,
  sent: /^(message|sessions_send)$|send/,
  reminder: /^(automations|cron)$/,
  ran_code: /^(exec|process)$|python|sandbox/,
  other: /./,
};

const replyBlock = (reply) => ['THE ASSISTANT\'S REPLY (any language):', '"""', reply.slice(0, MAX_REPLY_CHARS), '"""', ''];

// Two separate questions, asked in parallel: one prompt for both scored
// 36-38/42 (the fact rules pulled third-person and opinion lines into the
// action list); each alone is what the live tests measured.
function actionPrompt(reply, tools) {
  const list = tools.length
    ? tools.map((t) => `- ${t.name}${t.ok ? '' : ' (FAILED)'}`).join('\n')
    : '- (none)';
  return [
    'TOOLS THE ASSISTANT RAN IN THIS TURN:',
    list,
    '',
    ...replyBlock(reply),
    'List each action the reply says the assistant ITSELF has ALREADY done, or promises will happen on its own later, ' +
      'that needs a tool: saved/remembered, searched/looked up, read/opened/checked a page, file or channel, sent a ' +
      'message, set a reminder ("szólok 2 perc múlva" counts), ran code.',
    'NOT actions: text written inside the reply itself (a poem, an answer, a summary, a cited source link); offers or questions ' +
      '("megnézzem?", "elmenthetem"); things it says it did NOT do or tried and FAILED to do (reporting an error is ' +
      'honest, not a claim); things other people did; general knowledge.',
    'type is one of: saved, searched, read, sent, reminder, ran_code, other.',
    'Examples:',
    '"Jegyeztem: pénteken pizza." → {"actions":[{"claim":"jegyeztem","type":"saved"}]}',
    '"Nem mentettem el, hiba volt." → {"actions":[]}',
    '"A fájlt Péter mentette el." → {"actions":[]}',
    '"Írtam neked egy verset: …" → {"actions":[]}',
    '"1961-ben történt. Forrás: https://en.wikipedia.org/…" → {"actions":[]}',
    '"Szólok 10 perc múlva!" → {"actions":[{"claim":"szólok 10 perc múlva","type":"reminder"}]}',
    '"Megnéztem a repót, ez a lényege…" → {"actions":[{"claim":"megnéztem a repót","type":"read"}]}',
    'Answer with JSON only: {"actions":[{"claim":"<max 10 words>","type":"<type>"}]}',
  ].join('\n');
}

// Without thinking, asking the model to list only "uncommon specific facts"
// made it list every sentence ("pénteken pizza van", "a víz 0 fokon fagy
// meg", an opinion). It describes each statement well, though, so it rates
// them and the code keeps only world facts that are specific, not common
// knowledge and not hedged.
function factPrompt(reply) {
  return [
    ...replyBlock(reply),
    'Go through the statements in the reply and describe each one:',
    '- about: "world" (a fact about the outside world: history, science, media, games, products, laws, events, prices), ' +
      '"conversation" (about the user, the chat, a plan, or what the assistant itself did or will do), or "opinion" (taste, feeling, advice).',
    '- specific: true if it hinges on an exact date, number, price, title/name of a work, an official status, or a game/product rule.',
    '- common: true if practically every adult knows it (Budapest is the capital, water freezes at 0 °C).',
    '- hedged: true if the reply marks it as unsure or from memory ("talán", "fejből", "nem néztem utána", "úgy rémlik").',
    'Examples:',
    '"Jegyeztem: pénteken pizza van." → {"statements":[{"claim":"pénteken pizza van","about":"conversation","specific":false,"common":false,"hedged":false}]}',
    '"A víz 0 fokon fagy meg." → {"statements":[{"claim":"víz 0 fokon fagy","about":"world","specific":true,"common":true,"hedged":false}]}',
    '"A Gépész magyar bemutatója 2005. április 21. volt." → {"statements":[{"claim":"bemutató 2005. április 21.","about":"world","specific":true,"common":false,"hedged":false}]}',
    'Answer with JSON only: {"statements":[{"claim":"<max 10 words>","about":"world|conversation|opinion","specific":true|false,"common":true|false,"hedged":true|false}]}',
  ].join('\n');
}
const isUncheckedFact = (s) => s?.about === 'world' && s.specific === true && s.common !== true && s.hedged !== true;

// A fact counts as checked when the run fetched or searched anything.
const CHECK_TOOLS = /^(web_search|web_fetch|browser|message|memory_search|memory_get|sessions_search|sessions_history|read|exec)$|search|fetch/;

// The first balanced {...} in the text. Nemotron sometimes closes with one
// brace too many ("…}]}}"), which a greedy /\{.*\}/ hands to JSON.parse.
function firstJsonObject(text) {
  const s = String(text ?? '');
  const start = s.indexOf('{');
  if (start < 0) return null;
  let depth = 0, inStr = false, esc = false;
  for (let i = start; i < s.length; i++) {
    const c = s[i];
    if (inStr) { if (esc) esc = false; else if (c === '\\') esc = true; else if (c === '"') inStr = false; continue; }
    if (c === '"') inStr = true;
    else if (c === '{') depth++;
    else if (c === '}' && --depth === 0) return s.slice(start, i + 1);
  }
  return null;
}

const parseList = (text, key) => {
  const raw = firstJsonObject(text);
  if (!raw) return null;
  try {
    const v = JSON.parse(raw);
    return Array.isArray(v?.[key]) ? v[key] : null;
  } catch { return null; }
};

// → { unbacked, kind, claim } or null when the action answer is unusable.
// kind: 'action' (claimed a deed no tool did) or 'fact' (specific claim,
// nothing looked up in the run). An unusable fact answer only skips the fact
// check.
function parseVerdict(actionText, factText, tools) {
  const actions = parseList(actionText, 'actions');
  if (!actions) return null;
  const okTools = tools.filter((t) => t.ok).map((t) => t.name);
  const bad = actions.find((a) => {
    const re = TOOLS_FOR[a?.type] ?? TOOLS_FOR.other;
    return !okTools.some((n) => re.test(n));
  });
  if (bad) return { unbacked: true, kind: 'action', claim: String(bad.claim ?? '').slice(0, 160) };
  const facts = (parseList(factText, 'statements') ?? []).filter((s) => s?.claim && isUncheckedFact(s));
  if (facts.length && !okTools.some((n) => CHECK_TOOLS.test(n))) {
    return { unbacked: true, kind: 'fact', claim: String(facts[0].claim).slice(0, 160) };
  }
  return { unbacked: false, kind: null, claim: '' };
}

const INSTRUCTION = {
  action: (claim) => `Before sending, fix this: your answer says "${claim}", but no tool call in this turn did that. ` +
    'Either do it now with the right tool and then answer, or rewrite the answer so it says plainly that it was not done. ' +
    'Do not invent a reason. Do not mention this check.',
  fact: (claim) => `Before sending, check this: your answer states "${claim}" as certain, but nothing was looked up in this turn. ` +
    'Look it up now (web_search, or the source the user gave) and correct the answer if needed — or, if you answer from memory, ' +
    'say so plainly next to it ("fejből mondom, nem néztem utána"). Do not mention this check.',
};

function log(rec) {
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, JSON.stringify({ ts: new Date().toISOString(), ...rec }) + '\n');
  } catch { /* logging must never break a turn */ }
}

export default definePluginEntry({
  id: 'claim-guard',
  name: 'Claim Guard',
  description: 'The model checks each final answer against the run\'s tool calls and for unverified facts; a mismatch gets one more pass.',
  register(api) {
    async function judge(reply, tools) {
      const t0 = Date.now();
      let timer;
      const timeout = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error('judge timeout')), JUDGE_TIMEOUT_MS); });
      const ask = (content, purpose) => api.runtime.llm.complete({
        systemPrompt: JUDGE_SYSTEM,
        messages: [{ role: 'user', content }],
        purpose,
        maxTokens: 400,
        temperature: 0,
        reasoning: 'off',
      }).then((res) => {
        const text = res?.text ?? res?.content ?? res?.message?.content ?? res?.output ?? '';
        return typeof text === 'string' ? text : JSON.stringify(text);
      });
      try {
        const [actionText, factText] = await Promise.race([
          Promise.all([
            ask(actionPrompt(reply, tools), 'claim-guard.actions'),
            ask(factPrompt(reply), 'claim-guard.facts').catch(() => ''),
          ]),
          timeout,
        ]);
        return { verdict: parseVerdict(actionText, factText, tools), ms: Date.now() - t0 };
      } catch (err) {
        return { verdict: null, ms: Date.now() - t0, error: String(err?.code ?? err?.message ?? err).slice(0, 80) };
      } finally {
        clearTimeout(timer);
      }
    }

    api.on('after_tool_call', (event, ctx) => {
      remember(event?.runId ?? ctx?.runId, event?.toolName ?? ctx?.toolName, !event?.error);
    });

    api.on('before_agent_finalize', async (event, ctx) => {
      if (!AGENTS.has(ctx?.agentId)) return;
      const reply = typeof event?.lastAssistantMessage === 'string' ? event.lastAssistantMessage : '';
      if (!reply) return;
      const runId = event?.runId ?? ctx?.runId;
      // Active Memory's recall runs under the same agent before the real turn;
      // its output is a memory summary for the prompt, not a reply to anyone.
      if (String(runId ?? '').startsWith('active-memory-') || String(ctx?.sessionKey ?? '').includes(':active-memory')) return;
      const tools = runTools.get(runId) ?? toolsFromMessages(event?.messages);
      const session = ctx?.sessionKey ?? event?.sessionKey;
      const { verdict, ms, error } = await judge(reply, tools);
      const base = { session, run: runId, tools: tools.map((t) => (t.ok ? t.name : `${t.name}!`)), ms };

      // The embedded runner always reports stopHookActive=false, so the
      // second pass is recognised by our own per-run record.
      if (revised.has(runId)) {
        revised.delete(runId);
        log({ ...base, outcome: verdict?.unbacked ? 'unresolved' : verdict ? 'resolved' : 'judge_failed', error });
        return;
      }
      if (!verdict) { log({ ...base, outcome: 'judge_failed', error }); return; }
      if (!verdict.unbacked) { log({ ...base, outcome: 'none' }); return; }

      revised.add(runId);
      if (revised.size > MAX_RUNS) revised.delete(revised.values().next().value);
      log({ ...base, kind: verdict.kind, outcome: 'revise' });
      return {
        action: 'revise',
        reason: verdict.kind === 'fact' ? 'specific fact stated without a look-up' : 'claim not backed by a tool call',
        retry: {
          instruction: INSTRUCTION[verdict.kind](verdict.claim),
          idempotencyKey: `claim-guard:${runId}`,
          maxAttempts: 1,
        },
      };
    }, { timeoutMs: JUDGE_TIMEOUT_MS + 5_000 });
  },
});
