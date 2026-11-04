"""Plugin tests never write the operator's task journal."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
root = Path(__file__).resolve().parents[4]
if not (root / "delegation_journal.py").is_file():
    root = Path(__file__).resolve().parents[3] / "jarvis"
sys.path.insert(0, str(root))

import plugin_api as pa
from delegation_journal import Journal
from proactivity import Store


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    # Existing cases exercise the retained Gemini transport; local voice has its own suite.
    monkeypatch.setattr(pa, "_local_enabled", lambda: False)
    monkeypatch.setattr(pa, "_local_runtime", None)
    journal = Journal(tmp_path / "tasks.sqlite3")
    monkeypatch.setattr(pa, "_task_journal", lambda: journal)
    monkeypatch.setattr(pa, "_proactivity_store", lambda: Store(tmp_path / "proactivity.json"))
    monkeypatch.setattr(pa, "_delegation_queue", [])
    monkeypatch.setattr(pa, "_delegation_lock", asyncio.Lock())
    return journal
