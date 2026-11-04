"""Small integration layer. No ML dependency is imported into Hermes."""
from __future__ import annotations

import asyncio
import importlib.util
import time
import uuid
from pathlib import Path

spec = importlib.util.spec_from_file_location("voice_local_client", Path(__file__).with_name("client.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
client = module.client

confirmation_spec = importlib.util.spec_from_file_location(
    "voice_confirmation_policy", Path(__file__).with_name("confirmation.py"))
confirmation_module = importlib.util.module_from_spec(confirmation_spec)
confirmation_spec.loader.exec_module(confirmation_module)
requires_screen = confirmation_module.requires_screen

session = ""
session_kind = ""
enrolling = False
_terminal_enrollment = {}
_enrollment_lock = asyncio.Lock()


async def begin():
    global session, session_kind
    session = uuid.uuid4().hex
    session_kind = "live"
    try:
        await asyncio.to_thread(client.ensure)
        await asyncio.to_thread(client.call, "/session", {"session": session})
    except (OSError, ValueError, RuntimeError):
        pass  # screen confirmation fails closed if the local helper is unavailable


async def end():
    global session, session_kind, enrolling
    session = ""
    session_kind = ""
    enrolling = False
    try:
        await asyncio.to_thread(client.call, "/session", {"session": ""})
    except (OSError, ValueError):
        pass


async def permit(request: str, ws, expected_session: str) -> bool:
    if not expected_session or expected_session != session:
        return False
    if not requires_screen(request):
        return True
    try:
        await asyncio.to_thread(client.ensure)
        state = await asyncio.to_thread(client.call, "/status")
        if state.get("session") != session:
            await asyncio.to_thread(client.call, "/session", {"session": session})
        pending = await asyncio.to_thread(client.call, "/challenge", {
            "request": request, "session": session,
            "screen_only": True,
        })
        if not pending.get("ok"):
            return False
        deadline = asyncio.get_running_loop().time() + 92
        while asyncio.get_running_loop().time() < deadline and expected_session == session:
            result = await asyncio.to_thread(client.call, "/result", {
                "id": pending["id"], "digest": pending["digest"]})
            if result.get("done"):
                return result.get("allowed") is True
            await asyncio.sleep(0.4)
    except (OSError, ValueError, RuntimeError, AttributeError):
        return False
    return False


async def status(ws=None) -> dict:
    async with _enrollment_lock:
        return await _status(ws)


async def _status(ws=None) -> dict:
    global session, session_kind, enrolling, _terminal_enrollment
    result = await asyncio.to_thread(client.status)
    if result.get("available") and result.get("mode") != "off" and result.get("session") != session:
        try:
            await asyncio.to_thread(client.call, "/session", {"session": session})
            client._status_at = 0
            result = await asyncio.to_thread(client.status)
        except (OSError, ValueError):
            result["error"] = "Sessão local indisponível; confirmação na tela suspensa."
    if result.get("available"):
        enrolling = result.get("enrollment", {}).get("state") == "recording"
    if session_kind == "enrollment" and result.get("enrollment", {}).get("state") in ("ready", "error"):
        _terminal_enrollment = dict(result["enrollment"])
        try:
            await asyncio.to_thread(client.call, "/session", {"session": ""})
            session = ""
            session_kind = ""
        except (OSError, ValueError):
            pass
    if not result.get("enrollment") and _terminal_enrollment:
        result = {**result, "enrollment": _terminal_enrollment}
    return result


async def action(operation: str, body: dict):
    if operation == "status":
        return {"ok": True, "voice_identity": await status()}
    async with _enrollment_lock:
        return await _action(operation, body)


async def meeting_hint(text: str):
    return await asyncio.to_thread(client.meeting_hint, text)


async def _action(operation: str, body: dict):
    global session, session_kind, enrolling, _terminal_enrollment
    if operation not in ("settings", "enroll", "delete", "sample", "review", "decision", "enrollment", "enrollment-audio"):
        return {"ok": False, "error": "Operação inválida"}
    try:
        if operation != "enrollment-audio":
            await asyncio.to_thread(client.ensure)
        if operation == "enrollment":
            created_session = False
            if not session:
                session = uuid.uuid4().hex
                session_kind = "enrollment"
                created_session = True
                await asyncio.to_thread(client.call, "/session", {"session": session})
            result = await asyncio.to_thread(client.call, "/enrollment", body)
            if result.get("ok"):
                _terminal_enrollment = {}
                enrolling = True
                client._status_at = 0
            elif created_session:
                await asyncio.to_thread(client.call, "/session", {"session": ""})
                session = ""
                session_kind = ""
            return result
        if operation == "enrollment-audio":
            if not enrolling or not session:
                return {"ok": False, "error": "Cadastro vocal não está gravando"}
            return await asyncio.to_thread(client.call, "/audio", {
                "data": body.get("data", ""), "source": "enrollment", "session": session,
                "playback": False, "captured_at": time.monotonic(),
            })
        timeout = 30 if operation == "enroll" else 15 if operation == "sample" else 2
        result = await asyncio.to_thread(client.call, "/" + operation, body, timeout)
        if result.get("ok") and operation in ("enroll", "delete", "settings", "review"):
            _terminal_enrollment = {}
        client._status_at = client._mode_at = 0
        if operation == "settings" and result.get("ok") and session:
            await asyncio.to_thread(client.call, "/session", {"session": session})
        return result
    except (OSError, ValueError, RuntimeError):
        return {"ok": False, "error": "Serviço local iniciando ou indisponível; tente novamente"}

