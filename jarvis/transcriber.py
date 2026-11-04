"""
Módulo de transcrição neural local com faster-whisper e aceleração CUDA na RTX 2070.
"""
import time
import os
from pathlib import Path
import numpy as np
from typing import Optional


def _configure_cuda_runtime():
    """Expose CUDA 12 runtime DLLs bundled in this venv to CTranslate2 on Windows."""
    if os.name != "nt":
        return
    site_packages = Path(__file__).resolve().parent / ".venv311" / "Lib" / "site-packages"
    for relative in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
        dll_dir = site_packages / relative
        if dll_dir.is_dir():
            os.add_dll_directory(str(dll_dir))
            os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")


_configure_cuda_runtime()

from faster_whisper import WhisperModel  # noqa: E402

from transcript_hygiene import hallucination_reason  # noqa: E402

DEFAULT_MODEL = "large-v3-turbo"  # medido em 30/09: ~metade dos erros do small, mesmo tempo na RTX 2070
HOTWORDS = "Jarvis __JARVIS_USER__ __JARVIS_CIDADE__"
# Segmento que o próprio Whisper marca como provável não-fala com baixa confiança,
# confiança muito baixa ou texto repetitivo é ruído transcrito, não fala.
NO_SPEECH_MAX = 0.6
LOGPROB_WITH_NO_SPEECH = -0.5
LOGPROB_MIN = -1.0
COMPRESSION_MAX = 2.4


def keep_segment(seg) -> bool:
    text = (seg.text or "").strip()
    if not text or hallucination_reason(text):
        return False
    if seg.no_speech_prob > NO_SPEECH_MAX and seg.avg_logprob < LOGPROB_WITH_NO_SPEECH:
        return False
    return seg.avg_logprob >= LOGPROB_MIN and seg.compression_ratio <= COMPRESSION_MAX

class LocalTranscriber:
    def __init__(self, model_size: str = DEFAULT_MODEL, device: str = "cuda", compute_type: str = "int8_float16"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        print(f"[*] Inicializando Faster-Whisper ({model_size}) no dispositivo {device} [{compute_type}]...")
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        print("[+] Modelo Faster-Whisper carregado com sucesso.")

    def transcribe_audio_chunk(self, audio_data: np.ndarray, sample_rate: int = 16000, language: Optional[str] = "pt") -> str:
        """
        Transcreve um array numpy 1D float32 a 16kHz.
        """
        if len(audio_data) == 0:
            return ""

        # Garante float32 normalizado entre -1.0 e 1.0
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)

        start_time = time.time()
        segments, info = self.model.transcribe(
            audio_data,
            beam_size=3,
            language=language,
            # hotwords reforça nomes próprios sem virar texto. O prompt inicial antigo era
            # devolvido pelo Whisper em ruído ("Transcrição fiel…", "J.A.R.V.I.S., __JARVIS_USER__…").
            hotwords=HOTWORDS,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            condition_on_previous_text=False
        )
        full_text = " ".join(seg.text.strip() for seg in segments if keep_segment(seg)).strip()
        elapsed = time.time() - start_time
        if full_text:
            print(f"[{info.language}] transcrição concluída em {elapsed:.2f}s")
        return full_text

if __name__ == "__main__":
    # Teste rápido de carregamento do modelo na GPU
    t = LocalTranscriber(model_size="tiny", device="cuda")
    dummy_audio = np.zeros(16000 * 2, dtype=np.float32) # 2 segundos de silêncio
    print("Teste com silêncio:", t.transcribe_audio_chunk(dummy_audio))
