"""Telegram (gateway do Hermes) e WhatsApp (Evolution API em Docker)."""
from __future__ import annotations

import json
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import marcadores, sistema
from ..estado import AVISO, ERRO, OK

EVOLUTION_URL = "http://127.0.0.1:8090"
INSTANCIA = "jarvis"
_ID = re.compile(r"^\d{5,15}$")
_TOKEN_BOT = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")


# ---------- Telegram ----------

def telegram(ctx, rec) -> tuple[str, str]:
    con = ctx.console
    if not sistema.ler_env(ctx.home, "TELEGRAM_BOT_TOKEN"):
        con.dizer("\nCrie um bot no Telegram: abra @BotFather, mande /newbot e copie o token que ele devolver.")
        token = con.segredo("Token do bot (não aparece na tela)")
        if not _TOKEN_BOT.match(token):
            return ERRO, "token do bot inválido; rode de novo e cole o token do @BotFather"
        sistema.gravar_env(ctx.home, "TELEGRAM_BOT_TOKEN", token)
    if not sistema.ler_env(ctx.home, "TELEGRAM_ALLOWED_USERS"):
        con.dizer("Descubra seu número de usuário mandando qualquer mensagem para @userinfobot.")
        uid = con.perguntar("Seu ID numérico do Telegram")
        if not _ID.match(uid):
            return ERRO, "ID do Telegram inválido (só números)"
        sistema.gravar_env(ctx.home, "TELEGRAM_ALLOWED_USERS", uid)
        sistema.gravar_env(ctx.home, "TELEGRAM_HOME_CHANNEL", uid)
    hermes, env = sistema.hermes(ctx.home), sistema.env_hermes(ctx.home)
    if not ctx.ex.rodar([hermes, "gateway", "install", "--start-now", "--start-on-login"], env=env, timeout=300).ok:
        return AVISO, "Telegram configurado, mas o serviço do gateway não foi instalado; rode 'hermes gateway install'"
    if not ctx.ex.rodar([hermes, "gateway", "status"], env=env, timeout=120).ok:
        return AVISO, "gateway instalado, mas não respondeu ao status; rode 'hermes gateway status'"
    return OK, "Telegram ativo; mande uma mensagem para o seu bot para testar"


# ---------- WhatsApp ----------

def pasta_evolution(ctx) -> Path:
    # wa.py procura o .env neste lugar por padrão (EVOLUTION_ENV_FILE).
    return Path(ctx.estado.perfil.pasta_usuario) / "Projects" / "evolution-whatsapp"


def preparar_pasta(ctx) -> Path:
    destino = pasta_evolution(ctx)
    marcadores.copiar(ctx.origem / "templates" / "evolution-whatsapp", destino, ctx.valores)
    env = destino / "sistema" / ".env"
    if not env.exists():
        texto = (destino / "sistema" / ".env.example").read_text(encoding="utf-8")
        senha = secrets.token_hex(24)
        texto = texto.replace("TROQUE_POR_SENHA_FORTE", senha).replace("TROQUE_POR_CHAVE_ALEATORIA", secrets.token_hex(32))
        if "TROQUE_POR" in texto:
            raise ValueError("modelo .env do Evolution tem marcador TROQUE_POR não previsto")
        env.write_text(texto, encoding="utf-8")
    return env


def chave_api(env: Path) -> str:
    for linha in env.read_text(encoding="utf-8").splitlines():
        if linha.startswith("AUTHENTICATION_API_KEY="):
            return linha.split("=", 1)[1].strip().strip("'\"")
    raise ValueError("AUTHENTICATION_API_KEY ausente no .env do Evolution")


def chamar(metodo: str, caminho: str, chave: str, corpo: dict | None = None, timeout: float = 20) -> tuple[int, dict]:
    dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(EVOLUTION_URL + caminho, data=dados, method=metodo,
                                 headers={"apikey": chave, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def esperar_api(chave: str, limite: float = 240, pausa: float = 5) -> bool:
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        if chamar("GET", "/instance/fetchInstances", chave)[0] == 200:
            return True
        time.sleep(pausa)
    return False


def criar_instancia(chave: str) -> bool:
    status, _ = chamar("GET", f"/instance/connectionState/{INSTANCIA}", chave)
    if status == 200:
        return True
    corpo = {"instanceName": INSTANCIA, "integration": "WHATSAPP-BAILEYS", "qrcode": True,
             "readMessages": False, "readStatus": False}
    return chamar("POST", "/instance/create", chave, corpo, timeout=60)[0] in (200, 201)


def whatsapp(ctx, rec) -> tuple[str, str]:
    con = ctx.console
    if not ctx.ex.rodar(["docker", "version"], timeout=60).ok:
        return ERRO, "Docker não encontrado ou parado: instale/abra o Docker Desktop e rode de novo"
    env = preparar_pasta(ctx)
    pasta = env.parent.parent
    con.dizer("Subindo o Evolution API no Docker (a primeira vez baixa as imagens)...")
    if not ctx.ex.rodar(["docker", "compose", "up", "-d"], cwd=pasta, interativo=True, timeout=1800).ok:
        return ERRO, "docker compose up falhou"
    chave = chave_api(env)
    if not esperar_api(chave):
        return ERRO, "o Evolution API não respondeu em 127.0.0.1:8090"
    if not criar_instancia(chave):
        return ERRO, "não consegui criar a instância 'jarvis' no Evolution"
    con.dizer("Vai abrir um QR code no navegador: no celular, WhatsApp > Aparelhos conectados > Conectar aparelho.")
    ctx.ex.rodar([sistema.python_do_venv(ctx.home / "hermes-agent" / "venv"), "conectar.py", INSTANCIA],
                 cwd=pasta / "sistema", interativo=True, timeout=900)
    estado = chamar("GET", f"/instance/connectionState/{INSTANCIA}", chave)[1]
    if (estado.get("instance") or {}).get("state") != "open":
        return AVISO, f"Evolution no ar, mas o WhatsApp não foi pareado; rode de novo ou use {EVOLUTION_URL}/manager"
    return OK, f"WhatsApp pareado (pilha Docker em {pasta})"
