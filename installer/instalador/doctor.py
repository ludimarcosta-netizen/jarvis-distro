"""Diagnóstico final: confere o que cada recurso instalado precisa e lista o que ficou pendente."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import sistema
from .estado import ICONES, OK
from .recursos import Catalogo

Checagem = tuple[str, bool]


def _py(pasta: Path) -> bool:
    return sistema.python_do_venv(pasta).exists()


def _face(ctx) -> list[Checagem]:
    from .passos.face import EXE
    return [("app desktop compilado", (ctx.home / "hermes-agent" / EXE).exists())]


def _voz(ctx) -> list[Checagem]:
    j = ctx.home / "jarvis"
    kokoro = j / "models" / "kokoro"
    return [
        ("ambiente .venv311", _py(j / ".venv311")),
        ("ambiente do Kokoro", _py(j / ".venv-kokoro-gpu") or _py(j / ".venv-kokoro")),
        ("modelo Kokoro", all((kokoro / n).is_file() for n in ("config.json", "kokoro-v1_0.pth", "pm_santa.pt"))),
        ("voice_mode.json", (j / "voice_mode.json").is_file()),
        ("chave do Gemini (GOOGLE_API_KEY)", sistema.ler_env(ctx.home, "GOOGLE_API_KEY")),
    ]


def _reunioes(ctx) -> list[Checagem]:
    v = ctx.home / "voice_identity"
    return [
        ("ambiente voice_identity", _py(v / ".venv")),
        ("modelos de diarização", all((v / "models" / n).is_file() for n in ("segmentation.onnx", "speaker.onnx",
                                                                             "speaker-ecapa512-sherpa.onnx"))),
    ]


def _cerebro(ctx) -> list[Checagem]:
    return [("cofre com a pasta pessoas", (Path(ctx.estado.perfil.pasta_cerebro) / "pessoas").is_dir())]


def _telegram(ctx) -> list[Checagem]:
    return [(f"{k} no .env", sistema.ler_env(ctx.home, k)) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS")]


def _whatsapp(ctx) -> list[Checagem]:
    from .passos.canais import pasta_evolution
    return [(".env do Evolution", (pasta_evolution(ctx) / "sistema" / ".env").is_file())]


def _pdf(ctx) -> list[Checagem]:
    return [("ambiente runtime/pdf-signing", _py(ctx.home / "runtime" / "pdf-signing"))]


def _prefeitura(ctx) -> list[Checagem]:
    from .passos.documentos import pasta_oficios
    return [("base de ofícios", (pasta_oficios(ctx) / "oficios.json").is_file())]


def _spotify(ctx) -> list[Checagem]:
    return [("login do Spotify", (ctx.home / "jarvis" / "database" / ".spotify_token.json").is_file())]


def _homeassistant(ctx) -> list[Checagem]:
    return [("HASS_TOKEN no .env", sistema.ler_env(ctx.home, "HASS_TOKEN"))]


CHECAGENS: dict[str, Callable] = {
    "face": _face, "voz": _voz, "reunioes": _reunioes, "cerebro": _cerebro, "telegram": _telegram,
    "whatsapp": _whatsapp, "pdf": _pdf, "prefeitura": _prefeitura, "spotify": _spotify, "homeassistant": _homeassistant,
}


def checagens(ctx, cat: Catalogo, rid: str) -> list[Checagem]:
    rec = cat.recursos[rid]
    itens = [(f"arquivos em {c.para}", (ctx.home / c.para).exists()) for c in rec.copiar]
    if rid in CHECAGENS:
        itens += CHECAGENS[rid](ctx)
    return itens


def rodar(ctx, cat: Catalogo) -> int:
    """Mostra o diagnóstico; devolve quantos itens ficaram pendentes."""
    con = ctx.console
    con.dizer("\n== Diagnóstico ==")
    pendentes = 0
    for rid in cat.ordem(ctx.estado.escolhidos):
        sit = ctx.estado.situacao(rid)
        falhas = [nome for nome, ok in checagens(ctx, cat, rid) if not ok]
        icone = ICONES[sit.status] if sit.status != OK or not falhas else ICONES["aviso"]
        con.dizer(f"{icone} {cat.recursos[rid].nome}")
        for nome in falhas:
            con.dizer(f"     falta: {nome}")
        if sit.status != OK and sit.mensagem:
            con.dizer(f"     {sit.mensagem}")
        pendentes += len(falhas) + (sit.status != OK)
    s = ctx.ex.rodar([sistema.hermes(ctx.home), "doctor"], env=sistema.env_hermes(ctx.home), timeout=600)
    con.dizer(("✅" if s.ok else "⚠️ ") + " hermes doctor" + ("" if s.ok else " apontou problemas (rode 'hermes doctor' para ver)"))
    pendentes += not s.ok
    con.dizer("Tudo certo." if not pendentes else f"{pendentes} pendência(s). Rode o instalador de novo para tentar.")
    return pendentes
