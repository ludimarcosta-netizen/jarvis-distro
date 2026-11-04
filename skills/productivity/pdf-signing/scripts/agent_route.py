"""Deterministic request controller used by the actual Jarvis entry points."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import pymupdf
from analyze import analyze
from evolution import Blocked, Evolution, NeedsChoice
from windows_signer import inventory
from workflow import Workflow

ROOT = Path(__file__).resolve().parents[4]


def fold(text):
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )


def placements(raw, candidates, certificates):
    result = []
    with pymupdf.open(stream=raw, filetype="pdf") as pdf:
        for alias in candidates:
            matches = []
            for page in pdf:
                blocks = page.get_text("dict")["blocks"]
                text_lines = [
                    pymupdf.Rect(line["bbox"])
                    for block in blocks
                    for line in block.get("lines", [])
                    if "".join(span["text"] for span in line["spans"]).strip()
                ]
                for block in blocks:
                    for line in block.get("lines", []):
                        text = " ".join(span["text"] for span in line["spans"]).strip()
                        folded = fold(text)
                        # A signature line holds only the bare or full name ("BELTRANO SEIS ...");
                        # it may sit high on a short last page. Body text carries punctuation or more words.
                        if (
                            (folded == alias or folded.startswith(alias + " "))
                            and len(folded.split()) <= 6
                            and not re.search(r"[,;:]", text)
                        ):
                            matches.append(
                                (
                                    page.number,
                                    page.rect,
                                    pymupdf.Rect(line["bbox"]),
                                    text_lines,
                                )
                            )
            mapped = [
                row
                for row in certificates
                if row.get("alias") == alias
                and row.get("has_private_key")
                and row.get("valid_now")
            ]
            pages = [match[0] for match in matches]
            # The model's candidate list is a hypothesis: a candidate without a signature line in a
            # multi-candidate list is left out (Decreto 134: "beltrano, __JARVIS_USER_LOWER__", only the mayor signs).
            if not matches and len(candidates) > 1:
                continue
            # One signature line per page (law + annex may each carry one); two on a page is ambiguous.
            if not matches or len(pages) != len(set(pages)) or len(mapped) != 1:
                raise Blocked(
                    "Assinante, posição ou certificado ambíguo; conferir antes de preparar a prévia."
                )
            for page_number, page_rect, name_rect, text_lines in matches:
                center = (name_rect.x0 + name_rect.x1) / 2
                left = max(0, center - 175)
                right = min(page_rect.width, center + 175)
                # Stamp the free gap between the nearest text above and the signer's name (top-down coords).
                bottom = name_rect.y0 - 2
                above = [
                    r.y1
                    for r in text_lines
                    if r.y1 <= name_rect.y0 and r.x0 < right and r.x1 > left
                ]
                top = max(max(above, default=0) + 2, bottom - 65)
                if bottom - top < 30:
                    raise Blocked(
                        "Sem espaço livre acima do nome do assinante; conferir antes de preparar a prévia."
                    )
                result.append(
                    {
                        "signer": alias,
                        "page": page_number,
                        "box": [
                            left,
                            page_rect.height - bottom,
                            right,
                            page_rect.height - top,
                        ],
                        "certificate": mapped[0]["fingerprint"],
                    }
                )
    if not result:
        raise Blocked("Nenhum assinante inequívoco identificado; revisão necessária.")
    return result


def review_report(workflow, ids, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    sections = []
    for request_id in ids:
        row = workflow.get(request_id)
        plan = workflow.checked_plan(row)
        parts = [
            f"<h2>Documento {html.escape(workflow.label(request_id))}</h2>",
            f"<p>Identificador interno: <code>{html.escape(request_id)}</code></p>",
            f"<p><strong>Resumo:</strong> {html.escape(plan['summary'])}</p>",
            "<ul>"
            + "".join(f"<li>{html.escape(x)}</li>" for x in plan["obligations"])
            + "</ul>",
            f"<p>SHA-256 do PDF: <code>{row['pdf_hash']}</code></p>",
        ]
        for placement in plan["placements"]:
            parts.append(
                f"<p>Assinante: <b>{placement['signer']}</b> · Certificado SHA-256: <code>{placement['certificate']}</code></p>"
            )
        for name in plan["previews"]:
            parts.append(
                f'<img alt="Prévia da posição da assinatura" src="{(workflow.folder(request_id) / name).as_uri()}">'
            )
        sections.append("".join(parts))
    destination.write_text(
        "<!doctype html><meta charset=utf-8><title>Revisão de documentos</title>"
        "<style>body{font:16px system-ui;max-width:900px;margin:32px auto;padding:16px}"
        "img{max-width:100%;border:1px solid #aaa}code{overflow-wrap:anywhere}</style>"
        "<h1>Documentos preparados — aguardando autorização</h1>"
        "<p>A assinatura e a devolução à conversa original só ocorrerão após confirmação humana específica.</p>"
        + "".join(sections),
        encoding="utf-8",
    )


def execute(
    request,
    scope,
    state_dir=None,
    client_factory=Evolution,
    analyzer=analyze,
    certificates=inventory,
    reviewer=None,
    opener=None,
    existing_requests=None,
    chat_authorized=False,
):
    state_dir = Path(
        state_dir or Path(os.environ.get("HERMES_HOME", ROOT)) / "state/pdf-signing"
    )
    workflow = Workflow(state_dir)
    routes = state_dir / "routes"
    routes.mkdir(exist_ok=True)
    route_file = routes / (hashlib.sha256(scope.encode()).hexdigest() + ".json")
    normalized = fold(request)
    try:
        ids = (
            json.loads(route_file.read_text(encoding="utf-8"))
            if route_file.exists()
            else []
        )
        if not ids and existing_requests:
            reusable = []
            for identifier in list(dict.fromkeys(existing_requests))[:10]:
                try:
                    row = workflow.get(identifier)
                    workflow.original(row)
                    if row["state"] in {
                        "received",
                        "prepared",
                        "approved",
                        "signed",
                        "sending",
                    }:
                        reusable.append(identifier)
                except (Blocked, OSError):
                    continue
            if reusable:
                ids = reusable
                route_file.write_text(json.dumps(ids), encoding="utf-8")
        source = re.search(
            r"\b(?:da|do|de)\s+((?:(?!\b(?:documentos?|pdfs?|anexos?|recebi)\b).)+?)\s+(?:no|pelo|via|por)\s+whats(?:app)?\b",
            request,
            re.IGNORECASE,
        )
        if not source:
            source = re.search(
                r"\b(?:a|o)\s+((?:(?!\b(?:que|q|documentos?|pdfs?|arquivos?|anexos?)\b)[\w .,'-]){1,120}?)\s+(?:me\s+)?(?:mandou|mandaram|enviou|enviaram|encaminhou|encaminharam)\b",
                request,
                re.IGNORECASE,
            )
        requested_count = (
            2
            if re.search(r"\b(dois|duas|2)\b", normalized)
            else None
            if re.search(r"\b(documentos|pdfs|arquivos|anexos)\b", normalized)
            else 1
        )
        choice_file = routes / (route_file.stem + ".choice.json")
        pick = re.fullmatch(
            r"\s*(?:(?:e\s+)?(?:o|a)\s+|opcao\s+|numero\s+)?(\d)\s*[.!]?\s*", normalized
        )
        pending_choice = (
            json.loads(choice_file.read_text(encoding="utf-8"))
            if choice_file.exists()
            else {}
        )
        if pick and pending_choice.get("options"):
            options = pending_choice["options"]
            number = int(pick.group(1))
            if not 1 <= number <= len(options):
                raise Blocked(f"Responda com um número de 1 a {len(options)}.")
            option = options[number - 1]
            client = client_factory()
            # The user's pick becomes an alias: next time the same name resolves directly.
            client.remember(pending_choice["contact"], option["chat"])
            choice_file.write_text("{}", encoding="utf-8")
            # Senders already resolved in the same request, then the ones still to resolve.
            selected = [tuple(x) for x in pending_choice.get("selected", [])]
            selected += client.select_chat(option["chat"], pending_choice["count"])
            names = pending_choice.get("names", []) + [option["name"]]
            resolved, question = resolve_senders(
                client,
                pending_choice.get("queue", []),
                pending_choice["count"],
                choice_file,
                selected,
                names,
            )
            if question:
                return question
            selected, names = resolved
            return ingest_and_prepare(
                workflow,
                client,
                selected,
                route_file,
                analyzer,
                certificates,
                opener,
                ", ".join(names),
            )
        # The list may never have reached the user (Hermes dropped the turn): a bare follow-up
        # repeats it instead of answering with an older batch's status.
        if (
            pending_choice.get("options")
            and not source
            and time.time() - choice_file.stat().st_mtime <= PENDING_WINDOW
            and re.fullmatch(
                r"\s*(?:(?:sim|ok)[,.!]?\s*)?(?:nada ainda|e os documentos|e ai|e entao|continue|continua|pode continuar"
                r"|sim|ok|status|pode assinar|assine|assina|assinar|autorizo|aprovo)?[?!.,\s]*",
                normalized,
            )
        ):
            return choice_question(
                pending_choice["contact"],
                pending_choice["options"],
                pending_choice.get("names", []),
            )
        terminal = all(
            workflow.get(identifier)["state"] in {"delivered", "failed"}
            for identifier in ids
        )
        # "pode assinar ..." with nothing pending here: the batch may be in another conversation.
        borrowed = False
        if (
            (not ids or terminal)
            and not source
            and AUTHORIZATION.match(normalized)
            and not re.search(r"\bwhats", normalized)
        ):
            elsewhere = pending_elsewhere(workflow, routes, route_file)
            if elsewhere:
                ids, borrowed = elsewhere, True
        continuing = not re.search(
            r"\b(recebi|recebido|recebidos|chegou|chegaram)\b", normalized
        ) and not (source and terminal)
        if ids and continuing:
            # "me mandou ... no whats para assinar" describes the request; it is not an authorization.
            # "... da Simone" names a sender: never authorizes this chat's other documents.
            describing = re.search(
                r"\b(me\s+(?:mandou|mandaram|enviou|enviaram|encaminhou|encaminharam)|(?:no|pelo|via)\s+whats(?:app)?|para\s+(?:assinar|assinatura)"
                r"|(?:da|do)\s+(?!(?:leis?|portarias?|decretos?|oficios?|primeira|segunda|terceira|ultima|pagina|assinatura|documentos?"
                r"|prefeit[oa]|prefeitura|municipio|gabinete|secretari[oa])\b)[a-z]{3,})\b",
                normalized,
            )
            authorizing = not describing and re.search(
                r"\b(autorizar|autorizo|autorizado|aprovar|aprovo|aprovado|assinar|assine|assina)\b",
                normalized,
            )
            # "pode assinar o da Sicrana": authorizes only that sender's documents of this batch.
            restricted = None if source else re.match(
                r"\s*(?:sim[,.!]?\s*)?(?:pode\s+assinar|assine|assina|autorizo|aprovo)\s+(?:(?:so|somente|apenas)\s+)?"
                r"(?:(?:o|a|os|as)\s+)?(?:(?:documentos?|pdfs?|arquivos?)\s+)?(?:da|do|das|dos|de)\s+"
                r"(?!(?:leis?|portarias?|decretos?|oficios?|minutas?|primeira|segunda|terceira|ultima|pagina|assinatura)\b)"
                r"([a-z][\w .'-]*?)\s*(?:[,.;!?]|$)",
                normalized,
            )
            held = []
            if restricted:
                client = client_factory()
                wanted = split_contacts(restricted.group(1))

                def matching(candidates):
                    labels = sender_labels(workflow, client, candidates)
                    return [
                        i
                        for i in candidates
                        if any(sender_matches(n, labels[i]) for n in wanted)
                    ]

                subset = matching(ids)
                if not subset and not borrowed:
                    elsewhere = pending_elsewhere(workflow, routes, route_file)
                    subset = matching(elsewhere)
                    if subset:
                        ids, borrowed = elsewhere, True
                if not subset:
                    return (
                        f"Não achei documento pendente de {', '.join(wanted)} nesta conversa"
                        " nem nas outras das últimas 24 horas. Nada foi assinado."
                        + ("\n" + status(workflow, ids) if ids and not borrowed else "")
                    )
                held = [i for i in ids if i not in subset]
                ids = subset
                authorizing = True
            elif borrowed:
                # Without a sender's name, a batch from another conversation is only listed.
                labels = sender_labels(workflow, client_factory(), ids)
                return "\n".join(
                    [
                        "Nesta conversa não há documento aguardando. Em outra conversa estão aguardando autorização:",
                        *(f"- {workflow.label(i)} (de {sender_name(labels[i])})" for i in ids),
                        'Diga de quem assinar, por exemplo "pode assinar o da '
                        + sender_name(labels[ids[0]]).split()[0]
                        + '". Nada foi assinado.',
                    ]
                )
            if source and not authorizing:
                # Naming the sender again re-reads that sender's PDFs: documents left out earlier
                # are re-evaluated. ingest() reuses already downloaded messages (no new download).
                client = client_factory()
                for contact in split_contacts(source.group(1)):
                    try:
                        selected = client.select_documents(contact, requested_count)
                        ids = list(
                            dict.fromkeys(
                                ids + [workflow.ingest(client, c, m) for c, m in selected]
                            )
                        )
                    except Blocked:
                        pass  # sender not resolvable now: keep working on the known documents
            states = [workflow.get(identifier)["state"] for identifier in ids]
            # Analysis failed earlier (e.g. 503): resume it. Authorization only reaches documents
            # already previewed; a pending one never blocks signing the prepared ones.
            pending = "received" in states or any(retryable(workflow, i) for i in ids)
            if pending and not (authorizing and "prepared" in states):
                return prepare_batch(
                    workflow, ids, route_file, analyzer, certificates, opener
                )
            choices, problem = signature_choices(workflow, ids, normalized)
            if problem:
                return problem + " Nada foi assinado."
            for request_id, keep in choices:
                plan = workflow.checked_plan(workflow.get(request_id))
                workflow.prepare(
                    request_id,
                    {
                        "summary": plan["summary"],
                        "title": plan.get("title"),
                        "obligations": plan["obligations"],
                        "placements": [plan["placements"][i] for i in keep],
                    },
                )
            if choices and not authorizing:
                return (
                    prepared_reply(
                        workflow,
                        [request_id for request_id, _ in choices],
                        routes / (route_file.stem + ".html"),
                        opener,
                    )
                    + '\n\nAssinaturas ajustadas. Para assinar, diga "pode assinar".'
                )
            if (
                not choices
                and authorizing
                and not restricted
                and re.search(
                    r"\b(somente|so|apenas|exceto|menos|primeira|segunda|ultima|nao)\b",
                    normalized,
                )
            ):
                # Conditions would change the approved plan; never sign something other than the preview.
                multiple = [
                    f"documento {workflow.label(i)}: {len(workflow.checked_plan(workflow.get(i))['placements'])} assinaturas"
                    for i in ids
                    if workflow.get(i)["plan"]
                    and len(json.loads(workflow.get(i)["plan"])["placements"]) > 1
                ]
                return "\n".join(
                    [
                        "Não entendi a condição da autorização. Nada foi assinado.",
                        *multiple,
                        'Para escolher assinaturas, diga por exemplo "só a primeira assinatura da Lei 1.674".',
                        'Para assinar tudo como na prévia, diga só "pode assinar".',
                    ]
                )
            if authorizing and all(
                workflow.get(i)["state"] in {"delivered", "failed"} for i in ids
            ):
                # Never answer an authorization with an older, already finished document's status.
                return (
                    "Nenhum documento novo aguardando assinatura nesta conversa. Nada foi assinado ou enviado agora."
                    + (
                        " Ainda falta escolher a conversa: responda com o número da lista."
                        if pending_choice.get("options")
                        else ' Diga de quem é o documento, por exemplo "a Beltrana me mandou um documento no WhatsApp, assina".'
                    )
                )
            if authorizing:
                # Spoken command only opens the human review; it never substitutes for its decision.
                if reviewer is None and not chat_authorized:
                    from main import human_approval

                    reviewer = human_approval
                failures = []
                # A signing that failed without output returns with the plan already shown.
                prepare_received(
                    workflow,
                    [i for i in ids if retryable(workflow, i)],
                    analyzer,
                    certificates,
                )
                for request_id in ids:
                    row = workflow.get(request_id)
                    try:
                        if row["state"] == "prepared":
                            if chat_authorized:
                                workflow.checked_plan(row)
                                workflow.approve(request_id, row["plan_hash"])
                                with workflow.db:
                                    workflow.audit(
                                        request_id, "explicit-chat-authorization"
                                    )
                            else:
                                reviewer(workflow, request_id)
                        if workflow.get(request_id)["state"] == "approved":
                            workflow.sign(request_id)
                    except Exception as exc:  # noqa: BLE001 — one bad PDF must not stop the batch
                        log_failure(request_id, exc)
                        failures.append(
                            f"Falha ao assinar o documento {workflow.label(request_id)}; registrado no log. Os demais seguiram."
                        )
                # Explicit chat authorization covers signing and return to the original chat.
                if chat_authorized:
                    deliver_signed(workflow, ids, client_factory)
                waiting = [i for i in held if workflow.get(i)["state"] == "prepared"]
                notes = (
                    ["Continuam aguardando autorização: " + "; ".join(workflow.label(i) for i in waiting)]
                    if waiting
                    else []
                )
                return "\n\n".join([status(workflow, ids), *failures, *notes])
            if re.search(r"\b(enviar|envie|devolver|devolva)\b", normalized):
                deliver_signed(workflow, ids, client_factory)
                return status(workflow, ids)
            if all(
                workflow.get(identifier)["state"] == "prepared" for identifier in ids
            ):
                report = routes / (route_file.stem + ".html")
                review_report(workflow, ids, report)
                if opener:
                    opener(str(report))
                return status(workflow, ids) + "\nResumo e prévias: " + str(report)
            return status(workflow, ids)
        client = client_factory()
        if source:
            contacts = split_contacts(source.group(1))
        else:
            # Phrasing outside the patterns: look for a recent PDF sender named anywhere in it.
            contacts = client.senders_named_in(request)
            if not contacts:
                raise Blocked(
                    'Não reconheci quem mandou o documento entre quem enviou PDF nas últimas 24 horas. Diga o nome, por exemplo "a Sicrana me mandou um documento no WhatsApp, assina".'
                )
        # "o último documento": every recent one from the sender, then keep the newest.
        latest = requested_count == 1 and re.search(r"\bultim[oa]\b", normalized)
        resolved, question = resolve_senders(
            client, contacts, None if latest else requested_count, choice_file
        )
        if question:
            return question
        selected, names = resolved
        if latest:
            selected = selected[-1:]
        return ingest_and_prepare(
            workflow,
            client,
            selected,
            route_file,
            analyzer,
            certificates,
            opener,
            ", ".join(names) or None,
        )
    finally:
        workflow.close()


def split_contacts(text):
    """'Beltrana e a Sicrana' / 'Beltrana, Sicrana e Ciclana' -> one name per sender."""
    names = (
        re.sub(r"^(?:a|o|as|os|da|do|de)\s+", "", part.strip(), flags=re.IGNORECASE)
        for part in re.split(r"\s*,\s*|\s+e\s+", text.strip(), flags=re.IGNORECASE)
    )
    return [name for name in names if name]


AUTHORIZATION = re.compile(
    r"\s*(?:(?:sim|ok|autorizado|aprovado)[,.!]?\s*)?(?:pode\s+assinar|assine|assina|autorizo|aprovo|autorizado|aprovado)\b"
)
PENDING_WINDOW = 24 * 3600  # batches of other conversations older than this are not reused


def pending_elsewhere(workflow, routes, route_file):
    """Previewed documents awaiting authorization in other conversations of the last 24 h."""
    found = []
    for route in routes.glob("*.json"):
        if (
            route == route_file
            or len(route.stem) != 64  # skips <hash>.choice.json
            or time.time() - route.stat().st_mtime > PENDING_WINDOW
        ):
            continue
        try:
            ids = json.loads(route.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for request_id in ids if isinstance(ids, list) else []:
            try:
                if workflow.get(str(request_id))["state"] == "prepared":
                    found.append(request_id)
            except Blocked:
                continue
    return list(dict.fromkeys(found))


def sender_name(labels):
    """Most readable of a sender's names ('.' profiles skipped)."""
    return next((label for label in labels if len(label.strip(" .")) >= 2), "remetente")


