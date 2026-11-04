import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""

import cerebro_graph


def write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class CerebroGraphTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name)

    def test_links_by_stem_alias_and_accent_folding(self):
        write(self.vault, "pessoas/__JARVIS_USER_FULLNAME__.md", "---\naliases: [Ludi]\n---\nDono do [[Jarvis]].")
        write(self.vault, "projetos/Jarvis.md", "Feito por [[ludi]] e [[Inexistente]].")
        write(self.vault, "pessoas/Beltrano.md", "Fala com [[Beltrano|o prefeito]].")
        graph = cerebro_graph.build_graph(self.vault)
        ids = [n["id"] for n in graph["nodes"]]
        self.assertEqual(sorted(ids), ["pessoas/__JARVIS_USER_FULLNAME__", "pessoas/Beltrano", "projetos/Jarvis"])
        index = {i: n for n, i in enumerate(ids)}
        pair = tuple(sorted((index["pessoas/__JARVIS_USER_FULLNAME__"], index["projetos/Jarvis"])))
        self.assertEqual([tuple(link) for link in graph["links"]], [pair])

    def test_skips_templates_and_hidden_dirs(self):
        write(self.vault, "_modelos/Pessoa.md", "modelo")
        write(self.vault, ".obsidian/x.md", "config")
        write(self.vault, "pessoas/Ana.md", "oi")
        graph = cerebro_graph.build_graph(self.vault)
        self.assertEqual([n["label"] for n in graph["nodes"]], ["Ana"])

    def test_degree_counts_unique_edges(self):
        write(self.vault, "a.md", "[[b]] [[b]]")
        write(self.vault, "b.md", "[[a]]")
        graph = cerebro_graph.build_graph(self.vault)
        self.assertEqual([n["degree"] for n in graph["nodes"]], [1, 1])
        self.assertEqual(len(graph["links"]), 1)

    def test_graph_is_none_without_vault(self):
        self.assertIsNone(cerebro_graph.graph())


if __name__ == "__main__":
    unittest.main()
