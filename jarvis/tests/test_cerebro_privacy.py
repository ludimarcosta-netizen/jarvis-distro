"""Proteções de texto em nuvem e memória local, sem chamadas reais de API."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import context_extractor as extractor
import project_notes
import session_harvest


class PrivacyTest(unittest.TestCase):
    def test_cloud_flag_blocks_even_cached_client(self):
        with patch.object(extractor, "_env_value", return_value="0"), patch.object(extractor, "_text_client", Mock()), self.assertRaises(PermissionError):
            extractor.get_text_client()

    def test_transcript_preserves_secrets_and_cpf(self):
        text = extractor.format_transcripts_for_prompt([{"text": 'senha: hunter22abc CPF 123.456.789-09 e 12345678909 "api_key": "abcdefghijklmnop"', "source":"mic"}])
        for secret in ("hunter22abc", "123.456.789-09", "12345678909", "abcdefghijklmnop"):
            self.assertIn(secret, text)

    def test_condensation_preserves_input(self):
        client = Mock()
        client.chats.create.return_value.send_message.return_value = Mock(text="notas")
        with patch.object(extractor, "get_text_client", return_value=client):
            extractor._condense_transcripts("senha: hunter22abc")
        self.assertIn("hunter22abc", client.chats.create.return_value.send_message.call_args.args[0])

    def test_legacy_cloud_harvest_is_blocked_and_classification_local(self):
        with self.assertRaises(PermissionError):
            session_harvest.summarize_cloud(None)
        self.assertTrue(session_harvest.classify_prefeitura("Revisar licitações e contratos da prefeitura"))
        self.assertNotIn("gemini", session_harvest.DEFAULT_TOOLS)

    def test_project_documentation_never_uses_text_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            project = {"nome":"Teste", "aliases":["teste"], "palavras_chave":["teste"]}
            info = {"nome":"Teste", "caminhos":["C:/Teste"], "stack":["Python"]}
            with patch.object(project_notes, "PROJECTS", [project]), patch.object(project_notes, "facts", return_value=info), patch.object(extractor, "get_text_client", side_effect=AssertionError("network forbidden")):
                self.assertEqual(project_notes.build(vault), ["Teste"])
            self.assertIn("ainda não validado", (vault / "projetos/Teste.md").read_text(encoding="utf-8"))
