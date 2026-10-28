// strata-router: Claude Code -> Strata (DeepSeek-V4.1-Flash on strata-dsv41) proxy. See README.md.
//
// Claude Code gives its auto-mode classifier a fixed ~60 s limit that cannot be configured. The classifier prompt
// is a ~35K-token rule set (constant) plus a short transcript, with another prefix than the conversation. Read cold
// on a GB10 it takes ~54 s, Claude Code gives up at 60 s or earlier, the engine stops reading, nothing is cached, and
// every Bash call in auto mode was refused after a minute. Warm, the same model judged a 20-case bench 20/20 in
// ~2 s (stage 1) and ~15 s (stage 2).
//
// So, for classifier requests only (Claude Code's CLAUDE_CODE_AUTO_MODE_MODEL = CLASSIFIER_ALIAS):
//   - the model name is rewritten to the real one;
//   - when Claude Code disconnects, the upstream request is NOT cancelled: it runs to the end, so the rule set
//     lands in Strata's conversation cache and the next classifier call is fast (cold: a client that gave up at
//     5 s, Strata finished reading at 54 s, the next call took 1 s);
//   - the router keeps the rule set (system prompt, transcript removed) and replays it once at start, so even the
//     first Bash call after a restart finds it cached.
// Every other request is passed through, and a disconnect (Esc) cancels it upstream as before. One change:
// THINKING_BUDGET caps the thinking budget Claude Code asks for (~32K on every call, MAX_THINKING_TOKENS on the
// client did not change it). On a hard task DeepSeek filled the 24K the server allows on every call, ~20 min at
// ~20 tok/s before each small step, and the context grew 24K a call until Claude Code compacted. Strata reads a
// budget under 8192 as effort "medium" as well as a hard stop.
import http from "node:http";
import fs from "node:fs";

const PORT = 8096;
const UPSTREAM = { host: process.env.STRATA_HOST || "strata", port: Number(process.env.STRATA_PORT || 8095) };
const CLASSIFIER_ALIAS = process.env.CLASSIFIER_ALIAS || "deepseek-v4.1-flash-classifier";
const REAL_MODEL = process.env.REAL_MODEL || "deepseek-v4.1-flash";
const KEY = process.env.STRATA_API_KEY || "";
const THINKING_BUDGET = Number(process.env.THINKING_BUDGET) || 0;     // 0 = pass through
const WARM_FILE = "/data/classifier-warm.json";
const STAGE2_FILE = "/data/classifier-stage2.json";
const LOG_FILE = "/data/router.log";

const log = (...a) => {
  const line = `${new Date().toISOString()} ${a.join(" ")}`;
  console.log(line);
  fs.appendFile(LOG_FILE, line + "\n", () => {});
};

// The classifier request without its transcript: the system prompt (the rule set) and the stage instruction.
// Claude Code's content parts are "<transcript>\n", one part per transcript entry, "</transcript>\n", the instruction.
function warmTemplate(req) {
  const parts = req.messages?.[0]?.content;
  if (!Array.isArray(parts) || parts.length < 3) return null;
  return {
    ...req, model: REAL_MODEL, max_tokens: 1, stream: false,
    messages: [{ role: "user", content: [parts[0], parts[parts.length - 2], parts[parts.length - 1]] }],
  };
}

function upstream(method, path, headers, body, onResponse) {
  const h = { ...headers, host: `${UPSTREAM.host}:${UPSTREAM.port}`, "content-length": Buffer.byteLength(body) };
  const up = http.request({ ...UPSTREAM, method, path, headers: h }, onResponse);
  up.end(body);
  return up;
}

async function strataReady() {
  return new Promise((res) => {
    const r = http.get({ ...UPSTREAM, path: "/v1/models", headers: { authorization: `Bearer ${KEY}` }, timeout: 3000 },
      (x) => { x.resume(); res(x.statusCode === 200); });
    r.on("error", () => res(false)).on("timeout", () => { r.destroy(); res(false); });
  });
}

// Strata may still be loading or not started yet (it starts after the rest of the stack): wait for
// it, then replay. A Strata restart keeps its conversation cache on disk, so one replay per router start is enough.
async function warmUp() {
  let body;
  try { body = fs.readFileSync(WARM_FILE); } catch { log("warm-up: no saved rule set yet"); return; }
  while (!(await strataReady())) await new Promise((r) => setTimeout(r, 30000));
  const t0 = Date.now();
  log("warm-up: replaying the classifier rule set");
  const up = upstream("POST", "/v1/messages", { "content-type": "application/json", authorization: `Bearer ${KEY}` }, body,
    (ur) => { ur.resume(); ur.on("end", () => log(`warm-up done: HTTP ${ur.statusCode} in ${Date.now() - t0} ms`)); });
  up.on("error", (e) => log(`warm-up failed: ${e.message}`));
}

http.createServer((req, res) => {
  if (req.url === "/router/health") { res.end("ok"); return; }
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    let body = Buffer.concat(chunks), json = null;
    try { json = JSON.parse(body.toString() || "null"); } catch { /* not JSON: pass through */ }
    const classifier = json?.model === CLASSIFIER_ALIAS;
    if (classifier) {
      json.model = REAL_MODEL;
      body = Buffer.from(JSON.stringify(json));
      const tpl = warmTemplate(json);
      // stage 1 (it has the stop sequence) is replayed at start; stage 2 is kept for cls-bench.mjs
      if (tpl) fs.writeFile(json.stop_sequences ? WARM_FILE : STAGE2_FILE, JSON.stringify(tpl), () => {});
    } else if (THINKING_BUDGET && json?.thinking?.type === "enabled" && json.thinking.budget_tokens > THINKING_BUDGET) {
      log(`thinking budget ${json.thinking.budget_tokens} -> ${THINKING_BUDGET}`);
      json.thinking.budget_tokens = THINKING_BUDGET;
      body = Buffer.from(JSON.stringify(json));
    }
    const t0 = Date.now();
    let gone = false, upRes = null;
    const up = upstream(req.method, req.url, req.headers, body, (ur) => {
      upRes = ur;
      if (gone) { ur.resume(); return; }
      res.writeHead(ur.statusCode, ur.headers);
      ur.pipe(res);
    });
    if (classifier) up.on("response", (ur) => ur.on("end", () =>
      log(`classifier ${gone ? "finished after the client left" : "done"} in ${Date.now() - t0} ms`)));
    up.on("error", (e) => {
      log(`upstream error: ${e.message}`);
      if (gone) return;
      if (!res.headersSent) res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ type: "error", error: { type: "api_error", message: `router: ${e.message}` } }));
    });
    res.on("close", () => {
      if (res.writableFinished) return;
      gone = true;
      if (classifier) {
        log(`classifier: client left after ${Date.now() - t0} ms, letting Strata finish`);
        if (upRes) { upRes.unpipe(res); upRes.resume(); }    // drain what is left instead of stalling the engine
      } else {
        up.destroy();
      }
    });
  });
}).listen(PORT, "0.0.0.0", () => {
  log(`router on ${PORT} -> ${UPSTREAM.host}:${UPSTREAM.port}, classifier alias ${CLASSIFIER_ALIAS}`);
  setTimeout(warmUp, 5000);
});

process.on("SIGTERM", () => process.exit(0));
