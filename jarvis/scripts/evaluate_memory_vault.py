"""Avalia perguntas documentadas contra o cofre real, sem escrever nele ou expor trechos."""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cerebro_memory import Memory
from cerebro_semantic import SemanticIndex

CASES = [
    ("Como iniciar interação falando com Jarvis?", "projetos/Jarvis.md", "projetos"),
    ("Qual motor transforma voz em texto no computador?", "projetos/Jarvis.md", "projetos"),
    ("Quem pode declarar uma obrigação encerrada?", "perfil/Preferencias vigentes.md", "perfil"),
    ("Qual implantação facilita levar um sistema para outra máquina?", "perfil/Preferencias por assunto.md", "perfil"),
    ("Posso substituir a tecnologia de uma aplicação já existente?", "perfil/Preferencias por assunto.md", "perfil"),
]


class ReadOnlyMemory(Memory):
    def __init__(self, vault: Path, index: Path):
        super().__init__(vault)
        self.index = index

    def connect(self):
        conn = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def sync(self):
        return {}

    def semantic_index(self):
        return SemanticIndex(self.index, Path(__file__).resolve().parents[1] / "models/cerebro")


def evaluate(vault: Path, index: Path) -> list[dict]:
    memory = ReadOnlyMemory(vault, index)
    # O arquivo de ativação permanece no cofre; o teste usa somente sua presença lógica.
    from unittest.mock import patch
    original = Path.is_file
    enabled = vault / "_indice/semantica.db"
    report = []
    with patch.object(Path, "is_file", lambda path: True if path == enabled else original(path)):
        for query, expected, folder in CASES:
            case = {"query": query, "expected": expected}
            for mode in ("textual", "hybrid"):
                start = time.perf_counter()
                rows = memory.search(query, 3, folder, semantic=mode == "hybrid")
                paths = [row["path"] for row in rows]
                case[mode] = {"rank": paths.index(expected) + 1 if expected in paths else None,
                              "seconds": round(time.perf_counter() - start, 3)}
            report.append(case)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.vault, args.index), ensure_ascii=True, indent=2))
