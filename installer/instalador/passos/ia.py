"""IA principal: persona (SOUL.md), tema, fuso, escolha do provedor/modelo e teste real."""
from __future__ import annotations

import re
import shutil

from .. import marcadores, sistema
from ..estado import AVISO, ERRO, OK

CEREBRO = re.compile(r"<!-- CEREBRO_START -->.*?<!-- CEREBRO_END -->", re.DOTALL)
PERGUNTA_TESTE = "Responda apenas com a palavra OK."


def gravar_persona(ctx) -> str:
    """Escreve o SOUL.md do Jarvis. Mantém o bloco do Cerebro, se já houver; guarda cópia do anterior."""
    modelo = (ctx.origem / "templates" / "SOUL.md").read_text(encoding="utf-8")
    texto, _ = marcadores.preencher(modelo, ctx.valores)
    alvo = ctx.home / "SOUL.md"
    if alvo.exists():
        atual = alvo.read_text(encoding="utf-8")
        bloco = CEREBRO.search(atual)
        if bloco:
            texto = texto.rstrip() + "\n\n" + bloco.group(0) + "\n"
        if atual == texto:
            return "persona já instalada"
        if "J.A.R.V.I.S." not in atual and not ctx.console.confirmar(
                "Já existe um SOUL.md (persona). Trocar pela persona do Jarvis? O atual fica guardado", True):
            return "persona atual mantida"
        shutil.copyfile(alvo, alvo.with_name("SOUL.md.bak-instalador"))
    alvo.write_text(texto, encoding="utf-8")
    return "persona do Jarvis instalada"


def configurar(ctx, chave: str, valor: str) -> bool:
    return ctx.ex.rodar([sistema.hermes(ctx.home), "config", "set", chave, valor],
                        env=sistema.env_hermes(ctx.home), timeout=120).ok


def testar(ctx) -> sistema.Saida:
    return ctx.ex.rodar([sistema.hermes(ctx.home), "-z", PERGUNTA_TESTE], env=sistema.env_hermes(ctx.home),
                        cwd=ctx.home, timeout=300)


def ia(ctx, rec) -> tuple[str, str]:
    msgs = [gravar_persona(ctx)]
    falhas = [c for c, v in (("display.skin", "jarvis"), ("timezone", ctx.estado.perfil.fuso)) if not configurar(ctx, c, v)]
    if falhas:
        msgs.append(f"não consegui gravar: {', '.join(falhas)}")

    ja_ok = ctx.estado.situacao(rec.id).status == OK
    if not ja_ok or ctx.console.confirmar("Trocar o provedor ou o modelo de IA?", False):
        ctx.console.dizer("\nAbrindo o assistente do Hermes para escolher o provedor e o modelo de IA.\n"
                          "Siga as perguntas; se usar uma conta (Google, OpenAI, Anthropic, Nous...), o login abre no navegador.")
        ctx.ex.rodar([sistema.hermes(ctx.home), "model"], env=sistema.env_hermes(ctx.home), interativo=True)

    ctx.console.dizer("Testando a IA (pode levar até um minuto)...")
    s = testar(ctx)
    if not s.ok or not s.texto.strip():
        return ERRO, "; ".join(msgs + ["a IA não respondeu ao teste; rode o instalador de novo e escolha outro provedor"])
    resposta = s.texto.strip().splitlines()[-1][:80]
    status = OK if not falhas else AVISO
    return status, "; ".join(msgs + [f"teste respondeu: {resposta}"])
