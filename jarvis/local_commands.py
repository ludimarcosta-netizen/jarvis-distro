"""Conservative text shortcuts shared with the desktop workspace executors."""
from __future__ import annotations

import re

from local_files import FilesSession, fold

_SEARCH = re.compile(r"^(?:(?:ei|hey|oi)\s+)?(?:jarvis[, ]+)?"
                     r"(?:procura|procure|busca|busque|encontre|localize)\s+"
                     r"(?:(?:o|um|os)\s+)?arquivos?\s+(?:chamados?\s+)?(.+?)[.!?]*$", re.IGNORECASE)
_FOLLOWUP = re.compile(r"^(?:jarvis[, ]+)?(abra|abre|mostre|mostra|leia)\s+"
                       r"(?:(?:o|esse|este)\s+)?(?:(primeiro|segundo|terceiro|[1-9]|10)\s+)?"
                       r"arquivo[.!?]*$", re.IGNORECASE)
_ORDINALS = {"primeiro": 1, "segundo": 2, "terceiro": 3}


def try_command(text: str, files: FilesSession) -> str | None:
    if not isinstance(text, str) or not 1 <= len(text) <= 220:
        return None
    search = _SEARCH.fullmatch(text.strip())
    if search:
        query = search.group(1)
        if re.search(r"[,;\n]|\s(?:e|depois|entao)\s", fold(query)):
            return None
        try:
            result = files.search(query)
        except (OSError, ValueError):
            return "Não consegui concluir a busca local. Tente novamente ou use o painel Arquivos."
        lines = [f"{number}. {row['name']} ({row['location']})"
                 for number, row in enumerate(result["results"], 1)]
        message = "Arquivos encontrados (mais recentes primeiro):\n" + "\n".join(lines) if lines else "Não encontrei arquivo nos diretórios locais permitidos."
        if result["partial"] or result["unreadable"]:
            message += " A busca foi parcial; refine o nome."
        return message + ("\nVocê pode pedir: mostre o primeiro arquivo." if lines else "")
    followup = _FOLLOWUP.fullmatch(fold(text.strip()))
    if not followup:
        return None
    verb, ordinal = followup.groups()
    if ordinal is None and not files.selected_ref and len(files.last_refs) != 1:
        return "Qual arquivo? Selecione um número dos resultados da busca atual."
    number = _ORDINALS.get(ordinal, int(ordinal) if ordinal and ordinal.isdigit() else 1)
    try:
        ref = files.select(number) if ordinal else files.current()
        if verb in {"abra", "abre"}:
            return files.open_file(ref)["message"]
        path = files.resolve(ref)
        return (f"Arquivo selecionado: {path.name}. Para ler sem enviar conteúdo ao modelo, "
                "busque esse nome no painel Arquivos e clique em Prévia ou Abrir arquivo.")
    except (OSError, ValueError):
        return "Não foi possível acessar a referência local. Use o painel Arquivos para conferir ou busque novamente."
