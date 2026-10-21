// The agent's tool surface: web search, speech-to-text, the CDP browser, the
// Python sandbox MCP server, exec, and the agentic toggles (plan tool, loop
// detection, cross-agent messaging, tool search).
//
// Each service is wired only when its token is set in .env; clearing the
// token (and parking the service in compose) is the opt-out.

import * as env from '../lib/env.mjs';

// Search goes to the SearxNG sibling container. The searxng plugin ships
// bundled but disabled; without the explicit enable the web_search tool never
// appears in the agent runtime. Privacy posture lives in
// searxng/settings/settings.yml. "general,news" scored 35/40 on the fact bench
// against 34/40 for "general"; a stored "science" category put arXiv papers on
// top of plain fact questions. The agent can still ask for science per call.
//
// OPENCLAW_WEB_SEARCH_PROVIDER=parallel-free sends web_search to Parallel's
// free Search MCP instead (search.parallel.ai: no key, but the queries leave
// the host). On the three sets of scripts/websearch-bench.py it found 56/60
// against SearxNG's 52/60 (2026-10-20), with fuller excerpts. It needs the official plugin installed once
// (docs/CUSTOMIZATION.md); SearxNG stays wired as the way back.
function webSearch(config) {
  const provider = env.oneOf('OPENCLAW_WEB_SEARCH_PROVIDER', ['searxng', 'parallel-free'], 'searxng');
  config.set('tools.web.search.provider', provider);
  if (provider === 'parallel-free') config.set('plugins.entries.parallel.enabled', true);
  config.set('plugins.entries.searxng.enabled', true, 'bundled plugin is off by default');
  config.merge('plugins.entries.searxng.config.webSearch', {
    baseUrl: 'http://searxng:8080',
    language: '',
    categories: 'general,news',
  });
}

// Whisper STT. OpenClaw's audio pipeline (Control UI voice notes, Discord
// voice, Talk nodes) uses the first tools.media.models[] entry whose
// capabilities include "audio". The Bearer goes into the entry's own headers so
// it never mixes with a cloud OpenAI key in the provider auth chain. The entry
// is matched by baseUrl, so operator-added entries (a cloud fallback) stay.
function stt(config) {
  const token = env.str('STT_API_TOKEN');
  // Trailing slash is the OpenClaw convention for baseUrls; accept either form.
  const baseUrl = env.str('OPENCLAW_STT_BASE_URL', 'http://openclaw-stt-whisper:8080/v1').replace(/\/?$/, '/');
  if (!token) {
    // Without a token the Whisper entry would only fail auth; drop the one
    // this step wrote (other audio models stay).
    const models = config.get('tools.media.models');
    if (Array.isArray(models) && models.some((m) => m?.baseUrl === baseUrl)) {
      config.set('tools.media.models', models.filter((m) => m?.baseUrl !== baseUrl), 'STT_API_TOKEN unset');
    }
    return;
  }
  const language = env.str('OPENCLAW_STT_LANGUAGE');

  config.set('tools.media.audio.enabled', true);
  const models = config.get('tools.media.models') ?? [];
  const idx = models.findIndex((m) => m?.provider === 'openai' && m?.baseUrl === baseUrl);
  const prev = idx >= 0 ? models[idx] : {};
  const entry = {
    ...prev,
    provider: 'openai',
    model: env.str('OPENCLAW_STT_MODEL', 'deepdml/faster-whisper-large-v3-turbo-ct2'),
    baseUrl,
    capabilities: [...new Set([...(prev.capabilities ?? []), 'audio'])],
    ...(language ? { language } : {}),
  };
  if (idx >= 0) config.set(['tools', 'media', 'models', String(idx)], entry, 'Whisper STT');
  else config.set('tools.media.models', [entry, ...models], 'Whisper STT first');
  // Its own leaf, so the log masks the token and any extra header an
  // operator layered on the entry survives a re-run.
  config.set(['tools', 'media', 'models', String(Math.max(idx, 0)), 'headers', 'Authorization'], `Bearer ${token}`);
}

