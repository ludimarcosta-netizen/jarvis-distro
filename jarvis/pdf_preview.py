"""Read-only PDF previews through the application's existing PDF runtime."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 12000
MAX_PAGES = 10


def _runtime_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except ImportError:
        return Path(__file__).resolve().parent.parent


def preview_pdf(raw: bytes) -> tuple[str, bool]:
    if len(raw) > MAX_BYTES:
        raise ValueError("Prévia limitada a PDFs de até 8 MB.")
    python = _runtime_home() / "runtime/pdf-signing/Scripts/python.exe"
    if not python.is_file():
        raise ValueError("Leitor PDF local indisponível; use Abrir arquivo.")
    try:
        result = subprocess.run([str(python), str(Path(__file__).resolve())], input=raw,
                                capture_output=True, check=False, timeout=6,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(result.stdout.decode("utf-8"))
        if result.returncode or not data.get("ok"):
            raise ValueError("PDF protegido, inválido ou indisponível para prévia.")
        text = data["text"]
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ValueError("Resposta PDF inválida.")
        return text, bool(data["truncated"])
    except subprocess.TimeoutExpired:
        raise ValueError("A prévia PDF excedeu seis segundos; use Abrir arquivo.") from None
    except (OSError, KeyError, json.JSONDecodeError, UnicodeError):
        raise ValueError("Leitor PDF indisponível; use Abrir arquivo.") from None


def _read(raw: bytes) -> dict:
    import pymupdf

    if len(raw) > MAX_BYTES:
        raise ValueError("PDF too large")
    with pymupdf.open(stream=raw, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("Encrypted PDF")
        parts = []
        length = 0
        has_text = False
        truncated = len(document) > MAX_PAGES
        for index in range(min(MAX_PAGES, len(document))):
            remaining = MAX_TEXT - length
            text = document[index].get_text()
            has_text = has_text or bool(text.strip())
            page = f"Página {index + 1}\n{text}"
            parts.append(page[:remaining])
            length += len(parts[-1])
            if len(page) > remaining:
                truncated = True
                break
        text = "".join(parts)
        if not has_text:
            text = "PDF sem camada de texto nas páginas consultadas. Abra no leitor local; OCR não foi executado."
        return {"ok": True, "text": text, "truncated": truncated}


if __name__ == "__main__":
    try:
        output = _read(sys.stdin.buffer.read(MAX_BYTES + 1))
    except Exception:  # noqa: BLE001 — native parser errors never reach the UI/logs
        output = {"ok": False}
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))