def sender_labels(workflow, client, ids):
    """request id -> names the user may call its sender: profile, agenda and confirmed aliases."""
    labels = {}
    for request_id in ids:
        row = workflow.get(request_id)
        source = json.loads(row["source"])
        chats = {row["chat"], (source.get("key") or {}).get("remoteJidAlt") or row["chat"]}
        labels[request_id] = [source.get("pushName") or "", *client.chat_labels(chats)]
    return labels


def sender_matches(name, labels):
    """'Sicrana' matches 'Sicrana (.)'; every word typed must appear in one of the sender's names."""
    words = [w for w in re.sub(r"[^\w]+", " ", fold(name)).split() if len(w) >= 3]
    return bool(words) and any(
        set(words) <= set(re.sub(r"[^\w]+", " ", fold(label)).split()) for label in labels
    )


def choice_question(contact, options, names=()):
    lines = [f"Já identifiquei: {', '.join(names)}."] if names else []
    lines.append(
        f'Não identifiquei "{contact}" com certeza. Quem mandou PDF nas últimas 24 horas:'
    )
    lines.extend(
        f"{n}. {o['name']} ({o['documents']} PDF{'s' if o['documents'] > 1 else ''})"
        for n, o in enumerate(options, 1)
    )
    lines.append(
        "Responda só com o número. Vou lembrar a escolha. Nada foi baixado ou assinado."
    )
    return "\n".join(lines)


