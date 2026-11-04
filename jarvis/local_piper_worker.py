"""Isolated Piper synthesis using the local voice worker JSONL protocol."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    import numpy as np
    from local_voice import settings
    from piper import PiperVoice
    from scipy.signal import resample_poly

    model_path = Path(settings()["piper_model"])
    if not model_path.is_file():
        raise FileNotFoundError("Modelo Piper não instalado")
    voice = PiperVoice.load(model_path)

    for line in sys.stdin:
        try:
            body = json.loads(line)
            if body.get("op") in {"prepare", "warmup"}:
                if body["op"] == "warmup":
                    list(voice.synthesize("Olá, senhor."))
                result = {"ok": True, "engine": "tts", "device": "cpu"}
            else:
                text = body.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 300:
                    raise ValueError("Texto de síntese inválido")
                chunks = list(voice.synthesize(text))
                if not chunks:
                    raise RuntimeError("Síntese Piper vazia")
                pcm = np.concatenate([np.frombuffer(chunk.audio_int16_bytes, dtype="<i2") for chunk in chunks])
                rate = chunks[0].sample_rate
                if rate != 24000:
                    import math

                    divisor = math.gcd(rate, 24000)
                    pcm = resample_poly(pcm, 24000 // divisor, rate // divisor)
                pcm = np.clip(pcm, -32768, 32767).astype("<i2").tobytes()
                result = {"ok": True, "pcm": base64.b64encode(pcm).decode("ascii"), "rate": 24000}
        except Exception as exc:  # noqa: BLE001 -- process boundary returns a generic error
            print(f"Piper: {type(exc).__name__}", file=sys.stderr)
            result = {"ok": False, "error": "Falha no motor Piper. Consulte local-tts.log."}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
