"""Dá ao Jarvis o contexto das propostas do Vigia quando o senhor responde "instala N".

O Vigia envia as propostas direto pela API do Telegram, fora da sessão do agente; sem isto o
Jarvis recebe "instala 1" sem saber a que se refere. A fonte é a última entrega confirmada em
`profiles/vigia/cron/vigia.db` (somente leitura) e o bloco completo em `propostas/propostas.md`.
Só acrescenta contexto: não instala nada nem responde no lugar do agente.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
QUEUE = HOME / "profiles" / "vigia" / "cron" / "vigia.db"
HISTORY = HOME / "jarvis" / "propostas" / "propostas.md"

_VERB = r"(?:instala|instalar|instale|instalem|aprova|aprovo|aprovar|aprove|aplica|aplicar|aplique)"
_REPLY = re.compile(
    rf"(?:pode |vigia )?{_VERB}(?: a| as| o| os)?(?: proposta| propostas| item| itens| opcao| numero)?"
    r" (?P<nums>[1-9](?:(?: e| a)? [1-9])*)(?: do vigia)?"
)


def _norm(text: str) -> str:
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )
    return " ".join(re.sub(r"[^\w]+", " ", folded).split())


def requested(message: str) -> list[int]:
    """Números de proposta aprovados em respostas curtas como "instala 1" ou "aprova 1 e 2"."""
    said = _norm(message)
    match = _REPLY.fullmatch(said) if len(said) <= 60 else None
    return sorted({int(n) for n in re.findall(r"[1-9]", match["nums"])}) if match else []


def _last_delivery(queue: Path) -> tuple[str, str] | None:
    if not queue.exists():
        return None
    with closing(sqlite3.connect(f"file:{queue.as_posix()}?mode=ro", uri=True, timeout=5)) as con:
        row = con.execute(
            "SELECT ref, message FROM outbox WHERE status='sent' AND message LIKE '%instala N%' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    return (row[0], row[1]) if row else None


def _proposals(history: Path, ref: str) -> dict[int, str]:
    text = history.read_text(encoding="utf-8") if history.exists() else ""
    marker = f"<!-- relatorio:{ref} -->"
    if marker not in text:
        return {}
    block = text.split(marker, 1)[1].split("<!-- relatorio:", 1)[0]
    found: dict[int, str] = {}
    for part in re.split(r"(?m)^(?=### Proposta \d+:)", block):
        head = re.match(r"### Proposta (\d+):", part)
        if head:
            found[int(head[1])] = re.sub(r"\n*<!-- problema:[^>]*-->\s*$", "", part.strip()).strip()
    return found


def _from_message(message: str) -> dict[int, str]:
    """Reserva: os itens como foram enviados, caso o bloco não esteja em propostas.md."""
    items = re.split(r"(?m)^(?=\d+\. )", message)
    return {int(i.split(".", 1)[0]): i.strip() for i in items if re.match(r"\d+\. ", i)}


def context_for(message: str, queue: Path | None = None, history: Path | None = None) -> str | None:
    numbers = requested(message)
    if not numbers:
        return None
    delivery = _last_delivery(queue or QUEUE)
    if delivery is None:
        return None
    ref, sent = delivery
    proposals = _proposals(history or HISTORY, ref) or _from_message(sent)
    date = re.match(r"Vigia \((\d{4}-\d{2}-\d{2})\)", sent)
    lines = [
        f"Contexto: o senhor está respondendo à última entrega do Vigia"
        f"{f' de {date[1]}' if date else ''} (referência {ref}), enviada pelo Telegram fora desta conversa "
        "com o pedido \"Responda 'instala N' para aprovar\".",
        f"Esta mensagem é aprovação explícita somente da(s) proposta(s) {', '.join(map(str, numbers))}; "
        "não aplique nenhuma outra. Execute a sugestão, confira pelo item 'verificar' e informe o resultado real.",
    ]
    for number in numbers:
        lines.append(proposals.get(number, f"Proposta {number}: não existe nesta entrega; pergunte ao senhor qual ele quer."))
    return "\n\n".join(lines)


def inject(user_message="", **_kwargs):
    if not isinstance(user_message, str):
        return None
    try:
        text = context_for(user_message)
    except (OSError, sqlite3.Error):
        return None
    return {"context": text} if text else None


def register(ctx):
    ctx.register_hook("pre_llm_call", inject)
