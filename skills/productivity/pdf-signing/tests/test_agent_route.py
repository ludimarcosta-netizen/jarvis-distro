"""Regression of the real Desktop/voice entry points and the local-file incident."""

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(ROOT / "jarvis"))

import agent_route
import hermes_bridge
import pdf_agent_gate
import pdf_bridge
from evolution import Blocked, NeedsChoice
from pdf_engine import certificate_id
from test_workflow import synthetic_signer
from workflow import Workflow


def document_for(name):
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(600, 800))
    pdf.drawString(50, 700, "ATTACHMENT: perform within five days.")
    pdf.drawCentredString(300, 138, name)
    pdf.save()
    return buffer.getvalue()


class RealAgentRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "incident_plugin_api",
            ROOT / "plugins/gemini-live-bridge/dashboard/plugin_api.py",
        )
        cls.plugin = importlib.util.module_from_spec(spec)
        with patch("threading.Thread.start"):
            spec.loader.exec_module(cls.plugin)
        cls.certs = [
            {
                "alias": name,
                "fingerprint": certificate_id(synthetic_signer(name).signing_cert),
                "has_private_key": True,
                "valid_now": True,
            }
            for name in ("__JARVIS_USER_LOWER__", "beltrano")
        ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state/pdf-signing"
        local = self.root / "output/pdf/minutas-assinatura"
        local.mkdir(parents=True)
        self.local = local / "minuta_decreto_celulares_beltrano.pdf"
        self.local.write_bytes(b"LOCAL FILE MUST NOT BE USED")
        self.local_hash = hashlib.sha256(self.local.read_bytes()).hexdigest()
        self.client = Mock(instance="synthetic")
        self.client.select_documents.return_value = [
            ("123@s.whatsapp.net", "ONE"),
            ("123@s.whatsapp.net", "TWO"),
        ]
        self.raw = {"ONE": document_for("__JARVIS_USER__"), "TWO": document_for("Beltrano")}
        self.messages = {
            identifier: {
                "key": {
                    "id": identifier,
                    "remoteJid": "123@s.whatsapp.net",
                    "fromMe": False,
                },
                "message": {
                    "documentMessage": {
                        "mimetype": "application/pdf",
                        "fileName": self.local.name,
                        "fileSha256": base64.b64encode(
                            hashlib.sha256(raw).digest()
                        ).decode(),
                    }
                },
            }
            for identifier, raw in self.raw.items()
        }
        self.client.message.side_effect = lambda _chat, identifier: self.messages[
            identifier
        ]
        self.client.download.side_effect = lambda message: self.raw[
            message["key"]["id"]
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def analyze(self, folder, _model):
        pages = json.loads((folder / "extracted.json").read_text(encoding="utf-8"))
        name = "__JARVIS_USER_LOWER__" if "__JARVIS_USER__" in pages[0]["text"] else "beltrano"
        return {
            "requires_signature": True,
            "summary": "Resumo do anexo: providência em cinco dias.",
            "obligations": ["Responsável deve cumprir prazo de cinco dias."],
            "signer_candidates": [name],
        }

    def child(self, argv, **kwargs):
        self.assertEqual(Path(argv[2]), pdf_bridge.CONTROLLER)
        payload = json.loads(kwargs["input"])
        result = agent_route.execute(
            payload["request"],
            payload["scope"],
            state_dir=self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        return Mock(
            returncode=0,
            stdout=json.dumps(
                {"controller": "pdf-signing", "message": result}
            ).encode(),
        )

    def test_full_name_signature_line_is_placed(self):
        raw = document_for("BELTRANO SEIS CAMARGO SETE")
        result = agent_route.placements(raw, ["beltrano"], self.certs)
        self.assertEqual(result[0]["signer"], "beltrano")
        self.assertEqual(result[0]["page"], 0)

    def test_suggested_signer_without_signature_line_is_left_out(self):
        raw = document_for("BELTRANO SEIS CAMARGO SETE")
        result = agent_route.placements(raw, ["beltrano", "__JARVIS_USER_LOWER__"], self.certs)
        self.assertEqual([p["signer"] for p in result], ["beltrano"])
        with self.assertRaises(Blocked):
            agent_route.placements(raw, ["__JARVIS_USER_LOWER__"], self.certs)

    def test_stamp_fits_free_gap_without_covering_text(self):
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(600, 800))
        pdf.drawString(50, 200, "Estado de Goiás, aos 15 dias do mês de outubro.")
        pdf.drawCentredString(300, 130, "BELTRANO SEIS CAMARGO SETE")
        pdf.save()
        box = agent_route.placements(buffer.getvalue(), ["beltrano"], self.certs)[0][
            "box"
        ]
        # Bottom-up coords: stamp stays above the name (baseline 130) and below the text at 200.
        self.assertGreater(box[1], 130)
        self.assertLess(box[3], 200)
        self.assertGreaterEqual(box[3] - box[1], 30)

    def test_no_free_gap_above_name_blocks(self):
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(600, 800))
        pdf.drawString(50, 150, "Estado de Goiás, aos 15 dias do mês de outubro.")
        pdf.drawCentredString(300, 130, "BELTRANO SEIS CAMARGO SETE")
        pdf.save()
        with self.assertRaises(Blocked):
            agent_route.placements(buffer.getvalue(), ["beltrano"], self.certs)

    def test_signature_high_on_short_page_and_on_two_pages(self):
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(600, 800))
        pdf.drawString(
            50, 700, "Art. 3º Esta lei entra em vigor na data de sua publicação."
        )
        pdf.drawCentredString(300, 560, "BELTRANO SEIS CAMARGO SETE")
        pdf.showPage()
        pdf.drawString(50, 300, "Carta-Compromisso Institucional.")
        pdf.drawCentredString(300, 150, "Beltrano Seis Camargo Sete")
        pdf.drawString(50, 100, "Beltrano Seis, Prefeito, assina abaixo.")
        pdf.save()
        result = agent_route.placements(buffer.getvalue(), ["beltrano"], self.certs)
        self.assertEqual([p["page"] for p in result], [0, 1])
        self.raw["ONE"] = buffer.getvalue()
        self.messages["ONE"]["message"]["documentMessage"]["fileSha256"] = (
            base64.b64encode(hashlib.sha256(self.raw["ONE"]).digest()).decode()
        )
        workflow = Workflow(self.state)
        try:
            request = workflow.ingest(self.client, "123@s.whatsapp.net", "ONE")
            plan = {"summary": "Lei.", "obligations": [], "placements": result}
            # Same signer on two pages is a valid plan (law + annex).
            workflow.prepare(request, plan)
        finally:
            workflow.close()

    def test_repeated_full_name_lines_stay_blocked(self):
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(600, 800))
        pdf.drawCentredString(300, 200, "BELTRANO SEIS CAMARGO SETE")
        pdf.drawCentredString(300, 100, "Beltrano Sete")
        pdf.save()
        with self.assertRaises(Blocked):
            agent_route.placements(buffer.getvalue(), ["beltrano"], self.certs)

    def test_natural_sender_phrase_prepares_recent_pdfs_without_signing(self):
        request = "Jarvis, a Lia me mandou alguns documentos no whats para assinar"
        with (
            patch.object(Workflow, "sign", side_effect=AssertionError("No approval")),
            patch.object(
                Workflow, "deliver", side_effect=AssertionError("No delivery")
            ),
        ):
            reply = agent_route.execute(
                request,
                "natural-sender",
                state_dir=self.state,
                client_factory=lambda: self.client,
                analyzer=self.analyze,
                certificates=lambda: self.certs,
            )
        self.client.select_documents.assert_called_once_with("Lia", None)
        self.assertEqual(self.client.download.call_count, 2)
        self.assertIn("Resumo do anexo", reply)
        self.assertIn("aguardando sua revisão e autorização", reply)

    def test_sender_phrase_without_me_finds_sender(self):
        # Real prompt from 20/10: "q a Sicrana mandou", no "me", no "da Sicrana no whats".
        request = "assina o documento último documento q a Sicrana mandou no Whatsapp"
        with (
            patch.object(Workflow, "sign", side_effect=AssertionError("No approval")),
            patch.object(
                Workflow, "deliver", side_effect=AssertionError("No delivery")
            ),
        ):
            agent_route.execute(
                request,
                "sender-without-me",
                state_dir=self.state,
                client_factory=lambda: self.client,
                analyzer=self.analyze,
                certificates=lambda: self.certs,
            )
        # "último": all recent ones from her, the newest is kept.
        self.client.select_documents.assert_called_once_with("Sicrana", None)
        self.assertEqual(self.client.download.call_count, 1)

    def test_sender_named_outside_patterns_is_found_among_recent_senders(self):
        self.client.senders_named_in.return_value = ["sicrana"]
        agent_route.execute(
            "assina aquele pdf da portaria, foi a Sicrana",
            "sender-free-text",
            state_dir=self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        self.client.select_documents.assert_called_once_with("sicrana", 1)

    def test_unknown_sender_explains_instead_of_generic_block(self):
        self.client.senders_named_in.return_value = []
        with self.assertRaises(Blocked) as raised:
            agent_route.execute(
                "assina aquele pdf da portaria",
                "sender-unknown",
                state_dir=self.state,
                client_factory=lambda: self.client,
                analyzer=self.analyze,
                certificates=lambda: self.certs,
            )
        self.assertIn("Não reconheci quem mandou", str(raised.exception))
        self.client.select_documents.assert_not_called()

    def test_failed_real_prompt_routes_to_download_summary_preview_without_general_agent(
        self,
    ):
        request = "dois documentos da Recebi dois documentos da Lia no WhatsApp. Eh, utiliza a skill de assinar PDF e e todos os trâmites dela, por favor."
        with (
            patch("pdf_bridge.subprocess.run", side_effect=self.child),
            patch.object(
                self.plugin,
                "_gateway_rpc",
                side_effect=AssertionError("General agent must not run"),
            ),
            patch.object(Workflow, "sign", side_effect=AssertionError("No approval")),
            patch.object(
                Workflow, "deliver", side_effect=AssertionError("No delivery")
            ),
        ):
            reply = asyncio.run(self.plugin._hermes_gateway_turn(request))
        self.assertIn("aguardando sua revisão e autorização", reply)
        self.assertIn("Resumo do anexo", reply)
        self.assertIn("Obrigação:", reply)
        self.assertIn("__JARVIS_USER_LOWER__", reply)
        self.assertIn("beltrano", reply)
        self.assertEqual(self.client.download.call_count, 2)
        self.client.select_documents.assert_called_once_with("Lia", 2)
        self.assertEqual(
            hashlib.sha256(self.local.read_bytes()).hexdigest(), self.local_hash
        )
        workflow = Workflow(self.state)
        try:
            rows = workflow.db.execute("SELECT id,state FROM requests").fetchall()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["state"], "prepared")
                self.assertIn(
                    (workflow.folder(row["id"]) / "original.pdf").read_bytes(),
                    self.raw.values(),
                )
                self.assertFalse((workflow.folder(row["id"]) / "signed.pdf").exists())
            self.assertEqual(
                workflow.db.execute("SELECT count(*) FROM provenance").fetchone()[0], 2
            )
        finally:
            workflow.close()

    def test_local_fallback_uses_same_controller_even_with_old_resumed_session(self):
        with (
            patch(
                "hermes_bridge.asyncio.create_subprocess_exec",
                side_effect=AssertionError("No unrestricted CLI"),
            ),
            patch("pdf_bridge.subprocess.run", side_effect=self.child),
        ):
            reply = asyncio.run(
                hermes_bridge.ask_hermes(
                    "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
                    session={"hermes_session_id": "old-signer-context"},
                )
            )
        self.assertIn("Nenhum PDF foi assinado ou enviado", reply)

    def test_missing_contact_does_not_substitute_local_homonym(self):
        self.client.select_documents.side_effect = Blocked("Conversa ambígua")
        with self.assertRaisesRegex(Blocked, "ambígua"):
            agent_route.execute(
                "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
                "test",
                self.state,
                client_factory=lambda: self.client,
                analyzer=self.analyze,
                certificates=lambda: self.certs,
            )
        self.client.download.assert_not_called()
        self.assertEqual(
            hashlib.sha256(self.local.read_bytes()).hexdigest(), self.local_hash
        )

    def test_unproven_local_file_or_tampered_origin_blocks(self):
        workflow = Workflow(self.state)
        try:
            request = workflow.ingest(self.client, "123@s.whatsapp.net", "ONE")
            with workflow.db:
                workflow.db.execute(
                    "DELETE FROM provenance WHERE request=?", (request,)
                )
            with self.assertRaisesRegex(Blocked, "Proveniência"):
                workflow.original(workflow.get(request))
        finally:
            workflow.close()

    def test_declared_signed_state_without_crypto_result_cannot_report_success(self):
        workflow = Workflow(self.state)
        try:
            request = workflow.ingest(self.client, "123@s.whatsapp.net", "ONE")
            (workflow.folder(request) / "signed.pdf").write_bytes(self.raw["ONE"])
            with workflow.db:
                workflow.db.execute(
                    "UPDATE requests SET state='signed',output_hash=? WHERE id=?",
                    (hashlib.sha256(self.raw["ONE"]).hexdigest(), request),
                )
            with self.assertRaisesRegex(Blocked, "relatório"):
                agent_route.status(workflow, [request])
        finally:
            workflow.close()

    def test_prepared_chat_requests_are_resumed_without_new_download_or_approval(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "initial",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        workflow = Workflow(self.state)
        ids = [r[0] for r in workflow.db.execute("SELECT id FROM requests")]
        workflow.close()
        blocked_client = Mock(
            side_effect=AssertionError("Must reuse prepared requests")
        )
        result = agent_route.execute(
            "nada ainda?",
            "desktop:existing-chat",
            self.state,
            existing_requests=ids,
            client_factory=blocked_client,
        )
        self.assertIn("aguardando revisão e autorização", result)
        self.assertIn("Resumo e prévias", result)
        blocked_client.assert_not_called()

    def test_repeated_request_resumes_failed_analysis_without_authorizing(self):
        request = "Jarvis, a Lia me mandou alguns documentos no whats para assinar"
        with self.assertRaises(OSError):
            agent_route.execute(
                request,
                "face:lia",
                self.state,
                client_factory=lambda: self.client,
                analyzer=Mock(side_effect=OSError("503 UNAVAILABLE")),
                certificates=lambda: self.certs,
            )
        workflow = Workflow(self.state)
        states = [r[0] for r in workflow.db.execute("SELECT state FROM requests")]
        workflow.close()
        self.assertEqual(states, ["received", "received"])
        downloads = self.client.download.call_count
        no_reviewer = Mock(side_effect=AssertionError("Request is not approval"))
        with (
            patch.object(Workflow, "sign", side_effect=AssertionError("No approval")),
            patch.object(
                Workflow, "deliver", side_effect=AssertionError("No delivery")
            ),
        ):
            reply = agent_route.execute(
                request,
                "face:lia",
                self.state,
                client_factory=lambda: self.client,
                analyzer=self.analyze,
                certificates=lambda: self.certs,
                reviewer=no_reviewer,
            )
            again = agent_route.execute(
                request,
                "face:lia",
                self.state,
                client_factory=lambda: self.client,
                reviewer=no_reviewer,
            )
        self.assertIn("Resumo do anexo", reply)
        self.assertIn("Nenhum PDF foi assinado ou enviado", reply)
        self.assertIn("aguardando revisão e autorização", again)
        # Already downloaded messages are reused; nothing is fetched twice.
        self.assertEqual(self.client.download.call_count, downloads)
        no_reviewer.assert_not_called()
        workflow = Workflow(self.state)
        try:
            for row in workflow.db.execute("SELECT id,state FROM requests"):
                self.assertEqual(row["state"], "prepared")
                self.assertFalse((workflow.folder(row["id"]) / "signed.pdf").exists())
        finally:
            workflow.close()

    def test_unclear_contact_lists_pdf_senders_and_learns_the_pick(self):
        options = [
            {"name": "Fulano Dois", "chat": "2@lid", "documents": 1},
            {"name": "Fulano Três", "chat": "123@s.whatsapp.net", "documents": 2},
        ]
        self.client.select_documents.side_effect = NeedsChoice(
            "Fulano Um", options
        )
        self.client.select_chat.return_value = [
            ("123@s.whatsapp.net", "ONE"),
            ("123@s.whatsapp.net", "TWO"),
        ]
        request = "Jarvis assina os documentos que o Fulano Um me enviou por whats"
        with patch.object(Workflow, "sign", side_effect=AssertionError("No approval")):
            listing = agent_route.execute(
                request, "tg", self.state, client_factory=lambda: self.client
            )
            self.assertIn("2. Fulano Três (2 PDFs)", listing)
            self.client.download.assert_not_called()
            with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
                self.assertTrue(pdf_bridge.is_pdf_request("2", "tg"))
                self.assertFalse(pdf_bridge.is_pdf_request("2", "other"))
            reply = agent_route.execute(
                "2",
                "tg",
                self.state,
                client_factory=lambda: self.client,
                analyzer=self.analyze,
                certificates=lambda: self.certs,
            )
        self.client.remember.assert_called_once_with(
            "Fulano Um", "123@s.whatsapp.net"
        )
        self.client.select_chat.assert_called_once_with("123@s.whatsapp.net", None)
        self.assertTrue(reply.startswith("Conversa: Fulano Três"))
        self.assertIn("aguardando sua revisão e autorização", reply)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            self.assertFalse(pdf_bridge.is_pdf_request("2", "tg"))

    def test_new_sender_after_delivered_batch_fetches_instead_of_status(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "tg",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        workflow = Workflow(self.state)
        with workflow.db:
            workflow.db.execute("UPDATE requests SET state='delivered'")
        workflow.close()
        self.client.select_documents.reset_mock()
        with patch.object(agent_route, "ingest_and_prepare", return_value="novo"):
            reply = agent_route.execute(
                "o Fulano me enviou uns documentos por whats",
                "tg",
                self.state,
                client_factory=lambda: self.client,
            )
        self.assertEqual(reply, "novo")
        self.client.select_documents.assert_called_once_with("Fulano", None)

    def test_pending_choice_accepts_name_or_number_with_extra_words(self):
        options = [
            {"name": "Sicrana (.)", "chat": "1@lid", "documents": 3},
            {"name": "Beltrana Quatro (Beltrana Quattro)", "chat": "2@lid", "documents": 1},
            {"name": "Ciclana Cinco", "chat": "3@lid", "documents": 1},
        ]
        self.client.select_documents.side_effect = NeedsChoice("Lila", options)
        agent_route.execute(
            "a Lila me mandou um documento, assina no whatsapp",
            "tg",
            self.state,
            client_factory=lambda: self.client,
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            self.assertEqual(pdf_bridge.pending_pick("Beltrana", "tg"), "2")
            self.assertEqual(pdf_bridge.pending_pick("2 cara", "tg"), "2")
            self.assertEqual(pdf_bridge.pending_pick("a Ciclana", "tg"), "3")
            self.assertIsNone(pdf_bridge.pending_pick("9", "tg"))
            self.assertIsNone(pdf_bridge.pending_pick("que horas são", "tg"))
            self.assertIsNone(pdf_bridge.pending_pick("Beltrana", "other"))

    def test_authorization_after_delivered_batch_never_reports_old_document(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "tg",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        workflow = Workflow(self.state)
        with workflow.db:
            workflow.db.execute("UPDATE requests SET state='delivered'")
        workflow.close()
        with patch.object(Workflow, "sign", side_effect=AssertionError("Nothing to sign")):
            reply = agent_route.execute(
                "Pode assinar",
                "tg",
                self.state,
                client_factory=Mock(side_effect=AssertionError("No delivery")),
                chat_authorized=True,
            )
        self.assertIn("Nenhum documento novo aguardando assinatura", reply)
        self.assertNotIn("envio confirmado", reply)

    def test_followup_repeats_pending_choice_instead_of_old_status(self):
        # 22/10: the list was dropped by Hermes; "Continue"/"Pode assinar" answered with the old batch.
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "tg",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        workflow = Workflow(self.state)
        with workflow.db:
            workflow.db.execute("UPDATE requests SET state='delivered'")
        workflow.close()
        options = [
            {"name": "mengano oito (Mengano Oito)", "chat": "1@lid", "documents": 1},
            {"name": "Sicrana (.)", "chat": "2@lid", "documents": 3},
        ]
        self.client.select_documents.side_effect = NeedsChoice("Jervis", options)
        agent_route.execute(
            "o Jervis me mandou um documento no WhatsApp pra assinar, assina pra mim",
            "tg",
            self.state,
            client_factory=lambda: self.client,
        )
        with patch.object(Workflow, "sign", side_effect=AssertionError("No choice yet")):
            for followup in ("Continue", "Pode assinar"):
                reply = agent_route.execute(
                    followup,
                    "tg",
                    self.state,
                    client_factory=lambda: self.client,
                    chat_authorized=followup == "Pode assinar",
                )
                self.assertIn("1. mengano oito (Mengano Oito) (1 PDF)", reply)
                self.assertNotIn("envio confirmado", reply)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            self.assertEqual(pdf_bridge.pending_pick("Mengano", "tg"), "1")

    def test_split_contacts(self):
        self.assertEqual(agent_route.split_contacts("Beltrana e a Sicrana"), ["Beltrana", "Sicrana"])
        self.assertEqual(
            agent_route.split_contacts("Beltrana, Sicrana e o Fulano"), ["Beltrana", "Sicrana", "Fulano"]
        )
        self.assertEqual(agent_route.split_contacts("Ciclana Cinco"), ["Ciclana Cinco"])

    def test_two_senders_in_one_request_are_prepared_together(self):
        documents = self.client.select_documents.return_value
        calls = []

        def select(contact, count):
            calls.append((contact, count))
            return documents[:1] if contact == "Beltrana" else documents[1:]

        self.client.select_documents.side_effect = select
        with patch.object(Workflow, "sign", side_effect=AssertionError("No approval")):
            reply = agent_route.execute(
                "Jarvis, a Beltrana e a Sicrana me enviaram documentos no whats, assina por favor",
                "tg",
                self.state,
                client_factory=lambda: self.client,
                analyzer=self.analyze,
                certificates=lambda: self.certs,
            )
        self.assertEqual(calls, [("Beltrana", None), ("Sicrana", None)])
        self.assertIn("aguardando sua revisão e autorização", reply)
        self.assertEqual(reply.count("Documento "), len(documents))

    def test_unclear_second_sender_keeps_the_first_after_the_pick(self):
        documents = self.client.select_documents.return_value
        options = [
            {"name": "Sicrana (.)", "chat": "9@lid", "documents": 1},
            {"name": "Lenir", "chat": "8@lid", "documents": 1},
        ]

        def select(contact, count):
            if contact == "Sicrana":
                raise NeedsChoice(contact, options)
            return documents[:1]

        self.client.select_documents.side_effect = select
        self.client.select_chat.return_value = documents[1:]
        listing = agent_route.execute(
            "a Beltrana e a Sicrana me enviaram documentos no whats, assina",
            "tg",
            self.state,
            client_factory=lambda: self.client,
        )
        self.assertIn('Não identifiquei "Sicrana"', listing)
        self.client.download.assert_not_called()
        with patch.object(Workflow, "sign", side_effect=AssertionError("No approval")):
            reply = agent_route.execute(
                "1",
                "tg",
                self.state,
                client_factory=lambda: self.client,
                analyzer=self.analyze,
                certificates=lambda: self.certs,
            )
        self.client.select_chat.assert_called_once_with("9@lid", None)
        self.assertIn("Sicrana (.)", reply)
        self.assertEqual(reply.count("Documento "), len(documents))

    def test_authorizing_one_sender_signs_only_that_senders_documents(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "tg",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        route = next((self.state / "routes").glob("*.json"))
        beltrana, sicrana = json.loads(route.read_text(encoding="utf-8"))
        signed, delivered = [], []
        with patch.object(
            agent_route,
            "sender_labels",
            return_value={beltrana: ["Beltrana Quatro"], sicrana: [".", "Sicrana"]},
        ), patch.object(
            Workflow, "sign", autospec=True, side_effect=lambda w, i: signed.append(i)
        ), patch.object(
            agent_route, "deliver_signed", side_effect=lambda w, ids, f: delivered.extend(ids)
        ):
            reply = agent_route.execute(
                "pode assinar o da Sicrana, o da Beltrana vou ver...",
                "tg",
                self.state,
                client_factory=lambda: self.client,
                chat_authorized=True,
            )
            self.assertEqual((signed, delivered), ([sicrana], [sicrana]))
            self.assertIn("Continuam aguardando autorização: " , reply)
            nobody = agent_route.execute(
                "pode assinar os da Ciclana",
                "tg",
                self.state,
                client_factory=lambda: self.client,
                chat_authorized=True,
            )
        self.assertIn(
            "Não achei documento pendente de ciclana nesta conversa nem nas outras", nobody
        )
        self.assertIn("Nada foi assinado.", nobody)
        self.assertEqual(signed, [sicrana])

    def test_authorization_reaches_batch_prepared_in_another_conversation(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "desktop:chat",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        route = next((self.state / "routes").glob("*.json"))
        beltrana, sicrana = json.loads(route.read_text(encoding="utf-8"))
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            self.assertTrue(pdf_bridge.pending_anywhere())
        signed = []
        with patch.object(
            agent_route,
            "sender_labels",
            side_effect=lambda w, c, ids: {
                i: ["Beltrana Quatro"] if i == beltrana else [".", "Sicrana"] for i in ids
            },
        ), patch.object(
            Workflow, "sign", autospec=True, side_effect=lambda w, i: signed.append(i)
        ), patch.object(agent_route, "deliver_signed"):
            listing = agent_route.execute(
                "pode assinar", "desktop:telegram", self.state,
                client_factory=lambda: self.client, chat_authorized=True,
            )
            self.assertIn("Em outra conversa estão aguardando autorização", listing)
            self.assertIn("(de Beltrana Quatro)", listing)
            self.assertIn("(de Sicrana)", listing)
            self.assertEqual(signed, [])
            reply = agent_route.execute(
                "pode assinar o da Beltrana", "desktop:telegram", self.state,
                client_factory=lambda: self.client, chat_authorized=True,
            )
        self.assertEqual(signed, [beltrana])
        self.assertIn("Continuam aguardando autorização", reply)
        # Batches older than 24 h are never reused.
        old = time.time() - agent_route.PENDING_WINDOW - 60
        os.utime(route, (old, old))
        workflow = Workflow(self.state)
        try:
            self.assertEqual(
                agent_route.pending_elsewhere(workflow, route.parent, route.parent / "x.json"), []
            )
        finally:
            workflow.close()

    def test_replies_name_documents_by_their_whatsapp_file_name(self):
        reply = agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "tg",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        self.assertRegex(reply, r'Documento "[^"]+\.pdf": ')
        self.assertNotRegex(reply, r"Documento [a-f0-9]{32}")

    def test_document_without_signature_does_not_block_the_batch(self):
        def analyze(folder, model):
            result = self.analyze(folder, model)
            if result["signer_candidates"] == ["beltrano"]:
                result.update(
                    requires_signature=False, summary="Orçamento de fornecedor."
                )
                result["signer_candidates"] = ["unknown"]
            return result

        with patch.object(Workflow, "sign", side_effect=AssertionError("No approval")):
            reply = agent_route.execute(
                "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
                "tg",
                self.state,
                client_factory=lambda: self.client,
                analyzer=analyze,
                certificates=lambda: self.certs,
            )
        self.assertIn("aguardando sua revisão e autorização", reply)
        self.assertIn("Assinante(s): __JARVIS_USER_LOWER__", reply)
        self.assertIn("Fora da assinatura", reply)
        self.assertIn("Orçamento de fornecedor. (não pede assinatura)", reply)
        route = next((self.state / "routes").glob("*.json"))
        kept = json.loads(route.read_text(encoding="utf-8"))
        workflow = Workflow(self.state)
        try:
            self.assertEqual([workflow.get(i)["state"] for i in kept], ["prepared"])
        finally:
            workflow.close()

    def test_failed_analysis_stays_pending_and_flag_does_not_drop_known_signer(self):
        def analyze(folder, model):
            result = self.analyze(folder, model)
            if result["signer_candidates"] == ["beltrano"]:
                raise Blocked(
                    "Análise indisponível; PDF permanece pendente para revisão."
                )
            # Laws often come back with requires_signature=False despite the signer's line.
            return dict(result, requires_signature=False)

        request = "Recebi dois documentos da Lia no WhatsApp para assinar PDF"
        reply = agent_route.execute(
            request,
            "tg",
            self.state,
            client_factory=lambda: self.client,
            analyzer=analyze,
            certificates=lambda: self.certs,
        )
        self.assertIn("Assinante(s): __JARVIS_USER_LOWER__", reply)
        self.assertIn("Análise pendente", reply)
        route = next((self.state / "routes").glob("*.json"))
        self.assertEqual(len(json.loads(route.read_text(encoding="utf-8"))), 2)
        with patch.object(Workflow, "sign") as signing:
            agent_route.execute(
                "pode assinar",
                "tg",
                self.state,
                chat_authorized=True,
                analyzer=Mock(side_effect=AssertionError("Authorization, not retry")),
                client_factory=lambda: self.client,
            )
        signing.assert_called_once()

    def test_long_document_is_summarized_from_head_and_signature_pages(self):
        from analyze import excerpt

        pages = [{"page": n, "text": "x" * 2000} for n in range(117)]
        chosen, note = excerpt(pages)
        self.assertEqual(chosen[-3:], pages[-3:])
        self.assertLess(len(json.dumps(chosen)), 150000)
        self.assertIn("117 páginas", note)
        self.assertEqual(excerpt(pages[:5]), (pages[:5], ""))

    def test_hybrid_xref_pdf_is_rewritten_with_same_text_before_signing(self):
        import pdf_engine

        raw = document_for("__JARVIS_USER__")
        self.assertIs(pdf_engine.without_hybrid_xref(raw), raw)
        hybrid = Mock()
        hybrid.return_value.xrefs.hybrid_xrefs_present = True
        with patch.object(pdf_engine, "PdfFileReader", hybrid):
            rewritten = pdf_engine.without_hybrid_xref(raw)
        self.assertNotEqual(rewritten, raw)
        self.assertEqual(
            pdf_engine.inspect(rewritten)[0]["text"], pdf_engine.inspect(raw)[0]["text"]
        )

    def test_strict_read_failure_rewrites_only_unsigned_pdf(self):
        import pdf_engine
        from pyhanko.pdf_utils.misc import PdfStrictReadError

        raw = document_for("__JARVIS_USER__")

        def reader(signatures):
            def build(_stream, strict):
                if strict:
                    raise PdfStrictReadError("free xref with next generation 0")
                return SimpleNamespace(embedded_signatures=signatures)

            return build

        with patch.object(pdf_engine, "PdfFileReader", reader([])):
            rewritten = pdf_engine.without_hybrid_xref(raw)
        self.assertNotEqual(rewritten, raw)
        self.assertEqual(
            pdf_engine.inspect(rewritten)[0]["text"], pdf_engine.inspect(raw)[0]["text"]
        )
        with (
            patch.object(pdf_engine, "PdfFileReader", reader([object()])),
            self.assertRaises(Blocked),
        ):
            pdf_engine.without_hybrid_xref(raw)

    def two_signature_law(self):
        """Prepare a law (2 signature pages) plus an ofício citing it, as in the Simone batch."""
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(600, 800))
        pdf.drawString(50, 700, "Lei 1.674: Plano pela Primeira Infância.")
        pdf.drawCentredString(300, 560, "BELTRANO SEIS CAMARGO SETE")
        pdf.showPage()
        pdf.drawString(50, 300, "Carta-Compromisso Institucional.")
        pdf.drawCentredString(300, 150, "Beltrano Seis Camargo Sete")
        pdf.save()
        self.raw["ONE"] = buffer.getvalue()
        self.messages["ONE"]["message"]["documentMessage"]["fileSha256"] = (
            base64.b64encode(hashlib.sha256(self.raw["ONE"]).digest()).decode()
        )

        def analyze(folder, model):
            text = json.loads((folder / "extracted.json").read_text(encoding="utf-8"))
            law = "Primeira" in text[0]["text"]
            return {
                "requires_signature": True,
                "summary": "Lei Municipal nº 1.674/2026, Primeira Infância."
                if law
                else "Ofício que encaminha as Leis nº 1.674 a 1.681.",
                "obligations": [],
                "signer_candidates": ["beltrano"],
            }

        self.raw["TWO"] = document_for("BELTRANO SEIS CAMARGO SETE")
        self.messages["TWO"]["message"]["documentMessage"]["fileSha256"] = (
            base64.b64encode(hashlib.sha256(self.raw["TWO"]).digest()).decode()
        )
        reply = agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "tg",
            self.state,
            client_factory=lambda: self.client,
            analyzer=analyze,
            certificates=lambda: self.certs,
        )
        self.assertIn("1ª na página 1 (beltrano); 2ª na página 2 (beltrano)", reply)
        workflow = Workflow(self.state)
        try:
            return workflow.ingest(self.client, "123@s.whatsapp.net", "ONE")
        finally:
            workflow.close()

    def test_keep_only_first_signature_then_authorize_in_same_message(self):
        law = self.two_signature_law()
        signed = []

        def sign(workflow, request_id):
            signed.append(
                (
                    request_id,
                    len(workflow.checked_plan(workflow.get(request_id))["placements"]),
                )
            )

        with (
            patch.object(Workflow, "sign", autospec=True, side_effect=sign),
            patch.object(agent_route, "status", return_value="estado"),
        ):
            agent_route.execute(
                "Assine todos, somente o documento da Lei 1.674/2026, assine só a primeira assinatura",
                "tg",
                self.state,
                chat_authorized=True,
                client_factory=lambda: self.client,
            )
        self.assertEqual(len(signed), 2)
        self.assertIn((law, 1), signed)
        workflow = Workflow(self.state)
        try:
            self.assertEqual(
                workflow.checked_plan(workflow.get(law))["placements"][0]["page"], 0
            )
        finally:
            workflow.close()

    def test_signature_choice_alone_updates_preview_without_signing(self):
        law = self.two_signature_law()
        with patch.object(Workflow, "sign", side_effect=AssertionError("No signing")):
            reply = agent_route.execute(
                "só a segunda assinatura da lei 1.674", "tg", self.state
            )
            unclear = agent_route.execute(
                "só a terceira assinatura da lei 1.674", "tg", self.state
            )
        self.assertIn("Assinaturas ajustadas", reply)
        self.assertIn("Nada foi assinado", unclear)
        workflow = Workflow(self.state)
        try:
            placements = workflow.checked_plan(workflow.get(law))["placements"]
            self.assertEqual([p["page"] for p in placements], [1])
        finally:
            workflow.close()

    def test_typed_authorization_with_conditions_reaches_controller_as_chat(self):
        pdf_agent_gate._turns.clear()
        history = [{"role": "user", "content": "Assinar os PDFs da Simone"}]
        for message, expected in (
            ("Assine todos, só a primeira assinatura da Lei 1.674", True),
            ("sim, pode assinar", True),
            ("assina os documentos que a Simone me enviou por whats", False),
        ):
            pdf_agent_gate.capture(
                session_id="s",
                turn_id=message,
                user_message=message,
                conversation_history=history,
            )
            self.assertEqual(
                pdf_agent_gate._turns[("s", message)]["chat_authorized"], expected
            )
        pdf_agent_gate._turns.clear()

    def test_conditional_authorization_signs_nothing(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "scope",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        with patch.object(Workflow, "sign", side_effect=AssertionError("No signing")):
            reply = agent_route.execute(
                "Assine todos, somente o documento da Lei 1.674, só a primeira assinatura",
                "scope",
                self.state,
                reviewer=Mock(side_effect=AssertionError("No review")),
            )
        self.assertIn("Nada foi assinado", reply)

    def test_one_failed_signature_does_not_stop_batch_and_can_be_retried(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "scope",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        calls = []

        def sign(workflow, request_id):
            calls.append(request_id)
            with workflow.db:
                workflow.db.execute(
                    "UPDATE requests SET state=? WHERE id=?",
                    ("failed" if len(calls) == 1 else "approved", request_id),
                )
            if len(calls) == 1:
                raise ValueError("hybrid xref")

        with (
            patch.dict(os.environ, {"HERMES_HOME": str(self.root)}),
            patch.object(Workflow, "sign", autospec=True, side_effect=sign),
            patch.object(agent_route, "status", return_value="estado"),
        ):
            reply = agent_route.execute(
                "pode assinar",
                "scope",
                self.state,
                chat_authorized=True,
                client_factory=lambda: self.client,
            )
        self.assertEqual(len(calls), 2)
        workflow = Workflow(self.state)
        try:
            label = workflow.label(calls[0])
        finally:
            workflow.close()
        self.assertTrue(label.endswith('.pdf"'))  # the WhatsApp file name, not the internal id
        self.assertIn("Falha ao assinar o documento " + label, reply)
        self.assertIn("hybrid xref", (self.state / "controller-error.log").read_text())
        reply = agent_route.execute("continue", "scope", self.state)
        workflow = Workflow(self.state)
        try:
            self.assertEqual(workflow.get(calls[0])["state"], "prepared")
        finally:
            workflow.close()

    def test_pending_short_approval_never_returns_to_old_agent(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "scope",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            self.assertTrue(pdf_bridge.is_pdf_request("pode assinar", "scope"))
            self.assertFalse(pdf_bridge.is_pdf_request("abra o Photoshop", "scope"))
        with patch.object(
            Workflow, "sign", side_effect=AssertionError("Without GUI approval")
        ):
            result = agent_route.execute(
                "pode assinar", "scope", self.state, reviewer=lambda *_args: None
            )
        self.assertIn("aguardando revisão e autorização", result)

    def test_explicit_chat_authorization_does_not_request_second_gui_approval(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "scope",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        with patch.object(Workflow, "sign") as signing:
            reply = agent_route.execute(
                "pode assinar",
                "scope",
                self.state,
                chat_authorized=True,
                reviewer=Mock(side_effect=AssertionError("No repeated GUI approval")),
            )
        self.assertEqual(signing.call_count, 2)
        self.assertIn("aprovado; ainda não assinado", reply)

    def test_authorization_with_remark_about_the_mayor_signs(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "scope",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )
        for request in (
            "Pode assinar, o alias “Beltrano” é do prefeito msm",
            "Autorizado, assina",
        ):
            with self.subTest(request=request), patch.object(Workflow, "sign") as signing:
                agent_route.execute(
                    request,
                    "scope",
                    self.state,
                    chat_authorized=True,
                    reviewer=Mock(side_effect=AssertionError("No GUI approval")),
                )
                self.assertEqual(signing.call_count, 2)

    def test_chat_authorization_returns_signed_pdfs_to_original_chat(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "scope",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )

        def signed(workflow, request_id):
            with workflow.db:
                workflow.db.execute(
                    "UPDATE requests SET state='signed' WHERE id=?", (request_id,)
                )

        with (
            patch.object(Workflow, "sign", autospec=True, side_effect=signed),
            patch.object(Workflow, "deliver", autospec=True) as delivering,
            patch.object(agent_route, "status", return_value="ok"),
        ):
            agent_route.execute(
                "pode assinar",
                "scope",
                self.state,
                chat_authorized=True,
                client_factory=lambda: self.client,
            )
            delivering.reset_mock()
            agent_route.execute(
                "pode assinar",
                "scope",
                self.state,
                reviewer=Mock(side_effect=AssertionError("Spoken is not chat")),
                client_factory=Mock(side_effect=AssertionError("No delivery")),
            )
        delivering.assert_not_called()

    def test_chat_authorization_delivers_each_signed_pdf_once(self):
        agent_route.execute(
            "Recebi dois documentos da Lia no WhatsApp para assinar PDF",
            "scope",
            self.state,
            client_factory=lambda: self.client,
            analyzer=self.analyze,
            certificates=lambda: self.certs,
        )

        def signed(workflow, request_id):
            with workflow.db:
                workflow.db.execute(
                    "UPDATE requests SET state='signed' WHERE id=?", (request_id,)
                )

        def delivered(workflow, request_id, client):
            self.assertIs(client, self.client)
            with workflow.db:
                workflow.db.execute(
                    "UPDATE requests SET state='delivered' WHERE id=?", (request_id,)
                )

        with (
            patch.object(Workflow, "sign", autospec=True, side_effect=signed),
            patch.object(
                Workflow, "deliver", autospec=True, side_effect=delivered
            ) as delivering,
            patch.object(agent_route, "status", return_value="ok"),
        ):
            agent_route.execute(
                "pode assinar",
                "scope",
                self.state,
                chat_authorized=True,
                client_factory=lambda: self.client,
            )
            self.assertEqual(delivering.call_count, 2)
            agent_route.execute(
                "pode enviar",
                "scope",
                self.state,
                client_factory=Mock(side_effect=AssertionError("Already delivered")),
            )
        self.assertEqual(delivering.call_count, 2)


class NativeDesktopGateTests(unittest.TestCase):
    def setUp(self):
        pdf_agent_gate._turns.clear()

    def test_typed_request_uses_native_middleware_without_provider_or_tools(self):
        request = "Jarvis, a Lia me mandou uns arquivos pdfs para assinatura, utilize a skill e faça o trabalho"
        self.assertTrue(pdf_bridge.is_pdf_request(request))
        pdf_agent_gate.capture(session_id="chat", turn_id="turn", user_message=request)
        downstream = Mock(side_effect=AssertionError("No general model"))
        with patch.object(
            pdf_agent_gate.bridge,
            "run",
            return_value="Preparado; revisão e autorização pendentes",
        ) as controller:
            result = pdf_agent_gate.execute(
                {"stream": True}, downstream, session_id="chat", turn_id="turn"
            )
            again = pdf_agent_gate.execute(
                {}, downstream, session_id="chat", turn_id="turn"
            )
        controller.assert_called_once()
        downstream.assert_not_called()
        self.assertEqual(
            result.choices[0].message.content, again.choices[0].message.content
        )
        self.assertIsNone(result.choices[0].message.tool_calls)
        self.assertEqual(
            pdf_agent_gate.block_tools(session_id="chat", turn_id="turn")["action"],
            "block",
        )

    def test_other_skill_load_mentioning_signing_words_is_not_a_signing_request(self):
        # A /reverse-engineer-anything load injects the skill text, which lists action words
        # ("assinar", "enviar"...) in a do-not-click prose list. It must not route to the controller.
        skill_load = (
            '[IMPORTANT: The user has invoked the "reverse-engineer-anything" skill, indicating '
            "they want you to follow its instructions. The full skill content is loaded below.]\n\n"
            "Mapeie o sistema. Nunca clique em nada que grave ou dispare ação: salvar, enviar, "
            "assinar, arquivar, anexar; na dúvida, registre o documento e não clique."
        )
        self.assertFalse(pdf_bridge.is_pdf_request(skill_load))
        self.assertIsNone(
            pdf_agent_gate.capture(
                session_id="chat", turn_id="turn", user_message=skill_load, conversation_history=[]
            )
        )
        # A real typed signing request, and the pdf-signing skill's own load, still route.
        self.assertTrue(pdf_bridge.is_pdf_request("Assinar os PDFs que a Lia me mandou"))
        self.assertTrue(
            pdf_bridge.is_pdf_request(
                '[IMPORTANT: The user has invoked the "pdf-signing" skill...] assina os documentos'
            )
        )

    def test_real_hermes_execution_chain_invokes_pdf_gate_instead_of_provider(self):
        sys.path.insert(0, str(ROOT / "hermes-agent"))
        from hermes_cli.middleware import run_llm_execution_middleware

        manager = SimpleNamespace(
            _middleware={"llm_execution": [pdf_agent_gate.execute]}
        )
        plugin_adapter = SimpleNamespace(_delivery_manager=lambda: manager)
        pdf_agent_gate.capture(
            session_id="chat", turn_id="turn", user_message="Assinar PDFs"
        )
        provider = Mock(side_effect=AssertionError("Provider must not execute"))
        with (
            patch.dict(sys.modules, {"hermes_cli.plugins": plugin_adapter}),
            patch.object(
                pdf_agent_gate.bridge,
                "run",
                return_value="Preparado; autorização pendente",
            ),
        ):
            result = run_llm_execution_middleware(
                {"messages": []}, provider, session_id="chat", turn_id="turn"
            )
        self.assertEqual(result.choices[0].finish_reason, "stop")
        self.assertEqual(
            result.choices[0].message.content, "Preparado; autorização pendente"
        )
        provider.assert_not_called()

    def test_authorized_and_dropped_reply_followups_reach_controller(self):
        history = [
            {"role": "user", "content": "a Beltrana me mandou documentos pra assinar no whats"},
            {"role": "assistant", "content": "Documento x: preparado; aguardando revisão e autorização"},
        ]
        pdf_agent_gate.capture(
            session_id="chat", turn_id="t1", user_message="Autorizado, assina",
            conversation_history=history,
        )
        self.assertTrue(pdf_agent_gate._turns[("chat", "t1")]["chat_authorized"])
        dropped = history[:1] + [
            {"role": "assistant", "content": "Your request was not processed. Send it again if you still want me to carry it out."}
        ]
        with patch.object(pdf_agent_gate.bridge, "pending_pick", return_value=None):
            for turn, message in (("t2", "E aí"), ("t3", "Continue")):
                pdf_agent_gate.capture(
                    session_id="chat", turn_id=turn, user_message=message,
                    conversation_history=dropped,
                )
                self.assertIn(("chat", turn), pdf_agent_gate._turns)
            pdf_agent_gate.capture(
                session_id="chat", turn_id="t4", user_message="E aí",
                conversation_history=[{"role": "assistant", "content": "Your request was not processed."}],
            )
        self.assertNotIn(("chat", "t4"), pdf_agent_gate._turns)

    def test_controller_failure_does_not_fall_through_to_old_agent(self):
        pdf_agent_gate.capture(
            session_id="chat", turn_id="turn", user_message="Assinar PDFs"
        )
        next_call = Mock(side_effect=AssertionError("Cannot fall through"))
        with patch.object(
            pdf_agent_gate.bridge, "run", side_effect=RuntimeError("synthetic failure")
        ):
            result = pdf_agent_gate.execute(
                {}, next_call, session_id="chat", turn_id="turn"
            )
        self.assertIn("indisponível", result.choices[0].message.content)
        next_call.assert_not_called()

    def test_status_followup_adopts_only_ids_from_successful_tool_results(self):
        identifier = "1" * 32
        history = [
            {"role": "user", "content": "Assinar PDFs"},
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "exit_code": 0,
                        "output": json.dumps(
                            {"request": identifier, "state": "prepared"}
                        ),
                    }
                ),
            },
            {"role": "user", "content": "2" * 32},
            {"role": "assistant", "content": "Estado: preparado, aguardando sua revisão."},
        ]
        pdf_agent_gate.capture(
            session_id="chat",
            turn_id="turn",
            user_message="nada ainda?",
            conversation_history=history,
        )
        with patch.object(
            pdf_agent_gate.bridge, "run", return_value="Preparado"
        ) as controller:
            pdf_agent_gate.execute({}, Mock(), session_id="chat", turn_id="turn")
        self.assertEqual(controller.call_args.kwargs["existing_requests"], [identifier])

    def test_bare_yes_belongs_to_pdf_flow_only_right_after_its_reply(self):
        # Incident 19/10 19:08: "sim" answered the agent's Vigia question, not the PDF flow.
        history = [
            {"role": "user", "content": "a Ciclana me mandou um documento no whats, assina"},
            {"role": "assistant", "content": "Documento \"x.pdf\": preparado; aguardando revisão e autorização"},
            {"role": "user", "content": "porque não estou vendo o chat do vigia?"},
            {"role": "assistant", "content": "Quer que eu verifique a proposta #1 do Vigia agora?"},
        ]
        self.assertIsNone(
            pdf_agent_gate.capture(
                session_id="s", turn_id="a", user_message="sim", conversation_history=history
            )
        )
        self.assertIsNotNone(
            pdf_agent_gate.capture(
                session_id="s", turn_id="b", user_message="sim", conversation_history=history[:2]
            )
        )

    def test_message_quoting_a_controller_reply_goes_to_the_agent(self):
        complaint = (
            'pqp, o que vc me perguntou? Respondi "Sim" vc responde com: Documento "118.pdf": '
            "assinado, validado e envio confirmado na conversa original"
        )
        history = [{"role": "user", "content": "a Ciclana me mandou um documento no whats, assina"}]
        self.assertIsNone(
            pdf_agent_gate.capture(
                session_id="s", turn_id="c", user_message=complaint, conversation_history=history
            )
        )

    def test_unrelated_chat_keeps_normal_provider_and_plugin_registration(self):
        ctx = Mock()
        pdf_agent_gate.register(ctx)
        self.assertEqual(ctx.register_hook.call_count, 2)
        self.assertEqual(ctx.register_middleware.call_args.args[0], "llm_execution")
        pdf_agent_gate.capture(
            session_id="chat", turn_id="turn", user_message="Abra o Photoshop"
        )
        downstream = Mock(return_value="normal")
        self.assertEqual(
            pdf_agent_gate.execute({}, downstream, session_id="chat", turn_id="turn"),
            "normal",
        )
        self.assertIsNone(pdf_agent_gate.block_tools(session_id="chat", turn_id="turn"))


if __name__ == "__main__":
    unittest.main()
