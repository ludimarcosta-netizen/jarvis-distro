"""Saved-name address book and tolerant PDF-sender matching (synthetic data only)."""

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from evolution import Evolution, NeedsChoice

_spec = importlib.util.spec_from_file_location(
    "wa_contacts",
    Path(__file__).resolve().parents[2] / "whatsapp-evolution" / "wa.py",
)
wa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wa)

VCF = (
    "BEGIN:VCARD\r\nVERSION:2.1\r\n"
    "N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Um;Fulano;;;\r\n"
    "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Fulano Um =C3=81gua\r\n"
    "TEL;CELL:+55 64 99000-0001\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Lia Prefeitura\r\n"
    "TEL;TYPE=CELL:(64) 3000-00\r\n 03\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Sem Telefone\r\nEND:VCARD\r\n"
)


class AddressBookTests(unittest.TestCase):
    def test_vcf_quoted_printable_folding_and_ninth_digit(self):
        book = wa.parse_vcf(VCF)
        self.assertEqual(len(book), 2)
        # WhatsApp may carry the old 8-digit form (without the ninth digit).
        self.assertEqual(book[wa.phone_key("556490000001")], "Fulano Um Água")
        self.assertEqual(book[wa.phone_key("6430000003")], "Lia Prefeitura")

    def test_saved_name_uses_alternate_number_for_lid_chats(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "agenda.vcf").write_text(VCF, encoding="utf-8")
            with patch.object(wa, "CONTACTS_DIR", Path(folder)):
                wa._book_cache = (None, {})
                self.assertEqual(
                    wa.saved_name("999@lid", "5564990000001@s.whatsapp.net"),
                    "Fulano Um Água",
                )
                self.assertEqual(wa.saved_name("999@lid"), "")

    def test_learned_alias_resolves_directly(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(wa, "__file__", str(Path(folder) / "wa.py")),
        ):
            wa.learn_alias("Fulano Um", "999@lid")
            self.assertEqual(wa.resolve("fulano um", {}), ["999@lid"])


class SignedFileNameTests(unittest.TestCase):
    def test_names_follow_the_document_title(self):
        from workflow import document_title, signed_file_name

        source = {
            "message": {"documentMessage": {"fileName": "Lei Ordinária 1676 DE 16.pdf"}}
        }
        cases = {
            "Lei Municipal nº 1.674/2026, que aprova o Plano.": "Lei Municipal nº 1.674_2026.pdf",
            "Lei Municipal nº 1.678, de 16 de outubro de 2026, que altera a Lei nº 390/2002.": "Lei Municipal nº 1.678_2026.pdf",
            "Portaria n.º 217/GAB/2026 da Prefeitura.": "Portaria nº 217_GAB_2026.pdf",
            "Ofício nº 117/2026 – GAB, emitido em 16 de outubro.": "Ofício nº 117_2026 – GAB.pdf",
            "Orçamento de fornecedor sem número.": "documento-assinado.pdf",
        }
        for summary, expected in cases.items():
            # Without a WhatsApp file name the title names the signed PDF.
            self.assertEqual(signed_file_name({"summary": summary}, {}), expected)
        # The WhatsApp file name always wins: the signed PDF keeps its original name.
        self.assertEqual(
            signed_file_name({"summary": "Lei Municipal nº 1.674/2026."}, source),
            "Lei Ordinária 1676 DE 16.pdf",
        )
        self.assertEqual(
            signed_file_name({"summary": "x", "title": "Decreto nº 5/2026"}, {}),
            "Decreto nº 5_2026.pdf",
        )
        self.assertEqual(document_title("Sem título"), "")


class SenderMatchTests(unittest.TestCase):
    def client(self, records, saved=None):
        client = Evolution.__new__(Evolution)
        client._directory = type(
            "Directory",
            (),
            {
                "saved_name": staticmethod(
                    lambda jid, alt="": (saved or {}).get(alt, "")
                )
            },
        )
        client.call = lambda _route, _body=None: {
            "messages": {"records": records, "pages": 1}
        }
        return client

    def record(self, chat, push, alt="", age=60):
        return {
            "key": {"remoteJid": chat, "fromMe": False, "remoteJidAlt": alt},
            "pushName": push,
            "messageTimestamp": int(time.time() - age),
            "message": {"documentMessage": {"mimetype": "application/pdf"}},
        }

    def test_saved_name_gives_unique_full_match(self):
        records = [
            self.record("1@lid", "Mk", "5564990000001@s.whatsapp.net"),
            self.record("2@lid", "Fulano Dois"),
        ]
        client = self.client(
            records, {"5564990000001@s.whatsapp.net": "Fulano Um"}
        )
        self.assertEqual(
            client._match_sender("Fulano Um", {}), ("1@lid", "Fulano Um (Mk)")
        )

    def test_partial_or_old_matches_ask_instead_of_guessing(self):
        records = [
            self.record("2@lid", "Fulano Dois"),
            self.record("3@lid", "Fulano Três"),
            self.record("4@lid", "Fulano Antigo", age=3 * 86400),
            self.record("5@g.us", "Fulano Um"),
        ]
        with self.assertRaises(NeedsChoice) as raised:
            self.client(records)._match_sender("Fulano Um", {})
        self.assertEqual(
            [o["chat"] for o in raised.exception.options], ["2@lid", "3@lid"]
        )


if __name__ == "__main__":
    unittest.main()
