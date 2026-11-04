import base64
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_bridge
import ambient_engine
from ambient_engine import JarvisAmbientEngine


def drained_stop():
    """Parada já pedida: o worker só drena a fila e retorna."""
    stop = threading.Event()
    stop.set()
    return stop


class WakeVoiceTransportTests(unittest.TestCase):
    def test_desktop_bridge_sends_pcm16_with_expected_rate(self):
        pcm16 = b"\x01\x02\x03\x04"

        with patch.object(desktop_bridge, "_request", return_value=(17832, {"ok": True})) as request:
            self.assertTrue(desktop_bridge.wake_desktop("abra o youtube", pcm16=pcm16))

        request.assert_called_once_with(
            "POST",
            "/wake",
            body={
                "text": "abra o youtube",
                "audio_pcm16": base64.b64encode(pcm16).decode("ascii"),
                "audio_rate": 16000,
            },
        )

    def test_empty_wake_keeps_legacy_payload_without_audio(self):
        with patch.object(desktop_bridge, "_request", return_value=(17832, {"ok": True})) as request:
            self.assertTrue(desktop_bridge.wake_desktop(""))

        request.assert_called_once_with("POST", "/wake", body={"text": ""})

    def test_capture_worker_forwards_original_pcm_and_source(self):
        engine = JarvisAmbientEngine.__new__(JarvisAmbientEngine)
        audio = np.array([0.1, -0.2, 0.3], dtype=np.float32)
        engine.language = "pt"
        engine.is_running = False
        engine.audio_queue = queue.Queue()
        engine.audio_queue.put((audio, "mic", 0.25, None, True))
        engine._discard_pending = False
        engine._update_gpu_signal = lambda: None
        engine.capture_mode = "capture"
        engine.on_meeting_command = None
        engine.transcriber = type("Transcriber", (), {
            "transcribe_audio_chunk": staticmethod(lambda _audio, language: "abra o youtube")
        })()
        captured = []
        engine.on_captured_chunk = lambda text, pcm, source, duration: captured.append(
            (text, pcm.copy(), source, duration)
        )

        engine._transcription_worker(drained_stop())

        self.assertEqual(len(captured), 1)
        text, pcm, source, duration = captured[0]
        self.assertEqual(text, "abra o youtube")
        np.testing.assert_array_equal(pcm, audio)
        self.assertEqual(source, "mic")
        self.assertEqual(duration, 0.25)

    def test_save_worker_submits_mic_and_loopback_to_speaker_identity(self):
        engine = JarvisAmbientEngine.__new__(JarvisAmbientEngine)
        mic_audio = np.array([0.1, -0.2, 0.3], dtype=np.float32)
        loopback_audio = np.array([-0.4, 0.2, 0.1], dtype=np.float32)
        engine.language = "pt"
        engine.is_running = False
        engine.audio_queue = queue.Queue()
        engine.audio_queue.put((mic_audio, "mic", 0.25, None, True))
        engine.audio_queue.put((loopback_audio, "loopback", 0.30, None, True))
        engine._discard_pending = False
        engine._update_gpu_signal = lambda: None
        engine.capture_mode = "save"
        engine.on_captured_chunk = None
        engine.on_meeting_command = None
        engine.echo_guard = ambient_engine.EchoGuard()
        transcripts = iter(["eu sou a Debora", "o João apresentou os números"])
        engine.transcriber = type("Transcriber", (), {
            "transcribe_audio_chunk": staticmethod(lambda _audio, language: next(transcripts))
        })()

        with patch.object(ambient_engine._voice_module.client, "mode", return_value="local"), \
                patch.object(ambient_engine._voice_module.client, "submit") as submit, \
                patch.object(ambient_engine, "note_meeting_participant") as note, \
                patch.object(ambient_engine, "ignore_transcripts"), \
                patch.object(ambient_engine, "save_transcript", return_value=1):
            engine._transcription_worker(drained_stop())

        self.assertEqual(submit.call_count, 2)
        mic_payload = submit.call_args_list[0].args[0]
        loopback_payload = submit.call_args_list[1].args[0]
        self.assertEqual(mic_payload["source"], "ambient")
        self.assertEqual(loopback_payload["source"], "ambient")
        self.assertEqual(mic_payload["text"], "eu sou a Debora")
        self.assertNotIn("text", loopback_payload)
        self.assertTrue(mic_payload["data"])
        self.assertTrue(loopback_payload["data"])
        note.assert_called_once_with("eu sou a Debora")


if __name__ == "__main__":
    unittest.main()
