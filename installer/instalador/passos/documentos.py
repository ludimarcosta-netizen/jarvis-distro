"""Assinatura de PDF (runtime Python 3.12 isolado) e numeração de ofícios da skill prefeitura."""
from __future__ import annotations

import json
from pathlib import Path

from .. import marcadores, sistema
from ..estado import AVISO, ERRO, OK


def pdf(ctx, rec) -> tuple[str, str]:
    runtime = ctx.home / "runtime" / "pdf-signing"
    skill = ctx.home / "skills" / "productivity" / "pdf-signing"
    ctx.console.dizer("Instalando o ambiente da assinatura de PDF (Python 3.12)...")
    # requirements-dev inclui fastapi e reportlab, que o fluxo do agente usa além do núcleo de produção.
    if not sistema.criar_venv(ctx.ex, ctx.home, runtime, skill / "requirements-dev.txt", python="3.12").ok:
        return ERRO, "falhou ao instalar o ambiente da assinatura de PDF"
    py = sistema.python_do_venv(runtime)
    if not ctx.ex.rodar([str(py), "-c", "import pyhanko, pypdf, fitz, reportlab"], timeout=120).ok:
        return ERRO, "o ambiente da assinatura não importou as bibliotecas de PDF"
    certs = ctx.ex.rodar([str(py), str(skill / "scripts" / "main.py"), "certificates"], cwd=skill, timeout=120)
    aviso = ("nenhum certificado listado: importe o A1 (.pfx) no Windows ou conecte o token A3"
             if not certs.ok or '"alias"' not in certs.texto else "certificado encontrado")
    return (OK if "encontrado" in aviso else AVISO), f"ambiente pronto; {aviso}"


def pasta_oficios(ctx) -> Path:
    return Path(ctx.estado.perfil.pasta_usuario) / "Documents" / "JARVIS_Records" / "oficios"


def prefeitura(ctx, rec) -> tuple[str, str]:
    pasta = pasta_oficios(ctx)
    marcadores.copiar(ctx.origem / "templates" / "oficios" / "oficios.py", pasta / "oficios.py", ctx.valores)
    base = pasta / "oficios.json"
    if not base.exists():
        resposta = ctx.console.perguntar("Número do próximo ofício a emitir", "1")
        if not resposta.isdigit() or int(resposta) < 1:
            return ERRO, "número de ofício inválido"
        base.write_text(json.dumps({"next_number": int(resposta), "records": []}, indent=2) + "\n", encoding="utf-8")
    py = sistema.python_do_venv(ctx.home / "hermes-agent" / "venv")
    s = ctx.ex.rodar([str(py), str(pasta / "oficios.py"), "proximo"], timeout=60)
    if not s.ok:
        return ERRO, "o controle de ofícios não respondeu (oficios.py proximo)"
    return OK, f"numeração de ofícios em {pasta}"
