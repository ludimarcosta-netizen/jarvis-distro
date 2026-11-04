"""Visible identity comes from the signing certificate, with no manual profile."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from appearance import appearance_pdf
from test_workflow import synthetic_signer


class AppearanceTests(unittest.TestCase):
    def test_certificate_identity_and_local_date_are_visible(self):
        cert = synthetic_signer("PESSOA FICTICIA:00000000000").signing_cert
        when = datetime(2026, 10, 15, 14, 30, tzinfo=timezone(timedelta(hours=-3)))
        with fitz.open(stream=appearance_pdf(cert, when), filetype="pdf") as pdf:
            text = " ".join(pdf[0].get_text().split())
            self.assertEqual(text.count(cert.subject.native["common_name"]), 2)
            self.assertIn("Assinado de forma digital por", text)
            self.assertIn("2026.10.15 14:30:00 -0300", text)


if __name__ == "__main__":
    unittest.main()
