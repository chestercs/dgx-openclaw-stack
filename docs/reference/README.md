# Reference knowledge base — `docs/reference/`

Deeper reference / research material for the components used in this stack.
Complements the end-user docs at the top of `docs/` (`ARCHITECTURE.md`,
`CUSTOMIZATION.md`, `TROUBLESHOOTING.md`) — if you want to understand the
internal schema, the credential layout, the patcher step-by-step, or the
research rationale behind the chosen LLM / TTS stack, this is where to look.

## Contents

| File | Topic |
|---|---|
| [`llm-stack.md`](./llm-stack.md) | Gemma 4 31B variants (BF16 / LiteLLM / NVFP4) + bge-m3 embedding stack architecture |
| [`tts-stack.md`](./tts-stack.md) | Bilingual TTS (Kokoro EN + F5-TTS HU + router), schema enums, web chat limitation |
| [`tts-research-hungarian.md`](./tts-research-hungarian.md) | Hungarian TTS open-source landscape (F5-TTS, XTTS, Piper, tier list) |
| [`stt-stack.md`](./stt-stack.md) | Whisper large-v3 + Trendency HU fine-tune: three voice surfaces, CTranslate2 on Blackwell notes |
| [`discord-voice-agent.md`](./discord-voice-agent.md) | Discord voice-controlled agent: schema, workspace isolation, threat model, DAVE E2E notes |
| [`discord-text-agent.md`](./discord-text-agent.md) | Discord text-channel agent: mention pill, tools.profile gating, message tool, ackReactionScope cycle bug + agent-driven workaround, verify checklist |
| [`openclaw-internals.md`](./openclaw-internals.md) | OpenClaw v0.4.x internals: 3-store credential layout, schema, patcher steps, releases, persistence, CLI overhead |
| [`browser-automation.md`](./browser-automation.md) | Playwright Chromium over CDP: port-per-profile, query-string token, noVNC bridge, threat model |
| [`python-sandbox.md`](./python-sandbox.md) | Python code-execution sandbox: MCP wiring, kernel pool, threat model, why not OpenClaw native or `agents.defaults.sandbox` |
| [`image-comfyui-bridge.md`](./image-comfyui-bridge.md) | Image-generation MCP bridge → ComfyUI: cross-compose join, host-gateway hop, workflow template architecture, threat model |
| [`patterns.md`](./patterns.md) | Reusable Docker / dev patterns: cross-compose network, anchored grep, opt-in triple-gate, network namespace, SearxNG gotchas |

## Related docs

- Stack-level end-user docs: [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md), [`docs/CUSTOMIZATION.md`](../CUSTOMIZATION.md), [`docs/TROUBLESHOOTING.md`](../TROUBLESHOOTING.md)
- Setup walkthrough: [`SETUP.md`](../../SETUP.md)
- Agent / contributor guidance: [`CLAUDE.md`](../../CLAUDE.md)
