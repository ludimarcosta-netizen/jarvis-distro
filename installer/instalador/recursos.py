"""Catálogo de recursos (recursos.toml) e ordem de instalação com dependências."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Copiar:
    de: str
    para: str


@dataclass(frozen=True)
class Recurso:
    id: str
    nome: str
    descricao: str
    etapas: tuple[str, ...]
    depende: tuple[str, ...] = ()
    copiar: tuple[Copiar, ...] = ()
    obrigatorio: bool = False


@dataclass
class Catalogo:
    recursos: dict[str, Recurso] = field(default_factory=dict)

    @classmethod
    def carregar(cls, caminho: Path) -> "Catalogo":
        dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
        cat = cls()
        for r in dados.get("recurso", []):
            rec = Recurso(
                id=r["id"], nome=r["nome"], descricao=r["descricao"], etapas=tuple(r["etapas"]),
                depende=tuple(r.get("depende", ())), obrigatorio=bool(r.get("obrigatorio", False)),
                copiar=tuple(Copiar(c["de"], c["para"]) for c in r.get("copiar", ())),
            )
            if rec.id in cat.recursos:
                raise ValueError(f"recurso repetido: {rec.id}")
            cat.recursos[rec.id] = rec
        cat._validar()
        return cat

    def _validar(self) -> None:
        for r in self.recursos.values():
            for d in r.depende:
                if d not in self.recursos:
                    raise ValueError(f"{r.id} depende de recurso desconhecido: {d}")
            for c in r.copiar:
                if Path(c.para).is_absolute() or ".." in Path(c.para).parts:
                    raise ValueError(f"{r.id}: destino precisa ser relativo ao HERMES_HOME: {c.para}")
        self.ordem(list(self.recursos))  # acusa ciclo

    def obrigatorios(self) -> list[str]:
        return [r.id for r in self.recursos.values() if r.obrigatorio]

    def ordem(self, escolhidos: list[str]) -> list[str]:
        """Escolhidos + obrigatórios + dependências, cada um depois do que ele precisa."""
        saida: list[str] = []
        visitando: set[str] = set()

        def visitar(rid: str) -> None:
            if rid in saida:
                return
            if rid in visitando:
                raise ValueError(f"dependência circular em {rid}")
            if rid not in self.recursos:
                raise KeyError(f"recurso desconhecido: {rid}")
            visitando.add(rid)
            for d in self.recursos[rid].depende:
                visitar(d)
            visitando.discard(rid)
            saida.append(rid)

        for rid in self.obrigatorios() + list(escolhidos):
            visitar(rid)
        return saida
