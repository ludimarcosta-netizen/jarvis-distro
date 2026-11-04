"""Regressões determinísticas da integração semântica, sem rede ou modelo externo."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

from cerebro_memory import Memory
from cerebro_semantic import SemanticIndex, fuse


class FakeEncoder:
    def embed(self, texts, **kwargs):
        for text in texts:
            yield np.array([1, 0] if "infraestrutura" in text else [0, 1], dtype=np.float32)

    def query_embed(self, query):
        return iter([np.array([1, 0], dtype=np.float32)])


class SemanticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "projetos").mkdir()
        (self.root / "perfil").mkdir()
        self.note = self.root / "projetos/Infra.md"
        self.note.write_text("# Plano\nMigrar infraestrutura no sábado.", encoding="utf-8")
        (self.root / "perfil/Preferencia.md").write_text("# Perfil\nTexto sem relação.", encoding="utf-8")
        self.memory = Memory(self.root)
        self.mock = patch("cerebro_semantic.encoder", return_value=FakeEncoder()).start()
        self.addCleanup(patch.stopall)
        self.memory.sync()

    def test_paraphrase_and_folder_filter(self):
        self.assertEqual(self.memory.search("transferir serviços", semantic=False), [])
        self.memory.refresh_semantic()
        rows = self.memory.search("transferir serviços")
        self.assertEqual(rows[0]["fonte"], "projetos/Infra.md:1")
        self.assertEqual(self.memory.search("transferir serviços", folder="perfil"), [])

    def test_incremental_update_and_deletion(self):
        self.assertEqual(self.memory.refresh_semantic()["novos"], 2)
        self.assertEqual(self.memory.refresh_semantic()["novos"], 0)
        self.note.write_text("# Alterado\nTexto sem relação.", encoding="utf-8")
        self.assertEqual(self.memory.search("transferir serviços"), [])
        self.note.unlink()
        self.memory.sync()
        with closing(sqlite3.connect(self.memory.semantic_index().db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM vectors").fetchone()[0], 1)

    def test_model_failure_keeps_lexical_with_diagnostic(self):
        self.memory.refresh_semantic()
        self.mock.side_effect = RuntimeError("não deve aparecer no resultado")
        self.assertTrue(self.memory.search("infraestrutura"))
        self.assertEqual(self.memory.semantic_error, "RuntimeError")

    def test_changed_chunk_is_never_ranked_from_stale_embedding(self):
        self.memory.refresh_semantic()
        self.note.write_text("# Alterado\nTexto sem relação.", encoding="utf-8")
        self.mock.side_effect = RuntimeError("modelo indisponível")
        self.memory.sync()
        self.mock.side_effect = None
        self.assertEqual(self.memory.search("transferir serviços"), [])

    def test_fusion_promotes_agreement_and_keeps_exact_match(self):
        first = {"path": "a", "line": 1}
        second = {"path": "b", "line": 1}
        self.assertEqual(fuse([first, second], [second])[0], second)
        self.assertEqual(fuse([first], []), [first])

    def test_empty_index_and_missing_cache(self):
        self.assertEqual(SemanticIndex(self.root / "missing.db", self.root).rank("x", [], 3), [])
        self.assertEqual(self.memory.search("!!!"), [])

    def test_registered_date_is_preserved_in_source_context(self):
        self.note.write_text('---\ndata: "2026-10-14T12:00:00-03:00"\n---\n# Plano\nMigrar infraestrutura.', encoding="utf-8")
        result = self.memory.search("infraestrutura", semantic=False)
        self.assertEqual(result[0]["data"], "2026-10-14T12:00:00-03:00")
        self.assertIn("Data registrada: 2026-10-14", self.memory.context("infraestrutura")["contexto"])
