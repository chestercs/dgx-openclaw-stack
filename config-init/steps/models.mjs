// The local LLM: one OpenAI-compatible provider `vllm` pointing at the
// vllm-llm service, with one catalog entry for the model it serves.
//
// vLLM serves exactly one model id (LLM_MODEL_ID; the compose override for
// the backend sets the same id). Every other `vllm/...` entry is dropped:
// a stale one shows up in the model picker and answers 404.

import * as env from '../lib/env.mjs';
import { secret } from '../lib/secrets.mjs';

export const DEFAULT_MODEL_ID = 'nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4';

// A custom provider sends no thinking control at all unless the catalog
// entry's `compat` says how this model family takes it. Without it every
// Gemma turn ran without thinking (0 of 225 turns, 2026-10-18).
//   Gemma 4, Nemotron 3.x, DeepSeek-V4.1 on Strata: chat_template_kwargs.enable_thinking
//   Mistral Small 4: reasoning_effort, and vLLM accepts only none | high
export function thinkingCompat(id) {
  const mode = env.oneOf('LLM_THINKING_COMPAT', ['auto', 'off', 'qwen-chat-template'], 'auto');
  if (mode === 'off') return undefined;
  if (mode === 'qwen-chat-template' || /gemma-?4|nemotron|deepseek/i.test(id)) return { thinkingFormat: 'qwen-chat-template' };
  if (/mistral|magistral/i.test(id)) {
    const high = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
    return {
      supportsReasoningEffort: true,
      supportedReasoningEfforts: ['none', 'high'],
      reasoningEffortMap: { off: 'none', minimal: 'none', ...Object.fromEntries(high.map((l) => [l, 'high'])) },
    };
  }
  return undefined;
}

// Nemotron samples at temperature 1.0 from its generation_config, and at 1.0
// Hungarian replies fill with other scripts mid-word (38 foreign-script
// characters in six ~250-word answers, 2 at 0.6; measured 2026-10-20).
// LLM_TEMPERATURE: auto = 0.6 for Nemotron, a number = that value, off = none.
function temperature(id) {
  const v = env.str('LLM_TEMPERATURE', 'auto').toLowerCase();
  if (v === 'off') return undefined;
  if (v === 'auto') return /nemotron/i.test(id) ? 0.6 : undefined;
  return env.num('LLM_TEMPERATURE');
}

export default function models({ config }) {
  const id = env.str('LLM_MODEL_ID', DEFAULT_MODEL_ID);
  const ref = `vllm/${id}`;
  const p = ['models', 'providers', 'vllm'];

  config.merge(p, {
    baseUrl: env.str('LLM_BASE_URL', 'http://vllm-llm:8004/v1/'),
    api: 'openai-completions',
    // A long prefill on the GB10 (a coding session's context) outruns the
    // default ~120 s request watchdog; the turn then dies as "timed out".
    timeoutSeconds: env.int('LLM_REQUEST_TIMEOUT_SECONDS', 1800),
  });
  const key = env.str('VLLM_API_KEY');
  if (key) config.set([...p, 'apiKey'], secret('VLLM_API_KEY', key));

  const entry = {
    id,
    name: id,
    api: 'openai-completions',
    reasoning: true,
    // LLM_VISION=off for a text-only model: Strata's DeepSeek pack has no vision tower and answers a
    // request with a picture with a 400, so OpenClaw must not send it any.
    input: env.bool('LLM_VISION', true) ? ['text', 'image'] : ['text'],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    // What OpenClaw budgets per prompt. The models take more, but a full
    // 128K prefill already takes ~2 minutes on the GB10.
    contextWindow: env.int('LLM_CONTEXT_WINDOW', 131072),
    contextTokens: env.int('LLM_CONTEXT_WINDOW', 131072),
    // Thinking and answer share this budget; a long think followed by a big
    // tool-call JSON ended turns empty at 8192.
    maxTokens: env.int('LLM_MAX_TOKENS', 32768),
    ...(thinkingCompat(id) ? { compat: thinkingCompat(id) } : {}),
  };
  config.set([...p, 'models'], [entry], 'vLLM serves one model');

  // Agent-side defaults: the default model, its sampling, and every other
  // place Doctor or an operator may have pinned an older local model.
  config.set('agents.defaults.model', ref);
  const known = config.get('agents.defaults.models') ?? {};
  for (const k of Object.keys(known)) {
    if (k.startsWith('vllm') && k !== ref) config.remove(['agents', 'defaults', 'models', k], 'model no longer served');
  }
  config.set(['agents', 'defaults', 'models', ref, 'params', 'temperature'], temperature(id));
  if (config.get(['agents', 'defaults', 'models', ref]) === undefined) config.set(['agents', 'defaults', 'models', ref], {});

  const allow = config.get('agents.defaults.modelPolicy.allow');
  if (Array.isArray(allow)) {
    config.set('agents.defaults.modelPolicy.allow', [...allow.filter((k) => !k.startsWith('vllm')), ref]);
  }
  const imageModel = config.get('agents.defaults.imageModel');
  const localImage = (typeof imageModel === 'object' ? imageModel?.primary : imageModel)?.startsWith?.('vllm');
  if (localImage && !env.bool('LLM_VISION', true)) {
    // a text-only local model cannot read pictures: no image model rather than one that answers 400
    config.remove('agents.defaults.imageModel', 'the local model reads no pictures (LLM_VISION=off)');
  } else if (typeof imageModel === 'object' && localImage) config.set('agents.defaults.imageModel.primary', ref);
  else if (typeof imageModel === 'string' && localImage) config.set('agents.defaults.imageModel', ref);
  if (config.get('agents.defaults.utilityModel')?.startsWith?.('vllm')) config.set('agents.defaults.utilityModel', ref);
  for (const [agent, e] of Object.entries(config.get('agents.entries') ?? {})) {
    const m = typeof e.model === 'string' ? e.model : e.model?.primary;
    if (m?.startsWith('vllm') && m !== ref) config.set(['agents', 'entries', agent, 'model'], ref, 'model no longer served');
  }
}
