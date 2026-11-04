"""Guarda de áudio de reunião: só som de aplicativo de reunião entra na reunião."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""

import meeting_gate as gate
import meetings_offline as off
import storage


class ClassifyTest(unittest.TestCase):
    def test_native_meeting_app_playing_opens(self):
        self.assertEqual(gate.meeting_app_playing([("zoom.exe", 1)], {}), "zoom.exe")

    def test_idle_meeting_app_does_not(self):
        self.assertIsNone(gate.meeting_app_playing([("discord.exe", 0)], {}))

    def test_youtube_and_whatsapp_do_not(self):
        sessions = [("chrome.exe", 1), ("whatsapp.exe", 1), ("spotify.exe", 1)]
        self.assertIsNone(gate.meeting_app_playing(sessions, {"chrome.exe": ["Video - YouTube - Google Chrome"]}))

    def test_browser_needs_meeting_tab(self):
        titles = {"chrome.exe": ["Reunião semanal - Google Meet - Google Chrome"]}
        self.assertEqual(gate.meeting_app_playing([("chrome.exe", 1)], titles), "chrome.exe")


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "t.db")
        self.now = 1000.0
        self.sessions = []

    def make(self, **kw):
        return gate.MeetingAudioGate(self.db, hold_s=30, sessions=lambda: self.sessions, titles=lambda: {},
                                     clock=lambda: self.now, **kw)

    def test_hold_then_close_and_intervals_recorded(self):
        g = self.make()
        self.sessions = [("zoom.exe", 1)]
        g.poll()
        self.assertTrue(g.active())
        self.sessions, self.now = [], 1020.0
        g.poll()
        self.assertTrue(g.active())  # ainda segurando
        self.now = 1040.0
        g.poll()
        self.assertFalse(g.active())
        kept = gate.kept_intervals(990, 1100, self.db)
        self.assertTrue(any(s <= 1000 <= e for s, e in kept))
        self.assertFalse(any(s <= 1030 <= e for s, e in kept))  # coberto, sem reunião: fora

    def test_fail_open_when_unreadable(self):
        def boom():
            raise OSError("sem core audio")
        g = gate.MeetingAudioGate(self.db, sessions=boom, titles=lambda: {}, clock=lambda: self.now)
        g.poll()
        self.assertTrue(g.active())

    def test_unknown_period_is_kept(self):
        self.assertEqual(gate.kept_intervals(0, 50, self.db), [(0, 50)])


class AssembleMaskTest(unittest.TestCase):
    def test_audio_outside_keep_is_silenced(self):
        import av
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            (root / "loopback").mkdir(parents=True)
            # reaproveita o gerador de bloco do teste da passada offline
            sys.path.insert(0, str(Path(__file__).parent))
            from test_offline import E0, write_block
            write_block(root, "loopback", E0, tone_at=3.0, seconds=20)
            out = Path(tmp) / "x.wav"
            self.assertFalse(off.assemble("loopback", E0, E0 + 20, out, root, keep=[(E0 + 10, E0 + 20)]))
            self.assertTrue(off.assemble("loopback", E0, E0 + 20, out, root, keep=[(E0, E0 + 6)]))


if __name__ == "__main__":
    unittest.main()
