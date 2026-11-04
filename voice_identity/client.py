"""Standard-library client shared by the existing bridge and ambient listener."""
from __future__ import annotations

import importlib.util
import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:17839"


class Client:
    def __init__(self):
        self._queue = queue.Queue(maxsize=24)
        self._thread = None
        self._lock = threading.Lock()
        self._status: dict = {}
        self._status_at = 0.0
        self._spawn_at = 0.0
        self._token = ""
        self._mode_at = 0.0
        self._mode = "off"
        self.dropped = 0

    def mode(self) -> str:
        if time.monotonic() - self._mode_at < 1:
            return self._mode
        path = ROOT / "settings.json"
        try:
            mode = json.loads(path.read_text(encoding="utf-8"))["mode"] if path.exists() else "off"
            self._mode = "off" if mode == "off" else "observe"
        except (OSError, ValueError, KeyError):
            self._mode = "observe"  # recognition remains available; screen approval is independent
        self._mode_at = time.monotonic()
        return self._mode

    def _read_token(self):
        spec = importlib.util.spec_from_file_location("local_voice_store", ROOT / "store.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._token = module.protect((ROOT / "data" / "token.bin").read_bytes(), decrypt=True).decode()

    def call(self, path: str, body: dict | None = None, timeout: float = 2) -> dict:
        if not self._token:
            self._read_token()
        request = urllib.request.Request(URL + path, data=json.dumps(body or {}).encode(),
                                         headers={"Authorization": "Bearer " + self._token,
                                                  "Content-Type": "application/json"}, method="POST")
        try:
            # Loopback must not go through a user's external HTTP proxy.
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 503):
                return json.load(exc)
            raise

    def ensure(self):
        with self._lock:
            try:
                if self.call("/health", timeout=0.5).get("available"):
                    return
            except (OSError, ValueError):
                pass
            if time.monotonic() - self._spawn_at < 15:
                return
            self._spawn_at = time.monotonic()
            python = ROOT / ".venv" / "Scripts" / "python.exe"
            if not python.exists():
                raise RuntimeError("Ambiente local de voz não instalado")
            env = os.environ.copy()
            for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
                env.pop(key, None)
            env.update({"PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1"})
            with (ROOT / "service.log").open("ab") as log:
                subprocess.Popen([str(python), str(ROOT / "service.py")], cwd=str(ROOT), env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=0x08000000 if os.name == "nt" else 0)

            # The caller usually sends /session immediately after ensure(). Give
            # the new helper a short window to bind the loopback port first.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    if self.call("/health", timeout=0.5).get("available"):
                        return
                except (OSError, ValueError):
                    time.sleep(0.05)

    def status(self) -> dict:
        if time.monotonic() - self._status_at < 1:
            return self._status
        mode = self.mode()
        try:
            if mode != "off":
                self.ensure()
            self._status = self.call("/status", timeout=6)
        except (OSError, ValueError, RuntimeError):
            self._status = {"available": False, "mode": mode, "ready": False,
                            "error": "Serviço local indisponível" if mode != "off" else "",
                            "profiles": [], "candidates": []}
        self._status_at = time.monotonic()
        return {**self._status, "client_dropped": self.dropped}

    def submit(self, body: dict):
        if self.mode() == "off":
            return
        if self._thread is None:
            with self._lock:
                if self._thread is None:
                    self._thread = threading.Thread(target=self._send_audio, daemon=True, name="voice-copy")
                    self._thread.start()
        try:
            self._queue.put_nowait({"captured_at": time.monotonic(), **body})
        except queue.Full:
            self.dropped += 1

    def meeting_hint(self, text: str) -> dict | None:
        if self.mode() == "off" or "reuni" not in text.casefold() or "foi com" not in text.casefold():
            return None
        try:
            return self.call("/meeting-hint", {"text": text}, timeout=0.7)
        except (OSError, ValueError):
            return None

    def heard(self, since: float, until: float) -> list[dict]:
        """Recognized voices between two epoch times: [{id, name, count}]. Advisory only."""
        if self.mode() == "off":
            return []
        try:
            return self.call("/heard", {"since": since, "until": until}, timeout=1).get("voices") or []
        except (OSError, ValueError):
            return []

    def _send_audio(self):
        while True:
            body = self._queue.get()
            try:
                if self.mode() != "off":
                    self.call("/audio", body, timeout=5)
            except (OSError, ValueError):
                self.dropped += 1
            finally:
                self._queue.task_done()


client = Client()
