"""Invoke the installed Hermes agent for actions requested through Gemini Live."""

import asyncio
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES = ROOT / "hermes-agent"
PYTHON = HERMES / "venv" / "Scripts" / "python.exe"
PROCEDURE_SKILL = "jarvis-procedure-learning"

_PROCEDURE_REVIEW = """Review the immediately preceding delegated task in this Hermes session.
Use the saved transcript and actual tool results as evidence; the assistant's claim alone is not proof.
If no reusable operation completed and passed an observable success check, make no change and answer exactly NO_PROCEDURE.
If a matching procedure exists, verify it was loaded and its permanent artifact was executed; do not create another artifact.
If no matching procedure exists and a reusable operation was verified, use native skills_list/skill_view to search for equivalents, then persist one native skill with skill_manage. Put executable files under that skill's scripts/ directory and include dependencies, varying parameters, prerequisites, invocation, success criteria, and failure diagnostics. Do not save transcripts, credentials, recipients, private message content, or temporary paths.
Never write a procedure after an unconfirmed result, missing dependency, missing artifact, or other environment failure. Diagnose first; change a procedure only when evidence shows its logic is incompatible. A missing file/path/import is not that evidence.
Return only a short status: PROCEDURE_SAVED:<name>, PROCEDURE_REUSED:<name>, PROCEDURE_UNCHANGED:<reason>, or NO_PROCEDURE."""


def _load_root_env(env: dict[str, str]) -> None:
    """Pass configured provider credentials to the Hermes child process."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    allowed = {"GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"}
    for raw_line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in allowed and key not in env:
            env[key] = value.strip().strip('"').strip("'")


async def ask_hermes(
    request: str, timeout: int = 180, *, session: dict | None = None, _review: bool = True,
) -> str:
    """Run one Hermes turn with its configured tools and safety policy."""
    if not PYTHON.exists():
        raise RuntimeError("Ambiente Python do Hermes não encontrado")
    if not request.strip():
        raise ValueError("Solicitação vazia")
    import pdf_bridge
    if pdf_bridge.is_pdf_request(request, "local-voice"):
        return await asyncio.to_thread(pdf_bridge.run, request, "local-voice")
    env = os.environ.copy()
    _load_root_env(env)
    env["HERMES_HOME"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    query_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="jarvis-query-", suffix=".txt",
                                         delete=False) as query_file:
            query_file.write(request)
            query_path = Path(query_file.name)
        argv = [
            str(PYTHON), str(ROOT / "jarvis" / "hermes_runner.py"),
            "--query-file", str(query_path), "--oneshot", "--quiet",
        ]
        if session and session.get("hermes_session_id"):
            argv.extend(["--resume", str(session["hermes_session_id"])])
        argv.extend(["--skills", PROCEDURE_SKILL])
        if re.search(r"\b(?:whats?app|zap)\b", request, re.IGNORECASE):
            argv.extend(["--skills", "whatsapp-evolution"])
        model = os.environ.get("JARVIS_HERMES_MODEL", "").strip()
        provider = os.environ.get("JARVIS_HERMES_PROVIDER", "").strip()
        if model:
            argv.extend(["--model", model])
        if provider:
            argv.extend(["--provider", provider])
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(HERMES), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise TimeoutError("Hermes excedeu o tempo de resposta") from None
    finally:
        if query_path:
            query_path.unlink(missing_ok=True)
    session_match = re.search(rb"(?:^|\n)session_id:\s*([A-Za-z0-9_-]+)", stderr)
    if session is not None and session_match:
        session["hermes_session_id"] = session_match.group(1).decode("ascii")
    answer = stdout.decode("utf-8", errors="replace").strip()
    if process.returncode:
        raise RuntimeError(f"Hermes falhou (código {process.returncode})")
    if _review and session is not None and session.get("hermes_session_id"):
        review = await ask_hermes(_PROCEDURE_REVIEW, timeout, session=session, _review=False)
        return f"{answer or 'Hermes concluiu sem resposta textual.'}\n\nRevisão de procedimento: {review}"
    return answer or "Hermes concluiu sem resposta textual."
