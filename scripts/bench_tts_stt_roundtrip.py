#!/usr/bin/env python3
"""TTS -> STT roundtrip benchmark for the dgx-openclaw-stack voice pipeline.

Closes a loop: take a text, run it through Fish Audio S2 Pro (via the
openclaw-tts-fish shim) to get audio, send that audio into the Whisper STT
backend, compare the transcript back to the original. Reports WER plus
per-leg latency.

STT path is always the Whisper backend (:8093, deepdml/faster-whisper-
large-v3-turbo-ct2 by default).

Usage (on the GB10 host with the stack running):
    python3 scripts/bench_tts_stt_roundtrip.py [--runs 1]

Reads tokens from the stack's `.env` (expected at ../.env relative to the
script, or pass --env-file). Prints a markdown results table to stdout and a
full JSON dump to stderr so a pipe can separate the two.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

CORPUS = {
    "EN": {
        "short": "Hello Petya, this is a short production test.",
        "medium": (
            "Production benchmarks verify that the voice pipeline works end to end. "
            "We synthesize text, then transcribe the audio back and check how close we got."
        ),
        "long": (
            "A self hosted voice pipeline depends on two moving parts, text to speech "
            "synthesis and automatic speech recognition. This benchmark runs a closed "
            "loop. The same sentence goes from text into audio and back to text, which "
            "gives us a single number that captures how much information the pipeline "
            "loses along the way. If the round trip is nearly perfect, both sides are "
            "working well. If it degrades, the difference tells us where to look."
        ),
    },
    "HU": {
        "short": "Szia Petya, ez egy rövid teszt.",
        "medium": (
            "Egy önállóan üzemeltetett hangcsővezeték két részből áll, a "
            "beszédszintézisből és a beszédfelismerésből. Ez a benchmark egy zárt "
            "kört fut le, hogy mérjük a pontosságot és a sebességet."
        ),
        "long": (
            "A GB10 gépen futó hangszolgáltatás két komponensből áll. Az első "
            "komponens a beszédszintézis, amely a Fish Audio S2 Pro modellt "
            "használja mindkét nyelvre. A második komponens a beszédfelismerés, "
            "amely a Whisper turbo modellt futtatja. Ez a teszt azt ellenőrzi, "
            "hogy a kör lezáródik-e. Ha a leírt szöveg visszafelé is ugyanaz, "
            "akkor mindkét komponens megbízhatóan működik."
        ),
    },
}
VOICE = {"EN": "default_en", "HU": "default_hu"}
STT_MODEL_DEFAULT = "deepdml/faster-whisper-large-v3-turbo-ct2"


def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k] = v
    return env


def tts_fish(lang, text, tok):
    body = {"input": text, "voice": VOICE[lang], "response_format": "wav"}
    headers = {"Authorization": "Bearer " + tok}
    t0 = time.time()
    r = requests.post(
        "http://127.0.0.1:8091/v1/audio/speech",
        headers=headers, json=body, timeout=300,
    )
    r.raise_for_status()
    return r.content, time.time() - t0


def stt_direct(audio, lang, tok_stt, stt_model):
    files = {"file": ("a.wav", audio, "audio/wav")}
    data = {"model": stt_model, "language": lang.lower()}
    headers = {"Authorization": "Bearer " + tok_stt}
    t0 = time.time()
    r = requests.post(
        "http://127.0.0.1:8093/v1/audio/transcriptions",
        headers=headers, files=files, data=data, timeout=300,
    )
    r.raise_for_status()
    return r.json().get("text", "").strip(), time.time() - t0


def normalize(s):
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def wer(ref, hyp):
    r, h = normalize(ref).split(), normalize(hyp).split()
    if not r:
        return 0.0
    sm = difflib.SequenceMatcher(None, r, h)
    matches = sum(b.size for b in sm.get_matching_blocks())
    return (len(r) - matches) / len(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=str(Path(__file__).parent.parent / ".env"))
    ap.add_argument("--runs", type=int, default=1, help="repetitions per item (for warmth averaging)")
    ap.add_argument("--stt-model", default=STT_MODEL_DEFAULT,
                    help=f"override STT model id (default {STT_MODEL_DEFAULT})")
    args = ap.parse_args()

    env = load_env(args.env_file)
    # Fish Audio shim and gateway both expect TTS_API_TOKEN. If a deploy is
    # mid-rotation and the shim is still on the old token while the gateway
    # has the new key, the fall-back below keeps the bench usable.
    tok_fish = env.get("TTS_API_TOKEN") or env.get("OPENCLAW_TTS_FISH_API_KEY")
    if not tok_fish:
        print("ERROR: no TTS_API_TOKEN or OPENCLAW_TTS_FISH_API_KEY in .env",
              file=sys.stderr)
        sys.exit(2)
    tok_stt = env["STT_API_TOKEN"]

    results = []
    for lang, sizes in CORPUS.items():
        for size_name, text in sizes.items():
            for run_i in range(args.runs):
                print(f"  [fish] {lang}/{size_name} run {run_i+1} ({len(text)} chars)...", file=sys.stderr)
                try:
                    audio, tts_dt = tts_fish(lang, text, tok_fish)
                    hyp, stt_dt = stt_direct(audio, lang, tok_stt, args.stt_model)
                    w = wer(text, hyp)
                    results.append({
                        "path": "fish", "lang": lang, "size": size_name, "run": run_i + 1,
                        "chars": len(text), "words": len(text.split()),
                        "tts_ms": int(tts_dt * 1000),
                        "stt_ms": int(stt_dt * 1000),
                        "e2e_ms": int((tts_dt + stt_dt) * 1000),
                        "audio_kb": len(audio) // 1024,
                        "wer_pct": round(w * 100, 1),
                        "hyp_head": hyp[:90] + ("..." if len(hyp) > 90 else ""),
                    })
                except Exception as exc:
                    results.append({
                        "path": "fish", "lang": lang, "size": size_name, "run": run_i + 1,
                        "error": str(exc)[:160],
                    })

    print("\n| Path | Lang | Size | Chars | Words | TTS ms | STT ms | E2E ms | Audio KB | WER % | Transcript head |")
    print("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in results:
        if "error" in r:
            print(f"| {r['path']} | {r['lang']} | {r['size']} | – | – | ERROR | – | – | – | – | {r['error']} |")
        else:
            print(f"| {r['path']} | {r['lang']} | {r['size']} | {r['chars']} | {r['words']} | "
                  f"{r['tts_ms']} | {r['stt_ms']} | {r['e2e_ms']} | {r['audio_kb']} | "
                  f"{r['wer_pct']} | {r['hyp_head']} |")

    print(json.dumps(results, ensure_ascii=False, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
