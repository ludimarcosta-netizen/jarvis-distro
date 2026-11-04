"""R13: o diário registra quanto o Jarvis leva para começar a falar depois do operador, sem gravar texto."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import plugin_api as pa


def frame(content):
    return json.dumps({"serverContent": content})


class FirstSpeechMetricTests(unittest.TestCase):
    def run_reader(self, frames):
        ws = AsyncMock()
        ws.recv.side_effect = [*frames, asyncio.CancelledError()]
        logged = []
        with patch.object(pa, "_listen_only", return_value=False), patch.object(pa, "_capture_paused", False), \
                patch.object(pa, "_live_log", side_effect=logged.append), patch.object(pa._voice, "meeting_hint", AsyncMock()):
            pa._state.update(audio=[], input=[], output=[], manual=True, last_user_text_at=0.0, await_speech_at=0.0)
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(pa._reader(ws))
        return [line for line in logged if line.startswith("primeira fala")]

    def test_logs_once_per_user_turn_without_text(self):
        audio = {"modelTurn": {"parts": [{"inlineData": {"data": "pcm"}}]}}
        lines = self.run_reader([frame({"inputTranscription": {"text": "segredo do senhor"}}), frame(audio), frame(audio),
                                 frame({"turnComplete": True}), frame(audio)])
        self.assertEqual(len(lines), 1)
        self.assertNotIn("segredo", lines[0])
        self.assertRegex(lines[0], r"^primeira fala \d+\.\ds após a última transcrição do usuário$")

    def test_model_speech_without_user_turn_is_not_measured(self):
        audio = {"modelTurn": {"parts": [{"inlineData": {"data": "pcm"}}]}}
        self.assertEqual(self.run_reader([frame(audio)]), [])
