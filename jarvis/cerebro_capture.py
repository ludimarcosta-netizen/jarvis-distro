"""Captura de fim de tarefa: local, idempotente, sem interpretar relatos como fatos."""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import project_notes
from cerebro_io import atomic_write, file_lock
from cerebro_memory import Memory
from session_harvest import HOME, memory_excerpt, read_claude, read_hermes, redact

HERE = Path(__file__).resolve().parent
AGENTS = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity", "hermes": "Jarvis"}


def trusted_transcript(raw: str, source: str) -> Path:
    path = Path(raw).expanduser().resolve()
    roots = ([HOME / ".claude/projects"] if source == "claude" else
             [HOME / ".gemini" / a / "brain" for a in ("antigravity", "antigravity-cli", "antigravity-ide")])
    if not any(path.is_relative_to(r.resolve()) for r in roots) or path.suffix != ".jsonl":
        raise ValueError("Transcrição fora da origem permitida")
    if path.stat().st_size > 20_000_000:
        raise ValueError("Transcrição excede limite da captura imediata")
    return path


def antigravity_final(path: Path) -> str:
    last = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("source") == "MODEL" and item.get("type") == "PLANNER_RESPONSE":
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                last = content
    return last  # thinking, tool args and tool outputs are never captured here


def capture(source: str, payload: dict[str, Any], memory: Memory | None = None) -> dict[str, Any]:
    if source not in AGENTS or not isinstance(payload, dict):
        raise ValueError("Origem inválida")
    extra = payload.get("extra", {})
    if not isinstance(extra, dict):
        extra = {}
    if payload.get("agent_id") or payload.get("parent_session_id") or extra.get("failed") or extra.get("interrupted") or extra.get("completed") is False:
        return {"gravado": False, "motivo": "subagente ou execução incompleta"}
    if source == "codex" and payload.get("type") != "agent-turn-complete":
        return {"gravado": False, "motivo": "evento não final"}
    if source == "antigravity" and (payload.get("fullyIdle") is not True or payload.get("error")):
        return {"gravado": False, "motivo": "execução não concluída"}
    if source == "hermes" and payload.get("profile") not in {None, "default"}:
        return {"gravado": False, "motivo": "perfil fora do escopo do Jarvis"}
    sid = payload.get("session_id") or payload.get("thread-id") or payload.get("conversationId")
    if not isinstance(sid, str) or not 1 <= len(sid) <= 200:
        raise ValueError("Sessão inválida")
    cwd = payload.get("cwd") or ""
    text = payload.get("last_assistant_message") or payload.get("last-assistant-message") or ""
    if source == "claude" and not text and payload.get("transcript_path"):
        session = read_claude(trusted_transcript(payload["transcript_path"], source))
        if session:
            text = next((t for r, t in reversed(session.turns) if r == "assistant"), "")
    if source == "antigravity":
        text = antigravity_final(trusted_transcript(payload.get("transcriptPath", ""), source))
        paths = payload.get("workspacePaths") or []
        cwd = paths[0] if len(paths) == 1 and isinstance(paths[0], str) else ""
    if source == "hermes":
        db = HOME / "AppData/Local/hermes/state.db"
        session = read_hermes(db, sid)
        if session:
            text = next((t for r, t in reversed(session.turns) if r == "assistant"), "")
            cwd = session.project
    if not isinstance(text, str) or len(text.strip()) < 80:
        return {"gravado": False, "motivo": "resposta trivial ou ainda indisponível"}
    text = memory_excerpt(text.strip(), 2500)
    # Não usa o conteúdo para adivinhar o projeto; nome/pasta/alias devem corresponder.
    mem = memory or Memory()
    folder = str(cwd).replace("\\", "/").rstrip("/").split("/")[-1]
    project = project_notes.resolve(mem.vault, folder) or ""
    digest = hashlib.sha256(text.encode()).hexdigest()
    result = mem.record("resultado", "Relato automático da IA (não verificado):\n" + text,
                        f"hook local {source}; sessão {redact(sid)}", AGENTS[source], project,
                        "fim:" + hashlib.sha256((sid + digest).encode()).hexdigest())
    return {**result, "projeto": project, "verificado": False}


def health(source: str, result: dict[str, Any]) -> None:
    path = HERE / "capture_health.json"
    with file_lock(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[source] = {"at": datetime.now(timezone.utc).isoformat(), **result}
        atomic_write(path, json.dumps(data, ensure_ascii=False))


def forward_notify(raw: str) -> None:
    path = HERE / ".notify_original.json"
    if not path.exists():
        return
    command = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
        raise ValueError("Callback original inválido")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run([*command, raw], timeout=10, check=False, creationflags=flags)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=AGENTS)
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("payload", nargs="?")
    args = parser.parse_args()
    raw = args.payload if args.payload is not None else sys.stdin.read(1_000_001)
    if args.notify:
        try:
            forward_notify(raw)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            health("notify", {"ok": False, "erro": "callback original indisponível"})
    try:
        if len(raw) > 1_000_000:
            raise ValueError("Payload excessivo")
        result = capture(args.source, json.loads(raw))
        health(args.source, {"ok": True, **result})
    except (ValueError, TypeError, OSError, sqlite3.Error):
        health(args.source, {"ok": False, "erro": "captura indisponível; recuperação periódica permanece ativa"})
    if args.source == "antigravity":
        print('{"decision":"stop"}')
    # Saída 0: nunca bloqueia nem reinicia a tarefa da IA.


if __name__ == "__main__":
    main()
