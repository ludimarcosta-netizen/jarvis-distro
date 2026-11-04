"""Instalador do Jarvis: `python -m instalador --home <HERMES_HOME> --origem <repositório>`."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import doctor, etapas, marcadores, menu, perfil, sistema
from .console import Console, console_padrao
from .estado import ICONES, Estado
from .recursos import Catalogo

RAIZ = Path(__file__).resolve().parents[2]
NOME_ESTADO = Path("jarvis-instalador") / "estado.json"


class Recusado(RuntimeError):
    pass


def checar_destino(home: Path, estado_existe: bool, forcar: bool) -> None:
    """Não sobrescreve uma instância já configurada à mão (ex.: a do autor da distribuição)."""
    if (home / "jarvis" / "exportacao_regras.toml").exists():
        raise Recusado(f"{home} é a instância de origem da distribuição; o instalador não roda nela.")
    if not estado_existe and (home / "jarvis").exists() and not forcar:
        raise Recusado(f"{home}\\jarvis já existe e não foi criado por este instalador. "
                       "Use --forcar para instalar por cima (arquivos com o mesmo nome serão substituídos).")


def contexto(con: Console, est: Estado, origem: Path, home: Path, ex: sistema.Executor) -> etapas.Contexto:
    assert est.perfil is not None
    return etapas.Contexto(origem, home, marcadores.valores(est.perfil, str(home)), con, est, ex)


def instalar(ctx: etapas.Contexto, cat: Catalogo) -> None:
    for rid in cat.ordem(ctx.estado.escolhidos):
        rec = cat.recursos[rid]
        ctx.console.dizer(f"\n→ {rec.nome}")
        status, msg = etapas.executar(ctx, rec)
        ctx.estado.marcar(rid, status, msg)
        ctx.console.dizer(f"  {ICONES[status]} {msg}")


def resumo(con: Console, cat: Catalogo, est: Estado) -> None:
    con.dizer("\n== Situação ==")
    for rid in cat.ordem(est.escolhidos):
        s = est.situacao(rid)
        con.dizer(f"{ICONES[s.status]} {cat.recursos[rid].nome}: {s.mensagem or s.status}")


def rodar(con: Console, origem: Path, home: Path, forcar: bool = False, ex: sistema.Executor | None = None) -> int:
    caminho = home / NOME_ESTADO
    checar_destino(home, caminho.exists(), forcar)
    est = Estado.carregar(caminho)
    cat = Catalogo.carregar(origem / "installer" / "recursos.toml")
    ex = ex or sistema.Executor(home / NOME_ESTADO.parent / "instalador.log")
    con.dizer("Instalador do Jarvis — dá para parar a qualquer momento e retomar depois.")
    if est.perfil is None:
        est.perfil = perfil.perguntar(con)
        est.salvar()
    while True:
        acao, marcados = menu.escolher(con, cat, est)
        est.escolhidos = sorted(marcados)
        est.salvar()
        if acao == "sair":
            resumo(con, cat, est)
            return 0
        if acao == "perfil":
            est.perfil = perfil.perguntar(con, est.perfil)
            est.salvar()
            con.dizer("Perfil salvo. Rode 'instalar' de novo para atualizar os arquivos já copiados.")
            continue
        ctx = contexto(con, est, origem, home, ex)
        if acao == "instalar":
            instalar(ctx, cat)
            resumo(con, cat, est)
        doctor.rodar(ctx, cat)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="instalador", description=__doc__)
    padrao_home = os.environ.get("HERMES_HOME") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "hermes")
    ap.add_argument("--home", type=Path, default=Path(padrao_home), help="pasta HERMES_HOME")
    ap.add_argument("--origem", type=Path, default=RAIZ, help="raiz do repositório jarvis-distro")
    ap.add_argument("--forcar", action="store_true", help="instalar sobre uma pasta jarvis existente")
    a = ap.parse_args(argv)
    con = console_padrao()
    try:
        return rodar(con, a.origem.resolve(), a.home.resolve(), a.forcar)
    except Recusado as e:
        con.dizer(f"❌ {e}")
        return 2
    except KeyboardInterrupt:
        con.dizer("\nInterrompido. O progresso ficou salvo; rode de novo para continuar.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
