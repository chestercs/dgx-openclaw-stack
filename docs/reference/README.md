# Reference knowledge base — `docs/reference/`

Mélyebb reference / research anyag a stackben használt komponensekhez. A
`docs/` tetején lévő end-user doksikat (`ARCHITECTURE.md`, `CUSTOMIZATION.md`,
`TROUBLESHOOTING.md`) egészíti ki — aki a belső schema-t, a credential layoutot,
a patcher részletes viselkedését, vagy a választott LLM / TTS stack research
alapjait akarja megérteni, itt találja.

## Tartalom

| Fájl | Téma |
|---|---|
| [`llm-stack.md`](./llm-stack.md) | Gemma 4 31B variánsok (BF16 / LiteLLM / NVFP4) + bge-m3 embedding stack architektúra |
| [`tts-stack.md`](./tts-stack.md) | Kétnyelvű TTS (Kokoro EN + F5-TTS HU + router), schema enums, web chat limitáció |
| [`tts-research-magyar.md`](./tts-research-magyar.md) | Magyar TTS opensource landscape (F5-TTS, XTTS, Piper, tier-list) |
| [`openclaw-internals.md`](./openclaw-internals.md) | OpenClaw v0.4.x belsők: 3-store credential layout, schema, patcher lépések, releases, persistencia, CLI overhead |
| [`patterns.md`](./patterns.md) | Reusable Docker / dev pattern-ek: cross-compose network, anchored grep, opt-in triple-gate, network namespace, SearxNG gotcha-k |

## Kapcsolódó doksik

- Stack-szintű end-user doksik: [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md), [`docs/CUSTOMIZATION.md`](../CUSTOMIZATION.md), [`docs/TROUBLESHOOTING.md`](../TROUBLESHOOTING.md)
- Setup walkthrough: [`SETUP.md`](../../SETUP.md)
- Agent / contributor guidance: [`CLAUDE.md`](../../CLAUDE.md)
