"""Bounded YouTube lookup and explicit Windows media-session control."""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

MAX_HTML = 4 * 1024 * 1024
TTL = 600
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def search_url(query: str) -> str:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 180:
        raise ValueError("Informe uma busca de até 180 caracteres.")
    return "https://www.youtube.com/results?search_query=" + quote_plus(query.strip())


def _label(node: dict) -> str:
    return str(node.get("simpleText") or "".join(str(r.get("text", "")) for r in node.get("runs", []) if isinstance(r, dict)))[:300]


def parse_videos(html: str) -> list[dict]:
    """Only organic primary video results; never follow endpoints from HTML."""
    marker = re.search(r'(?:var\s+ytInitialData|window\["ytInitialData"\])\s*=\s*', html)
    if not marker:
        raise ValueError("O YouTube não forneceu resultados legíveis; use a busca no navegador.")
    try:
        data, _ = json.JSONDecoder().raw_decode(html, marker.end())
        sections = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"]
        rows = []
        seen = set()
        for section in sections:
            for item in section.get("itemSectionRenderer", {}).get("contents", []):
                video = item.get("videoRenderer", {})
                key = video.get("videoId", "")
                if not isinstance(key, str) or not VIDEO_ID.fullmatch(key) or key in seen:
                    continue
                seen.add(key)
                rows.append({"id": key, "title": _label(video.get("title", {})),
                             "channel": _label(video.get("ownerText", {})),
                             "duration": _label(video.get("lengthText", {}))})
                if len(rows) == 5:
                    return rows
        return rows
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise ValueError("Formato do YouTube indisponível; use a busca no navegador.") from None


def fetch_videos(query: str) -> list[dict]:
    request = Request(search_url(query), headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR,pt;q=0.9"})
    with urlopen(request, timeout=6) as response:
        if not response.geturl().startswith("https://www.youtube.com/"):
            raise ValueError("O YouTube exige acesso pelo navegador.")
        payload = response.read(MAX_HTML + 1)
    if len(payload) > MAX_HTML:
        raise ValueError("Resposta do YouTube excedeu o limite; use a busca no navegador.")
    return parse_videos(payload.decode("utf-8", "replace"))


def control(action: str, app: str = "", level: int = -1) -> dict:
    if action not in {"status", "play", "pause", "next", "previous", "volume"}:
        raise ValueError("Comando de mídia inválido.")
    if not isinstance(app, str) or len(app) > 250 or (action != "status" and not app):
        raise ValueError("Escolha uma sessão de mídia.")
    if type(level) is not int or not -1 <= level <= 100:
        raise ValueError("Volume deve ser de 0 a 100.")
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-File",
                                 str(Path(__file__).with_name("media_control.ps1")), "-Action", action, "-AppId", app,
                                 "-Level", str(level)],
                                capture_output=True, timeout=10, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(result.stdout.decode("utf-8-sig"))
        if result.returncode or not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            raise ValueError("Invalid media response")
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"ok": False, "error": "Controle de mídia indisponível. Não houve nova tentativa."}


def is_spotify(app: str) -> bool:
    return app.casefold() == "spotify.exe" or app.casefold().startswith("spotifyab.spotifymusic_")


