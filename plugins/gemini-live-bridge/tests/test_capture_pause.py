"""Controle explícito de pausa da captação de áudio."""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import plugin_api as pa


class CapturePauseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        state = dict(pa._state)
        paused = pa._capture_paused
        self.addCleanup(lambda: pa._state.update(state))
        self.addCleanup(setattr, pa, "_capture_paused", paused)
        pa._capture_paused = False
        pa._state.update(session=None, ws=None, task=None, farewell_task=None, audio=[])
        for name, replacement in [
            ("_listen_only", Mock(return_value=False)),
            ("_sentinel_state", Mock(return_value={"alive": True, "muted": True})),
            ("_send_sentinel_command", Mock(return_value={"muted": True})),
            ("close", AsyncMock(return_value={"ok": True})),
        ]:
            patcher = patch.object(pa, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_pause_closes_live_clears_audio_and_blocks_start_and_upload(self):
        pa._state.update(audio=["queued"], wake_text="old", wake_audio_pcm16="old")
        self.assertTrue((await pa.sentinel_mute({"muted": True}))["ok"])
        pa.close.assert_awaited_once()
        pa._send_sentinel_command.assert_called_once_with("MUTE")
        self.assertTrue(pa._capture_paused)
        self.assertEqual(pa._state["audio"], [])
        self.assertEqual(pa._state["wake_text"], "")
        with patch.object(pa, "_api_key") as key:
            self.assertFalse((await pa.start())["ok"])
        key.assert_not_called()
        self.assertTrue((await pa.audio({"data": "late"}))["skipped"])
        self.assertTrue((await pa.sentinel_mute({"muted": False}))["ok"])
        self.assertFalse(pa._capture_paused)
        self.assertEqual(pa._send_sentinel_command.call_args.args, ("UNMUTE",))

    async def test_invalid_values_never_change_capture(self):
        for value in [None, "false", 0, 1]:
            self.assertFalse((await pa.sentinel_mute({"muted": value}))["ok"])
        self.assertFalse(pa._capture_paused)
        pa.close.assert_not_awaited()
        pa._send_sentinel_command.assert_not_called()

    async def test_only_listen_does_not_override_explicit_pause(self):
        pa._capture_paused = True
        with patch.object(pa, "_jarvis_module", return_value=Mock()), patch.object(pa, "_sentinel_alive", return_value=True):
            self.assertTrue((await pa.listen_only({"enabled": True}))["ok"])
        self.assertTrue(pa._capture_paused)
        pa._send_sentinel_command.assert_not_called()

    async def test_failed_resume_keeps_capture_blocked(self):
        pa._capture_paused = True
        pa._send_sentinel_command.return_value = {"error": "offline"}
        result = await pa.sentinel_mute({"muted": False})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "offline")
        self.assertTrue(pa._capture_paused)


if __name__ == "__main__":
    unittest.main()
