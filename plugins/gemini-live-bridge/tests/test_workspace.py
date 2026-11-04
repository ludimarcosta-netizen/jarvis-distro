"""Authenticated plugin entry point and voice use the same local executor."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import plugin_api as pa

source = Path(__file__).resolve().parents[4]
if not (source / "local_files.py").is_file():
    source = Path(__file__).resolve().parents[3] / "jarvis"
sys.path.insert(0, str(source))
from local_files import FilesSession
from local_workspace import Workspace


class WorkspaceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "relatorio.txt").write_text("teste sem nuvem", encoding="utf-8")
        self.service = Workspace(lambda: FilesSession([root]))
        self.module = SimpleNamespace(for_profile=lambda home: self.service)
        self.loader = patch.object(pa, "_jarvis_module", return_value=self.module)
        self.loader.start()
        self.addCleanup(self.loader.stop)
        pa._voice_files_session = ""

    def test_endpoint_initializes_then_searches_and_rejects_arbitrary_paths(self):
        async def scenario():
            session = (await pa.local_workspace({"action": "start"}))["session"]
            result = await pa.local_workspace({"action": "files.search", "session": session, "query": "relatorio"})
            denied = await pa.local_workspace({"action": "files.preview", "session": session, "ref": "../../secret.txt"})
            return result, denied

        result, denied = asyncio.run(scenario())
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["name"], "relatorio.txt")
        self.assertFalse(denied["ok"])
        self.assertFalse(asyncio.run(pa.local_workspace({"action": []}))["ok"])

    def test_voice_search_then_preview_and_close_clears_reference_scope(self):
        async def scenario():
            with patch.object(pa, "_send_tool_response", new_callable=AsyncMock) as reply:
                await pa._run_files({"id": "search", "args": {"acao": "buscar", "termo": "relatorio"}})
                await pa._run_files({"id": "preview", "args": {"acao": "previa", "numero": 1}})
                self.assertNotIn("teste sem nuvem", reply.call_args.args[2])
                self.assertIn("não foi lido nem enviado", reply.call_args.args[2])
                with patch.object(pa, "_cleanup", new_callable=AsyncMock):
                    await pa.close()
                self.assertEqual(pa._voice_files_session, "")
                await pa._run_files({"id": "stale", "args": {"acao": "abrir", "numero": 1}})
                self.assertIn('"ok": false', reply.call_args.args[2])

        asyncio.run(scenario())
