import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis_live import receive_turns
from jarvis import JarvisOrchestrator


class LiveLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_receives_multiple_turns_and_propagates_disconnect(self):
        class Session:
            turn = 0

            async def receive(self):
                self.turn += 1
                if self.turn == 3:
                    raise ConnectionError("disconnected")
                yield self.turn

        responses = receive_turns(Session())
        self.assertEqual(await anext(responses), 1)
        self.assertEqual(await anext(responses), 2)
        with self.assertRaisesRegex(ConnectionError, "disconnected"):
            await anext(responses)

    async def test_capture_pause_drops_pending_and_stays_paused_until_explicit_resume(self):
        app = JarvisOrchestrator()
        self.assertFalse(app._muted)
        app.is_running = True
        app.ambient_engine = Mock()
        app.wake_detector = Mock()
        app._wake_event.set()
        self.assertTrue(app._set_muted(True)["muted"])
        self.assertFalse(app._wake_event.is_set())
        app.ambient_engine.stop.assert_called_once_with(discard_pending=True)
        app.wake_detector.stop.assert_called_once()
        app._ensure_listening()
        app._sync_desktop_session(None)
        app.ambient_engine.start.assert_not_called()
        app.wake_detector.start.assert_not_called()
        self.assertFalse(app._set_muted(False)["muted"])
        app.ambient_engine.start.assert_called_once()
        app.wake_detector.start.assert_called_once()

    async def test_manual_live_pauses_and_restores_ambient_without_unmuting(self):
        app = JarvisOrchestrator()
        app.is_running = True
        app.ambient_engine = Mock()
        app.wake_detector = Mock()
        app._sync_desktop_session("connecting")
        app._sync_desktop_session("live")
        app.ambient_engine.stop.assert_called_once()
        app.wake_detector.stop.assert_called_once()
        app._sync_desktop_session(None)
        app.ambient_engine.start.assert_called_once()
        app.wake_detector.start.assert_called_once()
        app._sync_desktop_session("live")
        app._muted = True
        app._sync_desktop_session(None)
        app.ambient_engine.start.assert_called_once()
        app.wake_detector.start.assert_called_once()
        app._signal_wake()
        self.assertFalse(app._wake_event.is_set())


if __name__ == '__main__':
    unittest.main()
