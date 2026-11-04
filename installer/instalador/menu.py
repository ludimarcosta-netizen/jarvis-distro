"""Menu de recursos: marcar/desmarcar, instalar, editar perfil, sair."""
from __future__ import annotations

from .console import Console
from .estado import ICONES, Estado
from .recursos import Catalogo

AJUDA = "Números (ex.: 3 5) marcam/desmarcam · i = instalar marcados · d = diagnóstico · p = editar perfil · s = sair"


def desenhar(con: Console, cat: Catalogo, est: Estado, marcados: set[str]) -> list[str]:
    ids = list(cat.recursos)
    con.dizer("\n== Recursos ==")
    for n, rid in enumerate(ids, 1):
        r = cat.recursos[rid]
        caixa = "[x]" if r.obrigatorio or rid in marcados else "[ ]"
        icone = ICONES[est.situacao(rid).status]
        extra = " (obrigatório)" if r.obrigatorio else ""
        con.dizer(f"{n:>2}. {caixa} {icone} {r.nome}{extra} — {r.descricao}")
    con.dizer(AJUDA)
    return ids


def escolher(con: Console, cat: Catalogo, est: Estado) -> tuple[str, set[str]]:
    """Laço do menu. Devolve ("instalar"|"perfil"|"sair", marcados)."""
    marcados = {r for r in est.escolhidos if r in cat.recursos}
    while True:
        ids = desenhar(con, cat, est, marcados)
        resposta = con.perguntar("Opção").strip().casefold()
        if resposta in {"i", "d", "p", "s"}:
            acao = {"i": "instalar", "d": "diagnostico", "p": "perfil", "s": "sair"}[resposta]
            return acao, marcados
        for parte in resposta.replace(",", " ").split():
            if not parte.isdigit() or not 1 <= int(parte) <= len(ids):
                con.dizer(f"Opção inválida: {parte}")
                continue
            rid = ids[int(parte) - 1]
            if cat.recursos[rid].obrigatorio:
                con.dizer(f"{cat.recursos[rid].nome} é obrigatório.")
            else:
                marcados ^= {rid}
