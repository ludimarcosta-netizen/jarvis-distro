"""Comandos externos (hermes, git, uv, nvidia-smi), .env e ambientes Python. Trocável nos testes."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Saida:
    codigo: int
    texto: str = ""

    @property
    def ok(self) -> bool:
        return self.codigo == 0


class Executor:
    """Roda comandos. Interativo herda o terminal (o usuário vê e responde); os outros vão para o log.

    Nunca passe segredo como argumento: segredos vão direto para o .env por `gravar_env`."""

    def __init__(self, log: Path | None = None) -> None:
        self.log = log

    def rodar(self, args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
              interativo: bool = False, timeout: float | None = None, ocultar: int = 0) -> Saida:
        """`ocultar`: quantos argumentos do fim não vão para o log (ex.: código de autorização de uso único)."""
        visivel = list(map(str, args))[: len(args) - ocultar] + ["<oculto>"] * ocultar
        self._registrar(f"$ {' '.join(visivel)}")
        ambiente = {**os.environ, **(env or {})}
        try:
            if interativo:
                return Saida(subprocess.run(list(map(str, args)), cwd=cwd, env=ambiente, timeout=timeout).returncode)
            r = subprocess.run(list(map(str, args)), cwd=cwd, env=ambiente, timeout=timeout, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
        except FileNotFoundError as e:
            self._registrar(f"não encontrado: {e}")
            return Saida(127, str(e))
        except subprocess.TimeoutExpired:
            self._registrar("tempo esgotado")
            return Saida(124, "tempo esgotado")
        texto = (r.stdout or "") + (r.stderr or "")
        self._registrar(texto[-4000:])
        return Saida(r.returncode, texto)

    def _registrar(self, texto: str) -> None:
        if self.log:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {texto}\n")


def achar(home: Path, nome: str, extras: tuple[str, ...] = ()) -> str:
    """Executável do Hermes (bin/, tools/) ou do PATH."""
    for padrao in (f"bin/{nome}.exe", *extras):
        achados = sorted(home.glob(padrao))
        if achados:
            return str(achados[-1])
    return shutil.which(nome) or nome


def hermes(home: Path) -> str:
    return achar(home, "hermes")


def git(home: Path) -> str:
    return achar(home, "git", ("tools/git-*/cmd/git.exe",))


def uv(home: Path) -> str:
    return achar(home, "uv", ("tools/uv-*/uv.exe",))


def env_hermes(home: Path) -> dict[str, str]:
    return {"HERMES_HOME": str(home), "PYTHONUTF8": "1"}


def tem_gpu_nvidia(ex: Executor) -> bool:
    s = ex.rodar(["nvidia-smi", "-L"], timeout=30)
    return s.ok and "GPU" in s.texto


# ---------- .env ----------

_CHAVE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def ler_env(home: Path, chave: str) -> bool:
    """Só diz se a chave existe com valor; o valor nunca sai daqui."""
    arq = home / ".env"
    if not arq.exists():
        return False
    for linha in arq.read_text(encoding="utf-8").splitlines():
        if linha.startswith(f"{chave}=") and linha.split("=", 1)[1].strip().strip("'\""):
            return True
    return False


def gravar_env(home: Path, chave: str, valor: str) -> None:
    if not _CHAVE.match(chave):
        raise ValueError(f"nome de variável inválido: {chave}")
    if not valor or any(c in valor for c in "\r\n"):
        raise ValueError(f"valor inválido para {chave}")
    arq = home / ".env"
    linhas = arq.read_text(encoding="utf-8").splitlines() if arq.exists() else []
    novas = [l for l in linhas if not l.startswith(f"{chave}=")] + [f"{chave}={valor}"]
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text("\n".join(novas) + "\n", encoding="utf-8")
    os.replace(tmp, arq)


# ---------- ambientes Python ----------

def python_do_venv(pasta: Path) -> Path:
    return pasta / "Scripts" / "python.exe" if os.name == "nt" else pasta / "bin" / "python"


def criar_venv(ex: Executor, home: Path, pasta: Path, requisitos: Path, python: str = "3.11",
               indice_extra: str = "") -> Saida:
    """Cria (se faltar) o ambiente com o Python pedido e instala os requisitos. O uv baixa o Python se precisar."""
    py = python_do_venv(pasta)
    if not py.exists():
        s = ex.rodar([uv(home), "venv", "--python", python, str(pasta)], timeout=900)
        if not s.ok:
            return s
    args = [uv(home), "pip", "install", "--python", str(py), "-r", str(requisitos)]
    if indice_extra:
        args += ["--extra-index-url", indice_extra, "--index-strategy", "unsafe-best-match"]
    return ex.rodar(args, interativo=True, timeout=3600)
