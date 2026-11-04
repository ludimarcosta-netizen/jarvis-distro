"""Regressão: respostas curtas também precisam produzir evidência vocal."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import offline_diarize as dia
except ModuleNotFoundError as exc:
    if exc.name != 'sherpa_onnx':
        raise
    dia = None


@unittest.skipIf(dia is None, 'Execute no Python do voice_identity (sherpa-onnx)')
class EvidenceTest(unittest.TestCase):
    def evidence(self, spans, all_spans=None, samples=None):
        with tempfile.TemporaryDirectory() as folder:
            models = Path(folder)
            (models / 'speaker-ecapa512-sherpa.onnx').write_bytes(b'test')
            audio = np.full(40 * dia.RATE, 0.1, np.float32) if samples is None else samples
            with patch.object(dia, 'MODELS', models), patch.object(
                    dia, 'speaker_embedding', return_value=[1., 0.]) as extract:
                result = dia.voice_evidence(None, audio, spans, all_spans or spans)
                return result, [len(c.args[1]) / dia.RATE for c in extract.call_args_list]

    def test_short_turns_of_same_speaker_accumulate(self):
        spans = [SimpleNamespace(start=i * 3, end=i * 3 + 2, speaker=0) for i in range(9)]
        result, windows = self.evidence(spans)
        self.assertEqual(result['windows'], 3)
        self.assertEqual(result['clean_seconds'], 15.)
        self.assertEqual(windows[:2], [5., 5.])

    def test_window_remainders_are_not_discarded(self):
        spans = [SimpleNamespace(start=i * 10, end=i * 10 + 7, speaker=0) for i in range(3)]
        result, _ = self.evidence(spans)
        self.assertEqual(result['windows'], 4)
        self.assertEqual(result['clean_seconds'], 20.)

    def test_nine_seconds_still_produce_three_independent_windows(self):
        spans = [SimpleNamespace(start=0, end=9.3, speaker=0)]
        result, windows = self.evidence(spans)
        self.assertEqual(result['windows'], 3)
        self.assertGreaterEqual(min(windows), 3.)
        self.assertGreaterEqual(result['clean_seconds'], 9.)

    def test_overlap_silence_and_clipping_never_become_evidence(self):
        spans = [SimpleNamespace(start=0, end=6, speaker=0)]
        rival = SimpleNamespace(start=0, end=6, speaker=1)
        self.assertEqual(self.evidence(spans, spans + [rival])[0]['windows'], 0)
        for value in [0., 1.]:
            result, _ = self.evidence(spans, samples=np.full(40 * dia.RATE, value, np.float32))
            self.assertEqual(result['windows'], 0)

    def test_insufficient_audio_is_not_padded_or_duplicated(self):
        spans = [SimpleNamespace(start=0, end=2, speaker=0)]
        self.assertEqual(self.evidence(spans)[0]['windows'], 0)

    def test_bad_window_does_not_discard_rest_of_long_turn(self):
        spans = [SimpleNamespace(start=0, end=25, speaker=0)]
        samples = np.full(40 * dia.RATE, .1, np.float32)
        samples[:6 * dia.RATE] = 1.
        result, _ = self.evidence(spans, samples=samples)
        self.assertGreaterEqual(result['windows'], 3)
        self.assertGreaterEqual(result['clean_seconds'], 14.)

    def test_limit_stays_at_twelve_independent_windows(self):
        spans = [SimpleNamespace(start=0, end=100, speaker=0)]
        result, windows = self.evidence(spans, samples=np.full(100 * dia.RATE, .1, np.float32))
        self.assertEqual(windows, [5.] * 12)
        self.assertEqual(result['clean_seconds'], 60.)


if __name__ == '__main__':
    unittest.main()