// The `browser` tool attaches over CDP to the openclaw-browser sidecar, one
// port per profile: the gateway does not pass cdpUrl query parameters other
// than the token into Playwright's connectOverCDP, so `?profile=` on a shared
// port cannot select a profile. The query-string token is the only auth the
// cdpUrl field takes; the CDP ports are bound to loopback on the host.
function browser(config) {
  // Wired only when the browser service runs (profile) and has a token.
  const token = env.profile('browser') ? env.str('BROWSER_API_TOKEN') : undefined;
  if (token) {
    const host = env.str('BROWSER_CDP_HOST', 'http://openclaw-browser');
    const base = env.int('BROWSER_PORT_BASE', 9222);
    const enc = encodeURIComponent(token);
    config.set('browser.enabled', true);
    // Order is the port mapping (base+1, base+2, ...): never reorder
    // BROWSER_PROFILE_NAMES, append. The compose file publishes 20 ports.
    const names = ['self-hosted', ...env.list('BROWSER_PROFILE_NAMES')];
    names.forEach((name, i) => {
      if (i > 19) return console.warn(`[patch-config] browser profile "${name}" is past the 20-port range; skipped.`);
      config.set(['browser', 'profiles', name, 'cdpUrl'], `${host}:${base + i}?token=${enc}`);
    });
    // The CDP host is a bridge DNS name in RFC1918 space, which the gateway's
    // SSRF guard blocks by default (BrowserCdpEndpointBlockedError). Exempt
    // only that host rather than allowing all private networks.
    config.addTo('browser.ssrfPolicy.allowedHostnames', [new URL(host).hostname]);
  } else {
    // Browser profile off or no token: drop the CDP profiles this step wrote
    // (they point at the sidecar host); profiles an operator added stay.
    const host = env.str('BROWSER_CDP_HOST', 'http://openclaw-browser');
    for (const [name, p] of Object.entries(config.get('browser.profiles') ?? {})) {
      if (typeof p?.cdpUrl === 'string' && p.cdpUrl.startsWith(`${host}:`)) config.remove(['browser', 'profiles', name], 'browser service off');
    }
  }

  // Without a default the tool falls through to sandbox/host targets this
  // stack does not run.
  const profile = env.str('OPENCLAW_BROWSER_DEFAULT_PROFILE', 'self-hosted');
  if (config.get(['browser', 'profiles', profile]) !== undefined) config.set('browser.defaultProfile', profile);

  // No OpenClaw sandbox-browser container runs here. Enabling it only adds a
  // noVNC hint to the system prompt, the model then calls the browser with
  // target="sandbox", and that fails as "Sandbox browser is unavailable".
  // With it off an omitted target resolves to "host", the CDP sidecar.
  config.set('agents.defaults.sandbox.browser.enabled', env.bool('OPENCLAW_SANDBOX_BROWSER', false));
}

// Python sandbox over streamable HTTP. requestTimeoutMs is per tool call: the
// MCP SDK's own default is 60 s, and a python_exec run (yt-dlp download,
// pip install) is allowed 120 s inside the sandbox.
function pythonSandbox(config) {
  const token = env.profile('python') ? env.str('PYTHON_SANDBOX_API_TOKEN') : undefined;
  const p = ['mcp', 'servers', 'python_sandbox'];
  if (!token) {
    config.remove(p, 'python profile off or no token; the gateway would dial a missing service');
    for (const parent of ['mcp.servers', 'mcp']) {
      const v = config.get(parent);
      if (v && typeof v === 'object' && !Object.keys(v).length) config.remove(parent);
    }
    return;
  }
  config.merge(p, {
    transport: 'streamable-http',
    url: env.str('PYTHON_SANDBOX_URL', 'http://openclaw-python-sandbox:8094/mcp'),
    connectionTimeoutMs: 10000,
    requestTimeoutMs: 120000,
  });
  // A leaf of its own so the log masks the token.
  config.set([...p, 'headers', 'Authorization'], `Bearer ${token}`);
}

