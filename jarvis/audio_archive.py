"""
Gravação contínua sem perdas do áudio captado, para revisão e recuperação.

Blocos de 10 min em FLAC, um arquivo por fonte (mic / loopback). Retenção de
72 h, preservando reuniões abertas, pendentes, com falha ou revisão não confirmada.
Arquivos Opus antigos continuam legíveis. Nomes usam a hora local de abertura.

A gravação nunca pode atrapalhar a captura: write() só enfileira, a codificação
roda numa thread própria e qualquer falha de disco/codec vira log e novo bloco.
Intervalos sem quadros (loopback fica mudo quando o PC não toca nada) viram
silêncio, para a posição no arquivo continuar batendo com o relógio.
"""
import calendar
import os
import queue
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from weakref import WeakSet

import av
import numpy as np

ARCHIVE_DIR = Path(__file__).resolve().parent / "audio_archive"
BLOCK_S = 600.0
RETENTION_S = 259200.0  # 72 h: a passada offline das reuniões precisa do áudio
MAX_GAP_FILL_S = BLOCK_S  # nunca preenche mais que um bloco de silêncio de uma vez
_GAP_TOLERANCE_S = 0.5
_recorders: WeakSet = WeakSet()


def block_start(path: Path) -> float:
    pattern = '%Y%m%d-%H%M%S.%f' if '.' in path.stem else '%Y%m%d-%H%M%S'
    return datetime.strptime(path.stem, pattern).astimezone().timestamp()


def flush_recordings() -> None:
    """Fecha os blocos atuais na thread do gravador antes de a revisão lê-los."""
    for recorder in list(_recorders):
        if recorder._thread and recorder._thread.is_alive():
            event = threading.Event()
            recorder._queue.put(event, timeout=5)
            if not event.wait(timeout=5):
                raise RuntimeError("Gravador não confirmou o fechamento do áudio")


