"""Isolated Whisper/F5 worker. stdout is exclusively the private JSONL protocol."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / (".venv-voice" if sys.argv[1] == "tts" else ".venv311") / "Lib/site-packages"
# The packaged Hermes uses Python 3.14 and may inject its site-packages through PYTHONPATH.
# These ML workers use Python 3.11. Never import binary extensions from the parent's runtime.
sys.path[:] = [str(ROOT), str(SITE)] + [path for path in sys.path if "site-packages" not in path.casefold()]


class Speech:
    def __init__(self, kind: str):
        self.kind = kind
        self.model = None
        self.warmed = False

    def load(self):
        if self.model is not None:
            return
        from local_voice import settings
        config = settings()
        if self.kind == "stt":
            from local_voice import recognition_device
            device, precision = recognition_device(config)
            if device == "cuda":
                # Reuse runtime DLL setup from the existing meeting transcriber.
                from transcriber import WhisperModel
            else:
                from faster_whisper import WhisperModel
            self.model = WhisperModel(config.get("stt_model", "small"), device=device,
                                      compute_type=precision, cpu_threads=2, num_workers=1)
        else:
            import f5_tts.api as f5_api
            import f5_tts.infer.utils_infer as f5_utils
            import torch
            from f5_tts.api import F5TTS
            torch.set_num_threads(2)
            # F5 constructs FP32 weights on CUDA before converting them, doubling peak VRAM.
            # Scope FP16 construction to its model loader; the vocoder keeps its required FP32.
            load_model = f5_api.load_model
            load_checkpoint = f5_utils.load_checkpoint
            def load_half(*args, **kwargs):
                original = torch.get_default_dtype()
                try:
                    if config.get("tts_device", "cuda").startswith("cuda"):
                        torch.set_default_dtype(torch.float16)
                    # Construct directly on the inference device: no temporary 675 MB CPU model.
                    with torch.device(config.get("tts_device", "cuda")):
                        return load_model(*args, **kwargs)
                finally:
                    torch.set_default_dtype(original)
            def load_streamed(model, ckpt_path, device, dtype=None, use_ema=True):
                # safetensors' CUDA bulk loader exhausts Windows commit memory here.
                # Copy one CPU-mapped tensor at a time into the existing CUDA parameters.
                from safetensors import safe_open
                targets = model.state_dict()
                seen = set()
                ignored = {"initted", "step", "mel_spec.mel_stft.mel_scale.fb",
                           "mel_spec.mel_stft.spectrogram.window"}
                with safe_open(ckpt_path, framework="pt", device="cpu") as checkpoint, torch.no_grad():
                    for key in checkpoint.keys():  # noqa: SIM118 -- safe_open is not iterable
                        name = key.removeprefix("ema_model.") if use_ema else key
                        if name in ignored:
                            continue
                        if name not in targets or targets[name].shape != checkpoint.get_tensor(key).shape:
                            raise ValueError("Checkpoint F5 incompatível")
                        targets[name].copy_(checkpoint.get_tensor(key))
                        seen.add(name)
                if seen != set(targets):
                    raise ValueError("Checkpoint F5 incompleto")
                return model
            f5_api.load_model = load_half
            f5_utils.load_checkpoint = load_streamed
            try:
                self.model = F5TTS(model="F5TTS_v1_Base", device=config.get("tts_device", "cuda"),
                                   ckpt_file=config["checkpoint"], vocab_file=config.get("vocab", ""),
                                   vocoder_local_path=config["vocoder"])
            finally:
                f5_api.load_model = load_model
                f5_utils.load_checkpoint = load_checkpoint
            # F5 samples inside its own thread pool, where the caller's autocast is not inherited.
            if config.get("tts_device", "cuda").startswith("cuda"):
                self.model.ema_model.sample = torch.autocast("cuda", dtype=torch.float16)(self.model.ema_model.sample)
        self.config = config

    def run(self, body: dict) -> dict:
        self.load()
        if body.get("op") == "warmup":
            if not self.warmed:
                if self.kind == "tts":
                    # Exercise reference preprocessing and CUDA kernels without publishing audio.
                    self.run({"text": self.config["reference_text"]})
                else:
                    import numpy as np
                    segments, _ = self.model.transcribe(np.zeros(16000, dtype=np.float32),
                        language="pt", beam_size=3, vad_filter=False, condition_on_previous_text=False)
                    list(segments)  # Internal silence only; never submit a transcript to Hermes.
                self.warmed = True
            return self.run({"op": "prepare"})
        if body.get("op") == "prepare":
            device = self.model.model.device if self.kind == "stt" else self.config.get("tts_device", "cuda")
            return {"ok": True, "engine": self.kind, "device": device}
        import numpy as np
        if self.kind == "stt":
            data = body.get("pcm", "")
            if not isinstance(data, str) or len(data) > 2000000:
                raise ValueError("Áudio excede o limite")
            pcm = base64.b64decode(data, validate=True)
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            segments, _ = self.model.transcribe(samples, language="pt", beam_size=3,
                                                 vad_filter=True, condition_on_previous_text=False)
            from transcript_hygiene import hallucination_reason
            text = " ".join(s.text.strip() for s in segments if s.avg_logprob >= -1
                            and s.no_speech_prob < .6 and s.compression_ratio <= 2.4)
            return {"ok": True, "text": "" if hallucination_reason(text) else text}
        text = body.get("text", "")
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError("Texto de síntese inválido")
        from local_voice import synthesis_steps
        steps = synthesis_steps(self.config, body)
        import torch
        from scipy.signal import resample_poly
        with contextlib.redirect_stdout(io.StringIO()), torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16,
                enabled=self.config.get("tts_device", "cuda").startswith("cuda")):
            wav, rate, _ = self.model.infer(
                ref_file=self.config["reference_audio"], ref_text=self.config["reference_text"],
                gen_text=text, nfe_step=steps, seed=42, show_info=lambda *_: None, progress=None)
        if rate != 24000:
            import math
            divisor = math.gcd(rate, 24000)
            wav = resample_poly(wav, 24000 // divisor, rate // divisor)
        pcm = (np.clip(wav, -1, 1) * 32767).astype("<i2").tobytes()
        return {"ok": True, "pcm": base64.b64encode(pcm).decode("ascii"), "rate": 24000}


def main():
    speech = Speech(sys.argv[1])
    for line in sys.stdin:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                from gpu_priority import voice_priority
                priority = voice_priority(kind=speech.kind)
                with priority:
                    result = speech.run(json.loads(line))
        except Exception as exc:  # noqa: BLE001 -- process boundary must return a protocol error
            # Details stay in the local log; never include credentials or traceback in UI responses.
            import traceback
            traceback.print_exc(file=sys.stderr)
            result = {"ok": False, "error": f"Falha no motor {speech.kind}: {type(exc).__name__}. Consulte o log local."}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
