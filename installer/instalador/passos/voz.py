"""Voz ao vivo: ambientes Python, modelos (Whisper, Kokoro, palavra de ativação), voice_mode.json e chave do Gemini."""
from __future__ import annotations

import json

from .. import sistema
from ..estado import AVISO, ERRO, OK
from .face import habilitar_plugin

INDICE_CUDA = "https://download.pytorch.org/whl/cu124"
WHISPER_VOZ = ("small", "large-v3-turbo")  # conversa ao vivo e sentinela
KOKORO_REPO = "hexgrad/Kokoro-82M"
KOKORO_ARQUIVOS = {"config.json": "config.json", "kokoro-v1_0.pth": "kokoro-v1_0.pth", "voices/pm_santa.pt": "pm_santa.pt"}
CHAVE_GEMINI = "GOOGLE_API_KEY"


def jarvis(ctx):
    return ctx.home / "jarvis"


def venv_principal(ctx):
    return jarvis(ctx) / ".venv311"


def garantir_venv_principal(ctx) -> bool:
    """Ambiente .venv311 do Jarvis (usado pela voz, Cerebro, agenda e Spotify)."""
    venv = venv_principal(ctx)
    if sistema.python_do_venv(venv).exists() and ctx.estado.situacao("voz").status == OK:
        return True
    ctx.console.dizer("Instalando o ambiente Python principal do Jarvis (pode baixar alguns GB)...")
    return sistema.criar_venv(ctx.ex, ctx.home, venv, jarvis(ctx) / "requirements.txt").ok


def baixar_whisper(ctx, modelos: tuple[str, ...]) -> sistema.Saida:
    codigo = "import sys\nfrom faster_whisper import download_model\nfor m in sys.argv[1:]:\n    print(m, download_model(m), flush=True)\n"
    return ctx.ex.rodar([str(sistema.python_do_venv(venv_principal(ctx))), "-c", codigo, *modelos],
                        interativo=True, timeout=7200)


def _venv_kokoro(ctx, gpu: bool):
    return jarvis(ctx) / (".venv-kokoro-gpu" if gpu else ".venv-kokoro")


def baixar_kokoro(ctx, gpu: bool) -> sistema.Saida:
    destino = jarvis(ctx) / "models" / "kokoro"
    codigo = (
        "import shutil, sys\nfrom pathlib import Path\nfrom huggingface_hub import hf_hub_download\n"
        "destino = Path(sys.argv[1]); destino.mkdir(parents=True, exist_ok=True)\n"
        "for par in sys.argv[3:]:\n"
        "    origem, nome = par.split('=', 1)\n"
        "    if not (destino / nome).is_file():\n"
        "        shutil.copyfile(hf_hub_download(sys.argv[2], origem), destino / nome)\n"
        # Uma síntese de teste deixa no cache tudo o que o worker usa offline.
        "from kokoro import KModel, KPipeline\n"
        "m = KModel(repo_id=sys.argv[2], config=str(destino / 'config.json'), model=str(destino / 'kokoro-v1_0.pth')).eval()\n"
        "p = KPipeline(lang_code='p', repo_id=sys.argv[2], model=m)\n"
        "print(sum(1 for _ in p('Sistemas online.', voice=str(destino / 'pm_santa.pt'))), 'trecho(s) sintetizado(s)')\n"
    )
    pares = [f"{o}={n}" for o, n in KOKORO_ARQUIVOS.items()]
    return ctx.ex.rodar([str(sistema.python_do_venv(_venv_kokoro(ctx, gpu))), "-c", codigo, str(destino), KOKORO_REPO, *pares],
                        interativo=True, timeout=3600)


def baixar_ativacao(ctx) -> sistema.Saida:
    codigo = "import openwakeword.utils as u\nu.download_models(['hey_jarvis_v0.1'])\n"
    return ctx.ex.rodar([str(sistema.python_do_venv(venv_principal(ctx))), "-c", codigo], timeout=1200)


def gravar_modo(ctx, gpu: bool) -> str:
    arq = jarvis(ctx) / "voice_mode.json"
    if arq.exists():
        return "voice_mode.json mantido"
    dispositivo = "cuda" if gpu else "cpu"
    modo = {"engine": "local", "acknowledgements": False, "stt_model": "small", "stt_device": dispositivo,
            "tts_device": dispositivo, "tts_engine": "kokoro", "kokoro_device": dispositivo,
            "kokoro_model_dir": "models/kokoro"}
    arq.write_text(json.dumps(modo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"voz configurada para {dispositivo.upper()}"


def pedir_chave(ctx) -> str:
    if sistema.ler_env(ctx.home, CHAVE_GEMINI):
        return "chave do Gemini já configurada"
    ctx.console.dizer("\nA conversa por voz usa o Gemini Live. Crie uma chave grátis em https://aistudio.google.com/apikey")
    chave = ctx.console.segredo("Cole a chave (não aparece na tela; Enter para pular)")
    if not chave:
        return "sem chave do Gemini: a conversa por voz fica desligada até você configurar"
    sistema.gravar_env(ctx.home, CHAVE_GEMINI, chave)
    return "chave do Gemini gravada no .env"


def voz(ctx, rec) -> tuple[str, str]:
    gpu = sistema.tem_gpu_nvidia(ctx.ex)
    ctx.console.dizer("Placa NVIDIA encontrada: a voz vai usar a GPU." if gpu else
                      "Sem placa NVIDIA: a voz roda no processador (mais lenta).")
    pastas = jarvis(ctx)
    ambientes = [(venv_principal(ctx), pastas / "requirements.txt", "")]
    ambientes.append((_venv_kokoro(ctx, gpu), pastas / ("requirements-kokoro-gpu.txt" if gpu else "requirements-kokoro.txt"),
                      INDICE_CUDA if gpu else ""))
    for venv, req, indice in ambientes:
        ctx.console.dizer(f"Instalando o ambiente {venv.name} (pode baixar alguns GB)...")
        s = sistema.criar_venv(ctx.ex, ctx.home, venv, req, indice_extra=indice)
        if not s.ok:
            return ERRO, f"falhou ao instalar {venv.name}"

    msgs, status = [], OK
    for nome, acao in (("Whisper", lambda: baixar_whisper(ctx, WHISPER_VOZ)), ("Kokoro", lambda: baixar_kokoro(ctx, gpu)),
                       ("palavra de ativação", lambda: baixar_ativacao(ctx))):
        ctx.console.dizer(f"Baixando modelos: {nome}...")
        if not acao().ok:
            status = AVISO
            msgs.append(f"falhou ao baixar {nome} (rode de novo para tentar)")
    msgs.append(gravar_modo(ctx, gpu))
    msgs.append(pedir_chave(ctx))
    (pastas / ".sentinel-disabled").unlink(missing_ok=True)
    if not habilitar_plugin(ctx, "gemini-live-bridge"):
        status = AVISO
        msgs.append("não consegui habilitar o plugin gemini-live-bridge")
    if not sistema.ler_env(ctx.home, CHAVE_GEMINI):
        status = AVISO
    return status, "; ".join(msgs)
