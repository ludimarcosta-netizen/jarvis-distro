"""Route document signing before the unrestricted agent, including resumed sessions."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import unicodedata
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / "runtime/pdf-signing/Scripts/python.exe"
CONTROLLER = ROOT / "skills/productivity/pdf-signing/scripts/agent_route.py"


def fold(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )


_PICK_FILLER = {
    "cara", "e", "o", "a", "opcao", "numero", "n", "esse", "essa", "pode", "ser", "por", "favor",
    "sim", "isso", "ai", "da", "do", "de", "foi", "eh", "mesmo", "mesma", "senhor", "jarvis",
}


def pending_pick(request: str, scope: str) -> str | None:
    """While a contact choice is pending, "2 cara" or "Beltrana" -> "2" (the option number)."""
    import hashlib

    home = Path(os.environ.get("HERMES_HOME", ROOT))
    choice = (
        home / "state/pdf-signing/routes" / (hashlib.sha256(scope.encode()).hexdigest() + ".choice.json")
    )
    try:
        options = json.loads(choice.read_text(encoding="utf-8")).get("options") or []
    except (OSError, ValueError, AttributeError):
        return None
    words = [w for w in re.sub(r"[^\w]+", " ", fold(request)).split() if w not in _PICK_FILLER]
    if not options or not words or len(words) > 3:
        return None
    if len(words) == 1 and words[0].isdigit():
        return words[0] if 1 <= int(words[0]) <= len(options) else None
    if any(w.isdigit() or len(w) < 3 for w in words):
        return None
    matches = [
        n
        for n, option in enumerate(options, 1)
        if all(w in re.sub(r"[^\w]+", " ", fold(option.get("name", ""))).split() for w in words)
    ]
    return str(matches[0]) if len(matches) == 1 else None


def pending_anywhere(max_age: float = 24 * 3600) -> bool:
    """A previewed document awaits authorization in some conversation of the last 24 h."""
    import time

    home = Path(os.environ.get("HERMES_HOME", ROOT))
    ids = []
    for route in (home / "state/pdf-signing/routes").glob("*.json"):
        try:
            if len(route.stem) != 64 or time.time() - route.stat().st_mtime > max_age:
                continue
            data = json.loads(route.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ids += [i for i in data if isinstance(i, str)] if isinstance(data, list) else []
    if not ids:
        return False
    try:
        database = home / "state/pdf-signing/requests.sqlite3"
        with closing(
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            return any(
                connection.execute(
                    "SELECT 1 FROM requests WHERE id=? AND state='prepared'", (i,)
                ).fetchone()
                for i in ids
            )
    except (OSError, sqlite3.Error):
        return False


# A slash-command skill load is injected as the user message with the skill's own text
# (agent/skill_commands.py). That text may list document/action words in prose, which is not
# a typed signing request — unless the pdf-signing skill itself is the one being invoked.
_SKILL_LOAD_MARKER = "The user has invoked the"
_OWN_SKILL_SLUG = "pdf-signing"


def is_pdf_request(request: str, scope: str | None = None) -> bool:
    raw = request or ""
    if _SKILL_LOAD_MARKER in raw and _OWN_SKILL_SLUG not in raw:
        return False
    text = fold(request)

    # Ignora documentos locais, caminhos no disco ou conversas sobre skills/codigo
    if re.search(r"\b(local|locais|downloads?|pasta|diretorio|computador|pc|maquina|disco)\b", text):
        return False
    if re.search(r"[a-z]:[/\\]|[/\\]|\.pdf\b", text):
        return False
    if re.search(r"\b(skills?|scripts?|codigo|programar|salvar|criar|ajustar|desenvolver)\b", text):
        return False

    if re.search(
        r"\b(?:primeira|segunda|terceira|ultima|\d)\s+assinatura\b|\bassinatura\s+\d\b",
        text,
    ):
        return True  # choosing which signature to keep
    document = re.search(
        r"\b(pdfs?|arquivos?|documentos?|minutas?|portarias?|decretos?|leis?|oficios?|solicitacao)\b",
        text,
    )
    signing = re.search(
        r"\b(assina\w*|certificado\w*|aprov\w*|autoriz\w*|previa\w*|devol\w*|envi\w*|status)\b",
        text,
    )
    if bool(document and signing) or bool(re.search(r"\bpdf[ -]signing\b", text)):
        return True
    if scope and re.fullmatch(
        r"\s*(?:sim|pode continuar|continue|pode assinar|assine|assina|assinar|autorizo|aprovo|pode enviar|envie|devolva|status|(?:(?:e\s+)?(?:o|a)\s+|opcao\s+|numero\s+)?\d)[.!?\s]*",
        text,
    ):
        import hashlib

        home = Path(os.environ.get("HERMES_HOME", ROOT))
        route = (
            home
            / "state/pdf-signing/routes"
            / (hashlib.sha256(scope.encode()).hexdigest() + ".json")
        )
        choice = route.with_suffix(".choice.json")
        if re.search(r"\d", text):
            # A bare number only belongs to the PDF flow while a contact choice is pending.
            try:
                return bool(json.loads(choice.read_text(encoding="utf-8")).get("options"))
            except (OSError, ValueError, AttributeError):
                return False
        if route.is_file():
            try:
                ids = json.loads(route.read_text(encoding="utf-8"))
                database = home / "state/pdf-signing/requests.sqlite3"
                with closing(
                    sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
                ) as connection:
                    return any(
                        connection.execute(
                            "SELECT 1 FROM requests WHERE id=? AND state NOT IN ('delivered','failed')",
                            (identifier,),
                        ).fetchone()
                        for identifier in ids
                    )
            except (OSError, ValueError, sqlite3.Error):
                return True
    return False


def run(
    request: str,
    scope: str,
    existing_requests: list[str] | None = None,
    chat_authorized: bool = False,
) -> str:
    """No shell, LLM tool runner, fallback signer or local filename search."""
    if not PYTHON.is_file() or not CONTROLLER.is_file():
        return "Fluxo de assinatura indisponível. Nenhum documento foi assinado ou enviado; não usarei ferramenta alternativa."
    try:
        completed = subprocess.run(
            [str(PYTHON), "-B", str(CONTROLLER)],
            input=json.dumps(
                {
                    "request": request,
                    "scope": scope,
                    "existing_requests": existing_requests or [],
                    "chat_authorized": chat_authorized,
                }
            ).encode("utf-8"),
            capture_output=True,
            timeout=300,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            # The gateway's PYTHONPATH belongs to Hermes' own Python; the signer runtime has its own.
            env={
                k: v
                for k, v in os.environ.items()
                if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
            },
        )
    except (OSError, subprocess.SubprocessError):
        return "O controlador não confirmou o resultado. Não há conclusão comprovada de assinatura ou envio; confira o estado antes de repetir."
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return "Resposta inválida do controlador. Assinatura e envio não foram confirmados."
    if (
        not isinstance(result, dict)
        or result.get("controller") != "pdf-signing"
        or not isinstance(result.get("message"), str)
    ):
        return "Resposta sem comprovação do fluxo de assinatura; nenhuma conclusão será declarada."
    return result["message"]
