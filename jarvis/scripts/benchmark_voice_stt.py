"""Compare the same local recording on CPU and CUDA without changing live settings."""
from __future__ import annotations

import gc
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from faster_whisper import decode_audio
from gpu_priority import voice_priority
from transcriber import WhisperModel


def main():
    audio = decode_audio(str(ROOT / "logs/f5-ptbr-question-final.wav"), sampling_rate=16000)
    rows = []
    for device, precision in (("cpu", "int8"), ("cuda", "int8_float16")):
        with voice_priority(kind="stt"):
            started = time.perf_counter()
            model = WhisperModel("small", device=device, compute_type=precision,
                                 cpu_threads=2, num_workers=1, local_files_only=True)
            load_s = time.perf_counter() - started
            runs = []
            for _ in range(3):
                started = time.perf_counter()
                segments, _ = model.transcribe(audio, language="pt", beam_size=3,
                                               vad_filter=True, condition_on_previous_text=False)
                text = " ".join(s.text.strip() for s in segments if s.avg_logprob >= -1
                                and s.no_speech_prob < .6 and s.compression_ratio <= 2.4)
                runs.append({"elapsed_s": round(time.perf_counter() - started, 3), "text": text})
            row = {"device": device, "compute_type": precision, "load_s": round(load_s, 3),
                   "warm_median_s": statistics.median(r["elapsed_s"] for r in runs[1:]), "runs": runs}
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "runs"}), flush=True)
            del model
            gc.collect()
    result = {"sample": "logs/f5-ptbr-question-final.wav", "audio_s": len(audio) / 16000,
              "same_transcript": len({r["text"] for row in rows for r in row["runs"]}) == 1,
              "results": rows}
    target = ROOT / "logs/voice-latency/stt-gpu-20261020.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"same_transcript": result["same_transcript"], "report": str(target)}), flush=True)


if __name__ == "__main__":
    main()
