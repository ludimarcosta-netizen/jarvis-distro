"""Persistent per-message workflow; approval is bound to immutable content."""

import json
import math
import re
import sqlite3
import time
import uuid
from functools import wraps
from itertools import pairwise
from pathlib import Path

from evolution import Blocked, document, media_hash
from pdf_engine import inspect, preview, real_providers, sha, sign_incrementally

SIGNERS = {"__JARVIS_USER_LOWER__", "beltrano"}
TITLE = re.compile(
    r"\b(Lei Municipal|Lei Complementar|Lei Ordinária|Lei|Portaria|Decreto|Ofício|Resolução|Memorando|Edital)"
    r"\s+n\.?\s*[º°o]?\.?\s*(\d[\d.]*)"
    r"(?:(/[A-Za-z]+)?/(\d{4})|,?\s+de\s+\d{1,2}º?\s+de\s+[a-zç]+\s+de\s+(\d{4}))"
    r"(\s*[–-]\s*[A-Z]{2,}\b)?",
    re.IGNORECASE,
)


def document_title(summary):
    """Title as the summary first states it ('Lei Municipal nº 1.674/2026'); '' if none."""
    match = TITLE.search(summary[:300])
    if not match:
        return ""
    kind, number, office, year, written_year, suffix = match.groups()
    kind = kind[0].upper() + kind[1:]
    return f"{kind} nº {number}{office or ''}/{year or written_year}{suffix or ''}"


def original_file_name(source):
    """File name the PDF had on WhatsApp ('' when the sender's app sent none)."""
    message = source.get("message") or {}
    doc = message.get("documentMessage") or (
        (message.get("documentWithCaptionMessage") or {}).get("message") or {}
    ).get("documentMessage", {})
    return Path(str(doc.get("fileName") or "")).name


def signed_file_name(plan, source):
    """Name sent back on WhatsApp: the original file name, else the document title ('/' -> '_')."""
    title = (
        Path(original_file_name(source)).stem
        or plan.get("title")
        or document_title(plan.get("summary", ""))
    )
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title).strip(" ._")
    return (safe[:150] or "documento-assinado") + ".pdf"


