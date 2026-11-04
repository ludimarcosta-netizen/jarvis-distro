"""Native execution middleware for PDF requests typed in Desktop chats."""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "pdf_chat_bridge", Path(__file__).with_name("pdf_bridge.py")
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)
_turns: dict[tuple[str, str], dict] = {}
_lock = threading.RLock()
# How the controller's own replies open: only right after one is a bare "sim" about PDFs.
CONTROLLER_REPLY = re.compile(
    r"\s*(?:Documento |Conversa: |Estado: |Não identifiquei |Nenhum documento |Nesta conversa não há documento"
    r"|Não achei documento |Não entendi a condição|Indique a conversa|Responda com um número"
    r"|Fluxo de assinatura|Controlador de PDFs|O controlador não|Resposta (?:inválida|sem comprovação))"
)
# Hermes' copy for a turn it discarded (agent/turn_failure_copy.py).
DROPPED_REPLY = re.compile(r"\s*Your request was not processed\.")
# Phrases of the controller's status lines; a message quoting them is a complaint, not a request.
CONTROLLER_QUOTE = re.compile(
    r"envio confirmado na conversa original|aguardando revisao e autorizacao|nenhum pdf foi assinado"
    r"|resumo e previas|assinado criptograficamente|nada foi assinado"
)


def capture(
    session_id="", turn_id="", user_message="", conversation_history=None, **_kwargs
):
    if not isinstance(user_message, str):
        return None
    # Scheduled jobs are not typed requests: their prompts carry e-mail/ata text ("Assinar online").
    if str(session_id).startswith("cron_"):
        return None
    history = conversation_history or []
    previous = next(
        (
            m.get("content", "")
            for m in reversed(history)
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and bridge.is_pdf_request(m["content"])
        ),
        "",
    )
    # A pending contact choice answered by name or with extra words ("Beltrana", "2 cara").
    pick = bridge.pending_pick(user_message, "desktop:" + session_id)
    if pick:
        user_message = pick
    folded = bridge.fold(user_message)
    # Typed text that opens with an authorization ("Assine todos, só a primeira da Lei 1.674").
    authorization = bool(
        re.match(
            r"\s*(?:(?:sim|ok|autorizado|aprovado)[,.!]?\s*)?(?:pode\s+assinar|assine|assina|autorizo|aprovo|autorizado|aprovado)\b",
            folded,
        )
    ) and not re.search(
        r"\bme\s+(?:mandou|mandaram|enviou|enviaram|encaminhou|encaminharam)\b|\bwhats",
        folded,
    )
    if CONTROLLER_QUOTE.search(folded) and not authorization:
        return None  # "pqp, vc respondeu: Documento ... envio confirmado" goes to the agent
    last_reply = next(
        (
            m["content"]
            for m in reversed(history)
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip()
        ),
        "",
    )
    # A bare "sim"/"continue" answers the controller only when its reply was the last one, or when
    # Hermes dropped that reply ("E aí" sent while a 2-minute batch ran discarded it).
    in_flow = bool(CONTROLLER_REPLY.match(last_reply)) or bool(
        previous and DROPPED_REPLY.match(last_reply)
    )
    followup = bool(
        (previous and authorization)
        or (
            in_flow
            and re.fullmatch(
                r"\s*(?:nada ainda|e os documentos|e ai|e entao|continue|continua|pode continuar|sim|status|pode assinar|assine|autorizo|aprovo|pode enviar|envie)[?!.,\s]*",
                folded,
            )
        )
    ) or (
        bool(re.search(r"\d", user_message))
        and bridge.is_pdf_request(user_message, "desktop:" + session_id)
    ) or (
        # "pode assinar o da Beltrana" in a conversation that never asked: the batch is in another one.
        authorization and bridge.pending_anywhere()
    )
    if not bridge.is_pdf_request(user_message) and not followup:
        return None
    known_ids = []
    for message in history:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and parsed.get("exit_code") == 0:
                    parsed = json.loads(parsed.get("output", "{}"))
                    if isinstance(parsed, dict) and re.fullmatch(
                        r"[a-f0-9]{32}", str(parsed.get("request", ""))
                    ):
                        known_ids.append(parsed["request"])
            except (ValueError, TypeError):
                pass
    with _lock:
        _turns[(session_id, turn_id)] = {
            "request": user_message,
            "ids": list(dict.fromkeys(known_ids)),
            "reply": None,
            "lock": threading.Lock(),
            "chat_authorized": authorization,
        }
        while len(_turns) > 64:
            _turns.pop(next(iter(_turns)))
    return {
        "context": "Pedido de assinatura controlado pelo fluxo determinístico de PDFs; não executar ferramentas alternativas."
    }


def response(text):
    return SimpleNamespace(
        id="pdf-controller",
        object="chat.completion",
        created=int(time.time()),
        model="pdf-controller",
        choices=[
            SimpleNamespace(
                index=0,
                finish_reason="stop",
                logprobs=None,
                message=SimpleNamespace(
                    role="assistant",
                    content=text,
                    tool_calls=None,
                    reasoning_content=None,
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )


def execute(request, next_call, session_id="", turn_id="", **_kwargs):
    with _lock:
        turn = _turns.get((session_id, turn_id))
    if turn is None:
        return next_call(request)
    with turn["lock"]:
        if turn["reply"] is None:
            try:
                turn["reply"] = bridge.run(
                    turn["request"],
                    "desktop:" + session_id,
                    existing_requests=turn["ids"],
                    chat_authorized=turn["chat_authorized"],
                )
            except Exception:  # noqa: BLE001 — a controller fault must never fall through to the provider
                turn["reply"] = (
                    "Controlador de PDFs indisponível. Nenhuma conclusão de assinatura ou envio foi confirmada."
                )
        return response(turn["reply"])


def block_tools(session_id="", turn_id="", **_kwargs):
    with _lock:
        if (session_id, turn_id) in _turns:
            return {
                "action": "block",
                "message": "Pedido restrito ao controlador de PDFs; ferramenta alternativa bloqueada.",
            }
    return None


def register(ctx):
    ctx.register_hook("pre_llm_call", capture)
    ctx.register_hook("pre_tool_call", block_tools)
    ctx.register_middleware("llm_execution", execute)
