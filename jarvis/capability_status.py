"""Bounded read-only integration checks; never plays media or refreshes OAuth tokens."""
from __future__ import annotations

import json
import math
import socket
import subprocess
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path


def spotify(path: Path, *, opener=urllib.request.urlopen, now=time.time) -> tuple[str, str]:
    if not path.is_file():
        return "off", "Login ainda não realizado."
    try:
        if path.stat().st_size > 16000:
            raise ValueError("Invalid token file")
        token = json.loads(path.read_text(encoding="utf-8"))
        access, expires = token.get("access_token"), token.get("expires_at")
        if not isinstance(access, str) or not access or type(expires) not in (float, int) or not math.isfinite(expires):
            raise ValueError("Invalid authorization")
        if expires <= now() + 60:
            return "warn", "Autorização precisa renovar; a renovação é feita ao usar o Spotify."
        request = urllib.request.Request("https://api.spotify.com/v1/me/player/devices",
                                         headers={"Authorization": "Bearer " + access})
        with opener(request, timeout=2) as response:
            payload = response.read(128001)
        if len(payload) > 128000:
            raise ValueError("Invalid response")
        devices = json.loads(payload).get("devices")
        if not isinstance(devices, list):
            raise TypeError("Invalid response")
        eligible = [d for d in devices if isinstance(d, dict) and d.get("id") and not d.get("is_restricted")]
        target = [d for d in eligible if d.get("is_active")] or [d for d in eligible
                  if d.get("type") == "Computer" and str(d.get("name", "")).strip().casefold() == socket.gethostname().strip().casefold()]
        if len(target) != 1:
            return "warn", "Spotify respondeu; selecione um aparelho no aplicativo."
        scopes = str(token.get("scope", "")).split()
        if "user-modify-playback-state" not in scopes:
            return "warn", "Leitura disponível; permissão de reprodução não confirmada. Faça login novamente."
        return "ok", "Spotify respondeu e o destino foi identificado; reprodução será confirmada ao executar."
    except (OSError, ValueError, TypeError, AttributeError, urllib.error.URLError):
        return "warn", "Configuração encontrada; autorização, rede ou aparelho não confirmados."


def pdf(home: Path, *, runner=subprocess.run) -> tuple[str, str]:
    python = home / "runtime/pdf-signing/Scripts/python.exe"
    if not python.is_file():
        return "off", "Leitor PDF não instalado."
    try:
        result = runner([str(python), "-c", "import pymupdf; print('jarvis-pdf-ready')"],
                        capture_output=True, timeout=2, check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0 and result.stdout.strip() == b"jarvis-pdf-ready":
            return "ok", "Leitor PDF carregado; cada documento ainda é validado ao abrir a prévia."
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "warn", "Runtime encontrado; não consegui confirmar o leitor PDF."


@lru_cache(maxsize=32)
def integration_checks(home: str, time_slot: int) -> dict[str, tuple[str, str]]:
    """A 30-second slot bounds repeated probes without retaining credentials in the cache."""
    root = Path(home)
    return {"spotify-faixa": spotify(root / "jarvis/database/.spotify_token.json"), "pdf": pdf(root)}
