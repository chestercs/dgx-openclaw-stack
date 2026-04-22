# Magyar TTS opensource landscape — kutatás (2026-04-22)

> **Public knowledge** — research, megosztható.

**Kulcs megállapítás**: NINCS perfekt magyar TTS sehol — még az ElevenLabs sem fully native-perfect (akcentus, hangsúly).

## Tier S — F5-TTS magyar fine-tune-ok (jelenlegi opensource SOTA)

Mindhárom **CC-BY-NC-4.0 license** → csak personal/research, commercial use TILOS.

- **sarpba/F5-TTS_V1_hun_v2** (HF) — beépített magyar text normalizer + onset anomáliák kezelve. **Legjobb production-ready választás.** GitHub: `sarpba/F5-TTS_hun`
- **Maxdorger29/f5-tts-hungarian** (HF) — legjobban dokumentált: ~280h training (YodaLingua 206h + CommonVoice 54h + CSS10 10h), ~2.4GB VRAM FP16, RTF 0.25 RTX 5060 Ti-n
- **mp3pintyo/F5-TTS-Hun** (HF) — community variáns

Voice cloning: 5-15s reference audio + pontos transzkript. Onset artifact ~200-400ms (workaround: filler szó + trim).

## Tier A — XTTS-v2 (Coqui)

Multilingual, hivatalosan magyar, real-world akcentusos. Coqui Public License (lazább mint CC-BY-NC). Coqui company megszűnt 2024, Idiap fork aktív.

## Tier B — Piper hu_HU

Hangok: anna, berta, imre, kalman + multi-speaker. Quality preset-ek (low/med/high). MIT, CPU-only, ~100MB. Robotikusabb prozódia, NINCS cloning.

## Tier C — Niche

- `facebook/mms-tts-hun` — VITS, lightweight, basic
- `legekka/diana-hungarian-tts-vits` — single speaker audiobook

## Magyar TILTÓLISTA (zero-shot nem értékelhető)

Voxtral-4B-TTS (Mistral 2026), Kokoro, Spark-TTS, IndexTTS, CosyVoice2, MeloTTS, StyleTTS2 — **nincs natív magyar**.

## Commercial referencia

ElevenLabs > Azure Neural TTS > Google WaveNet magyar minőségben. ElevenLabs WER ~2.83% (angolon), magyaron ennél gyengébb.

## Választás indoklása (sarpba V2 az OpenClaw stackbe)

A sarpba V2 azért nyert a Maxdorger29 felett, mert:
- Beépített magyar text normalizer van benne (csökkenti a kiejtéshibákat)
- Mondat-onset anomáliák kezelve

Ez a két fő F5-TTS gyengeség magyarul.
