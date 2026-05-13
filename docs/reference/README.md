# Reference knowledge base — `docs/reference/`

Deeper reference / research material for the components used in this stack.
Complements the end-user docs at the top of `docs/` (`ARCHITECTURE.md`,
`CUSTOMIZATION.md`, `TROUBLESHOOTING.md`) — if you want to understand the
internal schema, the credential layout, the patcher step-by-step, or the
research rationale behind the chosen LLM / TTS stack, this is where to look.

## Stack internals

| File | Topic |
|---|---|
| [`openclaw-internals.md`](./openclaw-internals.md) | OpenClaw v0.4.x internals: 3-store credential layout, schema, patcher steps, releases, persistence, CLI overhead |
| [`patterns.md`](./patterns.md) | Reusable Docker / dev patterns: cross-compose network, anchored grep, opt-in triple-gate, network namespace, SearxNG gotchas |

## Model serving

| File | Topic |
|---|---|
| [`llm-stack.md`](./llm-stack.md) | Gemma 4 NVFP4 (MoE + dense) + bge-m3 embedding stack architecture, GB10 benchmarks, remote-backend swap |
| [`tts-stack.md`](./tts-stack.md) | Bilingual TTS (Kokoro EN + F5-TTS HU + router), schema enums, `OPENCLAW_TTS_AUTO` knob, web chat limitation |
| [`tts-research-hungarian.md`](./tts-research-hungarian.md) | Hungarian TTS open-source landscape (F5-TTS, XTTS, Piper, tier list) |
| [`stt-stack.md`](./stt-stack.md) | Whisper large-v3 self-built CUDA 13 image: three voice surfaces, CTranslate2 on Blackwell notes |

## Discord integration

| File | Topic |
|---|---|
| [`discord-config.md`](./discord-config.md) | Patcher overrides at a glance (11 steps, env knobs, vanilla restore recipe), slash-command authz (#19310), slash command matrix, mention gate vs `/activation` slash |
| [`discord-text-agent.md`](./discord-text-agent.md) | Text-channel agent design: mention pill, `tools.profile` gating, `message` tool, ackReactionScope cycle bug + agent-driven workaround, progressive streaming UX, TTS opt-in, verify checklist |
| [`discord-voice-agent.md`](./discord-voice-agent.md) | Voice-channel agent: schema, workspace isolation, threat model, DAVE E2E notes |

## Tool services

| File | Topic |
|---|---|
| [`browser-automation.md`](./browser-automation.md) | Playwright Chromium over CDP: port-per-profile, query-string token, noVNC bridge, threat model, `browser.act` parameter coaching |
| [`python-sandbox.md`](./python-sandbox.md) | Python code-execution sandbox: MCP wiring, kernel pool, threat model, why not OpenClaw native or `agents.defaults.sandbox` |
| [`image-comfyui-bridge.md`](./image-comfyui-bridge.md) | Image-generation MCP bridge → ComfyUI: cross-compose join, host-gateway hop, workflow template architecture, chat-side render limitations, threat model |
| [`video-comfyui-bridge.md`](./video-comfyui-bridge.md) | Video-generation extension on the same bridge (LTX-Video 2.3, v0.12.0+): model bundle ~71 GB, native audio in a single pass, T2V + I2V tool, Discord auto-embed as the primary surface |
| [`img-bash-command.md`](./img-bash-command.md) | `!~/.openclaw/bin/img` Discord bash bypass for image-gen — why it exists, how it's wired |
| [`media-bridge-checklist.md`](./media-bridge-checklist.md) | End-to-end TTS / STT / image-gen pipeline verification checklist for new deploys |
| [`chat-surface-capability-matrix.md`](./chat-surface-capability-matrix.md) | What renders where: Discord text vs Discord voice vs OpenClaw web chat vs CLI (markdown / images / TTS / shortcodes) |

## Related docs

- Stack-level end-user docs: [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md), [`docs/CUSTOMIZATION.md`](../CUSTOMIZATION.md), [`docs/TROUBLESHOOTING.md`](../TROUBLESHOOTING.md)
- Setup walkthrough: [`SETUP.md`](../../SETUP.md)
- Agent / contributor guidance: [`CLAUDE.md`](../../CLAUDE.md)
