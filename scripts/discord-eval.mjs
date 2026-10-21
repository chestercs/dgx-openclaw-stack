// Discord agent regression eval. Runs inside the openclaw-cli container
// (`scripts/discord-eval.sh` copies it and the cases in and the report out).
// Case format and why: evals/discord/schema.md.
//
//   node discord-eval.mjs --cases <file.yaml> [--smoke] [--id <id>] [--runs N]
//                         [--side-effects] [--agent discord-friend] [--out report.json]
//
// Every run is a fresh Gateway turn under its own session key, deleted after
// the run, so cases never share history with each other or a live channel.
// Machine checks (tools, regexes) decide first; the rubric goes to a judge
// call on the same vLLM endpoint only when they pass, and the judge may say
// "unknown", which is reported and never counted as a pass.
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import YAML from '/app/node_modules/yaml/dist/index.js';

const args = process.argv.slice(2);
const opt = (name, def) => { const i = args.indexOf(`--${name}`); return i < 0 ? def : args[i + 1]; };
const flag = (name) => args.includes(`--${name}`);
const AGENT = opt('agent', 'discord-friend');
const RUNS = Number(opt('runs', 1));
const HOME = process.env.HOME || '/home/node';

let cases = YAML.parse(fs.readFileSync(opt('cases'), 'utf8'));
// A memory or schedule case can write the live people file or create a real
// automation; those run only when asked for.
const SIDE_EFFECTS = new Set(['memory', 'schedule']);
if (!flag('side-effects')) cases = cases.filter((c) => !SIDE_EFFECTS.has(c.kind));
if (flag('smoke')) cases = cases.filter((c) => c.smoke);
if (opt('id')) cases = cases.filter((c) => c.id === opt('id'));
if (!cases.length) throw new Error('no cases selected');

const cfg = JSON.parse(fs.readFileSync(`${HOME}/.openclaw/openclaw.json`, 'utf8'));
const vllm = cfg.models?.providers?.vllm;
const JUDGE_URL = `${String(vllm?.baseUrl ?? 'http://vllm-llm:8004/v1/').replace(/\/?$/, '/')}chat/completions`;
const JUDGE_MODEL = vllm?.models?.[0]?.id;

function openclaw(argv, timeoutMs) {
  return execFileSync('openclaw', argv, { encoding: 'utf8', timeout: timeoutMs, maxBuffer: 64 << 20, stdio: ['ignore', 'pipe', 'pipe'] });
}

function turn(c, n) {
  const key = `agent:${AGENT}:eval:${c.id}:${n}`;
  const message = c.context ? `[${c.context}] ${c.prompt}` : c.prompt;
  const t0 = Date.now();
  let res;
  try {
    res = JSON.parse(openclaw(['agent', '--agent', AGENT, '--session-key', key, '--message', message, '--json', '--timeout', '600'], 660e3));
  } catch (err) {
    res = { status: 'error', error: String(err.message).split('\n')[0] };
  }
  try { openclaw(['sessions', 'delete', '--agent', AGENT, '--yes', key], 60e3); } catch { /* best effort */ }
  const meta = res.result?.meta ?? {};
  return {
    key,
    status: res.status,
    error: res.error,
    text: meta.finalAssistantVisibleText ?? '',
    tools: meta.toolSummary?.tools ?? [],
    toolFailures: meta.toolSummary?.failures ?? 0,
    ms: meta.durationMs ?? Date.now() - t0,
    inputTokens: meta.agentMeta?.usage?.input ?? null,
  };
}

function machineCheck(c, r) {
  const e = c.expect ?? {};
  const fails = [];
  if (r.status !== 'ok') fails.push(`status ${r.status}${r.error ? `: ${r.error}` : ''}`);
  if (e.tools_any?.length && !e.tools_any.some((t) => r.tools.includes(t))) fails.push(`none of [${e.tools_any}] ran`);
  for (const t of e.tools_none ?? []) if (r.tools.includes(t)) fails.push(`${t} ran`);
  const abstained = /nem tudom|nem ellenőriztem|nem találtam|nem vagyok biztos/i.test(r.text);
  for (const re of e.must_match ?? []) {
    if (!new RegExp(re, 'i').test(r.text) && !(e.abstain_ok && abstained)) fails.push(`missing /${re}/`);
  }
  for (const re of e.must_not_match ?? []) if (new RegExp(re, 'i').test(r.text)) fails.push(`matched forbidden /${re}/`);
  return fails;
}

