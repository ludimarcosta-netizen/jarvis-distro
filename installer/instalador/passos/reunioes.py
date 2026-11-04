"""Reuniões: identificação de vozes (sherpa-onnx), modelos de diarização e Whisper das atas."""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import sistema
from ..estado import AVISO, ERRO, OK
from .voz import baixar_whisper

WHISPER_REUNIOES = ("base", "large-v3")  # identificação de voz e passada offline das atas
MODELO_FALANTE = "speaker-ecapa512-sherpa.onnx"
MODELO_PADRAO = "speaker.onnx"


def modelo_falante(ctx, modelos: Path) -> tuple[str, str]:
    """O extrator ECAPA não tem download público; sem ele usa o modelo padrão (identificação menos precisa)."""
    alvo = modelos / MODELO_FALANTE
    if alvo.is_file():
        return OK, "modelo de voz ECAPA presente"
    caminho = ctx.console.perguntar(f"Se você recebeu o arquivo {MODELO_FALANTE}, informe o caminho (Enter usa o modelo padrão)")
    origem = Path(caminho.strip('"')) if caminho else modelos / MODELO_PADRAO
    if not origem.is_file():
        return ERRO, f"arquivo não encontrado: {origem}"
    shutil.copyfile(origem, alvo)
    if caminho:
        return OK, "modelo de voz ECAPA instalado"
    return AVISO, "usando o modelo de voz padrão (wespeaker); a identificação de quem falou pode errar mais"


def testar_motor(ctx, venv: Path, pasta: Path) -> sistema.Saida:
    codigo = "from pathlib import Path\nfrom engine import Engine\nEngine(Path('models'))\nprint('motor de vozes ok')\n"
    return ctx.ex.rodar([str(sistema.python_do_venv(venv)), "-c", codigo], cwd=pasta, timeout=600)


def reunioes(ctx, rec) -> tuple[str, str]:
    pasta = ctx.home / "voice_identity"
    venv = pasta / ".venv"
    ctx.console.dizer("Instalando o ambiente de identificação de vozes...")
    if not sistema.criar_venv(ctx.ex, ctx.home, venv, pasta / "requirements.txt").ok:
        return ERRO, "falhou ao instalar o ambiente voice_identity"
    ctx.console.dizer("Baixando modelos de diarização...")
    if not ctx.ex.rodar([str(sistema.python_do_venv(venv)), "download_models.py"], cwd=pasta, interativo=True, timeout=1800).ok:
        return ERRO, "falhou ao baixar os modelos de diarização"

    msgs, status = [], OK
    s_modelo, m_modelo = modelo_falante(ctx, pasta / "models")
    msgs.append(m_modelo)
    if s_modelo == ERRO:
        return ERRO, "; ".join(msgs)
    if s_modelo == AVISO:
        status = AVISO

    config = pasta / "settings.json"
    if not config.exists():
        shutil.copyfile(pasta / "settings.example.json", config)
    ctx.console.dizer("Baixando o Whisper das atas (alguns GB)...")
    if not baixar_whisper(ctx, WHISPER_REUNIOES).ok:
        status = AVISO
        msgs.append("falhou ao baixar o Whisper das atas (rode de novo para tentar)")
    if not testar_motor(ctx, venv, pasta).ok:
        return ERRO, "; ".join(msgs + ["o motor de identificação de vozes não abriu"])
    msgs.append("cadastre as vozes no painel 'Vozes' da face ou na página de reuniões (127.0.0.1:17850)")
    return status, "; ".join(msgs)
