"""Progresso salvo em JSON: permite parar, retomar e rodar de novo para ligar recursos novos.

Guarda só perfil (sem segredos) e situação de cada recurso."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .perfil import Perfil

VERSAO = 1
OK, AVISO, ERRO, PENDENTE = "ok", "aviso", "erro", "pendente"
ICONES = {OK: "✅", AVISO: "⚠️ ", ERRO: "❌", PENDENTE: "··"}


@dataclass
class Situacao:
    status: str = PENDENTE
    mensagem: str = ""
    quando: str = ""


@dataclass
class Estado:
    caminho: Path
    perfil: Perfil | None = None
    escolhidos: list[str] = field(default_factory=list)
    recursos: dict[str, Situacao] = field(default_factory=dict)

    @classmethod
    def carregar(cls, caminho: Path) -> "Estado":
        if not caminho.exists():
            return cls(caminho)
        d = json.loads(caminho.read_text(encoding="utf-8"))
        if d.get("versao") != VERSAO:
            raise ValueError(f"estado {caminho} tem versão {d.get('versao')}, esperado {VERSAO}")
        return cls(
            caminho,
            perfil=Perfil.from_dict(d["perfil"]) if d.get("perfil") else None,
            escolhidos=list(d.get("escolhidos", [])),
            recursos={k: Situacao(**v) for k, v in d.get("recursos", {}).items()},
        )

    def salvar(self) -> None:
        d = {
            "versao": VERSAO,
            "perfil": self.perfil.to_dict() if self.perfil else None,
            "escolhidos": self.escolhidos,
            "recursos": {k: vars(v) for k, v in self.recursos.items()},
        }
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.caminho.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.caminho)

    def marcar(self, recurso: str, status: str, mensagem: str = "") -> None:
        quando = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.recursos[recurso] = Situacao(status, mensagem, quando)
        self.salvar()

    def situacao(self, recurso: str) -> Situacao:
        return self.recursos.get(recurso, Situacao())
