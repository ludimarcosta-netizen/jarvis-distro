"""Google, Spotify, navegador controlado, Antigravity, Home Assistant e lembrete diário."""
from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from .. import sistema
from ..estado import AVISO, ERRO, OK
from .face import habilitar_plugin
from .voz import garantir_venv_principal, venv_principal

REDIRECT_SPOTIFY = "http://127.0.0.1:8765/callback"
_CLIENT_ID = re.compile(r"^[0-9a-f]{32}$")
_LINHA_SPOTIFY = re.compile(r'(?m)^client_id\s*=\s*"[^"\n]*"')


def _hermes(ctx, *args, **kw) -> sistema.Saida:
    return ctx.ex.rodar([sistema.hermes(ctx.home), *args], env=sistema.env_hermes(ctx.home), **kw)


def _py_hermes(ctx) -> str:
    return str(sistema.python_do_venv(ctx.home / "hermes-agent" / "venv"))


# ---------- Google ----------

def _setup_google(ctx) -> Path | None:
    for base in (ctx.home / "skills", ctx.home / "hermes-agent" / "skills"):
        arq = base / "productivity" / "google-workspace" / "scripts" / "setup.py"
        if arq.exists():
            return arq
    return None


def google(ctx, rec) -> tuple[str, str]:
    setup = _setup_google(ctx)
    if setup is None:
        return ERRO, "skill google-workspace do Hermes não encontrada"
    py, con = _py_hermes(ctx), ctx.console
    if ctx.ex.rodar([py, str(setup), "--check"], timeout=120).ok:
        return OK, "Google já autorizado"
    con.dizer("\nNo Google Cloud Console: crie um projeto, ative Gmail e Calendar API, crie uma credencial\n"
              "OAuth do tipo 'App para computador' e baixe o JSON.")
    caminho = con.perguntar("Caminho do JSON baixado").strip('"')
    if not Path(caminho).is_file():
        return ERRO, f"arquivo não encontrado: {caminho}"
    for args in (["--install-deps"], ["--client-secret", caminho]):
        if not ctx.ex.rodar([py, str(setup), *args], interativo=args[0] == "--install-deps", timeout=900).ok:
            return ERRO, f"setup do Google falhou em {args[0]}"
    url = ctx.ex.rodar([py, str(setup), "--auth-url"], timeout=120)
    if not url.ok:
        return ERRO, "não consegui gerar o link de autorização do Google"
    con.dizer("Abra este link, autorize e copie o código (ou o endereço inteiro da página final):\n" + url.texto.strip())
    codigo = con.perguntar("Código de autorização")
    if not codigo or not ctx.ex.rodar([py, str(setup), "--auth-code", codigo], timeout=120, ocultar=1).ok:
        return ERRO, "o Google recusou o código; rode de novo"
    if not ctx.ex.rodar([py, str(setup), "--check"], timeout=120).ok:
        return ERRO, "autorização do Google não confirmada"
    return OK, "Agenda e Gmail autorizados"


# ---------- Spotify ----------

def gravar_client_id(config: Path, client_id: str) -> None:
    texto = config.read_text(encoding="utf-8")
    novo = f'client_id = "{client_id}"'
    if _LINHA_SPOTIFY.search(texto):
        texto = _LINHA_SPOTIFY.sub(novo, texto, count=1)
    else:
        texto = texto.rstrip() + f"\n\n[spotify]\n{novo}\n"
    config.write_text(texto, encoding="utf-8")


def spotify(ctx, rec) -> tuple[str, str]:
    con, j = ctx.console, ctx.home / "jarvis"
    if not garantir_venv_principal(ctx):
        return ERRO, "falhou ao instalar o ambiente Python do Jarvis"
    con.dizer("\nEm https://developer.spotify.com/dashboard crie um app (Web API), coloque o Redirect URI\n"
              f"{REDIRECT_SPOTIFY} e adicione seu e-mail em 'User Management'. Precisa de Spotify Premium.")
    cid = con.perguntar("Client ID do app").lower()
    if not _CLIENT_ID.match(cid):
        return ERRO, "Client ID inválido (32 caracteres hexadecimais)"
    gravar_client_id(j / "config.toml", cid)
    py = str(sistema.python_do_venv(venv_principal(ctx)))
    con.dizer("Vai abrir o navegador para você autorizar o Jarvis no Spotify.")
    ctx.ex.rodar([py, str(j / "spotify_api.py"), "login"], cwd=j, interativo=True, timeout=600)
    if not (j / "database" / ".spotify_token.json").is_file():
        return AVISO, "Client ID gravado, mas o login não terminou; rode de novo"
    return OK, "Spotify conectado"


# ---------- Navegador controlado ----------

