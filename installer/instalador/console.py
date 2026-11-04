"""Entrada e saída do instalador, trocável nos testes."""
from __future__ import annotations

import getpass
import sys
from typing import Callable


class Console:
    def __init__(self, ler: Callable[[str], str] = input, escrever: Callable[[str], None] | None = None,
                 ler_segredo: Callable[[str], str] = getpass.getpass) -> None:
        self._ler = ler
        self._escrever = escrever or (lambda s: print(s, flush=True))
        self._segredo = ler_segredo

    def dizer(self, texto: str) -> None:
        self._escrever(texto)

    def perguntar(self, pergunta: str, padrao: str = "") -> str:
        sufixo = f" [{padrao}]" if padrao else ""
        try:
            resposta = self._ler(f"{pergunta}{sufixo}: ").strip()
        except EOFError:
            resposta = ""
        return resposta or padrao

    def confirmar(self, pergunta: str, padrao: bool = True) -> bool:
        while True:
            r = self.perguntar(f"{pergunta} ({'S/n' if padrao else 's/N'})").casefold()
            if not r:
                return padrao
            if r in {"s", "sim", "y"}:
                return True
            if r in {"n", "nao", "não"}:
                return False

    def segredo(self, pergunta: str) -> str:
        """Lê sem eco. O valor nunca passa por `dizer` nem vai para log/estado."""
        return self._segredo(f"{pergunta}: ").strip()


def console_padrao() -> Console:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return Console()
