"""Narrow Evolution client: no history scans, read receipts or arbitrary sends."""

import base64
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

MAX_PDF = 25 * 1024 * 1024


class Blocked(ValueError):
    pass


class NeedsChoice(Blocked):
    """Contact only partially identified; the user picks one of the PDF senders."""

    def __init__(self, contact, options):
        self.contact = contact
        self.options = options  # [{"name", "chat", "documents"}]
        super().__init__("Escolha a conversa: " + "; ".join(o["name"] for o in options))


STOPWORDS = {"da", "de", "do", "das", "dos", "e", "a", "o"}


def tokens(text):
    folded = "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )
    return [t for t in re.findall(r"\w+", folded) if len(t) > 1 and t not in STOPWORDS]


class Evolution:
    def __init__(self):
        path = Path(__file__).resolve().parents[2] / "whatsapp-evolution" / "wa.py"
        spec = importlib.util.spec_from_file_location("existing_whatsapp", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._directory = module
        self.url = module.URL
        if urlparse(self.url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise Blocked("Evolution deve permanecer local.")
        self.instance = quote(module.INSTANCE, safe="")
        self.key = module._key()

    def call(self, route, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(
            self.url + route + "/" + self.instance,
            data=payload,
            headers={"apikey": self.key, "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read(MAX_PDF * 2 + 1)
            if len(raw) > MAX_PDF * 2:
                raise Blocked("Resposta da API excede limite.")
            return json.loads(raw)
        except (HTTPError, URLError) as exc:
            raise Blocked(
                "Evolution inacessível ou requisição rejeitada; sem repetição automática."
            ) from exc

    def unread_guard(self):
        response = self.call("/settings/find")
        settings = response.get("settings", response)
        for camel, snake in (
            ("readMessages", "read_messages"),
            ("readStatus", "read_status"),
        ):
            value = settings.get(camel, settings.get(snake))
            if value is not False:
                raise Blocked(
                    "Leitura automática ativa ou configuração desconhecida; operação bloqueada."
                )

    def message(self, chat, message_id):
        self.unread_guard()
        data = self.call(
            "/chat/findMessages",
            {
                "where": {"key": {"remoteJid": chat, "id": message_id}},
                "page": 1,
                "offset": 2,
            },
        )
        records = data.get("messages", {}).get("records", [])
        matches = [
            r
            for r in records
            if r.get("key", {}).get("id") == message_id
            and r.get("key", {}).get("remoteJid") == chat
        ]
        if len(matches) != 1:
            raise Blocked("Mensagem específica ausente ou ambígua.")
        return matches[0]

    def select_documents(self, contact, count):
        self.unread_guard()
        directory = self._directory.names()
        proven_chats = []
        home = Path(os.environ.get("HERMES_HOME", Path(__file__).resolve().parents[4]))
        database = home / "state/pdf-signing/requests.sqlite3"
        if database.is_file():
            with closing(
                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                for chat, source, source_hash in db.execute(
                    "SELECT r.chat,r.source,p.source_hash FROM requests r JOIN provenance p ON p.request=r.id WHERE p.transport='whatsapp-evolution'"
                ):
                    if hashlib.sha256(source.encode()).hexdigest() != source_hash:
                        continue
                    message = json.loads(source)
                    if (
                        message.get("key", {}).get("remoteJid") == chat
                        and message.get("key", {}).get("fromMe") is False
                    ):
                        name = message.get("pushName")
                        if (
                            isinstance(name, str)
                            and name.strip()
                            and not directory.get(chat)
                        ):
                            directory[chat] = name
                        if isinstance(name, str) and (
                            name.casefold() == contact.casefold()
                            or name.casefold().startswith(contact.casefold() + " ")
                        ):
                            proven_chats.append(chat)
        chats = self._directory.resolve(contact, directory)
        chats = list(chats) + proven_chats
        chats = list(dict.fromkeys(chats))
        candidates = []
        if 1 <= len(chats) <= 3:
            batches = [self._recent_documents(chat) for chat in chats]
            candidates = [batch for batch in batches if batch]
        self.chosen = None
        if len(candidates) == 1:
            selected = candidates[0]
            self.chosen = directory.get(selected[0][0])
        elif candidates:
            raise NeedsChoice(
                contact,
                [
                    {
                        "name": directory.get(batch[0][0]) or batch[0][0],
                        "chat": batch[0][0],
                        "documents": len(batch),
                    }
                    for batch in candidates
                ],
            )
        else:
            # Names in the API are profile names, not the saved ones: match among recent PDF senders only.
            chat, name = self._match_sender(contact, directory)
            selected = self._recent_documents(chat)
            self.chosen = name
        return self._check_count(selected, count)

    def remember(self, contact, chat):
        self._directory.learn_alias(contact, chat)

    def chat_labels(self, chats):
        """Names the user may call these chats by: agenda/profile name and confirmed aliases."""
        directory = self._directory.names()
        labels = [directory.get(chat, "") for chat in chats]
        labels += [
            alias for alias, jid in self._directory.load_aliases().items() if jid in chats
        ]
        return [label for label in labels if label]

    def select_chat(self, chat, count):
        """Documents of a conversation the user picked from a numbered list."""
        self.unread_guard()
        self.chosen = None
        return self._check_count(self._recent_documents(chat), count)

    def _match_sender(self, contact, directory):
        wanted = tokens(contact)
        senders = self._recent_pdf_senders(directory)
        if not wanted or not senders:
            raise Blocked(
                "Não encontrei PDFs recebidos desse contato nas últimas 24 horas. Informe quando chegaram ou encaminhe as mensagens específicas."
            )
        full, partial = [], []
        for chat, sender in senders.items():
            words = {t for name in sender["names"] for t in tokens(name)}
            if all(t in words for t in wanted):
                full.append(chat)
            elif any(t in words for t in wanted):
                partial.append(chat)
        if len(full) == 1:
            return full[0], senders[full[0]]["display"]
        options = full or partial or list(senders)
        raise NeedsChoice(
            contact,
            [
                {
                    "name": senders[chat]["display"],
                    "chat": chat,
                    "documents": senders[chat]["documents"],
                }
                for chat in options[:9]
            ],
        )

    def senders_named_in(self, text):
        """Words of free text naming a recent PDF sender ("o último que a Sicrana mandou" -> ["sicrana"])."""
        said = tokens(text)
        senders = self._recent_pdf_senders(self._directory.names())
        named = []
        for sender in senders.values():
            words = {t for name in sender["names"] for t in tokens(name) if len(t) > 2}
            hit = [t for t in said if t in words]
            if hit:
                named.append(" ".join(dict.fromkeys(hit)))
        return list(dict.fromkeys(named))

    def _recent_pdf_senders(self, directory):
        """Direct chats that sent a PDF in the last 24 h, with every known name for each."""
        floor = (datetime.now(UTC) - timedelta(hours=24)).timestamp()
        senders = {}
        for page in (1, 2, 3):
            data = self.call(
                "/chat/findMessages",
                {
                    "where": {
                        "messageType": "documentMessage",
                        "key": {"fromMe": False},
                    },
                    "page": page,
                    "offset": 100,
                },
            )
            messages = data.get("messages", {})
            records = messages.get("records", [])
            older = False
            for record in records:
                key = record.get("key", {})
                chat = key.get("remoteJid", "")
                try:
                    timestamp = float(record.get("messageTimestamp"))
                except (TypeError, ValueError):
                    continue
                if timestamp < floor:
                    older = True
                    continue
                if key.get("fromMe") is not False or chat.endswith("@g.us"):
                    continue
                try:
                    document(record)
                except Blocked:
                    continue
                entry = senders.setdefault(chat, {"names": set(), "documents": 0})
                entry["documents"] += 1
                saved = self._directory.saved_name(chat, key.get("remoteJidAlt") or "")
                for name in (saved, directory.get(chat), record.get("pushName")):
                    if isinstance(name, str) and name.strip():
                        entry["names"].add(name.strip())
                entry.setdefault(
                    "display",
                    saved or directory.get(chat) or record.get("pushName") or chat,
                )
            if older or page >= messages.get("pages", 1):
                break
        for entry in senders.values():
            others = sorted(n for n in entry["names"] if n != entry["display"])
            if others:
                entry["display"] += f" ({others[0]})"
        return senders

    def _check_count(self, selected, count):
        if (count is not None and len(selected) != count) or (
            count is None and not 1 <= len(selected) <= 10
        ):
            raise Blocked(
                "Quantidade de PDFs não corresponde ao pedido; indique as mensagens exatas. Nenhum arquivo local será usado."
            )
        return selected

    def _recent_documents(self, chat):
        now = datetime.now(UTC)
        data = self.call(
            "/chat/findMessages",
            {
                "where": {
                    "key": {"remoteJid": chat, "fromMe": False},
                },
                "page": 1,
                "offset": 200,
            },
        )
        messages = data.get("messages", {})
        pages = messages.get("pages", 1)
        if pages > 3:
            raise Blocked("Seleção ampla demais; indique as mensagens específicas.")
        records = list(messages.get("records", []))
        for page in range(2, pages + 1):
            batch = self.call(
                "/chat/findMessages",
                {
                    "where": {"key": {"remoteJid": chat, "fromMe": False}},
                    "page": page,
                    "offset": 200,
                },
            )
            records.extend(batch.get("messages", {}).get("records", []))
        selected = []
        for record in records:
            key = record.get("key", {})
            if key.get("remoteJid") != chat or key.get("fromMe") is not False:
                continue
            timestamp = record.get("messageTimestamp")
            try:
                timestamp = float(timestamp)
            except (ValueError, TypeError):
                continue
            if (
                not (now - timedelta(hours=24)).timestamp()
                <= timestamp
                <= now.timestamp() + 30
            ):
                continue
            try:
                document(record)
            except Blocked:
                continue
            selected.append((timestamp, chat, key["id"]))
        # Oldest first, so "o último documento" is the last one.
        return list(dict.fromkeys((c, i) for _, c, i in sorted(selected)))

    def download(self, message):
        self.unread_guard()
        data = self.call(
            "/chat/getBase64FromMediaMessage",
            {"message": message, "convertToMp4": False},
        )
        encoded = data.get("base64", "")
        if not isinstance(encoded, str) or len(encoded) > MAX_PDF * 4 // 3 + 1024:
            raise Blocked("Anexo ausente ou grande demais.")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise Blocked("Mídia inválida.") from exc
        if len(raw) > MAX_PDF or not raw.startswith(b"%PDF-"):
            raise Blocked("Anexo não é PDF válido ou excede 25 MiB.")
        if media_hash(message) != hashlib.sha256(raw).hexdigest():
            raise Blocked("Hash da mídia difere do anexo original do WhatsApp.")
        return raw

    def deliver(self, chat, original, raw, file_name="documento-assinado.pdf"):
        self.unread_guard()
        return self.call(
            "/message/sendMedia",
            {
                "number": chat,
                "mediatype": "document",
                "mimetype": "application/pdf",
                "fileName": file_name,
                "media": base64.b64encode(raw).decode(),
                "quoted": {"key": original["key"], "message": original["message"]},
            },
        )


def document(message):
    body = message.get("message") or {}
    if "documentWithCaptionMessage" in body:
        body = body["documentWithCaptionMessage"].get("message", {})
    doc = body.get("documentMessage")
    if not isinstance(doc, dict) or doc.get("mimetype") != "application/pdf":
        raise Blocked("Selecione uma mensagem contendo exatamente um PDF.")
    return doc


def media_hash(message):
    encoded = document(message).get("fileSha256")
    if isinstance(encoded, dict):
        encoded = bytes(encoded.get("data", []))
    if isinstance(encoded, list):
        encoded = bytes(encoded)
    try:
        digest = (
            base64.b64decode(encoded, validate=True)
            if isinstance(encoded, str)
            else encoded
        )
    except ValueError as exc:
        raise Blocked("Hash de origem do WhatsApp inválido.") from exc
    if not isinstance(digest, bytes) or len(digest) != 32:
        raise Blocked(
            "Mídia sem SHA-256 verificável do WhatsApp; origem não comprovada."
        )
    return digest.hex()
