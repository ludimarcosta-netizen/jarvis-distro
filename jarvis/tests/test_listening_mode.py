"""Modo só ouvir não silencia microfone nem aciona conversa por wake word."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import listening_mode
from jarvis_live import run_live_session

from jarvis import JarvisOrchestrator


class ListeningModeTests(unittest.IsolatedAsyncioTestCase):
    def test_preference_is_saved_and_corrupt_state_stays_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mode.json'
            self.assertFalse(listening_mode.enabled(path))
            listening_mode.save(True, path)
            self.assertTrue(listening_mode.enabled(path))
            listening_mode.save(False, path)
            self.assertFalse(listening_mode.enabled(path))
            path.write_text('broken', encoding='utf-8')
            self.assertTrue(listening_mode.enabled(path))
            with self.assertRaises(ValueError):
                listening_mode.save('false', path)

    async def test_wake_and_local_fallback_do_not_interrupt_ambient_or_speak(self):
        app = JarvisOrchestrator()
        app.is_running = True
        app.ambient_engine = Mock()
        app.wake_detector = Mock()
        with patch.object(listening_mode, 'enabled', return_value=True), \
                patch('jarvis.desktop_bridge.wake_desktop') as wake, \
                patch('jarvis_live.get_live_context_for_jarvis') as context:
            app._signal_wake()
            app._on_wake_detected()
            await run_live_session()
        self.assertFalse(app._wake_event.is_set())
        self.assertFalse(app._in_live_session)
        app.ambient_engine.stop.assert_not_called()
        app.wake_detector.stop.assert_not_called()
        wake.assert_not_called()
        context.assert_not_called()
