"""Busca de arquivos pelo índice do Windows: mesmos filtros da varredura e volta para ela."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_index
from local_files import FilesSession


class IndexedSearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)
        self.wanted = self._file("Relatório Final.pdf")
        self.rows = [
            self.wanted,
            self._file(".oculta/relatorio.pdf"),
            self._file("node_modules/relatorio.pdf"),
            self._file("relatorio.exe"),  # abrir executaria
            self._file("relatorio.pfx"),  # chave privada
            self._file("LoL-Backup-2026/relatorio.pdf"),  # pasta de jogo
            self._file("relatorio foto.jpg"),  # fotos agora entram
            self._file("contrato.pdf"),  # índice devolveu algo que não casa o termo
            self.root / "relatorio apagado.pdf",  # índice ainda lista arquivo removido
            Path(self.outside.name).resolve() / "relatorio.pdf",
        ]
        (Path(self.outside.name) / "relatorio.pdf").write_text("x", encoding="utf-8")
        self.calls = []

    def _file(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        return path

    def _index(self, rows):
        def find(roots, tokens):
            self.calls.append((list(roots), tokens))
            return rows
        return find

    def test_index_results_pass_the_same_filters_as_the_walk(self):
        result = FilesSession([self.root], index=self._index(self.rows)).search("relatorio")
        self.assertEqual(sorted(row["name"] for row in result["results"]), ["Relatório Final.pdf", "relatorio foto.jpg"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["unreadable"], 0)  # apagado depois de indexado não torna a busca parcial
        self.assertEqual(self.calls, [([self.root], ["relatorio"])])

    def test_falls_back_to_walk_when_index_is_unavailable_or_empty(self):
        for rows in (None, []):
            with self.subTest(rows=rows):
                result = FilesSession([self.root], index=self._index(rows)).search("relatorio final")
                self.assertEqual([row["name"] for row in result["results"]], ["Relatório Final.pdf"])

    def test_query_terms_never_carry_quotes_wildcards_or_operators(self):
        self.assertEqual(windows_index._terms(["relatorio", "o'k%_*\"", "040-2017"]), ["relatorio", "o", "k", "040", "2017"])
        self.assertIsNone(windows_index.find([self.root], ["'%\""]))


if __name__ == "__main__":
    unittest.main()
