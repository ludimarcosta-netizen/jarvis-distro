import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gpu_priority  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_gpu_signal(tmp_path, monkeypatch):
    """Nenhum teste escreve no sinal de GPU do Jarvis que está rodando de verdade."""
    monkeypatch.setattr(gpu_priority, "BUSY_FILE", tmp_path / ".gpu_live_busy")


_cleanup = tempfile.TemporaryDirectory.cleanup


def _cleanup_with_retry(self, timeout=5.0):
    """O antivírus (Kaspersky) abre os .db recém-gravados no %TEMP%; a exclusão fica
    pendente e o rmdir falha com WinError 145 ou 32. Repete até o arquivo ser solto;
    um handle que o próprio teste esqueceu aberto continua falhando após o prazo."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _cleanup(self)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


@pytest.fixture(autouse=True)
def tolerant_tempdir_cleanup(monkeypatch):
    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", _cleanup_with_retry)
