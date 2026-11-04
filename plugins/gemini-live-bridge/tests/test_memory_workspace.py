"""Plugin entry point exercises real revision files and SQLite on a synthetic vault."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import plugin_api as pa

source = Path(__file__).resolve().parents[4]
if not (source / "local_memory.py").is_file():
    source = Path(__file__).resolve().parents[3] / "jarvis"
sys.path.insert(0, str(source))
from cerebro_memory import Memory
from local_workspace import Workspace


class MemoryEndpointTests(unittest.TestCase):
    def test_search_prepare_and_confirm_are_explicit_and_preserve_original(self):
        async def scenario(vault):
            memory = Memory(vault)
            service = Workspace(memory_factory=lambda: memory)
            with patch.object(pa, "_local_file_workspace", return_value=service):
                session = (await pa.local_workspace({"action": "start"}))["session"]
                rows = await pa.local_workspace({"action": "memory.search", "session": session, "query": "fixtureoriginal"})
                ref = rows["results"][0]["ref"]
                prepared = await pa.local_workspace({"action": "memory.prepare", "session": session, "ref": ref,
                                                    "change": "correct", "text": "# Teste\n\nfixturenova", "reason": "Contrato sintético"})
                self.assertTrue(prepared["ok"])
                self.assertTrue(memory.search("fixtureoriginal"))
                result = await pa.local_workspace({"action": "memory.confirm", "session": session,
                                                  "ticket": prepared["ticket"], "change": "disable", "text": "ignorado"})
                self.assertTrue(result["ok"])
                self.assertTrue(memory.search("fixturenova"))
                self.assertFalse(memory.search("fixtureoriginal"))
                repeated = await pa.local_workspace({"action": "memory.confirm", "session": session, "ticket": prepared["ticket"]})
                self.assertFalse(repeated["ok"])
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / "projetos").mkdir()
            note = vault / "projetos/Teste.md"
            raw = "# Teste\n\nfixtureoriginal"
            note.write_text(raw, encoding="utf-8")
            asyncio.run(scenario(vault))
            self.assertEqual(note.read_text(encoding="utf-8"), raw)

    def test_arbitrary_path_and_other_workspace_session_cannot_be_reviewed(self):
        async def scenario():
            service = Workspace()
            with patch.object(pa, "_local_file_workspace", return_value=service):
                session = (await pa.local_workspace({"action": "start"}))["session"]
                result = await pa.local_workspace({"action": "memory.prepare", "session": session,
                                                  "ref": "../../outside.md", "change": "disable", "reason": "não executar"})
                self.assertFalse(result["ok"])
                result = await pa.local_workspace({"action": "memory.confirm", "session": "other", "ticket": "guessed"})
                self.assertFalse(result["ok"])
        asyncio.run(scenario())
