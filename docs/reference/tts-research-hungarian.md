# Hungarian TTS open-source landscape — research (2026-04-22)

> **SUPERSEDED.** The stack moved to **Fish Audio S2 Pro** for the
> multilingual TTS surface — see the [Unreleased] entry in `CHANGELOG.md`
> and `openclaw-tts-fish/README.md`. The research notes below are
> preserved for historical context and as a reference if a future
> commercial-license-friendly Hungarian-finetune swap becomes necessary.

> Reference material: research.

**Key finding:** there is no perfect Hungarian TTS anywhere — not even ElevenLabs is fully native-perfect (accent, stress).

## Tier S — F5-TTS Hungarian fine-tunes (current open-source SOTA)

All three are **CC-BY-NC-4.0 licensed** → personal / research use only; commercial use prohibited.

- **`sarpba/F5-TTS_V1_hun_v2`** (HF) — built-in Hungarian text normalizer + onset anomalies handled. **Best production-ready choice.** GitHub: `sarpba/F5-TTS_hun`.
- **`Maxdorger29/f5-tts-hungarian`** (HF) — best-documented: ~280 h training (YodaLingua 206 h + CommonVoice 54 h + CSS10 10 h), ~2.4 GB VRAM FP16, RTF 0.25 on an RTX 5060 Ti.
- **`mp3pintyo/F5-TTS-Hun`** (HF) — community variant.

Voice cloning: 5–15 s reference audio + accurate transcript. Onset artifact ~200–400 ms (workaround: filler word + trim).

## Tier A — XTTS-v2 (Coqui)

Multilingual, officially Hungarian, real-world accent. Coqui Public License (looser than CC-BY-NC). Coqui the company shut down in 2024; the Idiap fork is active.

## Tier B — Piper `hu_HU`

Voices: `anna`, `berta`, `imre`, `kalman` + multi-speaker. Quality presets (low / med / high). MIT, CPU-only, ~100 MB. More robotic prosody, NO cloning.

## Tier C — Niche

- `facebook/mms-tts-hun` — VITS, lightweight, basic.
- `legekka/diana-hungarian-tts-vits` — single-speaker audiobook.

## Hungarian blocklist (zero-shot not usable)

Voxtral-4B-TTS (Mistral 2026), Kokoro, Spark-TTS, IndexTTS, CosyVoice2, MeloTTS, StyleTTS2 — **no native Hungarian**.

## Commercial reference

ElevenLabs > Azure Neural TTS > Google WaveNet for Hungarian quality. ElevenLabs WER ~2.83% (English); weaker in Hungarian.

## Why sarpba V2 for the OpenClaw stack

sarpba V2 won over Maxdorger29 because:

- It includes a built-in Hungarian text normalizer (reduces pronunciation errors).
- Sentence-onset anomalies are handled.

These are the two main Hungarian weaknesses of F5-TTS.
