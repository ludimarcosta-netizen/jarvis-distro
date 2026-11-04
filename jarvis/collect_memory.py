"""Coleta periódica local e estado operacional, sem LLM nem rede."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from cerebro_io import atomic_write, file_lock
from cerebro_memory import Memory
from cerebro_projects import publish_states
from session_harvest import run


def refresh_project_map(vault: Path) -> None:
    """Acrescenta projetos ausentes ao índice; preserva as linhas existentes."""
    path = vault / "_mapa.md"
    if not path.exists():
        return
    with file_lock(path):
        text = path.read_text(encoding="utf-8")
        missing = [f"- [[{p.stem}]] — projeto\n" for p in sorted((vault / "projetos").glob("*.md"))
                   if f"[[{p.stem}]]" not in text]
        if not missing:
            return
        start = text.find("## Projetos")
        end = text.find("\n## ", start + 1) if start >= 0 else -1
        if start < 0:
            text = text.rstrip() + "\n\n## Projetos\n\n" + "".join(missing)
        else:
            end = len(text) if end < 0 else end
            section = text[start:end].replace("_(vazio)_", "").rstrip()
            text = text[:start] + section + "\n\n" + "".join(missing) + "\n" + text[end:]
        atomic_write(path, text)


def main() -> None:
    health = Path(__file__).resolve().parent / "memory_health.json"
    try:
        counts = run(max_sessions=25)
        memory = Memory()
        refresh_project_map(memory.vault)
        indexed = memory.sync()
        projects = publish_states(memory.vault, memory.db)
        if projects["atualizados"]:
            indexed = memory.sync()
        result = {"at": datetime.now(timezone.utc).isoformat(), "ok": counts["erros"] == 0,
                  "collection": counts, "index": indexed, "projects": projects, "mode": "local"}
    except (OSError, ValueError, RuntimeError):
        atomic_write(health, json.dumps({"at": datetime.now(timezone.utc).isoformat(), "ok": False,
                                       "error": "Armazenamento indisponível; nenhuma chamada externa efetuada."}))
        raise
    atomic_write(health, json.dumps(result, ensure_ascii=False))
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
