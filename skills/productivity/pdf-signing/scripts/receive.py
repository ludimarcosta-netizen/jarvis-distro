"""Opt-in webhook for new events; never polls history or marks read."""

import hmac
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from evolution import Blocked, document


def receive(event, workflow, client, started_at):
    if event.get("event", "").replace("_", ".").lower() != "messages.upsert":
        return {"action": "ignored"}
    data = event.get("data", {})
    if not isinstance(data, dict) or data.get("key", {}).get("fromMe") is not False:
        return {"action": "ignored"}
    timestamp = data.get("messageTimestamp")
    if (
        type(timestamp) not in (int, float)
        or not started_at <= timestamp <= time.time() + 30
    ):
        return {"action": "ignored-old-event"}
    key = data.get("key", {})
    chat, message_id = key.get("remoteJid"), key.get("id")
    if not chat or not message_id or chat == "status@broadcast":
        return {"action": "ignored"}
    try:
        document(data)
    except Blocked:
        body = data.get("message", {})
        extended = body.get("extendedTextMessage", {})
        text = extended.get("text", body.get("conversation", ""))
        if not isinstance(text, str) or not any(
            word in text.casefold() for word in ("assin", "sign")
        ):
            return {"action": "ignored"}
        quoted = extended.get("contextInfo", {}).get("stanzaId")
        row = workflow.db.execute(
            "SELECT id FROM requests WHERE chat=? AND message_id=?", (chat, quoted)
        ).fetchone()
        if row:
            folder = workflow.folder(row["id"])
            context_path = folder / "context.json"
            context = json.loads(context_path.read_text(encoding="utf-8"))
            context["signature_request"] = text[:8000]
            context_path.write_text(
                json.dumps(context, ensure_ascii=False), encoding="utf-8"
            )
            return {
                "action": "signature-request",
                "request": row["id"],
                "automatic_signature": False,
            }
        return {
            "action": "clarification-required",
            "reason": "Indicar PDF específico; nenhuma seleção por proximidade.",
        }
    request_id = workflow.ingest(client, chat, message_id)
    return {
        "action": "pdf-pending-analysis",
        "request": request_id,
        "automatic_signature": False,
        "notice": "PDF recebido; analisar texto e preparar prévia antes de pedir autorização.",
    }


def serve(workflow, client, config):
    if (
        config.get("enabled") is not True
        or config.get("phone_unread_notifications_verified") is not True
    ):
        raise Blocked(
            "Monitor desativado: comprovar primeiro recibos, não lidas e notificações no celular."
        )
    token = os.environ.get("PDF_SIGNING_WEBHOOK_TOKEN", "")
    if len(token) < 32:
        raise Blocked(
            "Defina segredo local PDF_SIGNING_WEBHOOK_TOKEN com pelo menos 32 caracteres."
        )
    client.unread_guard()
    started_at = time.time()
    jobs = queue.Queue(maxsize=16)

    def triage_worker():
        from analyze import analyze
        from workflow import Workflow

        local = Workflow(workflow.root)
        try:
            while True:
                request_id = jobs.get()
                if request_id is None:
                    return
                try:
                    if config.get("analysis_model"):
                        result = analyze(
                            local.folder(request_id), config["analysis_model"]
                        )
                        notice = {
                            "action": "analysis-ready",
                            "request": request_id,
                            "requires_signature": result["requires_signature"],
                        }
                    else:
                        notice = {
                            "action": "analysis-configuration-required",
                            "request": request_id,
                        }
                    print(json.dumps(notice), flush=True)
                    if config.get("windows_notice") is True:
                        import sys
                        from pathlib import Path

                        sys.path.insert(
                            0, str(Path(__file__).resolve().parents[4] / "jarvis")
                        )
                        from notifier import notify

                        notify(
                            "Jarvis: documento pendente",
                            "PDF recebido. Revise a análise antes de autorizar qualquer assinatura.",
                            local.folder(request_id).as_uri(),
                        )
                except (ValueError, OSError, KeyError, TypeError):
                    print(
                        json.dumps(
                            {"action": "analysis-failed", "request": request_id}
                        ),
                        flush=True,
                    )
                finally:
                    jobs.task_done()
        finally:
            local.close()

    worker = threading.Thread(target=triage_worker, daemon=True)
    worker.start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return  # Do not log message contents, headers or identities.

        def do_POST(self):
            if self.path != "/events" or not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise Blocked("Evento grande demais.")
                event = json.loads(self.rfile.read(length))
                if not isinstance(event, dict):
                    raise Blocked("Evento inválido.")
                if event.get("instance") != client.instance:
                    raise Blocked("Instância divergente.")
                result = receive(event, workflow, client, started_at)
                if result.get("request") and result.get("action") in {
                    "pdf-pending-analysis",
                    "signature-request",
                }:
                    try:
                        jobs.put_nowait(result["request"])
                    except queue.Full:
                        result["analysis_queue_full"] = True
                payload = json.dumps(result).encode()
                # Local stdout is the pending-work signal; never send an unsolicited WhatsApp message.
                print(json.dumps(result, ensure_ascii=False), flush=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError, KeyError, TypeError, OSError):
                self.send_error(422, "Evento bloqueado; verifique configuração local.")

    server = HTTPServer(("127.0.0.1", 8091), Handler)
    server.timeout = 2
    try:
        while True:
            server.handle_request()
    finally:
        server.server_close()
