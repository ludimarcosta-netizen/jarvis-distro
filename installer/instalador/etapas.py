"""Etapas de instalação. Cada etapa devolve (status, mensagem); a primeira com erro interrompe o recurso."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import marcadores, sistema
from .console import Console
from .estado import AVISO, ERRO, OK, Estado
from .recursos import Recurso


@dataclass
class Contexto:
    origem: Path          # raiz do repositório jarvis-distro
    home: Path            # HERMES_HOME do usuário
    valores: dict[str, str]
    console: Console
    estado: Estado
    ex: sistema.Executor = field(default_factory=sistema.Executor)


Etapa = Callable[[Contexto, Recurso], tuple[str, str]]


def copiar(ctx: Contexto, rec: Recurso) -> tuple[str, str]:
    total = 0
    faltando: dict[str, set[str]] = {}
    for c in rec.copiar:
        origem = ctx.origem / c.de
        if not origem.exists():
            return ERRO, f"arquivo da distribuição não encontrado: {c.de}"
        res = marcadores.copiar(origem, ctx.home / c.para, ctx.valores)
        total += res.arquivos
        faltando |= res.faltando
    if faltando:
        nomes = sorted({n for ns in faltando.values() for n in ns})
        return AVISO, f"{total} arquivos; marcadores sem valor: {', '.join(nomes)}"
    return OK, f"{total} arquivos copiados"


from .passos import canais, cerebro, documentos, face, ia, reunioes, servicos, voz  # noqa: E402

ETAPAS: dict[str, Etapa] = {
    "copiar": copiar, "ia": ia.ia, "patch": face.patch, "face": face.face, "voz": voz.voz,
    "reunioes": reunioes.reunioes, "cerebro": cerebro.cerebro, "telegram": canais.telegram,
    "whatsapp": canais.whatsapp, "pdf": documentos.pdf, "prefeitura": documentos.prefeitura,
    "google": servicos.google, "spotify": servicos.spotify, "navegador": servicos.navegador,
    "antigravity": servicos.antigravity, "homeassistant": servicos.homeassistant,
    "agendamentos": servicos.agendamentos,
}

_ORDEM_STATUS = {OK: 0, AVISO: 1, ERRO: 2}


def executar(ctx: Contexto, rec: Recurso) -> tuple[str, str]:
    status, mensagens = OK, []
    for nome in rec.etapas:
        etapa = ETAPAS.get(nome)
        if etapa is None:
            s, m = AVISO, f"etapa '{nome}' ainda não implementada nesta versão do instalador"
        else:
            try:
                s, m = etapa(ctx, rec)
            except Exception as e:  # a etapa falhou: registra e segue para o próximo recurso
                s, m = ERRO, f"{nome}: {type(e).__name__}: {e}"
        mensagens.append(m)
        if _ORDEM_STATUS[s] > _ORDEM_STATUS[status]:
            status = s
        if s == ERRO:
            break
    return status, " | ".join(m for m in mensagens if m)
