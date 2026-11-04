"""Perfil do usuário: as respostas que viram os valores dos marcadores. Nada aqui é segredo."""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .console import Console

FUSO_PADRAO = "America/Sao_Paulo"


@dataclass
class Perfil:
    nome: str
    nome_completo: str
    cidade: str
    cidade_curta: str
    pasta_usuario: str
    pasta_cerebro: str
    pasta_downloads: str
    fuso: str = FUSO_PADRAO
    dominio: str = ""
    github: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Perfil":
        nomes = {f.name for f in fields(cls)}
        return cls(**{k: str(v) for k, v in d.items() if k in nomes})


class PerfilInvalido(ValueError):
    pass


_NOME = re.compile(r"^[^\W\d_][^\W\d_'\- ]*(?:[ '\-][^\W\d_]+)*$")
_DOMINIO = re.compile(r"^(?=.{1,253}$)[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
_GITHUB = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_FUSO = re.compile(r"^[A-Za-z_]+(?:/[A-Za-z_\-+0-9]+)+$|^UTC$")


def validar(p: Perfil) -> None:
    erros = []
    if not _NOME.match(p.nome) or " " in p.nome:
        erros.append("nome: uma palavra, só letras")
    for campo in ("nome_completo", "cidade", "cidade_curta"):
        if not _NOME.match(getattr(p, campo)):
            erros.append(f"{campo}: só letras, espaços, hífen ou apóstrofo")
    if p.cidade_curta and p.cidade_curta.casefold() not in p.cidade.casefold():
        erros.append("cidade_curta: precisa ser parte do nome da cidade")
    for campo in ("pasta_usuario", "pasta_cerebro", "pasta_downloads"):
        valor = getattr(p, campo)
        if not Path(valor).is_absolute() or "__JARVIS_" in valor:
            erros.append(f"{campo}: caminho absoluto")
    if p.dominio and not _DOMINIO.match(p.dominio):
        erros.append("dominio: ex. prefeitura.gov.br")
    if p.github and not _GITHUB.match(p.github):
        erros.append("github: usuário inválido")
    if not _FUSO.match(p.fuso):
        erros.append("fuso: ex. America/Sao_Paulo")
    if erros:
        raise PerfilInvalido("; ".join(erros))


def _padrao_usuario() -> str:
    return os.environ.get("USERPROFILE") or str(Path.home())


def perguntar(con: Console, atual: Perfil | None = None) -> Perfil:
    """Pergunta cada campo (Enter mantém o valor sugerido) até o perfil ficar válido."""
    a = atual.to_dict() if atual else {}
    usuario = a.get("pasta_usuario") or _padrao_usuario()
    while True:
        con.dizer("\n== Perfil ==  (Enter mantém o valor entre colchetes)")
        nome = con.perguntar("Seu primeiro nome", a.get("nome", ""))
        completo = con.perguntar("Nome completo (usado em ofícios e assinaturas)", a.get("nome_completo", nome))
        cidade = con.perguntar("Cidade ou órgão (ex.: Vila Nova do Sul)", a.get("cidade", ""))
        curta = con.perguntar("Nome curto da cidade (como você fala)", a.get("cidade_curta", cidade.split(" ")[0]))
        usuario = con.perguntar("Pasta do usuário", usuario)
        cerebro = con.perguntar("Pasta do cofre de memória (Cerebro)",
                                a.get("pasta_cerebro", str(Path(usuario) / "Documents" / "Cerebro")))
        downloads = con.perguntar("Pasta de downloads", a.get("pasta_downloads", str(Path(usuario) / "Downloads")))
        fuso = con.perguntar("Fuso horário", a.get("fuso", FUSO_PADRAO))
        dominio = con.perguntar("Domínio de e-mail do órgão (opcional)", a.get("dominio", ""))
        p = Perfil(nome=nome.strip(), nome_completo=completo.strip(), cidade=cidade.strip(),
                   cidade_curta=curta.strip(), pasta_usuario=usuario.strip(), pasta_cerebro=cerebro.strip(),
                   pasta_downloads=downloads.strip(), fuso=fuso.strip(), dominio=dominio.strip().lower(),
                   github=a.get("github", ""))
        try:
            validar(p)
        except PerfilInvalido as e:
            con.dizer(f"⚠️  Ajuste: {e}")
            a = p.to_dict()
            continue
        return p
