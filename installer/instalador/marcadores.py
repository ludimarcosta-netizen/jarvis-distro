"""Marcadores __JARVIS_*__: calcula os valores a partir do perfil e preenche textos e pastas."""
from __future__ import annotations

import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .perfil import Perfil

PADRAO = re.compile(r"__JARVIS_([A-Z0-9_]+?)__")
# Preenchidos em tempo de execução pelo próprio Jarvis; o instalador não toca.
RESERVADOS = frozenset({"TOKEN"})


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def dobrar(texto: str) -> str:
    """Minúsculo e sem acento (mesma regra do `_fold` do Jarvis)."""
    return sem_acento(texto.casefold())


def como_regex(texto: str) -> str:
    """Padrão minúsculo que aceita a forma com e sem acento ("Céu" -> "c[ée]u")."""
    partes = []
    for c in texto.casefold():
        base = sem_acento(c)
        if c.isspace():
            partes.append(r"\s+")
        elif base != c and base:
            partes.append(f"[{c}{re.escape(base)}]")
        else:
            partes.append(re.escape(c))
    return "".join(partes)


def _caminhos(nome: str, caminho: str) -> dict[str, str]:
    barra = caminho.replace("/", "\\")
    return {
        f"{nome}_FS": barra.replace("\\", "/"),
        f"{nome}_BS": barra,
        f"{nome}_BS2": barra.replace("\\", "\\\\"),
    }


def valores(p: Perfil, home: str) -> dict[str, str]:
    v = {
        "USER": p.nome,
        "USER_LOWER": p.nome.casefold(),
        "USER_UPPER": p.nome.upper(),
        "USER_REGEX": como_regex(p.nome),
        "USER_FULLNAME": p.nome_completo,
        "USER_FULLNAME_LOWER": p.nome_completo.casefold(),
        "CIDADE": p.cidade,
        "CIDADE_ASCII": sem_acento(p.cidade),
        "CIDADE_FOLD": dobrar(p.cidade),
        "CIDADE_UPPER": p.cidade.upper(),
        "CIDADE_URL": quote(p.cidade, safe=""),
        "CIDADE_REGEX": como_regex(p.cidade),
        "CIDADE_CURTA": p.cidade_curta,
        "CIDADE_CURTA_FOLD": dobrar(p.cidade_curta),
        "CIDADE_CURTA_REGEX": como_regex(p.cidade_curta),
        "ORG_DOMAIN": p.dominio,
        "GITHUB_USER": p.github,
    }
    v |= _caminhos("HERMES_HOME", home)
    v |= _caminhos("USERPROFILE", p.pasta_usuario)
    v |= _caminhos("CEREBRO", p.pasta_cerebro)
    v |= _caminhos("DOWNLOADS", p.pasta_downloads)
    return v


def preencher(texto: str, vals: dict[str, str]) -> tuple[str, set[str]]:
    """Troca os marcadores conhecidos; devolve o texto e os nomes que ficaram sem valor."""
    faltando: set[str] = set()

    def troca(m: re.Match[str]) -> str:
        nome = m.group(1)
        if nome in RESERVADOS:
            return m.group(0)
        if nome not in vals:
            faltando.add(nome)
            return m.group(0)
        return vals[nome]

    return PADRAO.sub(troca, texto), faltando


@dataclass
class Copia:
    arquivos: int = 0
    preenchidos: int = 0
    faltando: dict[str, set[str]] = field(default_factory=dict)  # arquivo -> marcadores sem valor


def copiar(origem: Path, destino: Path, vals: dict[str, str]) -> Copia:
    """Copia a pasta (ou arquivo) `origem` para `destino`, preenchendo marcadores nos textos UTF-8.

    Arquivos que já existem no destino são substituídos; os que só existem no destino ficam."""
    res = Copia()
    itens = [origem] if origem.is_file() else sorted(f for f in origem.rglob("*") if f.is_file())
    for f in itens:
        alvo = destino if origem.is_file() else destino / f.relative_to(origem)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        bruto = f.read_bytes()
        try:
            texto = bruto.decode("utf-8")
        except UnicodeDecodeError:
            shutil.copyfile(f, alvo)
        else:
            novo, faltando = preencher(texto, vals)
            if faltando:
                res.faltando[str(alvo)] = faltando
            if novo != texto:
                res.preenchidos += 1
            alvo.write_text(novo, encoding="utf-8", newline="")
        res.arquivos += 1
    return res
