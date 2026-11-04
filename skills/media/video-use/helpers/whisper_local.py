"""Local Whisper (faster-whisper on the GPU) producing Scribe-shaped JSON.

Jarvis install (Windows): used by transcribe.py when no ELEVENLABS_API_KEY is set.
It runs under the Jarvis venv, which already has faster-whisper + CUDA 12 DLLs,
so this skill's own venv stays small.

Output matches the subset of the Scribe response the helpers read:
    {"language_code", "text", "words": [{"text","start","end","type","speaker_id"}]}
with "spacing" entries between words carrying the gaps. Whisper has no
diarization (every word is speaker_0) and tends to drop fillers.

Usage:
    <jarvis-python> helpers/whisper_local.py <audio.wav> <out.json> [--language pt] [--model large-v3-turbo]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def configure_cuda_runtime() -> None:
    """Expose the CUDA 12 runtime DLLs bundled in the running venv to CTranslate2."""
    if os.name != "nt":
        return
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    for relative in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
        dll_dir = site_packages / relative
        if dll_dir.is_dir():
            os.add_dll_directory(str(dll_dir))
            os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")


def to_scribe_words(segments) -> list[dict]:
    """Whisper segments (with .words) → Scribe word/spacing entries."""
    out: list[dict] = []
    prev_end: float | None = None
    for seg in segments:
        for w in seg.words or []:
            text = (w.word or "").strip()
            if not text:
                continue
            start, end = round(float(w.start), 3), round(float(w.end), 3)
            if prev_end is not None and start > prev_end:
                out.append({"text": " ", "start": prev_end, "end": start, "type": "spacing",
                            "speaker_id": "speaker_0"})
            out.append({"text": text, "start": start, "end": end, "type": "word",
                        "speaker_id": "speaker_0", "logprob": round(float(w.probability), 4)})
            prev_end = end
    return out


def transcribe(audio: Path, language: str | None, model_name: str) -> dict:
    configure_cuda_runtime()
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(model_name, device="cuda", compute_type="float16")
    except Exception as exc:  # GPU busy/unavailable: slower, but still local
        print(f"  cuda unavailable ({exc}); falling back to CPU", file=sys.stderr)
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(audio),
        language=language,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
    )
    words = to_scribe_words(list(segments))
    return {
        "language_code": info.language,
        "language_probability": round(float(info.language_probability), 4),
        "text": " ".join(w["text"] for w in words if w["type"] == "word"),
        "words": words,
        "engine": f"faster-whisper/{model_name}",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Local Whisper → Scribe-shaped JSON")
    ap.add_argument("audio", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--language", default=None)
    ap.add_argument("--model", default="large-v3-turbo")
    args = ap.parse_args()
    payload = transcribe(args.audio, args.language, args.model)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
