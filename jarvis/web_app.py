"""
Página local de revisão das reuniões: http://127.0.0.1:17850

Lista as reuniões por ano/mês/dia, mostra transcrição e ata, permite definir
participantes, enviar falas para outra reunião (existente ou nova), juntar
reuniões e responder à pergunta "encerrar a reunião?".

Segurança (as transcrições têm fala de terceiros):
- só 127.0.0.1; Host conferido (bloqueia DNS rebinding);
- toda a API exige o header X-Jarvis-Token (o token vai embutido na própria página,
  que outro site não consegue ler); POST só com application/json e Origin local.
"""
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import meetings_offline
import meetings_service as service
from storage import DB_PATH

DEFAULT_PORT = int(os.environ.get("JARVIS_WEB_PORT", "17850"))
WEB_DIR = Path(__file__).resolve().parent / "web"
TOKEN_FILE = Path(__file__).resolve().parent / ".web_token"
_STATIC = {"/app.js": ("app.js", "text/javascript; charset=utf-8"),
           "/style.css": ("style.css", "text/css; charset=utf-8")}
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def load_token(path: Path = TOKEN_FILE) -> str:
    """Token estável entre reinícios (o plugin do app lê o mesmo arquivo)."""
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_hex(24)
    path.write_text(token, encoding="utf-8")
    return token


