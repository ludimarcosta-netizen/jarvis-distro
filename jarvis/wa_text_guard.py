"""Nenhum texto sai pelo WhatsApp sem ter sido ditado ou confirmado pelo senhor.

O texto do `wa.py enviar --texto` precisa estar na mensagem atual do senhor, ou ter sido proposto
pelo Jarvis na resposta anterior e confirmado agora ("sim", "pode enviar"). Chamadas diretas à
Evolution API (porta 8090) ou ao módulo `wa` fora do CLI são sempre bloqueadas.
"""

from __future__ import annotations

import re
import shlex
import threading
import unicodedata

_turns: dict[str, tuple[str, str]] = {}
_lock = threading.Lock()
_AFFIRM = re.compile(
    r"(?:sim|ok|isso|confirmo|pode|pode sim|pode enviar|pode mandar|manda|mande|envia|envie)(?: sim| pode| ai)?"
)
_BYPASS = re.compile(
    r"(?:127\.0\.0\.1|localhost):8090|/message/send|sendText|sendMedia|\bimport\s+wa\b|\bfrom\s+wa\s+import\b",
    re.IGNORECASE,
)
BLOCK_MESSAGE = (
    "Envio bloqueado: o senhor não ditou nem confirmou este texto. Não escreva mensagens por conta própria. "
    "Pergunte ao senhor o texto exato e envie somente após ele confirmar."
)
BYPASS_MESSAGE = (
    "Acesso direto à Evolution/módulo wa bloqueado (vale também para leitura). Use só o CLI: "
    "`wa.py conversas`, `wa.py mensagens --conversa NOME --ids` (traz chat e id para o pdf-signing) "
    "ou `wa.py enviar` com texto ditado/confirmado pelo senhor."
)


def _norm(text: str) -> str:
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )
    return " ".join(re.sub(r"[^\w]+", " ", folded).split())


def capture(session_id="", user_message="", conversation_history=None, **_kwargs):
    if not isinstance(user_message, str):
        return None
    previous = next(
        (
            m.get("content", "")
            for m in reversed(conversation_history or [])
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"]
        ),
        "",
    )
    with _lock:
        _turns[session_id] = (user_message, previous)
        while len(_turns) > 64:
            _turns.pop(next(iter(_turns)))
    return None


def _sent_text(command: str) -> str | None:
    """Text passed to `wa.py enviar`; None when the command is not a send, "" when unreadable."""
    if not re.search(r"wa\.py", command) or not re.search(r"\benviar\b", command):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    for index, part in enumerate(parts):
        if part == "--texto" and index + 1 < len(parts):
            return parts[index + 1]
        if part.startswith("--texto="):
            return part.split("=", 1)[1]
    return ""


def guard(tool_name="", args=None, session_id="", **_kwargs):
    args = args if isinstance(args, dict) else {}
    command = str(args.get("command") or args.get("code") or "")
    if not command:
        return None
    if _BYPASS.search(command):
        return {"action": "block", "message": BYPASS_MESSAGE}
    text = _sent_text(command)
    if text is None:
        return None
    wanted = _norm(text)
    with _lock:
        user, previous = _turns.get(session_id, ("", ""))
    said = _norm(user)
    if wanted and (
        wanted in said or (_AFFIRM.fullmatch(said) and wanted in _norm(previous))
    ):
        return None
    return {"action": "block", "message": BLOCK_MESSAGE}


def register(ctx):
    ctx.register_hook("pre_llm_call", capture)
    ctx.register_hook("pre_tool_call", guard)
