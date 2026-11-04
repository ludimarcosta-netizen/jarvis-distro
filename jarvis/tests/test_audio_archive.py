import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio_archive as archive

RATE = 16000
FRAME = 480  # 30 ms


def decoded_seconds(path: Path) -> float:
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        return sum(f.samples for f in container.decode(stream)) / stream.rate


class FakeClock:
    def __init__(self, start: float):
        self.t = start

    def __call__(self) -> float:
        return self.t


class ContinuousRecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 10:08:00 local: o bloco vira às 10:10:00
        self.clock = FakeClock(datetime(2026, 10, 1, 10, 8, 0).timestamp())

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, rec, seconds: float):
        tone = (0.1 * np.sin(np.arange(FRAME) / RATE * 2 * np.pi * 440)).astype(np.float32)
        for i in range(int(seconds * RATE / FRAME)):
            self.clock.t += FRAME / RATE
            rec.write(tone)
            while i % 1000 == 999 and rec._queue.qsize() > 100:  # mais rápido que o tempo real
                time.sleep(0.01)

    def test_rotates_on_ten_minute_boundary_and_is_playable(self):
        rec = archive.ContinuousRecorder("mic", root=self.root, clock=self.clock)
        rec.start()
        self.feed(rec, 150)  # 10:08:00 → 10:10:30
        rec.stop()
        files = sorted((self.root / "mic").glob("*.flac"))
        self.assertEqual([f.name for f in files], ["20261001-100800.flac", "20261001-101000.flac"])
        self.assertAlmostEqual(decoded_seconds(files[0]), 120, delta=0.5)
        self.assertAlmostEqual(decoded_seconds(files[1]), 30, delta=0.5)

    def test_gap_without_frames_becomes_silence(self):
        rec = archive.ContinuousRecorder("loopback", root=self.root, clock=self.clock)
        rec.start()
        self.feed(rec, 5)
        self.clock.t += 20  # PC mudo: o loopback não entregou nada
        self.feed(rec, 5)
        rec.stop()
        (only,) = (self.root / "loopback").glob("*.flac")
        self.assertAlmostEqual(decoded_seconds(only), 30, delta=0.5)

    def test_flush_makes_final_audio_readable_without_stopping_capture(self):
        rec = archive.ContinuousRecorder("mic", root=self.root, clock=self.clock)
        rec.start()
        try:
            self.feed(rec, 2)
            archive.flush_recordings()
            (first,) = (self.root / "mic").glob("*.flac")
            self.assertAlmostEqual(decoded_seconds(first), 2, delta=0.05)
            self.feed(rec, 1)
            archive.flush_recordings()
            self.assertEqual(len(list((self.root / "mic").glob("*.flac"))), 2)
            self.assertTrue(rec._thread.is_alive())
        finally:
            rec.stop()

    def test_same_second_rotation_does_not_overwrite_previous_recording(self):
        rec = archive.ContinuousRecorder("mic", root=self.root, clock=self.clock)
        rec.start()
        try:
            self.feed(rec, 0.12)
            archive.flush_recordings()
            self.feed(rec, 0.12)
            archive.flush_recordings()
            files = list((self.root / "mic").glob("*.flac"))
            self.assertEqual(len(files), 2)
            self.assertTrue(all(decoded_seconds(f) > 0.1 for f in files))
            self.assertTrue(all(archive.block_start(f) > 0 for f in files))
        finally:
            rec.stop()

    def test_purge_removes_only_blocks_older_than_retention(self):
        folder = self.root / "mic"
        folder.mkdir()
        old, recent = folder / "old.ogg", folder / "recent.ogg"
        for path in (old, recent):
            path.write_bytes(b"x")
        now = time.time()
        os.utime(old, (now - archive.RETENTION_S - 100, now - archive.RETENTION_S - 100))
        os.utime(recent, (now - 3600, now - 3600))
        self.assertEqual(archive.purge(self.root, now=now, db_path=self.root / 'isolated.db'), 1)
        self.assertEqual([p.name for p in folder.glob("*.ogg")], ["recent.ogg"])

    def test_write_never_blocks_when_queue_is_full(self):
        rec = archive.ContinuousRecorder("mic", root=self.root, clock=self.clock)  # não iniciado
        frame = np.zeros(FRAME, np.float32)
        for _ in range(rec._queue.maxsize + 10):
            rec.write(frame)
        self.assertEqual(rec._dropped, 10)

    def test_disk_failure_is_logged_and_recording_continues(self):
        rec = archive.ContinuousRecorder("mic", root=self.root, clock=self.clock)
        calls = {"n": 0}
        original = rec._open

        def flaky_open(t0, t):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disco cheio")
            original(t0, t)

        rec._open = flaky_open
        rec.start()
        self.feed(rec, 3)
        rec.stop()
        self.assertTrue(list((self.root / "mic").glob("*.flac")))


if __name__ == "__main__":
    unittest.main()
