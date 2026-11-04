"""Local synthetic A/B: compare F5 steps with warmed workers and intelligibility checks."""
from __future__ import annotations

import base64
import difflib
import json
import statistics
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_voice import stt, tts
from local_voice_phrases import normalized

TEXTS = [
    "Este é um teste da voz do Jarvis. A resposta precisa ser clara, natural e objetiva, preservando todas as informações.",
    "Não envie o documento. Aguarde minha confirmação antes de executar qualquer ação. A reunião começa amanhã, às nove horas.",
]


def benchmark():
    folder = ROOT / "logs/voice-latency"
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        started = time.perf_counter()
        tts.call({"op": "prepare"})
        prepare_s = time.perf_counter() - started
        stt.call({"op": "prepare"})
        # Warm CUDA kernels/reference preprocessing once, outside the timed comparison.
        tts.call({"text": TEXTS[0], "steps": 32})
        for repeat in range(2):
            for index, text in enumerate(TEXTS):
                for steps in ([32, 16] if repeat == 0 else [16, 32]):
                    started = time.perf_counter()
                    result = tts.call({"text": text, "steps": steps})
                    elapsed = time.perf_counter() - started
                    pcm = base64.b64decode(result["pcm"], validate=True)
                    path = folder / f"steps-{steps}-text-{index}-run-{repeat}.wav"
                    with wave.open(str(path), "wb") as audio:
                        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                        audio.writeframes(pcm)
                    # STT expects 16 kHz; conversion is outside the timed synthesis.
                    import numpy as np
                    from scipy.signal import resample_poly
                    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
                    pcm16 = np.clip(resample_poly(samples, 2, 3), -32768, 32767).astype("<i2").tobytes()
                    heard = stt.call({"pcm": base64.b64encode(pcm16).decode()})["text"]
                    expected_words, heard_words = normalized(text).split(), normalized(heard).split()
                    row = {"steps": steps, "text_index": index, "repeat": repeat,
                           "synthesis_s": round(elapsed, 3), "audio_s": round(len(pcm) / 48000, 3),
                           "similarity": round(difflib.SequenceMatcher(None, expected_words, heard_words).ratio(), 4),
                           "critical_words_ok": index != 1 or all(word in heard_words for word in
                               ("nao", "aguarde", "confirmacao", "amanha", "nove")),
                           "text": text, "recognized": heard}
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=True), flush=True)
        medians = {str(step): statistics.median(row["synthesis_s"] for row in rows if row["steps"] == step)
                   for step in (16, 32)}
        report = {"prepare_s": round(prepare_s, 3), "median_s": medians,
                  "faster_percent": round((1 - medians["16"] / medians["32"]) * 100, 1), "rows": rows}
        (folder / "benchmark.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "rows"}), flush=True)
    finally:
        tts.close()
        stt.close()


if __name__ == "__main__":
    benchmark()