def _iso(value: Optional[str]) -> Optional[str]:
    """'YYYY-MM-DD HH:MM:SS' (UTC no banco) -> ISO 8601 com Z para o navegador."""
    if not value:
        return value
    try:
        return datetime.strptime(str(value)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return value


_TIME_KEYS = {"started_at", "ended_at", "prompt_at", "prompt_dismissed_at", "ata_next_try_at", "created_at",
              "timestamp", "start_at", "end_at", "offline_next_try_at"}
_SAMPLE_RE = re.compile(r"/api/voices/(\d+)/sample")
_REVIEW_AUDIO_RE = re.compile(r"/api/utterances/(\d+)/audio")


def _times(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (_iso(v) if k in _TIME_KEYS else _times(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_times(v) for v in value]
    return value


class _Handler(BaseHTTPRequestHandler):
    server_version = "JarvisMeetings/1.0"
    db_path: str = DB_PATH
    token: str = ""
    port: int = DEFAULT_PORT

    # ── infraestrutura ────────────────────────────────────────────────────────
    def log_message(self, fmt: str, *args: Any) -> None:  # sem transcrição no log do sentinel
        pass

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in {**_SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(_times(payload), ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    def _token_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Jarvis-Token") or "", self.token)

    def _body(self) -> Dict[str, Any]:
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise PermissionError("Content-Type deve ser application/json.")
        length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("Corpo JSON inválido.")
        return data

    # ── despacho ──────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "Host não permitido."})
        url = urlparse(self.path)
        if method == "GET" and url.path in ("/", "/index.html"):
            return self._index()
        if method == "GET" and url.path in _STATIC:
            name, ctype = _STATIC[url.path]
            return self._send(200, (WEB_DIR / name).read_bytes(), ctype)
        if not url.path.startswith("/api/"):
            return self._json(404, {"error": "Não encontrado."})
        if not self._token_ok() or not self._origin_ok():
            return self._json(403, {"error": "Acesso negado."})
        sample = _SAMPLE_RE.fullmatch(url.path)
        if method == "GET" and sample:
            return self._sample(int(sample.group(1)))
        review_audio = _REVIEW_AUDIO_RE.fullmatch(url.path)
        if method == "GET" and review_audio:
            return self._review_audio(int(review_audio.group(1)))
        route = _match(method, url.path)
        if route is None:
            return self._json(404, {"error": "Rota inexistente."})
        handler, meeting_id = route
        try:
            body = self._body() if method == "POST" else {}
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            self._json(200, handler(self.db_path, meeting_id, body, query))
        except PermissionError as exc:
            self._json(415, {"error": str(exc)})
        except LookupError as exc:
            self._json(404, {"error": str(exc)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc) or "Pedido inválido."})
        except Exception as exc:  # noqa: BLE001 — sem stack trace para o navegador
            print(f"[!] Página de reuniões: {type(exc).__name__}: {exc}")
            self._json(500, {"error": "Erro interno ao processar o pedido."})

    def _sample(self, voice_id: int) -> None:
        """Trecho de ~8 s da voz, para o senhor reconhecer quem é (a página toca via fetch + blob)."""
        try:
            voice = service.repo.get_voice(voice_id, self.db_path)
            if not voice or not voice.get("sample_file"):
                raise LookupError("Amostra não encontrada.")
            body = meetings_offline.sample_path(voice["sample_file"]).read_bytes()
        except LookupError as exc:
            return self._json(404, {"error": str(exc)})
        self._send(200, body, "audio/wav")

    def _index(self) -> None:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__JARVIS_TOKEN__", self.token)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _review_audio(self, utterance_id: int) -> None:
        try:
            sample = service.repo.get_review_sample(utterance_id, self.db_path)
            if not sample:
                raise LookupError("Trecho não encontrado.")
            body = meetings_offline.sample_path(sample).read_bytes()
        except LookupError as exc:
            return self._json(404, {"error": str(exc)})
        self._send(200, body, "audio/wav")


# ── rotas da API ───────────────────────────────────────────────────────────────
Route = Callable[[str, Optional[int], Dict[str, Any], Dict[str, str]], Any]


def _names(body: Dict[str, Any]) -> list:
    names = body.get("participants")
    if not isinstance(names, list):
        raise ValueError("participants deve ser uma lista de nomes.")
    return names


_ROUTES: Dict[Tuple[str, str], Route] = {
    ("GET", "meetings"): lambda db, _m, _b, q: {"meetings": service.repo.list_meetings(
        since=q.get("since"), until=q.get("until"), db_path=db)},
    ("GET", "prompt"): lambda db, _m, _b, _q: {"prompt": service.pending_prompt(db_path=db)},
    ("GET", "meeting"): lambda db, m, _b, _q: service.meeting_detail(m, db_path=db),
    ("POST", "participants"): lambda db, m, b, _q: {"meeting": service.set_participants(m, _names(b), db_path=db)},
    ("POST", "title"): lambda db, m, b, _q: {"meeting": service.set_title(m, str(b.get("title") or ""), db_path=db)},
    ("POST", "close"): lambda db, m, b, _q: _deferred(db, {"meeting": service.close_meeting(
        m, closed_by="user", participants=_names(b) if "participants" in b else None, db_path=db, defer_ata=True)}),
    ("POST", "continue"): lambda db, m, _b, _q: service.continue_meeting(m, db_path=db) or {"ok": True},
    ("POST", "move"): lambda db, m, b, _q: _deferred(db, service.move_transcripts(
        m, b.get("transcript_ids") or [], target_id=b.get("target_id"), new_title=str(b.get("new_title") or ""),
        db_path=db, defer_ata=True)),
    ("POST", "merge"): lambda db, m, b, _q: _deferred(db, {"meeting": service.merge_meetings(
        m, int(b["other_id"]), db_path=db, defer_ata=True)}),
    ("POST", "regenerate"): lambda db, m, _b, _q: _deferred(db, _mark_pending(db, m)),
    ("POST", "voices"): lambda db, m, b, _q: _deferred(db, {"meeting": service.set_voices(
        m, b.get("voices") if isinstance(b.get("voices"), list) else [], db_path=db)}),
    ("POST", "delete"): lambda db, m, _b, _q: service.delete_meeting(m, db_path=db) or {"ok": True},
    ("POST", "reprocess"): lambda db, m, _b, _q: _kick(db, {"meeting": service.reprocess_offline(m, db_path=db)}),
    ("POST", "review"): lambda db, m, b, _q: _resolve_review(db, m, b),
    ("POST", "speaker-counts"): lambda db, m, b, _q: {"meeting": service.set_speaker_counts(m, b, db)},
}


def _resolve_review(db_path: str, meeting_id: int, body: dict[str, Any]) -> dict:
    service.repo.resolve_review(meeting_id, int(body.get("utterance_id")), body.get("choice"),
                                body.get("text", ""), db_path)
    return _deferred(db_path, {"ok": True})


_regen_lock = threading.Lock()


def _deferred(db_path: str, payload: Any) -> Any:
    """Responde já; as atas marcadas como pendentes saem numa thread (Vertex leva segundos)."""
    def run() -> None:
        with _regen_lock:  # uma geração por vez, mesmo com cliques seguidos
            service.generate_pending_atas(db_path=db_path)
    threading.Thread(target=run, daemon=True, name="jarvis-meetings-ata").start()
    return payload


def _kick(db_path: str, payload: Any) -> Any:
    meetings_offline.kick(db_path)
    return payload


def _mark_pending(db_path: str, meeting_id: Optional[int]) -> dict:
    service.repo.update_meeting(int(meeting_id), db_path=db_path, ata_status="pending", ata_attempts=0,
                                ata_next_try_at=None)
    return {"meeting": service.repo.get_meeting(int(meeting_id), db_path)}


def _match(method: str, path: str) -> Optional[Tuple[Route, Optional[int]]]:
    """/api/meetings | /api/prompt | /api/meetings/<id> | /api/meetings/<id>/<ação>"""
    parts = [p for p in path.split("/") if p][1:]  # sem o 'api'
    if parts == ["meetings"]:
        key, meeting_id = "meetings", None
    elif parts == ["prompt"]:
        key, meeting_id = "prompt", None
    elif len(parts) in (2, 3) and parts[0] == "meetings" and parts[1].isdigit():
        key, meeting_id = (parts[2] if len(parts) == 3 else "meeting"), int(parts[1])
    else:
        return None
    handler = _ROUTES.get((method, key))
    return (handler, meeting_id) if handler else None


# ── ciclo de vida ──────────────────────────────────────────────────────────────

def make_server(port: int = DEFAULT_PORT, db_path: str = DB_PATH, token: Optional[str] = None) -> ThreadingHTTPServer:
    handler = type("JarvisMeetingsHandler", (_Handler,),
                   {"db_path": db_path, "token": token or load_token(), "port": port})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    handler.port = server.server_address[1]
    server.daemon_threads = True
    return server


def start_in_background(port: int = DEFAULT_PORT, db_path: str = DB_PATH) -> Optional[ThreadingHTTPServer]:
    """Sobe a página numa thread do sentinel. Porta ocupada (outra instância) = não sobe."""
    try:
        server = make_server(port=port, db_path=db_path)
    except OSError as exc:
        print(f"[!] Página de reuniões não iniciou na porta {port}: {exc}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="jarvis-meetings-web").start()
    print(f"[+] Página de reuniões: http://127.0.0.1:{server.server_address[1]}")
    return server


if __name__ == "__main__":
    srv = make_server()
    print(f"[+] Página de reuniões: http://127.0.0.1:{srv.server_address[1]}  (Ctrl+C encerra)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()
