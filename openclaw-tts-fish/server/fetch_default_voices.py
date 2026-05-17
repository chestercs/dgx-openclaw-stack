"""Fetch public-domain reference voices for Fish Audio S2 Pro voice cloning.

Two defaults are bundled so a fresh install can synthesize EN + HU out of the
box without the operator having to source a reference clip:

- default_en: LibriVox English narrator via the librispeech_asr dataset
  (public domain). Falls back to a hard-coded text marker if the dataset is
  unreachable at build time so the build doesn't fail outright.
- default_hu: Diana Majlinger reading "Egri csillagok" via
  KTH/hungarian-single-speaker-tts (public domain LibriVox source). Same
  Speaker the F5-TTS HU bundle used.

Idempotent: skips download when both <name>.wav + <name>.txt already exist,
so re-running on a volume that already has user-mounted voices is a no-op.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import soundfile as sf

OUT_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
TARGET_MIN_DURATION_S = 8.0
TARGET_MAX_DURATION_S = 14.0
TARGET_SAMPLE_RATE = 24000


def _log(msg: str) -> None:
    print(f"[fetch] {msg}", file=sys.stderr)


def _resample_if_needed(array, sr):
    if sr == TARGET_SAMPLE_RATE:
        return array, sr
    import librosa
    return librosa.resample(array, orig_sr=sr, target_sr=TARGET_SAMPLE_RATE), TARGET_SAMPLE_RATE


def _save(name: str, array, sr: int, transcript: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sf.write(str(OUT_DIR / f"{name}.wav"), array, sr, subtype="PCM_16")
    (OUT_DIR / f"{name}.txt").write_text(transcript.strip() + "\n", encoding="utf-8")
    _log(f"saved {name}.wav ({len(array) / sr:.2f}s @ {sr}Hz)")


def fetch_default_hu() -> bool:
    """KTH/hungarian-single-speaker-tts — Diana Majlinger, LibriVox PD."""
    wav = OUT_DIR / "default_hu.wav"
    txt = OUT_DIR / "default_hu.txt"
    if wav.exists() and txt.exists():
        _log("default_hu already present, skipping")
        return True
    try:
        from datasets import load_dataset
    except ImportError:
        _log("datasets package unavailable — cannot fetch default_hu")
        return False
    try:
        ds = load_dataset(
            "KTH/hungarian-single-speaker-tts", split="train", streaming=True
        )
    except Exception as e:
        _log(f"failed to open KTH/hungarian-single-speaker-tts: {e}")
        return False
    for sample in ds:
        audio = sample.get("audio")
        text = sample.get("sentence") or sample.get("text") or ""
        if not audio or not text:
            continue
        duration = len(audio["array"]) / audio["sampling_rate"]
        if TARGET_MIN_DURATION_S <= duration <= TARGET_MAX_DURATION_S and len(text) > 30:
            arr, sr = _resample_if_needed(audio["array"], audio["sampling_rate"])
            _save("default_hu", arr, sr, text)
            return True
    _log("no suitable HU sample found in target duration window")
    return False


def fetch_default_en() -> bool:
    """LibriSpeech / multilingual_librispeech English — LibriVox PD."""
    wav = OUT_DIR / "default_en.wav"
    txt = OUT_DIR / "default_en.txt"
    if wav.exists() and txt.exists():
        _log("default_en already present, skipping")
        return True
    try:
        from datasets import load_dataset
    except ImportError:
        _log("datasets package unavailable — cannot fetch default_en")
        return False
    candidates = [
        ("openslr/librispeech_asr", "clean", "train.100"),
        ("facebook/multilingual_librispeech", "english", "train"),
    ]
    for repo, config, split in candidates:
        try:
            _log(f"trying {repo} ({config}/{split}) ...")
            kwargs = {"split": split, "streaming": True}
            if config:
                kwargs["name"] = config
            ds = load_dataset(repo, **kwargs)
        except Exception as e:
            _log(f"  failed: {e}")
            continue
        for sample in ds:
            audio = sample.get("audio")
            text = (
                sample.get("text")
                or sample.get("transcript")
                or sample.get("sentence")
                or ""
            )
            if not audio or not text:
                continue
            duration = len(audio["array"]) / audio["sampling_rate"]
            if TARGET_MIN_DURATION_S <= duration <= TARGET_MAX_DURATION_S and len(text) > 30:
                arr, sr = _resample_if_needed(audio["array"], audio["sampling_rate"])
                _save("default_en", arr, sr, text)
                return True
    _log("no suitable EN sample found across candidate datasets")
    return False


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok_en = fetch_default_en()
    ok_hu = fetch_default_hu()
    # Soft-fail: a missing default voice is recoverable (operator can
    # `docker cp` one in later). Build progresses regardless.
    if not ok_en:
        _log("WARNING: default_en not bundled. Add one manually with docker cp.")
    if not ok_hu:
        _log("WARNING: default_hu not bundled. Add one manually with docker cp.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
