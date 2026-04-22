"""Fetch a public-domain Hungarian reference voice for F5-TTS cloning.

Source: KTH/hungarian-single-speaker-tts (HuggingFace dataset)
Speaker: Diana Majlinger, reading "Egri csillagok" by Geza Gardonyi
License: public domain (LibriVox source recording)

Diana Majlinger is also the speaker behind the CSS10 Hungarian corpus, which
was part of the F5-TTS Hungarian fine-tunes' training data. Using a sample of
her voice gives in-distribution cloning quality.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import soundfile as sf
from datasets import load_dataset

OUT_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
OUT_WAV = OUT_DIR / "default_hu.wav"
OUT_TXT = OUT_DIR / "default_hu.txt"
TARGET_MIN_DURATION_S = 8.0
TARGET_MAX_DURATION_S = 14.0
TARGET_SAMPLE_RATE = 24000


def main() -> int:
    if OUT_WAV.exists() and OUT_TXT.exists():
        print(f"[fetch] {OUT_WAV} already present, skipping", file=sys.stderr)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("[fetch] streaming KTH/hungarian-single-speaker-tts ...", file=sys.stderr)
    ds = load_dataset("KTH/hungarian-single-speaker-tts", split="train", streaming=True)

    chosen = None
    for sample in ds:
        audio = sample["audio"]
        text = sample.get("sentence") or sample.get("text") or ""
        duration = len(audio["array"]) / audio["sampling_rate"]
        if TARGET_MIN_DURATION_S <= duration <= TARGET_MAX_DURATION_S and len(text) > 30:
            chosen = (audio, text, duration)
            break

    if chosen is None:
        print("[fetch] no suitable sample found in target duration window", file=sys.stderr)
        return 1

    audio, text, duration = chosen
    array = audio["array"]
    sr = audio["sampling_rate"]

    if sr != TARGET_SAMPLE_RATE:
        import librosa
        array = librosa.resample(array, orig_sr=sr, target_sr=TARGET_SAMPLE_RATE)
        sr = TARGET_SAMPLE_RATE

    sf.write(str(OUT_WAV), array, sr, subtype="PCM_16")
    OUT_TXT.write_text(text.strip() + "\n", encoding="utf-8")

    print(
        f"[fetch] saved {OUT_WAV} ({duration:.2f}s @ {sr}Hz)\n"
        f"[fetch] transcript: {text.strip()!r}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