// Exec, the agentic-coding shell. Written deterministically so a drifted
// mode in openclaw.json cannot outlive a recreate.
//   OPENCLAW_EXEC_SECURITY=allowlist (default) -> mode "ask": commands in
//     exec-approvals.json run, anything else becomes a Discord approval prompt.
//   OPENCLAW_EXEC_SECURITY=full -> mode "full": no gate at all. Any sender the
//     bot answers can run commands in the gateway container, which mounts the
//     config volume (secrets) read-write.
// strictInlineEval must be off in full mode: the gateway skips approval only
// when it is not true, so `python3 -c` one-liners kept asking for approval.
function exec(config) {
  if (!env.bool('OPENCLAW_TOOLS_EXEC', true)) {
    config.remove('tools.exec', 'OPENCLAW_TOOLS_EXEC=off; upstream default-deny applies');
    return;
  }
  const full = env.oneOf('OPENCLAW_EXEC_SECURITY', ['allowlist', 'full'], 'allowlist') === 'full';
  config.merge('tools.exec', {
    mode: full ? 'full' : 'ask',
    strictInlineEval: !full,
    // npm install and cmake builds on the GB10 routinely run past the
    // upstream default; matches the LLM request timeout.
    timeoutSeconds: env.int('OPENCLAW_EXEC_TIMEOUT_SEC', 1800),
    // stdin-only filters that run without approval; no interpreters here.
    safeBins: env.list('OPENCLAW_EXEC_SAFE_BINS', ['cat', 'grep', 'sed', 'head', 'tail', 'cut', 'wc']),
  });
  // Structured apply_patch for surgical edits, limited to the agent workspace.
  config.merge('tools.exec.applyPatch', { enabled: true, workspaceOnly: true });
}

// Agentic toggles, schema taken from what the WebGUI persists.
function agentic(config) {
  // sessions_send to a different agent id (discord-friend -> main). Sub-agent
  // spawns need no config.
  if (env.bool('OPENCLAW_TOOLS_AGENT_TO_AGENT', true)) {
    config.merge('tools.agentToAgent', { enabled: true, allow: env.list('OPENCLAW_TOOLS_AGENT_TO_AGENT_ALLOW', ['*']) });
  } else {
    config.remove('tools.agentToAgent', 'OPENCLAW_TOOLS_AGENT_TO_AGENT=off');
  }

  // update_plan helps the model track multi-step work.
  config.set('tools.updatePlan', env.bool('OPENCLAW_TOOLS_PLAN_TOOL', true) ? true : undefined);

  // Circuit breaker for repeated tool calls, so a long agentic chain cannot
  // spin forever. A legitimate build-fix-build loop looks the same to it, so
  // the thresholds are tunable (unset = upstream 30 / 10 / 20).
  if (env.bool('OPENCLAW_TOOLS_LOOP_DETECTION', true)) {
    config.set('tools.loopDetection.enabled', true);
    for (const [name, key] of [
      ['OPENCLAW_TOOLS_LOOP_DETECTION_HISTORY_SIZE', 'historySize'],
      ['OPENCLAW_TOOLS_LOOP_DETECTION_WARNING_THRESHOLD', 'warningThreshold'],
      ['OPENCLAW_TOOLS_LOOP_DETECTION_CRITICAL_THRESHOLD', 'criticalThreshold'],
    ]) {
      const n = env.int(name);
      if (n !== undefined) config.set(['tools', 'loopDetection', key], n);
    }
  } else {
    config.remove('tools.loopDetection.enabled', 'OPENCLAW_TOOLS_LOOP_DETECTION=off');
  }

  // Unset means "on": the model then sees a search/describe/call meta-tool,
  // and MCP results come back as JSON text through the nested wrapper. With
  // the image bridge that put ~13.5k tokens of base64 per image into the
  // context while the image never reached the vision input.
  const search = env.oneOf('OPENCLAW_TOOL_SEARCH', ['off', 'on', 'directory'], 'off');
  config.set('tools.toolSearch', { off: false, on: true, directory: { enabled: true, mode: 'directory' } }[search]);
}

export default function tools({ config }) {
  webSearch(config);
  stt(config);
  browser(config);
  pythonSandbox(config);
  exec(config);
  agentic(config);
}
