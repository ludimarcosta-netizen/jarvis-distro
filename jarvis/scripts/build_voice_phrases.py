"""Recover Jarvis recordings and synthesize missing short phrases once, locally."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_voice import tts
from local_voice_phrases import Bank, normalized

GENERATED = [
    ("um-momento", "Um momento, senhor.", True),
    ("pedido-recebido", "Pedido recebido, senhor.", True),
    ("a-disposicao", "À disposição, senhor.", False),
    ("estou-ouvindo", "Estou ouvindo, senhor.", False),
    ("ate-a-proxima", "Até a próxima, senhor.", False),
]


def synthesize_short(text: str, recognizer) -> bytes:
    # F5 can truncate a single short utterance. Give it context, then extract a verified occurrence.
    import numpy as np
    result = tts.call({"text": " ".join([text] * 3)})
    if result.get("rate") != 24000:
        raise ValueError("Taxa de síntese inesperada")
    pcm = base64.b64decode(result["pcm"], validate=True)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    # Whisper expects 16 kHz floats; the resampling happens only during preparation.
    from scipy.signal import resample_poly
    segments, _ = recognizer.transcribe(resample_poly(samples, 2, 3), language="pt", beam_size=5,
                                        vad_filter=False, word_timestamps=True, condition_on_previous_text=False)
    words = [word for segment in segments for word in segment.words]
    expected = normalized(text).split()
    for index in range(len(words) - len(expected) + 1):
        end = index + len(expected)
        if [normalized(word.word) for word in words[index:end]] != expected:
            continue
        start_s = max(0, words[index].start - .08)
        end_s = words[end - 1].end + .12
        if end < len(words):
            end_s = min(end_s, (words[end - 1].end + words[end].start) / 2)
        crop = pcm[int(start_s * 24000) * 2:int(end_s * 24000) * 2]
        audio = np.frombuffer(crop, dtype="<i2").astype(np.float32) / 32768
        check, _ = recognizer.transcribe(resample_poly(audio, 2, 3), language="pt", beam_size=5,
                                         vad_filter=False, condition_on_previous_text=False)
        heard = " ".join(s.text.strip() for s in check if s.avg_logprob >= -1 and s.no_speech_prob < .6)
        if normalized(heard) == normalized(text):
            return crop
    raise ValueError(f"Frase não passou na verificação de áudio: {text}")


def build(downloads: Path, regenerate: bool = False):
    target = ROOT / "voice_phrases"
    archive = target / "recovered"
    archive.mkdir(parents=True, exist_ok=True)
    originals = sorted(p for p in downloads.glob("*.mp3") if p.name.startswith("[Jarvis (UCM)"))
    recovered = []
    seen = set()
    for source in originals:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest not in seen:
            shutil.copy2(source, archive / source.name)
            seen.add(digest)
            recovered.append({"file": "recovered/" + source.name, "sha256": digest})
    clips = []
    for slug, text, prefix in [("certo", "Certo.", "certo"),
                                ("deixa-eu-ver", "Deixa eu ver aqui.", "Deixa")]:
        source = next((p for p in originals if p.name.split("]", 1)[1].startswith(prefix)), None)
        if source is None:
            raise FileNotFoundError(f"Gravação antiga ausente: {text}")
        output = target / (slug + ".wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source),
                        "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(output)], check=True)
        clips.append({"file": output.name, "text": text, "bridge": True,
                      "source": source.name, "origin": "recovered"})
    try:
        recognizer = None
        for slug, text, bridge in GENERATED:
            output = target / (slug + ".wav")
            # Existing WAVs are verified below; don't regenerate on every install/start.
            if regenerate or not output.exists():
                if recognizer is None:
                    from faster_whisper import WhisperModel
                    recognizer = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=2)
                pcm = synthesize_short(text, recognizer)
                with wave.open(str(output), "wb") as audio:
                    audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    audio.writeframes(pcm)
            clips.append({"file": output.name, "text": text, "bridge": bridge,
                          "origin": "local-f5-tts", "reference": "models/voice/reference-online-7s.wav",
                          "validation": "local-whisper-small"})
            print(text, flush=True)
    finally:
        tts.close()
    manifest = {"version": 1, "clips": clips, "recovered_originals": recovered}
    path = target / "manifest.json"
    previous = path.read_bytes() if path.exists() else None
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        Bank(target).load()
    except (OSError, ValueError, KeyError, TypeError, wave.Error):
        if previous is not None:
            path.write_bytes(previous)
        else:
            path.unlink()
        raise
    print(json.dumps({"clips": len(clips), "originals": len(recovered)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", required=True, type=Path)
    parser.add_argument("--regenerate", action="store_true")
    args = parser.parse_args()
    build(args.downloads, args.regenerate)