def achar_chrome() -> str | None:
    candidatos = [shutil.which("chrome.exe") or ""]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(var):
            candidatos.append(str(Path(os.environ[var]) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    return next((c for c in candidatos if c and Path(c).is_file()), None)


def navegador(ctx, rec) -> tuple[str, str]:
    chrome = achar_chrome()
    if not chrome:
        return ERRO, "Google Chrome não encontrado: instale o Chrome e rode de novo"
    falhas = [k for k, v in (("browser.engine", "chrome"), ("browser.cdp_url", "http://127.0.0.1:9222"),
                             ("browser.auto_launch_cdp", "true"))
              if not _hermes(ctx, "config", "set", k, v, timeout=120).ok]
    if falhas:
        return AVISO, f"não consegui gravar: {', '.join(falhas)}"
    return OK, ("o Jarvis abre um Chrome próprio (perfil separado) quando precisar; "
                "faça login nos sites que ele deve usar nessa janela")


# ---------- Antigravity ----------

def achar_agy() -> str | None:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin"
    for nome in ("agy.exe", "agy.cmd", "agy"):
        if (local / nome).is_file():
            return str(local / nome)
    return shutil.which("agy")


def antigravity(ctx, rec) -> tuple[str, str]:
    con = ctx.console
    if not achar_agy():
        if not con.confirmar("O Antigravity CLI (agy) não está instalado. Instalar agora pelo instalador oficial do Google?", True):
            return AVISO, "agy não instalado; o provedor Antigravity fica indisponível"
        ctx.ex.rodar(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                      "irm https://antigravity.google/cli/install.ps1 | iex"], interativo=True, timeout=1800)
        if not achar_agy():
            return ERRO, "a instalação do agy não terminou; abra um terminal novo e rode o instalador de novo"
    if not habilitar_plugin(ctx, "antigravity-oauth"):
        return ERRO, "não consegui habilitar o plugin antigravity-oauth"
    con.dizer("Vai abrir o login da sua conta Google no Antigravity.")
    _hermes(ctx, "auth", "add", "antigravity-oauth", interativo=True, timeout=900)
    if not _hermes(ctx, "auth", "status", "antigravity-oauth", timeout=300).ok:
        return AVISO, "plugin habilitado, mas o login no Antigravity não foi confirmado"
    return OK, "Antigravity conectado (escolha-o em 'hermes model' ou como reserva em 'hermes fallback')"


# ---------- Home Assistant ----------

def testar_hass(url: str, token: str) -> bool:
    req = urllib.request.Request(url.rstrip("/") + "/api/", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def homeassistant(ctx, rec) -> tuple[str, str]:
    con = ctx.console
    url = con.perguntar("Endereço do Home Assistant", "http://homeassistant.local:8123").rstrip("/")
    if not re.match(r"^https?://[^\s/]+", url):
        return ERRO, "endereço inválido"
    con.dizer("No Home Assistant: seu perfil > Segurança > Tokens de acesso de longa duração > Criar token.")
    token = con.segredo("Token (não aparece na tela)")
    if not token:
        return ERRO, "sem token do Home Assistant"
    if not testar_hass(url, token):
        return ERRO, "o Home Assistant não aceitou o token nesse endereço"
    sistema.gravar_env(ctx.home, "HASS_URL", url)
    sistema.gravar_env(ctx.home, "HASS_TOKEN", token)
    if not habilitar_plugin(ctx, "homeassistant"):
        return AVISO, "token válido, mas não consegui habilitar o plugin homeassistant"
    return OK, "Home Assistant conectado"


# ---------- Agendamentos ----------

LEMBRETE = ("Liste as pendências abertas das reuniões (rode `jarvis.py pendencias` no Python do Jarvis) e as atas das "
            "últimas 24 horas. Lembre de forma curta o que está atrasado ou vence hoje. Se não houver nada, diga só isso.")


def agendamentos(ctx, rec) -> tuple[str, str]:
    if ctx.estado.situacao("telegram").status != OK:
        return AVISO, "o lembrete diário é entregue pelo Telegram: instale o recurso Telegram e rode de novo"
    existentes = _hermes(ctx, "cron", "list", timeout=120)
    if "Lembrete diário de pendências" in existentes.texto:
        return OK, "lembrete diário já agendado"
    hora = ctx.console.perguntar("Horário do lembrete diário de pendências (HH:MM)", "08:00")
    m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", hora)
    if not m:
        return ERRO, "horário inválido"
    args = ["cron", "create", f"{int(m.group(2))} {int(m.group(1))} * * *", LEMBRETE,
            "--name", "Lembrete diário de pendências", "--deliver", "telegram"]
    if ctx.estado.situacao("reunioes").status in (OK, AVISO):
        args += ["--skill", "jarvis-meetings"]
    if not _hermes(ctx, *args, timeout=120).ok:
        return ERRO, "não consegui criar o lembrete no cron do Hermes"
    return OK, f"lembrete diário às {hora} pelo Telegram"
