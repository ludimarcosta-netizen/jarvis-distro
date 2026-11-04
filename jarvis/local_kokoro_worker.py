"""Offline Kokoro synthesis using the local voice worker JSONL protocol."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    import numpy as np
    import torch
    from gpu_priority import voice_priority
    from kokoro import KModel, KPipeline
    from local_voice import settings

    config_values = settings()
    model_dir = Path(config_values["kokoro_model_dir"])
    config = model_dir / "config.json"
    weights = model_dir / "kokoro-v1_0.pth"
    voice = model_dir / "pm_santa.pt"
    if not all(path.is_file() for path in (config, weights, voice)):
        raise FileNotFoundError("Modelo Kokoro Santa não instalado")
    requested_device = config_values.get("kokoro_device", "cpu")
    if requested_device not in {"cpu", "cuda"}:
        raise ValueError("Dispositivo Kokoro inválido")
    device = "cuda" if requested_device == "cuda" and torch.cuda.is_available() else "cpu"
    with contextlib.redirect_stdout(io.StringIO()):
        model = KModel(repo_id="hexgrad/Kokoro-82M", config=str(config), model=str(weights)).to(device).eval()
        pipeline = KPipeline(lang_code="p", repo_id="hexgrad/Kokoro-82M", model=model)

    for line in sys.stdin:
        try:
            body = json.loads(line)
            if body.get("op") in {"prepare", "warmup"}:
                if body["op"] == "warmup":
                    priority = voice_priority() if device == "cuda" else contextlib.nullcontext()
                    with priority, contextlib.redirect_stdout(io.StringIO()), torch.inference_mode():
                        list(pipeline("Olá, senhor.", voice=str(voice)))
                result = {"ok": True, "engine": "tts", "device": device}
            else:
                text = body.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 300:
                    raise ValueError("Texto de síntese inválido")
                started = time.perf_counter()
                priority = voice_priority() if device == "cuda" else contextlib.nullcontext()
                with priority, contextlib.redirect_stdout(io.StringIO()), torch.inference_mode():
                    chunks = [item.audio.detach().cpu().numpy() for item in pipeline(text, voice=str(voice))]
                if not chunks:
                    raise RuntimeError("Síntese Kokoro vazia")
                pcm = (np.clip(np.concatenate(chunks), -1, 1) * 32767).astype("<i2").tobytes()
                result = {"ok": True, "pcm": base64.b64encode(pcm).decode("ascii"), "rate": 24000,
                          "synth_s": round(time.perf_counter() - started, 3)}
        except Exception as exc:  # noqa: BLE001 -- process boundary returns a generic error
            print(f"Kokoro: {type(exc).__name__}", file=sys.stderr)
            result = {"ok": False, "error": "Falha no motor Kokoro. Consulte local-tts.log."}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
