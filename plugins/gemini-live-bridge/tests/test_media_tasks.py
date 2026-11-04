"""Voice media selection and background task-result lifetime contracts."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import plugin_api as pa

source = Path(__file__).resolve().parents[4]
if not (source / "local_media.py").is_file():
    source = Path(__file__).resolve().parents[3] / "jarvis"
sys.path.insert(0, str(source))
from local_files import FilesSession
from local_media import MediaSession
from local_workspace import Workspace


class VoiceMediaTests(unittest.TestCase):
    def test_spotify_search_does_not_claim_playback_or_click_coordinates(self):
        with patch.object(pa._abrir, "_launch") as launch:
            result = pa._abrir.play_spotify("música de teste")
        self.assertTrue(result["ok"])
        self.assertIn("reprodução ainda não foi confirmada", result["mensagem"])
        launch.assert_called_once_with("spotify:search:m%C3%BAsica%20de%20teste", False)

    def test_voice_uses_same_reference_executor_and_close_expires_scope(self):
        async def scenario(root):
            opened = Mock()
            service = Workspace(lambda: FilesSession([root]), media_factory=lambda: MediaSession(
                fetch=lambda query: [{"id": "tRsQsTMvPNg", "title": "Música", "channel": "Anthropic"}], opener=opened))
            pa._voice_media_session = ""
            with patch.object(pa, "_local_file_workspace", return_value=service), \
                    patch.object(pa, "_send_tool_response", new_callable=AsyncMock) as reply:
                await pa._run_media({"id": "search", "args": {"acao": "buscar", "termo": "música"}})
                opened.assert_not_called()
                self.assertTrue(json.loads(reply.call_args.args[2])["results"])
                await pa._run_media({"id": "choose", "args": {"acao": "abrir", "numero": 1}})
                opened.assert_called_once_with("https://www.youtube.com/watch?v=tRsQsTMvPNg")
                with patch.object(pa, "_cleanup", new_callable=AsyncMock):
                    await pa.close()
                self.assertEqual(pa._voice_media_session, "")
                await pa._run_media({"id": "stale", "args": {"acao": "abrir", "numero": 1}})
                self.assertFalse(json.loads(reply.call_args.args[2])["ok"])
                opened.assert_called_once()
        with tempfile.TemporaryDirectory() as root:
            asyncio.run(scenario(Path(root)))

    def test_task_result_remains_visible_after_voice_session_changes_without_speaking(self):
        async def scenario():
            session = "original"
            pa._state["session"] = "live"
            def work(request):
                pa._voice.session = "new"
                return "resultado verificado"
            with patch.object(pa._voice, "session", session), \
                    patch.object(pa._voice, "permit", new_callable=AsyncMock, return_value=True), \
                    patch.object(pa._voice, "requires_screen", return_value=False), \
                    patch.object(pa, "_hermes_turn", side_effect=work), \
                    patch.object(pa, "_live_log"), \
                    patch.object(pa._pdf, "is_pdf_request", return_value=False), \
                    patch.object(pa, "_send_tool_response", new_callable=AsyncMock) as reply:
                await pa._guarded_delegation("one", "consulte o resultado", time.time())
                self.assertEqual(pa._state["delegation"]["status"], "done")
                self.assertEqual(pa._state["delegation"]["reply"], "resultado verificado")
                reply.assert_not_called()
        asyncio.run(scenario())

    def test_uncertain_task_failure_does_not_leak_details_or_retry(self):
        async def scenario():
            pa._state["session"] = "live"
            with patch.object(pa._voice, "permit", new_callable=AsyncMock, return_value=True), \
                    patch.object(pa._voice, "requires_screen", return_value=False), \
                    patch.object(pa, "_hermes_turn", side_effect=RuntimeError("private-secret")) as work, \
                    patch.object(pa, "_live_log"), \
                    patch.object(pa._pdf, "is_pdf_request", return_value=False), \
                    patch.object(pa, "_send_tool_response", new_callable=AsyncMock) as reply:
                await pa._guarded_delegation("one", "consulte o resultado", time.time())
                self.assertEqual(pa._state["delegation"]["status"], "uncertain")
                self.assertNotIn("private-secret", reply.call_args.args[2])
                self.assertIn("antes de repetir", reply.call_args.args[2])
                work.assert_called_once()
        asyncio.run(scenario())