class MediaSession:
    def __init__(self, *, fetch=fetch_videos, opener=None, clock=time.monotonic, controller=control, spotify=None,
                 spotify_volume=None):
        self.fetch, self.opener, self.clock, self.controller = fetch, opener, clock, controller
        self.spotify, self.spotify_volume = spotify, spotify_volume
        self.videos: dict[str, tuple[float, dict]] = {}
        self.apps: dict[str, tuple[float, str]] = {}
        self.lock = threading.Lock()
        self.query = ""
        self.selected_video: str | None = None

    def dispatch(self, action: str, body: dict) -> dict:
        if not self.lock.acquire(blocking=False):
            raise ValueError("Aguarde o comando de mídia em andamento.")
        try:
            if action == "videos.search":
                return self.search(body.get("query"))
            if action == "videos.open":
                ref = body.get("ref")
                if ref is None and "number" not in body:
                    ref = self.selected_video or (next(iter(self.videos)) if len(self.videos) == 1 else None)
                if "number" in body:
                    number = body["number"]
                    if type(number) is not int or not 1 <= number <= len(self.videos):
                        raise ValueError("Escolha um número da última busca de vídeos.")
                    ref = list(self.videos)[number - 1]
                row = self._resolve(self.videos, ref)
                url = "https://www.youtube.com/watch?v=" + row["id"]
                (self.opener or os.startfile)(url)
                self.selected_video = ref
                return {"message": "Solicitei a abertura do vídeo escolhido no navegador.", "url": url}
            if action == "media.status":
                data = self.controller("status")
                if not data.get("ok"):
                    return data
                self.apps.clear()
                rows = []
                for row in data.get("sessions", [])[:20]:
                    ref = secrets.token_urlsafe(18)
                    self.apps[ref] = (self.clock(), row["app"])
                    rows.append({"ref": ref, "app": row["app"], "status": row["status"]})
                return {"sessions": rows}
            if action == "spotify.play":
                query = body.get("query")
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
                    raise ValueError("Diga o que tocar no Spotify (até 120 caracteres).")
                if self.spotify is None:
                    from spotify_api import play
                    self.spotify = play
                return self.spotify(query.strip())
            if action == "media.volume":
                app = self._resolve(self.apps, body.get("ref"))
                level, step = body.get("level", -1), body.get("step")
                if step is not None and (type(step) is not int or not -100 <= step <= 100 or not step):
                    raise ValueError("Passo de volume inválido.")
                if type(level) is not int or not -1 <= level <= 100:
                    raise ValueError("Volume deve ser de 0 a 100.")
                spotify = is_spotify(app) and self._spotify_volume()
                # O volume que se ouve mudar é o do próprio Spotify; o do mixer do Windows costuma estar em 100%.
                setter = spotify or (lambda value: self.controller("volume", app, value))
                if step is not None:  # "sobe/abaixa": o cálculo é aqui, não um número chutado pelo modelo
                    current = setter(-1)
                    if not current.get("ok") or not isinstance(current.get("volume"), int):
                        return current
                    level = max(0, min(100, current["volume"] + step))
                if spotify:
                    return spotify(level)
                result = setter(level)
                if result.get("ok"):
                    result["message"] = (f"Volume do aplicativo em {result['volume']}%."
                                         if level < 0 else f"Volume do aplicativo confirmado em {result['volume']}%.")
                return result
            if action == "media.command":
                app = self._resolve(self.apps, body.get("ref"))
                command = body.get("command")
                if command not in {"play", "pause", "next", "previous"}:
                    raise ValueError("Comando de mídia inválido.")
                result = self.controller(command, app)
                if result.get("ok"):
                    result["message"] = "Comando aceito pelo aplicativo." if command in {"next", "previous"} else "Estado de reprodução confirmado pelo aplicativo."
                return result
            raise ValueError("Ação de mídia desconhecida.")
        finally:
            self.lock.release()

    def _spotify_volume(self):
        if self.spotify_volume is None:
            import spotify_api
            if not spotify_api.authorized():
                return None
            self.spotify_volume = spotify_api.volume
        return self.spotify_volume

    def _resolve(self, mapping: dict, ref: str):
        row = mapping.get(ref) if isinstance(ref, str) else None
        if not row or self.clock() - row[0] > TTL:
            raise ValueError("Referência vencida; atualize os resultados.")
        return row[1]

    def search(self, query: str) -> dict:
        fallback = search_url(query)
        self.videos.clear()
        self.selected_video = None
        self.query = query
        try:
            rows = self.fetch(query)
        except (OSError, ValueError, TimeoutError):
            return {"results": [], "fallback": fallback, "message": "Não consegui listar vídeos. Abra a busca no navegador."}
        results = []
        for row in rows[:5]:
            if not VIDEO_ID.fullmatch(row.get("id", "")):
                continue
            ref = secrets.token_urlsafe(18)
            self.videos[ref] = (self.clock(), row)
            results.append({"ref": ref, "title": row["title"], "channel": row.get("channel", ""), "duration": row.get("duration", "")})
        return {"results": results, "fallback": fallback}
