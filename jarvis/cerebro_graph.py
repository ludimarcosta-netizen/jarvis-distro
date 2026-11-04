"""Grafo somente-leitura do Segundo Cérebro: notas viram estrelas, [[wikilinks]] viram ligações.

Alimenta a "face" do Jarvis (galáxia de memórias). Lê só do disco local; nada sai da máquina.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import cerebro

_WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:[#|][^\]]*)?\]\]")
_READ_LIMIT = 40_000  # bytes por nota: o suficiente para achar os links sem ler documentos enormes
_SKIP_DIRS = {"_modelos", ".obsidian", ".trash", ".git"}
MAX_NODES = 1500


def _note_paths(vault: Path) -> List[Path]:
    paths = [p for p in vault.rglob("*.md") if not _SKIP_DIRS.intersection(p.relative_to(vault).parts)]
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_NODES]


def _read(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(_READ_LIMIT).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _kind(vault: Path, path: Path) -> str:
    parts = path.relative_to(vault).parts
    return parts[0] if len(parts) > 1 else "raiz"


def build_graph(vault: Path) -> Dict[str, Any]:
    paths = _note_paths(vault)
    texts = [_read(p) for p in paths]

    by_name: Dict[str, int] = {}
    for index, path in enumerate(paths):
        meta = cerebro._frontmatter(texts[index])
        names = [path.stem, meta.get("nome", "")]
        names += [a.strip().strip("\"'") for a in meta.get("aliases", meta.get("apelidos", "")).strip("[]").split(",")]
        for name in names:
            if name:
                by_name.setdefault(cerebro._fold(name), index)

    edges = set()
    for index, text in enumerate(texts):
        for target in _WIKILINK.findall(text):
            other = by_name.get(cerebro._fold(target))
            if other is not None and other != index:
                edges.add((min(index, other), max(index, other)))

    degree = [0] * len(paths)
    for a, b in edges:
        degree[a] += 1
        degree[b] += 1

    nodes = [
        {"id": path.relative_to(vault).as_posix()[:-3], "label": path.stem, "kind": _kind(vault, path), "degree": degree[i]}
        for i, path in enumerate(paths)
    ]
    return {"nodes": nodes, "links": sorted(edges)}


def graph() -> Optional[Dict[str, Any]]:
    vault = cerebro.vault_dir()
    return build_graph(vault) if vault else None
