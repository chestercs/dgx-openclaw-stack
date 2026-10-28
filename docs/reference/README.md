# Reference knowledge base — `docs/reference/`

Deeper reference material for the components of this stack. It complements the
end-user docs at the top of `docs/` (`ARCHITECTURE.md`, `CUSTOMIZATION.md`,
`TROUBLESHOOTING.md`): the internal schema, the credential layout, what config-init
writes and why, and the measurements behind the model choices.

## Configuration and internals

| File | Topic |
|---|---|
| [`configuration.md`](./configuration.md) | Every `.env` knob config-init and the compose files read, with its default |
| [`openclaw-internals.md`](./openclaw-internals.md) | OpenClaw internals: credential stores, per-agent `models.json` cache, schema notes, persistence |
| [`patterns.md`](./patterns.md) | Reusable Docker patterns: network namespace sharing, parked services, SearxNG gotchas |

## Model serving and hardware

| File | Topic |
|---|---|
| [`llm-stack.md`](./llm-stack.md) | LLM backends: Nemotron (default), Gemma 4 and Mistral Small 4 swaps, thinking and sampling settings, bge-m3 embeddings |
| [`stt-stack.md`](./stt-stack.md) | Whisper turbo on a self-built CUDA 13 image |
| [`gb10-power-thermal.md`](./gb10-power-thermal.md) | GB10 power-offs under load (clock cap, thermal guard), the 611 MHz PD wedge, firmware, unified-memory livelock |
| [`host-setup.md`](./host-setup.md) | Everything outside Docker on the host: systemd units, polkit rule, sysctl, cron jobs, their state; `scripts/gb10-host-setup.sh` installs and checks them |

## Discord integration

| File | Topic |
|---|---|
| [`discord-config.md`](./discord-config.md) | Discord settings config-init writes, keyed by knob and module; slash-command authz; mention gate vs `/activation` |
| [`discord-text-agent.md`](./discord-text-agent.md) | Text-channel agent design |
| [`discord-voice-agent.md`](./discord-voice-agent.md) | Voice-channel agent: schema, workspace isolation, threat model |
| [`agentic-coding.md`](./agentic-coding.md) | Exec and chat-side approvals, thread-per-task, long runs, skills layout, schema-gate runbook |

## Tool services

| File | Topic |
|---|---|
| [`browser-automation.md`](./browser-automation.md) | Playwright Chromium over CDP: port-per-profile, query-string token, noVNC login helper, threat model |
| [`python-sandbox.md`](./python-sandbox.md) | Python code-execution sandbox: MCP wiring, kernel pool, threat model |
| [`meshcore-bridge.md`](./meshcore-bridge.md) | MeshCore LoRa bridge: off-grid agent access from a mesh handheld |
| [`openclaw-image-comfyui/README.md`](../../openclaw-image-comfyui/README.md) | ComfyUI image / video / music bridge (optional, off on the reference host) |
| [`openclaw-tts-fish/README.md`](../../openclaw-tts-fish/README.md) | Fish Audio S2 Pro TTS (optional, off on the reference host) |

## Archive

[`docs/archive/`](../archive/) keeps superseded material for history: the changelog
before 1.0.0, the Kokoro / F5-TTS stack and Hungarian TTS research, the 2026.4-era
chat-surface capability matrix, and upstream feedback drafts.

## Related docs

- Stack-level end-user docs: [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md), [`docs/CUSTOMIZATION.md`](../CUSTOMIZATION.md), [`docs/TROUBLESHOOTING.md`](../TROUBLESHOOTING.md)
- Setup walkthrough: [`SETUP.md`](../../SETUP.md)
- Agent / contributor guidance: [`CLAUDE.md`](../../CLAUDE.md)