def exclusive(operation):
    """Cross-process exclusion; a crashed operation stays blocked for manual review."""

    @wraps(operation)
    def run(self, request_id, *args, **kwargs):
        lock = self.folder(request_id) / ".operation.lock"
        try:
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise Blocked(
                "Solicitação em execução ou interrompida; conferir antes de tentar novamente."
            ) from exc
        try:
            with handle:
                return operation(self, request_id, *args, **kwargs)
        finally:
            lock.unlink()

    return run


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Workflow:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "requests.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO schema_version VALUES(1);
            CREATE TABLE IF NOT EXISTS requests(
                id TEXT PRIMARY KEY, chat TEXT NOT NULL, message_id TEXT NOT NULL,
                source TEXT NOT NULL, pdf_hash TEXT NOT NULL, state TEXT NOT NULL,
                plan TEXT, plan_hash TEXT, approved_until REAL,
                output_hash TEXT, receipt TEXT,
                UNIQUE(chat, message_id)
            );
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, request TEXT NOT NULL REFERENCES requests(id),
                at REAL NOT NULL, action TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provenance(
                request TEXT PRIMARY KEY REFERENCES requests(id),
                transport TEXT NOT NULL, source_hash TEXT NOT NULL,
                media_hash TEXT NOT NULL, instance TEXT NOT NULL, downloaded_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS validations(
                request TEXT PRIMARY KEY REFERENCES requests(id), output_hash TEXT NOT NULL,
                report TEXT NOT NULL, validated_at REAL NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    def get(self, request_id):
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise Blocked("Identificador inválido.")
        row = self.db.execute(
            "SELECT * FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise Blocked("Solicitação não encontrada.")
        return dict(row)

    def label(self, request_id):
        """How the document is named to the user: its WhatsApp file name, else its id."""
        try:
            name = original_file_name(json.loads(self.get(request_id)["source"]))
        except (ValueError, TypeError):
            name = ""
        return f'"{name}"' if name else request_id

    def folder(self, request_id):
        self.get(request_id)
        return self.root / request_id

    def audit(self, request_id, action):
        self.db.execute(
            "INSERT INTO events(request, at, action) VALUES(?,?,?)",
            (request_id, time.time(), action),
        )

    def ingest(self, client, chat, message_id):
        existing = self.db.execute(
            "SELECT id FROM requests WHERE chat=? AND message_id=?", (chat, message_id)
        ).fetchone()
        if existing:
            return existing["id"]
        if not re.fullmatch(r"[0-9A-Za-z_.:@-]{3,180}", chat) or not re.fullmatch(
            r"[0-9A-Za-z_-]{1,180}", message_id
        ):
            raise Blocked("Conversa ou mensagem inválida.")
        message = client.message(chat, message_id)
        document(message)
        key = message.get("key", {})
        if key.get("remoteJid") != chat or key.get("id") != message_id:
            raise Blocked("Origem retornada não corresponde à mensagem solicitada.")
        raw = client.download(message)
        expected_hash = media_hash(message)
        if sha(raw) != expected_hash:
            raise Blocked("Conteúdo não corresponde ao hash do anexo WhatsApp.")
        pages = inspect(raw)
        request_id = uuid.uuid4().hex
        folder = self.root / request_id
        folder.mkdir()
        (folder / "original.pdf").write_bytes(raw)
        # Store only the selected message, not the conversation/history.
        with self.db:
            self.db.execute(
                "INSERT INTO requests(id,chat,message_id,source,pdf_hash,state) VALUES(?,?,?,?,?,?)",
                (
                    request_id,
                    chat,
                    message_id,
                    canonical(message),
                    sha(raw),
                    "received",
                ),
            )
            self.audit(request_id, "received")
            self.db.execute(
                "INSERT INTO provenance VALUES(?,?,?,?,?,?)",
                (
                    request_id,
                    "whatsapp-evolution",
                    sha(canonical(message).encode()),
                    expected_hash,
                    str(client.instance),
                    time.time(),
                ),
            )
        (folder / "extracted.json").write_text(canonical(pages), encoding="utf-8")
        body = message.get("message", {})
        context = {"caption": document(message).get("caption", "")[:8000]}
        if isinstance(body.get("extendedTextMessage"), dict):
            context["text"] = body["extendedTextMessage"].get("text", "")[:8000]
        (folder / "context.json").write_text(canonical(context), encoding="utf-8")
        return request_id

    def original(self, row):
        provenance = self.db.execute(
            "SELECT * FROM provenance WHERE request=?", (row["id"],)
        ).fetchone()
        if (
            provenance is None
            or provenance["transport"] != "whatsapp-evolution"
            or provenance["source_hash"] != sha(row["source"].encode())
            or provenance["media_hash"] != row["pdf_hash"]
        ):
            raise Blocked("Proveniência do anexo WhatsApp não comprovada.")
        raw = (self.root / row["id"] / "original.pdf").read_bytes()
        if sha(raw) != row["pdf_hash"]:
            raise Blocked("PDF alterado desde o recebimento.")
        return raw

    @exclusive
    def prepare(self, request_id, proposal):
        row = self.get(request_id)
        # A failed signing that saved no output may be prepared again (new preview + new approval).
        retry = (
            row["state"] == "failed"
            and not (self.folder(request_id) / "signed.pdf").exists()
        )
        if row["state"] not in {"received", "prepared", "approved"} and not retry:
            raise Blocked("Solicitação já consumida ou em execução.")
        raw = self.original(row)
        pages = inspect(raw)
        summary = proposal.get("summary")
        obligations = proposal.get("obligations")
        placements = proposal.get("placements")
        if not isinstance(summary, str) or not 1 <= len(summary) <= 12000:
            raise Blocked("Resumo obrigatório (até 12000 caracteres).")
        title = proposal.get("title") or document_title(summary)
        if not isinstance(title, str) or len(title) > 200:
            raise Blocked("Título inválido.")
        if (
            not isinstance(obligations, list)
            or len(obligations) > 100
            or any(not isinstance(x, str) or len(x) > 2000 for x in obligations)
        ):
            raise Blocked("Obrigações inválidas.")
        if not isinstance(placements, list) or not 1 <= len(placements) <= 2:
            raise Blocked("Indique um ou dois assinantes sem ambiguidade.")
        seen = set()
        cleaned = []
        for placement in placements:
            signer = placement.get("signer")
            page = placement.get("page")
            box = placement.get("box")
            cert = placement.get("certificate")
            # One signer may sign several pages (law + annex); never twice on the same page.
            if signer not in SIGNERS or (signer, page) in seen:
                raise Blocked("Assinante inválido ou repetido.")
            seen.add((signer, page))
            if type(page) is not int or not 0 <= page < len(pages):
                raise Blocked("Página inválida.")
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
            ):
                raise Blocked("Área inválida.")
            x0, y0, x1, y1 = box
            if (
                not 0 <= x0 < x1 <= pages[page]["width"]
                or not 0 <= y0 < y1 <= pages[page]["height"]
                or x1 - x0 < 100
                or y1 - y0 < 30
            ):
                raise Blocked("Área fora da página ou pequena demais.")
            for existing_box in pages[page]["signature_boxes"]:
                if max(x0, existing_box[0]) < min(x1, existing_box[2]) and max(
                    y0, existing_box[1]
                ) < min(y1, existing_box[3]):
                    raise Blocked("Área sobrepõe campo de assinatura existente.")
            if not isinstance(cert, str) or not re.fullmatch(r"[a-f0-9]{64}", cert):
                raise Blocked(
                    "SHA-256 do certificado público obrigatório; seleção por nome não basta."
                )
            cleaned.append(
                {"signer": signer, "page": page, "box": box, "certificate": cert}
            )
        for first, second in pairwise(cleaned):
            a, b = first["box"], second["box"]
            if (
                first["page"] == second["page"]
                and max(a[0], b[0]) < min(a[2], b[2])
                and max(a[1], b[1]) < min(a[3], b[3])
            ):
                raise Blocked("Áreas de assinatura sobrepostas.")
        paths = preview(raw, cleaned, self.folder(request_id))
        plan = {
            "request": request_id,
            "pdf_hash": row["pdf_hash"],
            "chat": row["chat"],
            "message_id": row["message_id"],
            "summary": summary,
            "title": title,
            "obligations": obligations,
            "placements": cleaned,
            "return_to_original_chat": True,
            "previews": {p.name: sha(p.read_bytes()) for p in paths},
        }
        encoded = canonical(plan)
        with self.db:
            self.db.execute(
                "UPDATE requests SET state='prepared',plan=?,plan_hash=?,approved_until=NULL WHERE id=?",
                (encoded, sha(encoded.encode()), request_id),
            )
            self.audit(request_id, "prepared")
        return plan

    def checked_plan(self, row):
        self.original(row)
        if not row["plan"] or sha(row["plan"].encode()) != row["plan_hash"]:
            raise Blocked("Plano ausente ou alterado.")
        plan = json.loads(row["plan"])
        for name, digest in plan["previews"].items():
            if (
                Path(name).name != name
                or sha((self.folder(row["id"]) / name).read_bytes()) != digest
            ):
                raise Blocked("Prévia alterada.")
        return plan

    @exclusive
    def approve(self, request_id, expected_hash):
        """Internal only: CLI calls this exclusively after a human GUI confirmation."""
        row = self.get(request_id)
        self.checked_plan(row)
        if row["state"] != "prepared" or expected_hash != row["plan_hash"]:
            raise Blocked("Aprovação não corresponde à prévia atual.")
        with self.db:
            cursor = self.db.execute(
                "UPDATE requests SET state='approved',approved_until=? WHERE id=? AND state='prepared' AND plan_hash=?",
                (time.time() + 900, request_id, expected_hash),
            )
            if cursor.rowcount != 1:
                raise Blocked("Estado alterado durante aprovação.")
            self.audit(request_id, "human-approved")

    @exclusive
    def sign(self, request_id, providers=None, roots=None, crls=None):
        row = self.get(request_id)
        plan = self.checked_plan(row)
        if row["state"] != "approved" or (row["approved_until"] or 0) < time.time():
            raise Blocked("Aprovação explícita ausente, consumida ou expirada.")
        if providers is None:
            providers, roots, crls = real_providers(
                plan["placements"], self.root / "validation"
            )
        with self.db:
            cursor = self.db.execute(
                "UPDATE requests SET state='signing' WHERE id=? AND state='approved' AND approved_until>=?",
                (request_id, time.time()),
            )
            if cursor.rowcount != 1:
                raise Blocked("Aprovação já consumida.")
            self.audit(request_id, "signing")
        try:
            raw, validation = sign_incrementally(
                self.original(row), plan["placements"], providers, roots, crls
            )
            (self.folder(request_id) / "signed.pdf").write_bytes(raw)
            with self.db:
                self.db.execute(
                    "UPDATE requests SET state='signed',output_hash=? WHERE id=?",
                    (sha(raw), request_id),
                )
                self.audit(request_id, "validated")
                self.db.execute(
                    "INSERT INTO validations VALUES(?,?,?,?)",
                    (request_id, sha(raw), canonical(validation), time.time()),
                )
            return validation
        except Exception:
            with self.db:
                self.db.execute(
                    "UPDATE requests SET state='failed' WHERE id=?", (request_id,)
                )
                self.audit(request_id, "signing-failed")
            raise

    @exclusive
    def deliver(self, request_id, client):
        row = self.get(request_id)
        self.checked_plan(row)
        self.confirmed_state(request_id)
        if row["state"] != "signed" or (row["approved_until"] or 0) < time.time():
            raise Blocked("Entrega exige assinatura validada e autorização vigente.")
        raw = (self.folder(request_id) / "signed.pdf").read_bytes()
        if sha(raw) != row["output_hash"]:
            raise Blocked("PDF assinado alterado.")
        with self.db:
            cursor = self.db.execute(
                "UPDATE requests SET state='sending' WHERE id=? AND state='signed'",
                (request_id,),
            )
            if cursor.rowcount != 1:
                raise Blocked("Entrega já iniciada.")
            self.audit(request_id, "sending")
        # On a timeout keep 'sending': do not risk a duplicate delivery.
        source = json.loads(row["source"])
        name = signed_file_name(self.checked_plan(row), source)
        result = client.deliver(row["chat"], source, raw, name)
        receipt = result.get("key", {})
        if not receipt.get("id") or receipt.get("remoteJid") != row["chat"]:
            raise Blocked(
                "Entrega sem confirmação da conversa; conferir manualmente, não repetir."
            )
        with self.db:
            self.db.execute(
                "UPDATE requests SET state='delivered',receipt=? WHERE id=?",
                (canonical(receipt), request_id),
            )
            self.audit(request_id, "delivered")
        return {"request": request_id, "state": "delivered"}

    def confirmed_state(self, request_id):
        row = self.get(request_id)
        self.original(row)
        if row["plan"]:
            self.checked_plan(row)
        if row["state"] in {"signed", "sending", "delivered"}:
            proof = self.db.execute(
                "SELECT * FROM validations WHERE request=?", (request_id,)
            ).fetchone()
            if proof is None:
                raise Blocked(
                    "Estado sem relatório de validação criptográfica; conclusão bloqueada."
                )
            raw = (self.folder(request_id) / "signed.pdf").read_bytes()
            if (
                proof is None
                or proof["output_hash"] != row["output_hash"]
                or sha(raw) != row["output_hash"]
            ):
                raise Blocked(
                    "Estado sem resultado criptográfico verificável; conclusão bloqueada."
                )
            if not json.loads(proof["report"]) or not all(
                item.get("valid") is True for item in json.loads(proof["report"])
            ):
                raise Blocked("Relatório criptográfico inválido.")
        if row["state"] == "delivered":
            receipt = json.loads(row["receipt"] or "{}")
            if not receipt.get("id") or receipt.get("remoteJid") != row["chat"]:
                raise Blocked(
                    "Estado sem recibo da conversa original; entrega não comprovada."
                )
        return row["state"]
