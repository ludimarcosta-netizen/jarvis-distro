"""Cerebro: cofre de notas compartilhado, servidor MCP no Hermes, bloco de memória no SOUL.md e coletas agendadas."""
from __future__ import annotations

from pathlib import Path

from .. import sistema
from ..estado import AVISO, ERRO, OK
from .voz import garantir_venv_principal, venv_principal

PASTAS = ("pessoas", "projetos", "organizacoes", "perfil", "sessoes", "reunioes", "registros")
TAREFAS = (
    # nome, script e argumentos, agenda do schtasks
    ("Jarvis-Cerebro-Incremental", ["collect_memory.py"], ["/SC", "MINUTE", "/MO", "15"]),
    ("Jarvis - Sessoes para o Cerebro", ["session_harvest.py", "run", "--max", "60"], ["/SC", "DAILY", "/ST", "12:40"]),
)

# Roda no Python do Jarvis: reaproveita os blocos de install_cerebro.py sem mexer em Claude/Codex/Gemini.
_BLOCOS = """
import sys
from pathlib import Path
runtime, python, cofre = map(Path, sys.argv[1:4])
sys.path.insert(0, str(runtime / "scripts"))
import install_cerebro as ic
soul = runtime.parent / "SOUL.md"
if soul.exists():
    texto = soul.read_text(encoding="utf-8-sig")
    soul.write_text(ic.upsert(texto, ic.START, ic.END, ic.jarvis_protocol(runtime, python)), encoding="utf-8")
agentes = cofre / "AGENTS.md"
texto = agentes.read_text(encoding="utf-8-sig") if agentes.exists() else "# Cofre de memória\\n\\nNotas em Markdown compartilhadas com as IAs.\\n"
agentes.write_text(ic.upsert(texto, ic.START, ic.END, ic.protocol(runtime, python)), encoding="utf-8")
print("blocos ok")
"""


def criar_cofre(cofre: Path) -> None:
    for pasta in PASTAS:
        (cofre / pasta).mkdir(parents=True, exist_ok=True)


def agendar(ctx, python: Path) -> str:
    pyw = python.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else python
    falhas = []
    for nome, args, agenda in TAREFAS:
        comando = " ".join(f'"{a}"' for a in [exe, "-B", ctx.home / "jarvis" / args[0], *args[1:]])
        if not ctx.ex.rodar(["schtasks", "/Create", "/TN", nome, "/TR", comando, *agenda, "/F"], timeout=60).ok:
            falhas.append(nome)
    return f"não consegui agendar: {', '.join(falhas)}" if falhas else "coletas agendadas (a cada 15 min e diária às 12:40)"


def cerebro(ctx, rec) -> tuple[str, str]:
    cofre = Path(ctx.estado.perfil.pasta_cerebro)
    criar_cofre(cofre)
    if not garantir_venv_principal(ctx):
        return ERRO, "falhou ao instalar o ambiente Python do Jarvis"
    runtime = ctx.home / "jarvis"
    python = sistema.python_do_venv(venv_principal(ctx))
    if not ctx.ex.rodar([str(python), "-c", _BLOCOS, str(runtime), str(python), str(cofre)], timeout=120).ok:
        return ERRO, "não consegui escrever os blocos de memória (SOUL.md e AGENTS.md do cofre)"

    msgs, status = [f"cofre em {cofre}"], OK
    mcp = [sistema.hermes(ctx.home), "mcp", "add", "cerebro", "--command", str(python),
           "--env", f"JARVIS_CEREBRO_DIR={cofre}", "--args", "-B", str(runtime / "cerebro_mcp.py")]
    if not ctx.ex.rodar(mcp, env=sistema.env_hermes(ctx.home), interativo=True, timeout=300).ok:
        status = AVISO
        msgs.append("não consegui registrar o servidor MCP do Cerebro no Hermes")
    if ctx.console.confirmar("Agendar as coletas automáticas de memória (conversas e sessões das IAs)?", True):
        m = agendar(ctx, python)
        msgs.append(m)
        if m.startswith("não"):
            status = AVISO
    teste = ctx.ex.rodar([str(python), str(runtime / "cerebro_memory.py"), "status"],
                         env={**sistema.env_hermes(ctx.home), "JARVIS_CEREBRO_DIR": str(cofre)}, timeout=300)
    if not teste.ok:
        return ERRO, "; ".join(msgs + ["o Cerebro não respondeu ao teste (cerebro_memory.py status)"])
    return status, "; ".join(msgs)
