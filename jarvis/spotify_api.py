"""Spotify Web API pelo app "Jarvis" do __JARVIS_USER__ (painel de desenvolvedor, modo de desenvolvimento).

Login uma vez por PKCE: sem client secret no PC; só o Client ID (público) no config.toml e o token em
`database/.spotify_token.json` (fora do Git). Tocar exige conta Premium e um aparelho do Spotify disponível
(o app no PC serve). "Tocou" só é dito depois que o próprio Spotify confirma o que está tocando.

    python spotify_api.py login     # abre o consentimento no navegador; o senhor aprova lá
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import socket
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / "database" / ".spotify_token.json"
LAUNCH_WAIT_STEPS = 10  # segundos esperando o app do PC aparecer depois de aberto
APP_READY_S = 3.0
REDIRECT = "http://127.0.0.1:8765/callback"  # o mesmo cadastrado no app do painel
SCOPES = "user-read-playback-state user-modify-playback-state"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
PLAYLIST_HINT = re.compile(r"^(?:a\s+)?(?:playlist|lista)\s+(?:de\s+|do\s+|da\s+)?(.+)$|^(?:m[uú]sicas?\s+)?(?:de\s+)?(sertanejo|rock|pagode|samba|forr[oó]|mpb|funk|gospel|jazz|cl[aá]ssica|lo-?fi|eletr[oô]nica|rap|reggae)$",
                           re.IGNORECASE)


def _fold(text: str) -> str:
    import unicodedata
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", plain)


class SpotifyError(RuntimeError):
    """Mensagem segura para o senhor; nunca inclui token nem resposta crua."""


def client_id() -> str:
    try:
        with open(HERE / "config.toml", "rb") as fh:
            value = str(tomllib.load(fh).get("spotify", {}).get("client_id", "")).strip()
    except (OSError, tomllib.TOMLDecodeError):
        value = ""
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise SpotifyError("Spotify não configurado: falta o Client ID em config.toml [spotify].")
    return value


def _post_form(url: str, fields: dict) -> dict:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SpotifyError("O Spotify recusou a autorização. Faça o login de novo.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SpotifyError("Spotify fora do alcance (rede).") from exc


def _save(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _store(reply: dict, previous: Optional[dict], path: Path, now: float) -> dict:
    data = {"access_token": reply["access_token"],
            "refresh_token": reply.get("refresh_token") or (previous or {}).get("refresh_token", ""),
            "expires_at": now + int(reply.get("expires_in", 3600)), "scope": reply.get("scope", "")}
    _save(data, path)
    return data


# ── login (PKCE) ─────────────────────────────────────────────────────────────
def login(open_url: Callable[[str], object] = webbrowser.open, timeout: float = 300,
          path: Path = TOKEN_FILE, port: int = 8765) -> str:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    cid = client_id()
    result: dict = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — nome exigido pelo http.server
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != "/callback" or query.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            result.update(code=query.get("code", [""])[0], error=query.get("error", [""])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<p>Jarvis autorizado no Spotify. Pode fechar esta aba.</p>".encode()
                             if result["code"] else "<p>Autorização não concedida.</p>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Callback)
    server.timeout = 1
    open_url(AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state}))
    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result.get("code"):
        raise SpotifyError("Login no Spotify não concluído (recusado ou tempo esgotado).")
    reply = _post_form(TOKEN_URL, {"grant_type": "authorization_code", "code": result["code"],
                                   "redirect_uri": REDIRECT, "client_id": cid, "code_verifier": verifier})
    _store(reply, None, path, time.time())
    return "Spotify autorizado."


# ── chamadas à API ───────────────────────────────────────────────────────────
class Client:
    def __init__(self, path: Path = TOKEN_FILE, opener=urllib.request.urlopen, clock=time.time, sleep=time.sleep,
                 launch: Optional[Callable[[], object]] = None, local_name: str | None = None):
        self.path, self.opener, self.clock, self.sleep = path, opener, clock, sleep
        self.launch = launch or (lambda: __import__("os").startfile("spotify:"))
        self.local_name = (socket.gethostname() if local_name is None else local_name).strip().casefold()
        self.lock = threading.Lock()

    def token(self) -> str:
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise SpotifyError("Spotify ainda não autorizado. Rode: python spotify_api.py login") from None
            if data.get("expires_at", 0) - 60 < self.clock():
                if not data.get("refresh_token"):
                    raise SpotifyError("Autorização do Spotify vencida. Faça o login de novo.")
                reply = _post_form(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                               "client_id": client_id()})
                data = _store(reply, data, self.path, self.clock())
            return data["access_token"]

    def call(self, method: str, route: str, params: Optional[dict] = None, body: Optional[dict] = None):
        url = API + route + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": "Bearer " + self.token(),
                                                  "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=10) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise SpotifyError("O Spotify negou o comando (é preciso Premium e o app autorizado).") from None
            if exc.code == 404:
                raise SpotifyError("Nenhum aparelho do Spotify disponível. Abra o Spotify no PC ou no celular.") from None
            if exc.code == 429:
                raise SpotifyError("Spotify pediu para aguardar; tente em instantes.") from None
            raise SpotifyError(f"O Spotify recusou o comando (HTTP {exc.code}).") from None
        except (urllib.error.URLError, TimeoutError):
            raise SpotifyError("Spotify fora do alcance (rede).") from None

    # ── tocar ────────────────────────────────────────────────────────────────
    def find(self, query: str) -> dict:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
            raise SpotifyError("Diga o que tocar (até 120 caracteres).")
        hint = PLAYLIST_HINT.match(query.strip())
        kind = "playlist" if hint else "track"
        term = (hint.group(1) or hint.group(2)) if hint else query.strip()
        # market=BR: "from_token" exige o escopo user-read-private, que o app não pede.
        _, data = self.call("GET", "/search", {"q": term, "type": kind, "limit": 5, "market": "BR"})
        items = [i for i in (data.get(kind + "s") or {}).get("items") or [] if isinstance(i, dict) and i.get("uri")]
        if not items:
            raise SpotifyError(f"Não encontrei {'playlist' if kind == 'playlist' else 'música'} para “{term}”.")

        def by(item):
            return ", ".join(a.get("name", "") for a in item.get("artists", [])[:2]) if kind == "track" \
                else (item.get("owner") or {}).get("display_name", "")
        words = set(_fold(term).split())
        # O 1º resultado do Spotify nem sempre é o pedido; vence o que tem mais palavras do pedido (empate: ordem dele).
        top = max(items, key=lambda i: len(words & set(_fold(i.get("name", "") + " " + by(i)).split())))
        return {"kind": kind, "uri": top["uri"], "name": top.get("name", ""), "by": by(top),
                "album": (top.get("album") or {}).get("uri", "") if kind == "track" else ""}

    def device(self) -> str:
        """O aparelho que já está tocando; senão o Spotify deste PC (abre se preciso). Nunca uma caixa de som
        escolhida por conta própria: "Todo lugar" tocaria na casa inteira."""
        for attempt in range(LAUNCH_WAIT_STEPS + 1):
            _, data = self.call("GET", "/me/player/devices")
            devices = [d for d in data.get("devices", []) if isinstance(d, dict) and d.get("id") and not d.get("is_restricted")]
            chosen = [d for d in devices if d.get("is_active")] or [
                d for d in devices if d.get("type") == "Computer" and self.local_name
                and str(d.get("name", "")).strip().casefold() == self.local_name]
            if len(chosen) > 1:
                raise SpotifyError("Mais de um aparelho corresponde ao destino. Selecione um aparelho no Spotify e tente de novo.")
            if len(chosen) == 1:
                if attempt:
                    self.sleep(APP_READY_S)  # o app recém-aberto aparece antes de aceitar comandos (visto em 17/10)
                return chosen[0]["id"]
            if attempt == 0:
                self.launch()
            self.sleep(1.0)
        raise SpotifyError("Não identifiquei o Spotify deste PC. Selecione o aparelho no Spotify e tente de novo.")

    def play(self, query: str) -> dict:
        choice = self.find(query)
        device = self.device()
        # Faixa solta ("uris") é aceita com 204 e ignorada pelo app do PC (17/10); dentro do álbum ela toca.
        if choice["kind"] == "track":
            body = {"context_uri": choice["album"], "offset": {"uri": choice["uri"]}} if choice["album"] \
                else {"uris": [choice["uri"]]}
        else:
            body = {"context_uri": choice["uri"]}
        self.call("PUT", "/me/player/play", {"device_id": device}, body)
        label = f"{choice['name']}" + (f" — {choice['by']}" if choice["by"] else "")
        for _ in range(6):  # confirma pelo próprio Spotify; não repete o comando
            self.sleep(0.5)
            status, now = self.call("GET", "/me/player")
            playing = status == 200 and now.get("is_playing")
            same = ((now.get("item") or {}).get("uri") == choice["uri"] if choice["kind"] == "track"
                    else (now.get("context") or {}).get("uri") == choice["uri"])
            if playing and same:
                return {"ok": True, "message": f"Tocando no Spotify: {label}.", "confirmed": True}
        return {"ok": True, "confirmed": False,
                "message": f"Pedi ao Spotify para tocar {label}, mas ele ainda não confirmou. Não repeti o comando."}


    # ── volume ───────────────────────────────────────────────────────────────
    def volume(self, level: int) -> dict:
        """Volume do próprio Spotify (o controle que o senhor vê no app), no aparelho em uso. -1 só lê."""
        if type(level) is not int or not -1 <= level <= 100:
            raise SpotifyError("Volume deve ser de 0 a 100.")
        _, data = self.call("GET", "/me/player/devices")
        active = [d for d in data.get("devices", []) if isinstance(d, dict) and d.get("is_active")]
        if not active:
            raise SpotifyError("Nada tocando no Spotify agora; não há volume para ajustar.")
        device, before = active[0], active[0].get("volume_percent")
        if level < 0:
            return {"ok": True, "volume": before, "message": f"Volume do Spotify em {before}%."}
        if device.get("supports_volume") is False:
            raise SpotifyError(f"O aparelho {device.get('name', '')} não aceita controle de volume.")
        self.call("PUT", "/me/player/volume", {"volume_percent": level, "device_id": device["id"]})
        for _ in range(6):  # confirma lendo de volta; não repete o comando
            self.sleep(0.5)
            _, data = self.call("GET", "/me/player/devices")
            now = next((d.get("volume_percent") for d in data.get("devices", []) if d.get("id") == device["id"]), None)
            if isinstance(now, int) and abs(now - level) <= 2:
                return {"ok": True, "volume": now, "message": f"Volume do Spotify: {before}% → {now}%."}
        return {"ok": False, "volume": before, "error": "O Spotify não confirmou o novo volume. Não repeti o comando."}


_client: Optional[Client] = None


def play(query: str) -> dict:
    """Ponto único para voz, texto e painel. Nunca levanta exceção: devolve ok/erro com mensagem segura."""
    global _client
    _client = _client or Client()
    try:
        return _client.play(query)
    except SpotifyError as exc:
        return {"ok": False, "error": str(exc)}


def volume(level: int) -> dict:
    global _client
    _client = _client or Client()
    try:
        return _client.volume(level)
    except SpotifyError as exc:
        return {"ok": False, "error": str(exc)}


def authorized(path: Path = TOKEN_FILE) -> bool:
    return path.is_file()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["login"]:
        print(login())
    elif len(sys.argv) > 2 and sys.argv[1] == "play":
        print(play(" ".join(sys.argv[2:])))
    else:
        print(__doc__)
