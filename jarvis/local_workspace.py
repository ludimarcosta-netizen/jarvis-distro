"""Profile-local desktop sessions; no arbitrary path is accepted from the renderer."""
from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from collections import deque

from local_files import TTL, FilesSession
from local_media import MediaSession
from local_memory import MemorySession, profile_memory

logger = logging.getLogger(__name__)


class Workspace:
    def __init__(self, factory=FilesSession, *, clock=time.monotonic, media_factory=MediaSession, memory_factory=None):
        self.factory = factory
        self.clock = clock
        self.media_factory = media_factory
        self.media: dict[str, MediaSession] = {}
        self.memory_factory = memory_factory
        self.memory: dict[str, MemorySession] = {}
        self.sessions: dict[str, tuple[float, FilesSession]] = {}
        self.lock = threading.Lock()
        self.metrics: deque[dict] = deque(maxlen=200)

    def dispatch(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise TypeError("Pedido inválido.")
        action = body.get("action")
        if not isinstance(action, str):
            raise TypeError("Ação inválida.")
        if action not in {"start", "files.search", "files.preview", "files.open", "files.cancel",
                          "videos.search", "videos.open", "media.status", "media.command", "media.volume", "spotify.play",
                          "memory.search", "memory.select", "memory.prepare", "memory.confirm"}:
            return {"ok": False, "error": "Ação desconhecida."}
        if action == "start":
            with self.lock:
                now = self.clock()
                self.sessions = {key: row for key, row in self.sessions.items() if now - row[0] <= TTL}
                self.media = {key: value for key, value in self.media.items() if key in self.sessions}
                self.memory = {key: value for key, value in self.memory.items() if key in self.sessions}
                if len(self.sessions) >= 32:
                    raise ValueError("Limite de sessões atingido; aguarde a expiração.")
                key = secrets.token_urlsafe(24)
                files = self.factory()
                self.sessions[key] = (now, files)
                self.media[key] = self.media_factory()
                if self.memory_factory:
                    self.memory[key] = MemorySession(self.memory_factory, clock=self.clock)
            return {"ok": True, "session": key, "roots": [root.name for root in files.roots]}
        key = body.get("session")
        if not isinstance(key, str):
            raise TypeError("Abra uma sessão pelo painel.")
        with self.lock:
            row = self.sessions.get(key)
            if not row or self.clock() - row[0] > TTL:
                self.sessions.pop(key, None)
                self.media.pop(key, None)
                self.memory.pop(key, None)
                raise ValueError("Sessão vencida; feche e reabra o painel.")
            files = row[1]
            media = self.media[key]
            memory = self.memory.get(key)
            self.sessions[key] = (self.clock(), files)
        started = self.clock()
        try:
            if action.startswith("memory."):
                if memory is None:
                    raise ValueError("Segundo Cérebro não configurado neste perfil.")
                result = memory.dispatch(action, body)
            elif action.startswith(("videos.", "media.", "spotify.")):
                result = media.dispatch(action, body)
            elif action == "files.search":
                result = files.search(body.get("query"))
            elif action == "files.cancel":
                files.cancelled.set()
                result = {"message": "Cancelamento solicitado."}
            elif action in {"files.preview", "files.open"}:
                if not files.lock.acquire(blocking=False):
                    raise ValueError("Aguarde o término da busca.")
                try:
                    ref = (files.select(body["number"]) if "number" in body else
                           files.current() if body.get("current") is True else body.get("ref"))
                    if not isinstance(ref, str) or len(ref) > 100:
                        raise ValueError("Referência inválida.")
                    result = files.preview(ref) if action == "files.preview" else files.open_file(ref)
                finally:
                    files.lock.release()
            else:
                raise ValueError("Ação desconhecida.")
        except (OSError, ValueError, KeyError, IndexError, sqlite3.Error) as exc:
            logger.info("workspace operation failed: %s", type(exc).__name__)
            if isinstance(exc, ValueError):
                return {"ok": False, "error": str(exc)}
            if action.startswith("memory."):
                return {"ok": False, "error": "Memória indisponível. Confira a configuração e o histórico local antes de repetir uma revisão."}
            return {"ok": False, "error": "Não consegui ler este arquivo. Confira o formato e tente novamente."}
        finally:
            with self.lock:
                self.metrics.append({"action": action, "elapsed_ms": round((self.clock() - started) * 1000, 2)})
        return {"ok": True, **result}


_profiles: dict[str, Workspace] = {}
_profiles_lock = threading.Lock()


def for_profile(home: str) -> Workspace:
    with _profiles_lock:
        if home not in _profiles:
            _profiles[home] = Workspace(memory_factory=lambda: profile_memory(home))
        return _profiles[home]
