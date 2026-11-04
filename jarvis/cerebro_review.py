"""Append-only Markdown revisions; original notes always remain intact."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from cerebro_io import atomic_write, file_lock

DIRECTORY = "_revisoes"
MAX_NOTE = 512_000
MAX_EVENTS = 2000
EVENT_NAME = re.compile(r"^\d{8}-[a-f0-9]{16}\.md$")
SECRET = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----|"
    r'''\b(?:[A-Za-z0-9]+_)*(?:senha|password|api[_ -]?key|(?:access[_ -]?)?token|authorization|bearer)["']?\s*[:= ]\s*["']?[^\s,;"']+|'''
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b(?:cpf|rg)\s*[:=]?\s*\d[\d. -]{7,18}", re.IGNORECASE)


def safe_text(text: str) -> str:
    return SECRET.sub("[dado protegido]", text)


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def body(raw: str) -> str:
    return raw.split("---", 2)[-1].lstrip() if raw.startswith("---") and "\n---" in raw[3:] else raw


def superseded(relative: str, raw: str) -> bool:
    """Only learned preference metadata can retire a note automatically."""
    if not relative.startswith("perfil/aprendido/") or not raw.startswith("---\n"):
        return False
    front = raw[4:].split("\n---", 1)[0]
    return bool(re.search(r'''^estado:\s*["']?substituida["']?\s*$''', front, re.MULTILINE))


class Reviews:
    def __init__(self, vault: Path):
        self.vault = vault.resolve()
        self.directory = self.vault / DIRECTORY

    def note(self, relative: str) -> tuple[Path, str]:
        if not isinstance(relative, str) or not relative.endswith(".md"):
            raise ValueError("Nota inválida.")
        candidate = self.vault / relative
        path = candidate.resolve()
        if not path.is_relative_to(self.vault) or any(part.startswith((".", "_")) for part in path.relative_to(self.vault).parts):
            raise ValueError("Nota fora das pastas revisáveis.")
        if candidate.is_symlink() or path.stat().st_size > MAX_NOTE:
            raise ValueError("Nota indisponível ou grande demais.")
        return path, path.read_text(encoding="utf-8-sig")

    def events(self) -> list[dict]:
        if self.directory.is_symlink() or not self.directory.resolve().is_relative_to(self.vault):
            raise ValueError("Diretório de revisões inválido.")
        paths = sorted(self.directory.glob("*.md"))
        if len(paths) > MAX_EVENTS:
            raise ValueError("Limite de revisões atingido; manutenção necessária.")
        result = []
        for path in paths:
            if not EVENT_NAME.fullmatch(path.name) or path.is_symlink() or path.stat().st_size > 16000:
                raise ValueError("Histórico de revisão inválido; consulte o arquivo local.")
            try:
                raw = path.read_text(encoding="utf-8")
                front, replacement = raw[4:].split("\n---\n", 1)
                event = {key: json.loads(value) for key, value in (line.split(": ", 1) for line in front.splitlines())}
                if (not raw.startswith("---\n") or type(event["version"]) is not int or event["version"] != 1
                        or not isinstance(event["action"], str) or event["action"] not in {"correct", "disable", "restore"}):
                    raise ValueError("Invalid review")
                if not all(isinstance(event.get(key), str) for key in ("target", "sha256", "reason", "at", "previous", "id")):
                    raise ValueError("Invalid review fields")
                if event["id"] != path.stem or not re.fullmatch(r"[a-f0-9]{64}", event["sha256"]):
                    raise ValueError("Invalid review identity")
                event.update(replacement=replacement.lstrip("\n"), source=f"{DIRECTORY}/{path.name}")
                result.append(event)
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise ValueError("Histórico de revisão ilegível; nenhuma alteração foi aplicada.") from None
        return result

    def latest(self) -> dict[str, dict]:
        return {event["target"]: event for event in self.events()}

    def view(self, relative: str, raw: str, latest: dict[str, dict] | None = None) -> dict:
        event = (self.latest() if latest is None else latest).get(relative)
        state = "active"
        text = raw
        if superseded(relative, raw):
            state, text = "disabled", ""
        if event:
            if event["sha256"] != digest(raw):
                state = "outdated" if text else "disabled"
            elif event["action"] == "disable":
                state, text = "disabled", ""
            elif event["action"] == "correct":
                state = "corrected"
                prefix = raw[:len(raw) - len(body(raw))] if raw.startswith("---") else ""
                text = prefix + event["replacement"]
            elif event["action"] == "restore":
                state, text = "active", raw
        return {"state": state, "text": text, "event": event}

    def commit(self, *, target: str, expected: str, previous: str, action: str, replacement: str, reason: str) -> dict:
        if action not in {"correct", "disable", "restore"}:
            raise ValueError("Revisão inválida.")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300 or safe_text(reason) != reason:
            raise ValueError("Informe um motivo de até 300 caracteres, sem dados protegidos.")
        if not isinstance(replacement, str) or len(replacement) > 4000 or safe_text(replacement) != replacement:
            raise ValueError("Texto inválido ou com dados protegidos.")
        if action == "correct" and not replacement.strip():
            raise ValueError("Informe o texto corrigido.")
        with file_lock(self.directory / "review"):
            _, raw = self.note(target)
            latest = self.latest()
            current = latest.get(target)
            if digest(raw) != expected or (current["id"] if current else "") != previous:
                raise ValueError("A nota ou sua revisão mudou. Busque novamente antes de confirmar.")
            if action == "correct" and (len(body(raw)) > 4000 or safe_text(raw) != raw):
                raise ValueError("Correção textual indisponível para esta nota; use o editor local.")
            paths = sorted(self.directory.glob("*.md"))
            if len(paths) >= MAX_EVENTS:
                raise ValueError("Limite de revisões atingido.")
            sequence = int(paths[-1].name[:8]) + 1 if paths else 1
            key = f"{sequence:08d}-{secrets.token_hex(8)}"
            event = {"version": 1, "id": key, "target": target, "sha256": expected, "previous": previous,
                     "action": action, "reason": reason.strip(), "at": datetime.now(timezone.utc).isoformat()}
            front = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in event.items())
            path = self.directory / f"{key}.md"
            atomic_write(path, f"---\n{front}\n---\n\n{replacement.strip() if action == 'correct' else ''}")
        return {"source": f"{DIRECTORY}/{path.name}", "state": {"correct": "corrected", "disable": "disabled", "restore": "active"}[action]}