def resolve_senders(client, contacts, count, choice_file, selected=(), names=()):
    """Documents of each sender in turn, nothing downloaded yet. An unclear sender pauses the
    request with a numbered choice that carries the senders already resolved and those left."""
    selected, names = list(selected), list(names)
    for index, contact in enumerate(contacts):
        try:
            selected.extend(client.select_documents(contact, count))
        except NeedsChoice as exc:
            choice_file.write_text(
                json.dumps(
                    {
                        "contact": contact,
                        "count": count,
                        "options": exc.options,
                        "selected": selected,
                        "names": names,
                        "queue": contacts[index + 1 :],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return None, choice_question(contact, exc.options, names)
        chosen = getattr(client, "chosen", None)
        if isinstance(chosen, str):
            names.append(chosen)
    return (selected, names), None


def ingest_and_prepare(
    workflow, client, selected, route_file, analyzer, certificates, opener, chosen
):
    ids = [workflow.ingest(client, chat, message_id) for chat, message_id in selected]
    route_file.write_text(json.dumps(ids), encoding="utf-8")
    reply = prepare_batch(workflow, ids, route_file, analyzer, certificates, opener)
    return f"Conversa: {chosen}\n\n{reply}" if chosen else reply


def prepare_batch(workflow, ids, route_file, analyzer, certificates, opener):
    """Prepare what can be signed; a document that needs no signature never blocks the rest."""
    skipped = prepare_received(workflow, ids, analyzer, certificates)
    retry = [i for i, (summary, _) in skipped.items() if summary is None]
    notes = [
        f'Análise pendente — documento {i}: {reason} Diga "continue" para tentar de novo.'
        for i, (summary, reason) in skipped.items()
        if summary is None
    ] + [
        f"Fora da assinatura — documento {i}: {summary[:300]} ({reason})"
        for i, (summary, reason) in skipped.items()
        if summary is not None
    ]
    ready = [i for i in ids if i not in skipped]
    # Documents waiting for a new analysis attempt stay in the route; excluded ones leave it.
    route_file.write_text(json.dumps(ready + retry), encoding="utf-8")
    if not ready:
        raise Blocked(
            "Nenhum documento pronto para assinar. Nenhuma assinatura aplicada.\n\n"
            + "\n\n".join(notes)
        )
    if not all(workflow.get(i)["state"] == "prepared" for i in ready):
        return "\n\n".join([status(workflow, ready), *notes])
    reply = prepared_reply(workflow, ready, route_file.with_suffix(".html"), opener)
    return "\n\n".join([reply, *notes])


ORDINALS = {"primeira": 0, "segunda": 1, "terceira": 2, "ultima": -1}
SIGNATURE_CHOICE = re.compile(
    r"\b(?:(?:so|somente|apenas)\s+(?:a\s+)?(?P<o1>primeira|segunda|terceira|ultima)(?:\s+assinatura)?"
    r"|(?P<o2>primeira|segunda|terceira|ultima|\d)\s+assinatura"
    r"|assinatura\s+(?P<o3>\d)"
    r"|(?:so|somente|apenas)\s+(?:a\s+)?(?:da\s+)?pagina\s+(?P<page>\d+))\b"
)
DOCUMENT_REFERENCE = re.compile(
    r"\b(lei|portaria|decreto|oficio|documento)\s+(?:municipal\s+)?(?:n[o.]*\s*)?([0-9a-f]{8,32}|\d[\d.]*\d|\d)"
)


def signature_choices(workflow, ids, normalized):
    """'só a primeira assinatura da Lei 1.674' -> [(id, [indexes to keep])]; (choices, problem)."""
    picks = list(SIGNATURE_CHOICE.finditer(normalized))
    if not picks:
        return [], None
    plans = {
        i: workflow.checked_plan(workflow.get(i))
        for i in ids
        if workflow.get(i)["state"] in {"prepared", "approved"}
    }
    targets = []
    for kind, reference in DOCUMENT_REFERENCE.findall(normalized):
        if kind == "documento" and re.fullmatch(r"[0-9a-f]{8,32}", reference):
            found = [i for i in plans if i.startswith(reference)]
        else:
            number = r"\b" + r"\.?".join(reference.replace(".", "")) + r"\b"
            found = [
                i
                for i, plan in plans.items()
                if kind in fold(plan["summary"]) and re.search(number, plan["summary"])
            ]
            if len(found) > 1:
                # e.g. an ofício citing the law: the choice targets the document with several signatures.
                found = [i for i in found if len(plans[i]["placements"]) > 1]
        if len(found) != 1:
            return (
                [],
                f'Não achei um único documento preparado para "{kind} {reference}".',
            )
        targets.append(found[0])
    if not targets:
        multiple = [i for i, plan in plans.items() if len(plan["placements"]) > 1]
        if len(multiple) != 1:
            return [], 'Diga de qual documento é a assinatura (ex.: "da Lei 1.674").'
        targets = multiple
    if len(set(targets)) == 1:
        pairs = [(targets[0], pick) for pick in picks]
    elif len(targets) == len(picks):
        pairs = list(zip(targets, picks, strict=True))
    else:
        return [], "Não consegui ligar cada escolha de assinatura a um documento."
    keep = {}
    for request_id, pick in pairs:
        placements = plans[request_id]["placements"]
        if pick["page"]:
            index = next(
                (
                    n
                    for n, p in enumerate(placements)
                    if p["page"] + 1 == int(pick["page"])
                ),
                None,
            )
        else:
            word = pick["o1"] or pick["o2"] or pick["o3"]
            index = ORDINALS[word] if word in ORDINALS else int(word) - 1
            index = (
                index % len(placements)
                if index == -1 or 0 <= index < len(placements)
                else None
            )
        if index is None:
            return (
                [],
                f"O documento {request_id} tem {len(placements)} assinatura(s); essa escolha não existe.",
            )
        keep.setdefault(request_id, set()).add(index)
    return [(i, sorted(k)) for i, k in keep.items()], None


def log_failure(context, exc):
    """Local traceback for diagnosis (state/pdf-signing/controller-error.log); chat stays generic."""
    import time
    import traceback

    path = (
        Path(os.environ.get("HERMES_HOME", ROOT))
        / "state/pdf-signing/controller-error.log"
    )
    try:
        with path.open("a", encoding="utf-8") as log:
            log.write(
                f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {context}\n"
                + "".join(traceback.format_exception(exc))[-6000:]
            )
    except OSError:
        pass


def deliver_signed(workflow, ids, client_factory):
    pending = [i for i in ids if workflow.get(i)["state"] == "signed"]
    if pending:
        client = client_factory()
        for request_id in pending:
            workflow.deliver(request_id, client)


def retryable(workflow, request_id):
    """Signing failed before any signed output was saved: safe to preview and approve again."""
    row = workflow.get(request_id)
    return (
        row["state"] == "failed"
        and bool(row["plan"])
        and not (workflow.folder(request_id) / "signed.pdf").exists()
    )


def prepare_received(workflow, ids, analyzer, certificates):
    """Analyze and prepare pending PDFs; returns {id: (summary, reason)} for those left out."""
    model = os.environ.get("JARVIS_PDF_ANALYSIS_MODEL", "gemini-3.6-flash")
    skipped = {}
    for request_id in ids:
        row = workflow.get(request_id)
        if retryable(workflow, request_id):
            plan = json.loads(row["plan"])
            workflow.prepare(
                request_id,
                {
                    k: plan.get(k)
                    for k in ("summary", "title", "obligations", "placements")
                },
            )
            continue
        if row["state"] != "received":
            continue
        try:
            analysis = analyzer(workflow.folder(request_id), model)
        except Blocked as exc:
            # Stays "received" in the route: "continue" retries it; the rest of the batch proceeds.
            skipped[request_id] = (None, str(exc))
            continue
        candidates = analysis["signer_candidates"]
        if not candidates or "unknown" in candidates:
            reason = (
                "não pede assinatura"
                if not analysis["requires_signature"]
                else "assinante não identificado; esclareça antes"
            )
            skipped[request_id] = (analysis["summary"], reason)
            continue
        # The model's requires_signature flag is unstable on laws; the signer's line in the PDF decides,
        # and the human review still precedes any signature.
        try:
            found = placements(workflow.original(row), candidates, certificates())
        except Blocked as exc:
            reason = (
                "não pede assinatura"
                if not analysis["requires_signature"]
                else str(exc)
            )
            skipped[request_id] = (analysis["summary"], reason)
            continue
        proposal = {
            "summary": analysis["summary"],
            "title": analysis.get("title") or "",
            "obligations": analysis["obligations"],
            "placements": found,
        }
        workflow.prepare(request_id, proposal)
    return skipped


def prepared_reply(workflow, ids, report, opener):
    review_report(workflow, ids, report)
    if opener is not None:
        opener(str(report))
    lines = [
        "Estado: preparado, aguardando sua revisão e autorização. Nenhum PDF foi assinado ou enviado."
    ]
    lines.append("Resumo completo, certificados e prévias: " + str(report))
    for request_id in ids:
        plan = workflow.checked_plan(workflow.get(request_id))
        lines.append(f"Documento {workflow.label(request_id)}: {plan['summary']}")
        lines.extend("Obrigação: " + text for text in plan["obligations"])
        lines.append(
            "Assinante(s): " + ", ".join(p["signer"] for p in plan["placements"])
        )
        if len(plan["placements"]) > 1:
            lines.append(
                "Assinaturas neste documento: "
                + "; ".join(
                    f"{n}ª na página {p['page'] + 1} ({p['signer']})"
                    for n, p in enumerate(plan["placements"], 1)
                )
                + '. Para usar só uma, diga por exemplo "só a primeira assinatura" e o documento.'
            )
    return "\n\n".join(lines)


def status(workflow, ids):
    labels = {
        "received": "anexo recebido; análise/prévia ainda pendentes",
        "prepared": "preparado; aguardando revisão e autorização",
        "approved": "aprovado; ainda não assinado",
        "signing": "assinatura em execução; resultado não confirmado",
        "signed": "assinado criptograficamente e validado; ainda não enviado",
        "sending": "envio iniciado; entrega não confirmada, não repetir",
        "delivered": "assinado, validado e envio confirmado na conversa original",
        "failed": "falha; nenhuma conclusão de assinatura/envio",
    }
    return "\n".join(
        f"Documento {workflow.label(request_id)}: {labels[workflow.confirmed_state(request_id)]}"
        for request_id in ids
    )


def main():
    for name in ("pyhanko", "pyhanko_certvalidator"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    try:
        payload = json.load(sys.stdin)
        message = execute(
            payload["request"],
            payload["scope"],
            opener=getattr(os, "startfile", None),
            existing_requests=payload.get("existing_requests"),
            chat_authorized=payload.get("chat_authorized") is True,
        )
    except Blocked as exc:
        message = "Fluxo bloqueado: " + str(exc)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log_failure("controller", exc)
        message = "Falha no controlador. Assinatura e envio não confirmados; confira o estado antes de repetir."
    print(
        json.dumps(
            {"controller": "pdf-signing", "message": message}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