async function judge(c, r) {
  if (!c.rubric || !JUDGE_MODEL) return { verdict: 'pass', reason: 'no rubric' };
  const content = [
    'You grade a Discord bot answer against a rubric. Reply with JSON only: {"verdict":"pass|fail|unknown","reason":"<max 20 words>"}.',
    'Use "unknown" when the rubric cannot be applied to this answer.',
    `RUBRIC: ${c.rubric}`,
    `USER MESSAGE: ${c.prompt}`,
    `BOT ANSWER: ${r.text.slice(0, 4000)}`,
  ].join('\n');
  try {
    const res = await fetch(JUDGE_URL, {
      method: 'POST',
      headers: { Authorization: `Bearer ${process.env.VLLM_API_KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: JUDGE_MODEL, temperature: 0, max_tokens: 200, chat_template_kwargs: { enable_thinking: false },
        messages: [{ role: 'user', content }] }),
    });
    const t = (await res.json()).choices?.[0]?.message?.content ?? '';
    const v = JSON.parse(t.slice(t.indexOf('{'), t.lastIndexOf('}') + 1));
    return { verdict: ['pass', 'fail', 'unknown'].includes(v.verdict) ? v.verdict : 'unknown', reason: String(v.reason ?? '') };
  } catch (err) {
    return { verdict: 'unknown', reason: `judge error: ${err.message}` };
  }
}

// claim-guard's verdict for the run, if it judged one.
function guardOutcome(key) {
  try {
    const lines = fs.readFileSync(`${HOME}/.openclaw/logs/claim-guard.jsonl`, 'utf8').trim().split('\n').slice(-200);
    const hits = lines.map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter((x) => x?.session === key);
    return hits.map((h) => h.outcome).join('>') || null;
  } catch { return null; }
}

const pct = (xs, p) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : null; };
const results = [];
for (const c of cases) {
  const runs = [];
  for (let n = 1; n <= RUNS; n++) {
    const r = turn(c, n);
    const fails = machineCheck(c, r);
    const j = fails.length ? { verdict: 'fail', reason: fails.join('; ') } : await judge(c, r);
    runs.push({ ...r, verdict: j.verdict, reason: j.reason, guard: guardOutcome(r.key) });
    console.log(`${j.verdict.padEnd(7)} ${c.id} #${n} ${(r.ms / 1000).toFixed(0)}s in=${r.inputTokens} tools=[${r.tools}] ${j.verdict === 'pass' ? '' : j.reason}`);
  }
  results.push({ id: c.id, kind: c.kind, smoke: Boolean(c.smoke), runs });
}

const all = results.flatMap((x) => x.runs);
const summary = {
  cases: results.length,
  runs: all.length,
  pass: all.filter((r) => r.verdict === 'pass').length,
  fail: all.filter((r) => r.verdict === 'fail').length,
  unknown: all.filter((r) => r.verdict === 'unknown').length,
  passAllRuns: results.filter((x) => x.runs.every((r) => r.verdict === 'pass')).length,
  latencyMedianS: pct(all.map((r) => r.ms / 1000), 0.5),
  latencyP90S: pct(all.map((r) => r.ms / 1000), 0.9),
  inputTokensMedian: pct(all.map((r) => r.inputTokens).filter((x) => x != null), 0.5),
  byKind: Object.fromEntries([...new Set(results.map((x) => x.kind))].map((k) => {
    const rs = results.filter((x) => x.kind === k).flatMap((x) => x.runs);
    return [k, `${rs.filter((r) => r.verdict === 'pass').length}/${rs.length}`];
  })),
};
console.log(JSON.stringify(summary, null, 2));
if (opt('out')) fs.writeFileSync(opt('out'), JSON.stringify({ at: new Date().toISOString(), agent: AGENT, summary, results }, null, 2));