def protected_windows(db_path: Path | None = None) -> list[tuple[float, float]] | None:
    """Consulta só leitura; banco indisponível suspende limpeza, nunca arrisca o áudio."""
    path = db_path or Path(os.environ.get("JARVIS_TRANSCRIPTS_DB") or Path(__file__).with_name("transcripts.db"))
    if not path.exists():
        return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(meeting_utterances)")}
            review_condition = ("OR EXISTS (SELECT 1 FROM meeting_utterances u WHERE u.meeting_id = m.id "
                                "AND json_extract(u.review, '$.status') = 'pending')") if "review" in columns else ""
            rows = conn.execute(f"SELECT started_at, ended_at FROM meetings m WHERE status = 'open' "
                                f"OR offline_status IN ('pending', 'running', 'error') {review_condition}").fetchall()
        windows = []
        for start, end in rows:
            a = float(calendar.timegm(time.strptime(start[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S'))) - 60
            b = (float(calendar.timegm(time.strptime(end[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S'))) + 15
                 if end else float('inf'))
            windows.append((a, b))
        return windows
    except (sqlite3.Error, ValueError, TypeError):
        print("[!] Limpeza de áudio adiada: não foi possível conferir reuniões protegidas.", file=sys.stderr)
        return None


def purge(root: Path = ARCHIVE_DIR, max_age_s: float = RETENTION_S, now: Optional[float] = None,
          keep: Optional[Path] = None, db_path: Path | None = None) -> int:
    """Apaga os blocos mais antigos que max_age_s (pela última escrita). Devolve quantos."""
    cutoff = (now if now is not None else time.time()) - max_age_s
    removed = 0
    windows = protected_windows(db_path)
    if windows is None:
        return 0
    for path in (*root.glob("*/*.ogg"), *root.glob("*/*.flac")):
        try:
            try:
                start = block_start(path)
                if any(start < b and start + BLOCK_S + 30 > a for a, b in windows):
                    continue
            except ValueError:
                pass  # arquivo antigo fora do padrão não é bloco de uma reunião
            if path != keep and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            print(f"[!] Arquivo de áudio {path.name} não pôde ser apagado: {exc}", file=sys.stderr)
    return removed


class ContinuousRecorder:
    def __init__(self, source: str, sample_rate: int = 16000, root: Path = ARCHIVE_DIR,
                 block_s: float = BLOCK_S, retention_s: float = RETENTION_S,
                 clock: Callable[[], float] = time.time):
        self.source = source
        self.sample_rate = sample_rate
        self.root = Path(root)
        self.block_s = block_s
        self.retention_s = retention_s
        self.clock = clock
        self._queue: "queue.Queue" = queue.Queue(maxsize=4000)  # ~2 min de quadros de 30 ms
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0
        self._container = None
        self._stream = None
        self._path: Optional[Path] = None
        self._block_end = 0.0
        self._t0 = 0.0
        self._written = 0
        _recorders.add(self)

    def write(self, frame: np.ndarray) -> None:
        """Chamado pela captura a cada quadro (float32 mono na taxa da captura)."""
        try:
            self._queue.put_nowait((self.clock(), frame))
        except queue.Full:
            self._dropped += 1

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name=f"audio-archive-{self.source}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        purge(self.root, self.retention_s, now=self.clock())
        while True:
            item = self._queue.get()
            if item is None:
                break
            if isinstance(item, threading.Event):
                self._close()
                item.set()
                continue
            t, frame = item
            try:
                self._handle(t, frame)
            except Exception as exc:
                print(f"[!] Gravação de áudio ({self.source}) falhou: {exc}; abrindo novo bloco.", file=sys.stderr)
                self._close()
        self._close()
        if self._dropped:
            print(f"[!] Gravação de áudio ({self.source}): {self._dropped} quadros descartados (fila cheia).",
                  file=sys.stderr)

    def _handle(self, t: float, frame: np.ndarray) -> None:
        if self._container is None or t >= self._block_end:
            self._close()
            self._open(t - len(frame) / self.sample_rate, t)
        gap = (t - self._t0) - (self._written + len(frame)) / self.sample_rate
        if gap > _GAP_TOLERANCE_S:
            self._encode(np.zeros(int(min(gap, MAX_GAP_FILL_S) * self.sample_rate), dtype=np.float32))
        self._encode(np.asarray(frame, dtype=np.float32))

    def _open(self, t0: float, t: float) -> None:
        folder = self.root / self.source
        folder.mkdir(parents=True, exist_ok=True)
        name_t = max(t0, t // self.block_s * self.block_s)  # bloco novo leva o nome da virada
        self._path = folder / f"{datetime.fromtimestamp(name_t).strftime('%Y%m%d-%H%M%S')}.flac"
        if self._path.exists():  # duas reuniões/reinícios no mesmo segundo não sobrescrevem áudio
            self._path = folder / f"{datetime.fromtimestamp(t0).astimezone().strftime('%Y%m%d-%H%M%S.%f')}.flac"
        self._container = av.open(str(self._path), mode="w", format="flac")
        self._stream = self._container.add_stream("flac", rate=self.sample_rate, layout="mono")
        self._t0 = t0
        self._written = 0
        # Vira no múltiplo de 10 min seguinte ao fim do quadro (t0 pode cair logo antes da virada).
        self._block_end = (t // self.block_s + 1) * self.block_s
        purge(self.root, self.retention_s, now=t0, keep=self._path)

    def _encode(self, samples: np.ndarray) -> None:
        if not len(samples):
            return
        frame = av.AudioFrame.from_ndarray(np.clip(samples, -1, 1)[None, :], format="flt", layout="mono")
        frame.sample_rate = self.sample_rate
        frame.pts = self._written
        self._written += len(samples)
        for packet in self._stream.encode(frame):
            self._container.mux(packet)

    def _close(self) -> None:
        container, self._container = self._container, None
        if container is None:
            return
        try:
            for packet in self._stream.encode(None):
                container.mux(packet)
        except Exception as exc:
            print(f"[!] Fechamento do bloco {self._path} incompleto: {exc}", file=sys.stderr)
        finally:
            try:
                container.close()
            except Exception:
                pass
            self._stream = None
