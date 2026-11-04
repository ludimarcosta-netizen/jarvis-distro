"""Face desktop: aplica o patch do Jarvis no Hermes (commit no ramo local "jarvis"), recompila o app
e opcionalmente abre com o Windows."""
from __future__ import annotations

import tempfile
from pathlib import Path

from .. import marcadores, sistema
from ..estado import AVISO, ERRO, OK

RAMO = "jarvis"
CHAVE_RUN = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
EXE = Path("apps/desktop/release/win-unpacked/Hermes.exe")


def _git(ctx, *args: str, **kw) -> sistema.Saida:
    repo = ctx.home / "hermes-agent"
    return ctx.ex.rodar([sistema.git(ctx.home), "-c", "core.longpaths=true", "-C", str(repo), *args], timeout=600, **kw)


def aplicar_patch(ctx) -> tuple[str, str]:
    if _git(ctx, "rev-parse", "--abbrev-ref", "HEAD").texto.strip() == RAMO:
        return OK, "patch do Jarvis já aplicado"
    base = (ctx.origem / "hermes-patches" / "BASE.txt").read_text(encoding="utf-8").strip()
    head, tag = _git(ctx, "rev-parse", "HEAD"), _git(ctx, "rev-parse", f"{base}^{{commit}}")
    if not head.ok:
        return ERRO, "hermes-agent não é um repositório git; reinstale com instalar.ps1 -ReinstalarHermes"
    if tag.ok and head.texto.strip() != tag.texto.strip():
        return ERRO, f"o Hermes instalado não está na versão {base}; reinstale com instalar.ps1 -ReinstalarHermes"
    if _git(ctx, "status", "--porcelain", "--untracked-files=no").texto.strip():
        return ERRO, "o hermes-agent tem alterações locais; não apliquei o patch para não misturar"

    patch = (ctx.origem / "hermes-patches" / "jarvis.patch").read_bytes().decode("utf-8")
    texto, faltando = marcadores.preencher(patch, ctx.valores)
    if faltando:
        return ERRO, f"patch com marcadores sem valor: {', '.join(sorted(faltando))}"
    with tempfile.TemporaryDirectory() as d:
        arq = Path(d, "jarvis.patch")
        arq.write_bytes(texto.encode("utf-8"))
        s = _git(ctx, "apply", "--index", "--whitespace=nowarn", str(arq))
    if not s.ok:
        return ERRO, f"o patch não aplicou: {s.texto.strip()[-300:]}"
    marcadores.copiar(ctx.origem / "hermes-patches" / "novos", ctx.home / "hermes-agent", ctx.valores)
    passos = (("checkout", "-b", RAMO), ("add", "-A"),
              ("-c", "user.name=Instalador Jarvis", "-c", "user.email=instalador@jarvis.invalid",
               "commit", "-q", "-m", f"Jarvis sobre o Hermes {base}"))
    for args in passos:
        s = _git(ctx, *args)
        if not s.ok:
            return ERRO, f"git {args[0]} falhou: {s.texto.strip()[-300:]}"
    return OK, f"patch aplicado no ramo local '{RAMO}'"


def patch(ctx, rec) -> tuple[str, str]:
    """Etapa compartilhada (face, navegador): o patch traz a face e o Chrome automático."""
    return aplicar_patch(ctx)


def compilar(ctx) -> tuple[str, str]:
    repo = ctx.home / "hermes-agent"
    ctx.console.dizer("Compilando o app desktop (vários minutos na primeira vez)...")
    s = ctx.ex.rodar(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(repo / "scripts" / "install.ps1"),
                      "-Stage", "desktop", "-HermesHome", str(ctx.home), "-InstallDir", str(repo)],
                     env=sistema.env_hermes(ctx.home), interativo=True, timeout=5400)
    if not s.ok or not (repo / EXE).exists():
        return ERRO, "a compilação do app desktop falhou (veja a saída acima)"
    return OK, "app desktop compilado"


def habilitar_plugin(ctx, nome: str) -> bool:
    return ctx.ex.rodar([sistema.hermes(ctx.home), "plugins", "enable", nome],
                        env=sistema.env_hermes(ctx.home), timeout=120).ok


def autoinicio(ctx) -> str:
    if not ctx.console.confirmar("Abrir o Jarvis automaticamente quando o Windows iniciar?", True):
        return "sem autoinício"
    exe = ctx.home / "hermes-agent" / EXE
    s = ctx.ex.rodar(["reg", "add", CHAVE_RUN, "/v", "Hermes", "/t", "REG_SZ", "/d", f'"{exe}"', "/f"], timeout=60)
    return "abre com o Windows" if s.ok else "não consegui registrar o autoinício"


def face(ctx, rec) -> tuple[str, str]:
    msgs = []
    status, msg = compilar(ctx)
    msgs.append(msg)
    if status == ERRO:
        return ERRO, "; ".join(msgs)
    status = OK
    if not habilitar_plugin(ctx, "gemini-live-bridge"):
        status = AVISO
        msgs.append("não consegui habilitar o plugin gemini-live-bridge")
    if ctx.estado.situacao("voz").status != OK:
        # Sem a voz instalada, o plugin não deve tentar subir o sentinela (precisa do ambiente da voz).
        (ctx.home / "jarvis" / ".sentinel-disabled").write_text("voz não instalada\n", encoding="utf-8")
    msgs.append(autoinicio(ctx))
    return status, "; ".join(msgs)
