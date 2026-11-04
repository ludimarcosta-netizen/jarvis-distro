"""Avaliação reproduzível sem dados privados: perguntas operacionais em português."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cerebro_memory import Memory

NOTES = {
    "infra": "A migração da infraestrutura ocorrerá no sábado. O backup deve ser validado antes da transferência.",
    "audio": "O reconhecimento de fala utiliza faster-whisper local. A transcrição funciona offline.",
    "memoria": "As decisões ficam em arquivos Markdown no Obsidian. SQLite FTS5 mantém um índice reconstruível.",
    "voz": "A expressão de ativação Hey Jarvis inicia a conversa por voz com Gemini Live.",
    "backup": "A restauração exige recuperar a cópia de segurança e conferir a integridade dos documentos.",
    "tarefas": "As pendências permanecem abertas até confirmação explícita do responsável.",
    "banco": "PostgreSQL atende múltiplos usuários e maior concorrência de escrita.",
    "codigo": "O identificador exato do procedimento é JRV-4827.",
}
CASES = [
    ("Como mudar os serviços para outra máquina?", "infra"),
    ("Converter gravação em texto sem internet", "audio"),
    ("Onde guardamos o conhecimento do assistente?", "memoria"),
    ("Como chamar o assistente falando?", "voz"),
    ("Como recuperar documentos perdidos?", "backup"),
    ("Quando considerar uma obrigação concluída?", "tarefas"),
    ("Qual armazenamento suporta acessos simultâneos?", "banco"),
    ("JRV-4827", "codigo"),
]


def evaluate() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "projetos").mkdir()
        for name, body in NOTES.items():
            (root / "projetos" / f"{name}.md").write_text(f"# {name}\n{body}\n", encoding="utf-8")
        memory = Memory(root)
        memory.sync()
        index_start = time.perf_counter()
        memory.refresh_semantic()
        report = {"corpus": "sintético, temas operacionais do Jarvis; não mede todo o cofre",
                  "index_seconds": round(time.perf_counter() - index_start, 3), "queries": []}
        for query, expected in CASES:
            item = {"query": query, "expected": f"projetos/{expected}.md"}
            for mode in ("textual", "hybrid"):
                start = time.perf_counter()
                results = memory.search(query, 3, semantic=mode == "hybrid")
                paths = [row["path"] for row in results]
                item[mode] = {"paths": paths, "seconds": round(time.perf_counter() - start, 4),
                              "rank": paths.index(item["expected"]) + 1 if item["expected"] in paths else None}
            report["queries"].append(item)
        for mode in ("textual", "hybrid"):
            ranks = [item[mode]["rank"] for item in report["queries"]]
            report[mode] = {"hit_at_1": sum(rank == 1 for rank in ranks),
                            "hit_at_3": sum(rank is not None for rank in ranks), "total": len(ranks)}
        return report


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
