"""Prioridade da GPU entre processos: a transcrição ao vivo sempre passa na frente da passada offline.

A GPU (RTX 2070 Max-Q) é uma só. Sem regra, a passada offline (large-v3, beam 5) ocupava a GPU
inteira, a fala ao vivo levava 20-35 s por trecho, a fila enchia e trechos eram descartados
(09/10/2026). Agora o motor ao vivo marca num arquivo até quando está ocupado, e a passada offline,
que roda noutro processo, espera esse prazo vencer antes de codificar cada janela de ~30 s de áudio.
O prazo expira sozinho: se o processo ao vivo cair, a offline volta a andar em no máximo BUSY_S.
"""
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

BUSY_FILE = Path(__file__).resolve().parent / ".gpu_live_busy"
TTS_BUSY_FILE = Path(__file__).resolve().parent / ".gpu_tts_busy"
STT_BUSY_FILE = Path(__file__).resolve().parent / ".gpu_stt_busy"
BUSY_S = 30.0   # um trecho na fila ou em transcrição; renovado a cada trecho
POLL_S = 0.25
OFFLINE_REST_FRACTION = 0.1
OFFLINE_MAX_REST_S = 1.0


def _write(until: float, path: Optional[Path]) -> None:
    # Escrita direta, sem replace: no Windows o replace falha enquanto o outro processo lê o arquivo.
    # O sinal é só um aviso; uma escrita perdida nunca pode derrubar a captura de áudio ao vivo.
    try:
        (path or BUSY_FILE).write_text(f"{until:.3f}", encoding="ascii")
    except OSError:
        pass


def mark_busy(path: Optional[Path] = None) -> None:
    """Fala ao vivo na fila ou sendo transcrita."""
    _write(time.time() + BUSY_S, path)


def mark_idle(path: Optional[Path] = None) -> None:
    """Fila ao vivo vazia: a passada offline pode usar a GPU."""
    _write(0.0, path)


def busy_until(path: Optional[Path] = None) -> float:
    latest = 0.0
    for signal in (path,) if path else (BUSY_FILE, TTS_BUSY_FILE, STT_BUSY_FILE):
        try:
            latest = max(latest, float(signal.read_text(encoding="ascii")))
        except (OSError, ValueError):
            continue
    return latest


@contextmanager
def voice_priority(kind: str = "tts"):
    """Keep interactive speech ahead of offline work; STT/TTS have independent leases."""
    if kind not in {"stt", "tts"}:
        raise ValueError("Motor de voz inválido")
    signal = STT_BUSY_FILE if kind == "stt" else TTS_BUSY_FILE
    stop = threading.Event()

    def refresh():
        until = time.time() + BUSY_S
        _write(until, signal)
        _write(until, BUSY_FILE)  # compatibility with an offline worker started before this upgrade

    def heartbeat():
        while not stop.wait(2):
            refresh()

    refresh()
    thread = threading.Thread(target=heartbeat, daemon=True, name=f"{kind}-gpu-priority")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=3)
        _write(0.0, signal)


def wait_for_live_idle(path: Optional[Path] = None, sleep=time.sleep) -> None:
    while time.time() < busy_until(path):
        sleep(POLL_S)


def yield_to_live(model: Any, sleep=time.sleep) -> Any:
    """Faz o WhisperModel esperar a fila ao vivo esvaziar antes de codificar cada janela de ~30 s.

    O ponto é o encode, não o laço de segmentos: janela sem fala não gera segmento e o gerador
    do faster-whisper atravessa silêncios longos sem devolver nada (a GPU ficava presa nele)."""
    encode = model.encode

    def encode_after_live(features, *args, **kwargs):
        wait_for_live_idle(sleep=sleep)
        return encode(features, *args, **kwargs)

    model.encode = encode_after_live
    return model


def pace_offline(model: Any, sleep=time.sleep, clock=time.monotonic) -> Any:
    """Pausa breve entre operações; mantém a prioridade da fala ao vivo.

    Encoder e decoder continuam com os mesmos parâmetros. A pausa não cresce
    indefinidamente com operações longas nem dobra o tempo da revisão.
    """
    def paced(operation):
        def run(*args, **kwargs):
            wait_for_live_idle(sleep=sleep)
            started = clock()
            result = operation(*args, **kwargs)
            elapsed = max(0.0, clock() - started)
            if elapsed:
                sleep(min(elapsed * OFFLINE_REST_FRACTION, OFFLINE_MAX_REST_S))
            return result
        return run

    model.encode = paced(model.encode)
    model.generate_with_fallback = paced(model.generate_with_fallback)
    return model
