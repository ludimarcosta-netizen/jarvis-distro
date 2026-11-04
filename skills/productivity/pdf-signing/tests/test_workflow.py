"""Offline contract tests: synthetic documents, identities and API only."""

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from asn1crypto import keys
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyhanko.sign import signers
from pyhanko_certvalidator.registry import SimpleCertificateStore
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analyze import analyze, generate
from evolution import Blocked, Evolution, NeedsChoice
from pdf_engine import certificate_id, sign_incrementally, validate
from receive import receive, serve
from validation_data import public_url, refresh
from windows_signer import WindowsSigner, load_providers
from workflow import Workflow


def synthetic_signer(label):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "SYNTHETIC TEST " + label)]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(True, True, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(private, hashes.SHA256())
    )
    cert = asn1_x509.Certificate.load(
        certificate.public_bytes(serialization.Encoding.DER)
    )
    key = keys.PrivateKeyInfo.load(
        private.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return signers.SimpleSigner(cert, key, SimpleCertificateStore())


def synthetic_pdf():
    output = io.BytesIO()
    page = canvas.Canvas(output, pagesize=(600, 800))
    page.drawString(50, 700, "SYNTHETIC CONTRACT: obligation within 10 days.")
    page.drawString(50, 150, "__JARVIS_USER__                          Beltrano")
    page.save()
    return output.getvalue()


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.providers = {
            name: synthetic_signer(name) for name in ("__JARVIS_USER_LOWER__", "beltrano")
        }
        cls.roots = [p.signing_cert for p in cls.providers.values()]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workflow = Workflow(self.tmp.name)
        self.raw = synthetic_pdf()
        self.client = Mock()
        self.client.instance = "synthetic"
        self.message = {
            "key": {
                "id": "ORIGINAL",
                "remoteJid": "123@s.whatsapp.net",
                "fromMe": False,
            },
            "message": {
                "documentMessage": {
                    "mimetype": "application/pdf",
                    "fileName": "test.pdf",
                    "fileSha256": base64.b64encode(
                        hashlib.sha256(self.raw).digest()
                    ).decode(),
                }
            },
            "messageTimestamp": int(time.time()),
        }
        self.client.message.return_value = self.message
        self.client.download.return_value = self.raw
        self.request = self.workflow.ingest(
            self.client, "123@s.whatsapp.net", "ORIGINAL"
        )

    def tearDown(self):
        self.workflow.close()
        self.tmp.cleanup()

    def proposal(self, names=("__JARVIS_USER_LOWER__", "beltrano")):
        return {
            "summary": "Synthetic contract.",
            "obligations": ["Perform within ten days."],
            "placements": [
                {
                    "signer": name,
                    "page": 0,
                    "box": [30 + 270 * i, 40, 270 + 270 * i, 100],
                    "certificate": certificate_id(self.providers[name].signing_cert),
                }
                for i, name in enumerate(names)
            ],
        }

    def prepare_approve(self, names=("__JARVIS_USER_LOWER__", "beltrano")):
        self.workflow.prepare(self.request, self.proposal(names))
        self.workflow.approve(
            self.request, self.workflow.get(self.request)["plan_hash"]
        )

    def test_end_to_end_two_signatures_and_quoted_delivery(self):
        self.prepare_approve()
        report = self.workflow.sign(self.request, self.providers, self.roots)
        self.assertEqual(len(report), 2)
        self.assertTrue(all(row["valid"] for row in report))
        output = (self.workflow.folder(self.request) / "signed.pdf").read_bytes()
        self.assertTrue(output.startswith(self.raw))
        if os.environ.get("PDF_SIGNING_QA_DIR"):
            import pymupdf

            target = Path(os.environ["PDF_SIGNING_QA_DIR"])
            target.mkdir(parents=True, exist_ok=True)
            (target / "synthetic-signed.pdf").write_bytes(output)
            (target / "preview.png").write_bytes(
                (self.workflow.folder(self.request) / "preview-1.png").read_bytes()
            )
            with pymupdf.open(stream=output, filetype="pdf") as pdf:
                pdf[0].get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2)).save(
                    target / "signed.png"
                )
        self.client.deliver.return_value = {
            "key": {"id": "DELIVERED", "remoteJid": "123@s.whatsapp.net"}
        }
        self.workflow.deliver(self.request, self.client)
        self.client.deliver.assert_called_once()
        chat, message, raw, name = self.client.deliver.call_args.args
        self.assertEqual(
            (chat, message, raw), ("123@s.whatsapp.net", self.message, output)
        )
        # The signed PDF goes back with the same file name it arrived with.
        from workflow import original_file_name

        self.assertEqual(name, original_file_name(self.message) or "documento-assinado.pdf")
        self.assertEqual(self.workflow.get(self.request)["state"], "delivered")
        with self.assertRaises(Blocked):
            self.workflow.deliver(self.request, self.client)

    def test_existing_signature_is_preserved(self):
        first, _ = sign_incrementally(
            self.raw,
            self.proposal(("__JARVIS_USER_LOWER__",))["placements"],
            self.providers,
            self.roots,
        )
        second_placement = self.proposal(("beltrano",))["placements"]
        second_placement[0]["box"] = [310, 40, 550, 100]
        second, report = sign_incrementally(
            first, second_placement, self.providers, [self.providers["beltrano"].signing_cert]
        )
        self.assertTrue(second.startswith(first))
        self.assertEqual(len(report), 1)

    def test_missing_expired_or_reused_approval_blocks(self):
        self.workflow.prepare(self.request, self.proposal())
        with self.assertRaises(Blocked):
            self.workflow.sign(self.request, self.providers, self.roots)
        self.prepare_approve()
        with self.workflow.db:
            self.workflow.db.execute(
                "UPDATE requests SET approved_until=0 WHERE id=?", (self.request,)
            )
        with self.assertRaises(Blocked):
            self.workflow.sign(self.request, self.providers, self.roots)

    def test_tampering_original_preview_and_plan_blocks(self):
        self.prepare_approve()
        folder = self.workflow.folder(self.request)
        preview = folder / "preview-0.png"
        original_preview = preview.read_bytes()
        preview.write_bytes(b"changed")
        with self.assertRaises(Blocked):
            self.workflow.sign(self.request, self.providers, self.roots)
        preview.write_bytes(original_preview)
        (folder / "original.pdf").write_bytes(self.raw + b"changed")
        with self.assertRaises(Blocked):
            self.workflow.sign(self.request, self.providers, self.roots)

    def test_reprepare_invalidates_approval_and_wrong_hash_blocks(self):
        self.prepare_approve()
        self.workflow.prepare(self.request, self.proposal(("__JARVIS_USER_LOWER__",)))
        self.assertEqual(self.workflow.get(self.request)["state"], "prepared")
        with self.assertRaises(Blocked):
            self.workflow.approve(self.request, "0" * 64)
        with self.assertRaises(Blocked):
            self.workflow.sign(self.request, self.providers, self.roots)

    def test_wrong_certificate_and_missing_real_provider_block(self):
        self.prepare_approve()
        with (
            patch(
                "workflow.real_providers",
                side_effect=Blocked("Certificados não mapeados"),
            ),
            self.assertRaisesRegex(Blocked, "não mapeados"),
        ):
            self.workflow.sign(self.request)
        with self.assertRaisesRegex(Blocked, "difere"):
            self.workflow.sign(
                self.request, {"__JARVIS_USER_LOWER__": self.providers["beltrano"]}, self.roots
            )
        self.assertEqual(self.workflow.get(self.request)["state"], "failed")

    def test_duplicate_receipt_does_not_download_again(self):
        result = self.workflow.ingest(self.client, "123@s.whatsapp.net", "ORIGINAL")
        self.assertEqual(result, self.request)
        self.client.download.assert_called_once()

    def test_invalid_geometry_and_ambiguous_signer_block(self):
        for change in (
            {"box": [-1, 0, 200, 50]},
            {"signer": "someone"},
            {"page": -1},
            {"box": [0, 0, float("nan"), 50]},
        ):
            proposal = self.proposal()
            proposal["placements"][0].update(change)
            with self.assertRaises(Blocked):
                self.workflow.prepare(self.request, proposal)

    def test_delivery_timeout_does_not_retry(self):
        self.prepare_approve(("__JARVIS_USER_LOWER__",))
        self.workflow.sign(self.request, self.providers, self.roots)
        self.client.deliver.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.workflow.deliver(self.request, self.client)
        with self.assertRaises(Blocked):
            self.workflow.deliver(self.request, self.client)
        self.client.deliver.assert_called_once()

    def test_changed_signed_output_blocks_delivery(self):
        self.prepare_approve(("__JARVIS_USER_LOWER__",))
        self.workflow.sign(self.request, self.providers, self.roots)
        (self.workflow.folder(self.request) / "signed.pdf").write_bytes(b"changed")
        with self.assertRaises(Blocked):
            self.workflow.deliver(self.request, self.client)
        self.client.deliver.assert_not_called()

    def test_validation_rejects_untrusted_cert_and_modified_content(self):
        output, _ = sign_incrementally(
            self.raw,
            self.proposal(("__JARVIS_USER_LOWER__",))["placements"],
            self.providers,
            self.roots,
        )
        with self.assertRaises(Blocked):
            validate(output, [self.providers["beltrano"].signing_cert])
        modified = output.replace(b"ReportLab", b"ReportLxb", 1)
        with self.assertRaises(Blocked):
            validate(modified, self.roots)

    def test_monitor_new_pdf_pending_and_old_events_ignored(self):
        event = {"event": "MESSAGES_UPSERT", "data": self.message}
        result = receive(event, self.workflow, self.client, time.time() - 2)
        self.assertEqual(result["action"], "pdf-pending-analysis")
        self.assertFalse(result["automatic_signature"])
        self.assertEqual(
            receive(event, self.workflow, self.client, time.time() + 2)["action"],
            "ignored-old-event",
        )
        self.client.deliver.assert_not_called()

    def test_monitor_text_requires_exact_quote(self):
        message = dict(self.message, message={"conversation": "Preciso de assinatura"})
        result = receive(
            {"event": "messages.upsert", "data": message},
            self.workflow,
            self.client,
            time.time() - 2,
        )
        self.assertEqual(result["action"], "clarification-required")
        message["message"] = {
            "extendedTextMessage": {
                "text": "Assinar",
                "contextInfo": {"stanzaId": "ORIGINAL"},
            }
        }
        result = receive(
            {"event": "messages.upsert", "data": message},
            self.workflow,
            self.client,
            time.time() - 2,
        )
        self.assertEqual(result["request"], self.request)

    def test_windows_adapter_uses_public_certificate_and_mocked_native_signature(self):
        from cryptography.hazmat.primitives.asymmetric import padding

        original = self.providers["__JARVIS_USER_LOWER__"]
        encoded = base64.b64encode(original.signing_cert.dump()).decode()
        record = {
            "alias": "__JARVIS_USER_LOWER__",
            "fingerprint": certificate_id(original.signing_cert),
            "certificate": encoded,
            "chain": [encoded],
            "has_private_key": True,
            "valid_now": True,
            "chain_ok": True,
        }
        private = serialization.load_der_private_key(
            original.signing_key.dump(), password=None
        )

        def native(_mode, body):
            self.assertEqual(body["fingerprint"], record["fingerprint"])
            signed = private.sign(
                base64.b64decode(body["data"]), padding.PKCS1v15(), hashes.SHA256()
            )
            return {"signature": base64.b64encode(signed).decode()}

        with patch("windows_signer.invoke", side_effect=native):
            output, report = sign_incrementally(
                self.raw,
                self.proposal(("__JARVIS_USER_LOWER__",))["placements"],
                {"__JARVIS_USER_LOWER__": WindowsSigner(record)},
                self.roots,
            )
        self.assertTrue(output.startswith(self.raw))
        self.assertTrue(report[0]["valid"])
        from pyhanko.pdf_utils.reader import PdfFileReader

        cms = PdfFileReader(io.BytesIO(output)).embedded_signatures[0].signed_data
        certificates = [item.chosen.dump() for item in cms["certificates"]]
        self.assertEqual(len(certificates), len(set(certificates)))
        with (
            patch(
                "windows_signer.inventory", return_value=[dict(record, chain_ok=False)]
            ),
            self.assertRaisesRegex(Blocked, "cadeia"),
        ):
            load_providers(
                self.proposal(("__JARVIS_USER_LOWER__",))["placements"],
                self.workflow.root / "validation",
            )
        with patch("windows_signer.inventory", return_value=[record]):
            providers, roots, crls = load_providers(
                self.proposal(("__JARVIS_USER_LOWER__",))["placements"],
                self.workflow.root / "validation",
            )
            self.assertIn("__JARVIS_USER_LOWER__", providers)
            self.assertEqual(len(roots), 1)
            self.assertEqual(crls, [])

    def test_stale_offline_revocation_refreshes_public_crls_once(self):
        from pdf_engine import real_providers
        from windows_signer import StaleRevocation

        loaded = ({"__JARVIS_USER_LOWER__": Mock()}, [Mock()], [Mock()])
        with (
            patch(
                "windows_signer.load_providers",
                side_effect=[StaleRevocation("revogação vencida"), loaded],
            ) as loading,
            patch("validation_data.refresh") as refreshing,
        ):
            self.assertIs(real_providers([], self.workflow.root), loaded)
        refreshing.assert_called_once_with(self.workflow.root)
        self.assertEqual(loading.call_count, 2)
        with (
            patch(
                "windows_signer.load_providers",
                side_effect=StaleRevocation("revogação vencida"),
            ),
            patch("validation_data.refresh") as refreshing,
            self.assertRaises(StaleRevocation),
        ):
            real_providers([], self.workflow.root)
        refreshing.assert_called_once()
        with (
            patch(
                "windows_signer.load_providers",
                side_effect=Blocked("Certificado não mapeado"),
            ),
            patch("validation_data.refresh") as refreshing,
            self.assertRaisesRegex(Blocked, "não mapeado"),
        ):
            real_providers([], self.workflow.root)
        refreshing.assert_not_called()

    def test_monitor_disabled_until_phone_condition_verified(self):
        with self.assertRaises(Blocked):
            serve(self.workflow, self.client, {"enabled": True})

    def test_analysis_without_tools_cached_by_exact_input(self):
        response = {
            "summary": "Synthetic contract",
            "requires_signature": True,
            "obligations": ["Perform in ten days"],
            "signer_candidates": ["__JARVIS_USER_LOWER__"],
        }
        run = Mock(
            return_value=Mock(returncode=0, stdout=json.dumps(response).encode())
        )
        folder = self.workflow.folder(self.request)
        result = analyze(folder, "configured-model", run=run)
        self.assertTrue(result["human_review_required"])
        analyze(folder, "configured-model", run=run)
        run.assert_called_once()
        (folder / "context.json").write_text(
            '{"signature_request":"Updated request"}', encoding="utf-8"
        )
        analyze(folder, "configured-model", run=run)
        self.assertEqual(run.call_count, 2)
        self.client.deliver.assert_not_called()

    def test_analysis_ignores_gateway_pythonpath_and_logs_failures(self):
        run = Mock(return_value=Mock(returncode=1, stdout=b"", stderr=b"boom"))
        folder = self.workflow.folder(self.request)
        with (
            patch.dict(os.environ, {"PYTHONPATH": "hermes-3.14", "VIRTUAL_ENV": "x"}),
            self.assertRaisesRegex(Blocked, "indisponível"),
        ):
            analyze(folder, "configured-model", run=run)
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("VIRTUAL_ENV", environment)
        self.assertEqual((folder / "analysis-error.log").read_bytes(), b"boom")

    def test_analysis_timeout_keeps_document_pending(self):
        run = Mock(side_effect=subprocess.TimeoutExpired("synthetic", 120))
        with self.assertRaises(Blocked):
            analyze(self.workflow.folder(self.request), "configured-model", run=run)
        self.assertEqual(self.workflow.get(self.request)["state"], "received")

    def test_analysis_retries_overload_then_falls_back(self):
        class Overloaded(Exception):
            code = 503

        primary = Mock(side_effect=Overloaded())
        fallback = Mock(return_value='{"ok": true}')
        sleep = Mock()
        self.assertEqual(generate([primary, fallback], sleep=sleep), '{"ok": true}')
        self.assertEqual(primary.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])
        fallback.assert_called_once()

    def test_analysis_recovers_after_transient_error(self):
        class RateLimited(Exception):
            code = 429

        primary = Mock(side_effect=[RateLimited(), "done"])
        fallback = Mock()
        self.assertEqual(generate([primary, fallback], sleep=Mock()), "done")
        fallback.assert_not_called()

    def test_analysis_permanent_error_skips_retries(self):
        class NotFound(Exception):
            code = 404

        primary = Mock(side_effect=NotFound())
        sleep = Mock()
        with self.assertRaises(RuntimeError):
            generate([primary], sleep=sleep)
        primary.assert_called_once()
        sleep.assert_not_called()

    def test_analysis_stops_at_budget(self):
        clock = Mock(side_effect=[0.0, 96.0])
        call = Mock()
        with self.assertRaises(RuntimeError):
            generate([call], clock=clock, budget=100.0)
        call.assert_not_called()

    def test_existing_signature_rectangle_cannot_be_overlaid(self):
        signed, _ = sign_incrementally(
            self.raw,
            self.proposal(("__JARVIS_USER_LOWER__",))["placements"],
            self.providers,
            self.roots,
        )
        self.client.message.return_value = dict(
            self.message,
            key={"id": "NEXT", "remoteJid": "123@s.whatsapp.net"},
            message={
                "documentMessage": {
                    "mimetype": "application/pdf",
                    "fileSha256": base64.b64encode(
                        hashlib.sha256(signed).digest()
                    ).decode(),
                }
            },
        )
        self.client.download.return_value = signed
        request = self.workflow.ingest(self.client, "123@s.whatsapp.net", "NEXT")
        with self.assertRaisesRegex(Blocked, "sobrepõe"):
            self.workflow.prepare(request, self.proposal(("beltrano",)))


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environment = patch.dict(os.environ, {"HERMES_HOME": home.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_sender_name_from_verified_provenance_recovers_unnamed_contact(self):
        with tempfile.TemporaryDirectory() as folder:
            workflow = Workflow(Path(folder) / "state/pdf-signing")
            source = json.dumps(
                {
                    "key": {"remoteJid": "C", "fromMe": False},
                    "pushName": "Lia Example",
                }
            )
            with workflow.db:
                workflow.db.execute(
                    "INSERT INTO requests(id,chat,message_id,source,pdf_hash,state) VALUES(?,?,?,?,?,?)",
                    ("known", "C", "ONE", source, "unused", "received"),
                )
                workflow.db.execute(
                    "INSERT INTO provenance VALUES(?,?,?,?,?,?)",
                    (
                        "known",
                        "whatsapp-evolution",
                        hashlib.sha256(source.encode()).hexdigest(),
                        "unused",
                        "test",
                        time.time(),
                    ),
                )
            workflow.db.close()
            client = self.client()
            client._directory = Mock()
            client._directory.names.return_value = {}
            client._directory.resolve.return_value = ["A", "B"]
            client._recent_documents = Mock(side_effect=[[], [], [("C", "ONE")]])
            with patch.dict(os.environ, {"HERMES_HOME": folder}):
                self.assertEqual(client.select_documents("Lia", None), [("C", "ONE")])

    def test_duplicate_contact_names_resolve_only_with_unique_recent_pdf_batch(self):
        for batches in ([[], [("B", "ONE")]], [[("A", "ONE")], [("B", "TWO")]]):
            client = self.client()
            client._directory = Mock()
            client._directory.names.return_value = {"A": "Lia A", "B": "Lia B"}
            client._directory.resolve.return_value = ["A", "B"]
            client._recent_documents = Mock(side_effect=batches)
            if not batches[0]:
                self.assertEqual(client.select_documents("Lia", None), [("B", "ONE")])
            else:
                # Two same-name chats with PDFs: the user picks; nothing is guessed.
                with self.assertRaises(NeedsChoice) as raised:
                    client.select_documents("Lia", None)
                self.assertEqual(
                    [o["name"] for o in raised.exception.options],
                    ["Lia A", "Lia B"],
                )
            self.assertEqual(
                client._recent_documents.call_args_list, [(("A",),), (("B",),)]
            )

    def test_unspecified_plural_selects_bounded_recent_batch_in_exact_chat(self):
        for amount in (0, 2, 11):
            with self.subTest(amount=amount):
                client = self.client()
                client._directory = Mock()
                client._directory.resolve.return_value = ["CHAT"]
                records = [
                    {
                        "key": {"id": str(i), "remoteJid": "CHAT", "fromMe": False},
                        "messageTimestamp": time.time(),
                        "message": {
                            "documentMessage": {
                                "mimetype": "application/pdf",
                                "fileName": "document.pdf",
                            }
                        },
                    }
                    for i in range(amount)
                ]
                client.call.side_effect = [
                    {"readMessages": False, "readStatus": False},
                    {"messages": {"records": records}},
                    # No PDF in the exact chat: the sender search finds none either.
                    {"messages": {"records": []}},
                ]
                if amount == 2:
                    self.assertEqual(
                        client.select_documents("Lia", None),
                        [("CHAT", "0"), ("CHAT", "1")],
                    )
                else:
                    with self.assertRaises(Blocked):
                        client.select_documents("Lia", None)
                exact = client.call.call_args_list[1].args[1]["where"]
                self.assertEqual(exact["key"]["remoteJid"], "CHAT")
                self.assertNotIn("messageTimestamp", exact)

    def test_revocation_download_rejects_local_endpoints(self):
        with self.assertRaises(Blocked):
            public_url("file:///private")
        with (
            patch(
                "validation_data.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
            ),
            self.assertRaises(Blocked),
        ):
            public_url("http://issuer.invalid/crl")

    def test_revocation_unavailable_is_distinct_from_revoked(self):
        with tempfile.TemporaryDirectory() as folder:
            for status, expected in (
                ("Revoked", "revogado"),
                ("OfflineRevocation", "Não equivale"),
            ):
                with (
                    patch(
                        "validation_data.inventory",
                        return_value=[{"chain_ok": False, "chain_status": [status]}],
                    ),
                    self.assertRaisesRegex(Blocked, expected),
                ):
                    refresh(folder)

    def client(self, settings=None):
        client = object.__new__(Evolution)
        client.call = Mock(
            return_value=settings or {"readMessages": False, "readStatus": False}
        )
        return client

    def test_read_guard_blocks_enabled_or_unknown_flags(self):
        for settings in ({"readMessages": True, "readStatus": False}, {"other": False}):
            with self.assertRaises(Blocked):
                self.client(settings).unread_guard()

    def test_message_query_is_exact_and_no_mark_read(self):
        client = self.client()
        message = {"key": {"id": "ONE", "remoteJid": "CHAT"}}
        client.call.side_effect = [
            {"readMessages": False, "readStatus": False},
            {"messages": {"records": [message]}},
        ]
        self.assertEqual(client.message("CHAT", "ONE"), message)
        self.assertEqual(client.call.call_args.args[0], "/chat/findMessages")
        self.assertEqual(
            client.call.call_args.args[1]["where"]["key"],
            {"remoteJid": "CHAT", "id": "ONE"},
        )

    def test_download_rejects_non_pdf(self):
        client = self.client()
        client.call.side_effect = [
            {"readMessages": False, "readStatus": False},
            {"base64": base64.b64encode(b"not pdf").decode()},
        ]
        with self.assertRaises(Blocked):
            client.download({})

    def test_delivery_payload_quotes_original(self):
        client = self.client()
        original = {"key": {"id": "ONE"}, "message": {"documentMessage": {}}}
        client.deliver("CHAT", original, b"%PDF-test")
        self.assertEqual(client.call.call_args.args[0], "/message/sendMedia")
        self.assertEqual(client.call.call_args.args[1]["quoted"], original)


if __name__ == "__main__":
    unittest.main()
