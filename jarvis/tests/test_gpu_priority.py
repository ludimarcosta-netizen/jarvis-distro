"""A fala ao vivo passa na frente da passada offline na GPU (problema de 09/10/2026)."""
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ambient_engine
import gpu_priority
import transcript_hygiene


class FakeWhisper:
    """Imita o WhisperModel: o gerador chama encode a cada janela e não devolve nada nas silenciosas."""

    def __init__(self):
        self.encoded = []

    def encode(self, features):
        self.encoded.append(features)
        return features

    def transcribe(self, windows):
        for window in windows:
            if self.encode(window) != "silêncio":
                yield window


class YieldToLiveTests(unittest.TestCase):
    def test_offline_does_not_encode_any_window_while_live_is_busy(self):
        model = FakeWhisper()
        gpu_priority.mark_busy()
        encoded_while_waiting = []

        def sleep(_s):
            encoded_while_waiting.append(len(model.encoded))
            if len(encoded_while_waiting) == 3:
                gpu_priority.mark_idle()  # a fila ao vivo esvaziou

        gpu_priority.yield_to_live(model, sleep=sleep)
        result = list(model.transcribe(["silêncio", "silêncio", "fala"]))
        self.assertEqual(result, ["fala"])
        # Nem as janelas silenciosas (que não geram segmento) passaram enquanto o ao vivo estava ocupado.
        self.assertEqual(encoded_while_waiting, [0, 0, 0])
        self.assertEqual(model.encoded, ["silêncio", "silêncio", "fala"])

    def test_live_speech_arriving_mid_pass_pauses_the_next_window(self):
        model = FakeWhisper()
        sleep = Mock(side_effect=lambda _s: gpu_priority.mark_idle())
        gpu_priority.yield_to_live(model, sleep=sleep)
        windows = model.transcribe(["a", "b"])
        self.assertEqual(next(windows), "a")
        gpu_priority.mark_busy()  # fala ao vivo chegou entre as janelas
        self.assertEqual(next(windows), "b")
        sleep.assert_called_once()

    def test_stale_busy_signal_expires(self):
        # Processo ao vivo caiu com o sinal "ocupado": a offline não pode ficar parada para sempre.
        gpu_priority._write(time.time() - 1, None)
        sleep = Mock()
        model = gpu_priority.yield_to_live(FakeWhisper(), sleep=sleep)
        self.assertEqual(list(model.transcribe([1, 2])), [1, 2])
        sleep.assert_not_called()

    def test_unreadable_signal_counts_as_idle(self):
        gpu_priority.BUSY_FILE.write_text("lixo", encoding="ascii")
        self.assertEqual(gpu_priority.busy_until(), 0.0)


class LiveEngineSignalTests(unittest.TestCase):
    def _engine(self):
        eng = object.__new__(ambient_engine.JarvisAmbientEngine)  # sem Whisper/GPU
        eng.language = "pt"
        eng.audio_queue = queue.Queue(maxsize=20)
        eng.active_session_id = "ambient"
        eng.capture_mode = "save"
        eng.on_captured_chunk = None
        eng.on_meeting_command = None
        eng._discard_pending = False
        eng.echo_guard = transcript_hygiene.EchoGuard()
        eng.meeting_gate = Mock(active=Mock(return_value=True))
        return eng

    def test_queued_speech_marks_gpu_busy_and_drained_queue_frees_it(self):
        eng = self._engine()
        seen_during_transcription = []

        def transcribe(*_args, **_kwargs):
            seen_during_transcription.append(gpu_priority.busy_until() > time.time())
            return ""

        eng.transcriber = Mock(transcribe_audio_chunk=transcribe)
        eng._on_audio_received(np.zeros(160, dtype="float32"), "mic", 1.0)
        self.assertGreater(gpu_priority.busy_until(), time.time())

        stop = threading.Event()
        stop.set()  # drena a fila e sai
        with patch.object(ambient_engine._voice_module.client, "mode", return_value="off"):
            eng._transcription_worker(stop)

        self.assertEqual(seen_during_transcription, [True])
        self.assertLessEqual(gpu_priority.busy_until(), time.time())


if __name__ == "__main__":
    unittest.main()
