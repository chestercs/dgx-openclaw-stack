// Discord agent regression eval. Runs inside the openclaw-cli container
// (`scripts/discord-eval.sh` copies it and the cases in and the report out).
// Case format and why: evals/discord/schema.md.
//
//   node discord-eval.mjs --cases <file.yaml> [--smoke] [--id <id>] [--runs N]
//                         [--concurrency 2] [--side-effects] [--agent discord-friend]
//                         [--out report.json]
//
// Every run is a fresh Gateway turn under its own session key, so cases never
// share history with each other or a live channel; the keys are deleted in
// one call at the end (each `openclaw` start costs ~15 s, once per run was
// most of the runtime). Machine checks (tools, regexes) decide first; the
// rubric goes to a judge call on the same vLLM endpoint only when they pass,
// and the judge may say "unknown", which is reported and never counted as a
// pass. Latency depends on --concurrency: compare runs made with the same one.
import fs from 'node:fs';
import { execFile } from 'node:child_process';
import YAML from '/app/node_modules/yaml/dist/index.js';

const args = process.argv.slice(2);
const opt = (name, def) => { const i = args.indexOf(`--${name}`); return i < 0 ? def : args[i + 1]; };
const flag = (name) => args.includes(`--${name}`);
const AGENT = opt('agent', 'discord-friend');
const RUNS = Number(opt('runs', 1));
const CONCURRENCY = Math.max(1, Number(opt('concurrency', 2)));
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
  return new Promise((resolve, reject) => {
    execFile('openclaw', argv, { encoding: 'utf8', timeout: timeoutMs, maxBuffer: 64 << 20 }, (err, stdout) => (err ? reject(err) : resolve(stdout)));
  });
}

// Per invocation, so claim-guard lines of an earlier run never match.
const STAMP = Date.now().toString(36);
const usedKeys = [];

async function turn(c, n) {
  const key = `agent:${AGENT}:eval:${c.id}:${STAMP}-${n}`;
  usedKeys.push(key);
  const message = c.context ? `[${c.context}] ${c.prompt}` : c.prompt;
  const t0 = Date.now();
  let res;
  try {
    res = JSON.parse(await openclaw(['agent', '--agent', AGENT, '--session-key', key, '--message', message, '--json', '--timeout', '600'], 660e3));
  } catch (err) {
    res = { status: 'error', error: String(err.message).split('\n')[0] };
  }
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
  // The judge is the same model and has no tools: on the Szeged case it
  // called two real landmarks invented. It grades against the rubric only.
  const content = [
    'You grade a Discord bot answer against a rubric. Reply with JSON only: {"verdict":"pass|fail|unknown","reason":"<max 20 words>"}.',
    'Judge ONLY by what the rubric says is correct or a failure. Do not use your own knowledge to call something in the answer invented or wrong unless the rubric names it; extra details the rubric does not mention are not a failure.',
    'Use "unknown" when the rubric cannot be applied to this answer or you are not sure.',
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

// claim-guard's verdicts for the run and the tools it saw succeed. After a
// revision the CLI's toolSummary lists only the last pass's tools (a turn that
// searched, was revised and answered reported tools=[]); claim-guard counts
// the whole run.
function guardInfo(key) {
  try {
    const lines = fs.readFileSync(`${HOME}/.openclaw/logs/claim-guard.jsonl`, 'utf8').trim().split('\n').slice(-400);
    const hits = lines.map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter((x) => x?.session === key);
    const tools = [...new Set(hits.flatMap((h) => h.tools ?? []).filter((t) => !t.endsWith('!')))];
    return { outcome: hits.map((h) => h.outcome).join('>') || null, tools };
  } catch { return { outcome: null, tools: [] }; }
}

const pct = (xs, p) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : null; };
// --rejudge <report.json>: grade the answers stored in an earlier report with
// the current cases and judge, without new agent turns, so two runs can be
// compared under the same grading.
const prior = opt('rejudge') ? JSON.parse(fs.readFileSync(opt('rejudge'), 'utf8')) : null;
const priorRuns = new Map((prior?.results ?? []).map((x) => [x.id, x.runs]));
if (prior) cases = cases.filter((c) => priorRuns.has(c.id));

const queue = cases.flatMap((c) => (prior ? priorRuns.get(c.id).map((r) => ({ c, n: r.n, stored: r }))
  : Array.from({ length: RUNS }, (_, i) => ({ c, n: i + 1 }))));
const runsOf = new Map(cases.map((c) => [c.id, []]));
async function worker() {
  for (let job = queue.shift(); job; job = queue.shift()) {
    const { c, n } = job;
    const r = job.stored ?? await turn(c, n);
    const g = job.stored ? { outcome: r.guard, tools: [] } : guardInfo(r.key);
    r.tools = [...new Set([...r.tools, ...g.tools])];
    const fails = machineCheck(c, r);
    const j = fails.length ? { verdict: 'fail', reason: fails.join('; ') } : await judge(c, r);
    runsOf.get(c.id).push({ ...r, n, verdict: j.verdict, reason: j.reason, guard: g.outcome });
    console.log(`${j.verdict.padEnd(7)} ${c.id} #${n} ${(r.ms / 1000).toFixed(0)}s in=${r.inputTokens} tools=[${r.tools}] ${j.verdict === 'pass' ? '' : j.reason}`);
  }
}
await Promise.all(Array.from({ length: CONCURRENCY }, worker));
for (let i = 0; i < usedKeys.length; i += 50) {
  try { await openclaw(['sessions', 'delete', '--agent', AGENT, '--yes', ...usedKeys.slice(i, i + 50)], 120e3); } catch { /* best effort */ }
}
const results = cases.map((c) => ({ id: c.id, kind: c.kind, smoke: Boolean(c.smoke), runs: runsOf.get(c.id).sort((a, b) => a.n - b.n) }));

const all = results.flatMap((x) => x.runs);
const summary = {
  cases: results.length,
  runs: all.length,
  concurrency: CONCURRENCY,
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
