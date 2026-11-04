"""Gravação atômica e locks cooperativos entre os processos locais das IAs."""
import hashlib
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_FILE = "notas.lock"  # um arquivo só em <cofre>/_indice/; cada nota trava a sua faixa de 1 byte


def lock_slot(path: Path) -> tuple[Path, int]:
    """Arquivo de lock e posição do byte travado. Dentro do cofre (pasta com `_indice/`), todas as notas
    compartilham `_indice/notas.lock`; fora dele (estado do Jarvis), mantém `<arquivo>.lock` ao lado."""
    resolved = path.resolve()
    for parent in resolved.parents:
        if (parent / "_indice").is_dir():
            key = resolved.relative_to(parent).as_posix().casefold()
            offset = 1 + int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % (1 << 30)
            return parent / "_indice" / LOCK_FILE, offset
    return path.with_name(path.name + ".lock"), 0


def _try_lock(handle, offset: int) -> None:
    handle.seek(offset)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1, offset)


def _unlock(handle, offset: int) -> None:
    handle.seek(offset)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.lockf(handle.fileno(), fcntl.LOCK_UN, 1, offset)


@contextmanager
def file_lock(path: Path, timeout: float = 15) -> Iterator[None]:
    lock_path, offset = lock_slot(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _try_lock(handle, offset)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Memória ocupada por outro processo") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle, offset)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".cerebro-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_if_changed(path: Path, text: str) -> bool:
    """Grava só quando o conteúdo mudou (evita mtime novo e reindexação à toa)."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    atomic_write(path, text)
    return True
