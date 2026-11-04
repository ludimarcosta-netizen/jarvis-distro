"""Revisão local: contexto, preservação, falhas, omissões e persistência."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""
os.environ["JARVIS_NOTIFICACOES"] = "0"

import audio_archive as archive
import audio_review as review
import meetings_offline as off
import meetings_repo as repo
import meetings_service as svc
import offline_transcribe as ot
import storage
import web_app


def word(text, start, end, probability=0.9):
    return Obj(word=text, start=start, end=end, probability=probability)


def segment(words, logprob=-0.1, compression=1.0):
    return Obj(words=words, text="".join(w.word for w in words), avg_logprob=logprob,
               compression_ratio=compression, no_speech_prob=0.1)


class ReviewTest(unittest.TestCase):
    def test_relistens_with_context_but_excludes_neighbouring_words_and_keeps_original(self):
        model = Mock()
        model.transcribe.return_value = (iter([segment([
            word(" antes", 1, 2), word(" contrato", 5, 6), word(" depois", 9, 10)])]), None)
        audio = np.zeros(20 * review.RATE, np.float32)
        u = {"start": 10, "end": 12, "text": "contato", "review_reasons": ["palavra incerta"]}
        with tempfile.TemporaryDirectory() as tmp:
            review.review_utterances(model, audio, [u], Path(tmp), "Jarvis")
            self.assertEqual(u["text"], "contato")
            self.assertEqual(u["review"]["original_text"], "contato")
            self.assertEqual(u["review"]["alternative_text"], "contrato")
            self.assertEqual(u["review"]["status"], "pending")
            self.assertFalse(u["review"]["agreement"])
            with wave.open(str(Path(tmp) / u["review"]["sample"])) as wav:
                self.assertEqual(wav.getnframes() / wav.getframerate(), 12)
        self.assertEqual(model.transcribe.call_args.kwargs["beam_size"], 8)
        self.assertFalse(model.transcribe.call_args.kwargs["condition_on_previous_text"])
        self.assertNotIn("initial_prompt", model.transcribe.call_args.kwargs)

    def test_nearby_fragments_share_one_decode_but_keep_individual_alternatives(self):
        model = Mock()
        model.transcribe.return_value = (iter([segment([
            word(" contrato", 5, 6), word(" prazo", 7, 8)])]), None)
        utterances = [{"start": 10, "end": 12, "text": "contato", "review_reasons": ["palavra incerta"]},
                      {"start": 12, "end": 14, "text": "prato", "review_reasons": ["palavra incerta"]}]
        with tempfile.TemporaryDirectory() as tmp:
            review.review_utterances(model, np.zeros(30 * review.RATE, np.float32), utterances, Path(tmp), "")
            self.assertEqual(len(list(Path(tmp).glob('*.wav'))), 1)
        self.assertEqual(model.transcribe.call_count, 1)
        self.assertEqual([u['text'] for u in utterances], ['contato', 'prato'])
        self.assertEqual([u['review']['alternative_text'] for u in utterances], ['contrato', 'prazo'])
        self.assertTrue(all(u['review']['status'] == 'pending' for u in utterances))

    def test_shared_review_never_exceeds_window_limit_or_merges_distant_audio(self):
        model = Mock()
        model.transcribe.side_effect = lambda *a, **k: (iter([]), None)
        utterances = [{"start": t, "end": t + 20, "text": "original", "review_reasons": ["baixa confiança"]}
                      for t in [10, 31, 80]]
        with tempfile.TemporaryDirectory() as tmp:
            review.review_utterances(model, np.zeros(110 * review.RATE, np.float32), utterances, Path(tmp), "")
        self.assertEqual(model.transcribe.call_count, 3)
        self.assertTrue(all(len(c.args[0]) <= review.MAX_REVIEW_S * review.RATE
                            for c in model.transcribe.call_args_list))

    def test_agreement_does_not_automatically_confirm_and_good_speech_is_not_reprocessed(self):
        model = Mock()
        model.transcribe.return_value = (iter([segment([word(" Bom dia!", 1, 2)])]), None)
        uncertain = {"start": 1, "end": 3, "text": "Bom dia", "review_reasons": ["baixa confiança"]}
        good = {"start": 4, "end": 5, "text": "certo", "review_reasons": []}
        with tempfile.TemporaryDirectory() as tmp:
            review.review_utterances(model, np.zeros(8 * review.RATE, np.float32), [uncertain, good], Path(tmp), "")
        self.assertTrue(uncertain["review"]["agreement"])
        self.assertEqual(uncertain["review"]["status"], "pending")
        self.assertNotIn("review", good)
        self.assertEqual(model.transcribe.call_count, 1)

    def test_gpu_failure_keeps_original_and_audio_available(self):
        model = Mock()
        model.transcribe.side_effect = RuntimeError("GPU failure")
        u = {"start": 0, "end": 2, "text": "original", "review_reasons": ["baixa confiança"]}
        with tempfile.TemporaryDirectory() as tmp:
            review.review_utterances(model, np.zeros(3 * review.RATE, np.float32), [u], Path(tmp), "")
            self.assertTrue((Path(tmp) / u["review"]["sample"]).exists())
        self.assertEqual(u["text"], "original")
        self.assertEqual(u["review"]["error"], "RuntimeError")

    def test_uncertain_words_and_repetition_trigger_review(self):
        self.assertIn("palavra incerta", review.suspicion(segment([word("x", 0, 1, 0.1)])))
        self.assertIn("texto repetitivo", review.suspicion(segment([], compression=3)))
        self.assertEqual(review.suspicion(segment([word(" contrato", 0, 1)])), [])

    def test_missing_speech_has_bounded_windows_and_no_duplicate_overlap(self):
        turns = [{"start": 0, "end": 60, "speaker": 0}, {"start": 10, "end": 20, "speaker": 1}]
        missing = ot.missing_speech(turns, [{"start": 0, "end": 5}])
        self.assertEqual([(u["start"], u["end"]) for u in missing], [(5, 33), (33, 60)])
        self.assertTrue(all(u["text"] == "" for u in missing))

    def test_transcribes_whole_audio_even_when_diarization_detects_nothing(self):
        model = Mock()
        model.transcribe.return_value = (iter([segment([word(" palavra", 2, 3)])]), None)
        with patch.object(ot, "decode_audio", return_value=np.zeros(8 * review.RATE, np.float32)), \
                patch.object(ot, "WhisperModel", return_value=model), \
                patch.object(ot, "run_diarization", return_value={"turns": [], "speakers": {}}):
            data = ot.transcribe_meeting(Path("unused.wav"))
        self.assertEqual(data["utterances"][0]["text"], "palavra")
        self.assertEqual(data["utterances"][0]["speaker"], -1)
        self.assertIn("-1", data["speakers"])
        self.assertEqual(len(model.transcribe.call_args.args[0]), 8 * review.RATE)
        self.assertNotIn('initial_prompt', model.transcribe.call_args.kwargs)
        self.assertFalse(model.transcribe.call_args.kwargs['condition_on_previous_text'])
        self.assertTrue(model.transcribe.call_args.kwargs['vad_filter'])

    def test_short_unidentified_speech_stays_unknown(self):
        words = [{"start": i * 0.5, "end": i * 0.5 + 0.4, "speaker": -1 if i == 4 else 0}
                 for i in range(10)]
        ot.smooth_speakers(words)
        self.assertEqual(words[4]["speaker"], -1)


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "review.db")
        storage.init_db(self.db)
        self.mid = repo.create_meeting("2026-10-01 10:00:00", "2026-10-01 10:10:00", status="closed", db_path=self.db)
        self.review = {"original_text": "contato", "alternative_text": "contrato", "status": "pending",
                       "sample_file": "review.wav", "reasons": ["palavra incerta"]}
        repo.save_offline_result(self.mid, [{"source": "mic", "label": 0}],
                                 [{"source": "mic", "label": 0, "start_at": "2026-10-01 10:00:00",
                                   "end_at": "2026-10-01 10:00:02", "text": "contato", "review": self.review}], self.db)
        self.uid = repo.meeting_utterances(self.mid, self.db)[0]["id"]

    def test_confirmation_preserves_original_and_requeues_ata(self):
        repo.resolve_review(self.mid, self.uid, "alternative", "", self.db)
        row = repo.meeting_utterances(self.mid, self.db)[0]
        self.assertEqual(row["text"], "contrato")
        self.assertEqual(row["review"]["original_text"], "contato")
        self.assertEqual(row["review"]["status"], "resolved")
        self.assertEqual(repo.get_meeting(self.mid, self.db)["ata_status"], "pending")

    def test_other_meeting_cannot_modify_review_and_processing_is_locked(self):
        other = repo.create_meeting("2026-10-01 11:00:00", "2026-10-01 11:10:00", status="closed", db_path=self.db)
        with self.assertRaises(ValueError):
            repo.resolve_review(other, self.uid, "custom", "alterado", self.db)
        repo.update_meeting(self.mid, db_path=self.db, offline_status="running")
        with self.assertRaises(ValueError):
            repo.resolve_review(self.mid, self.uid, "original", "", self.db)
        self.assertEqual(repo.meeting_utterances(self.mid, self.db)[0]["text"], "contato")

    def test_cleanup_returns_review_clip_and_migration_is_idempotent(self):
        storage.init_db(self.db)
        self.assertEqual(repo.get_review_sample(self.uid, self.db), "review.wav")
        self.assertEqual(repo.clear_offline(self.mid, self.db), ["review.wav"])

    def test_retention_protects_pending_review_and_failed_processing(self):
        windows = archive.protected_windows(Path(self.db))
        self.assertEqual(len(windows), 1)
        repo.resolve_review(self.mid, self.uid, "original", "", self.db)
        self.assertEqual(archive.protected_windows(Path(self.db)), [])
        repo.update_meeting(self.mid, db_path=self.db, offline_status="error")
        self.assertEqual(len(archive.protected_windows(Path(self.db))), 1)

    def test_cleanup_keeps_audio_until_review_resolved(self):
        start, _ = archive.protected_windows(Path(self.db))[0]
        folder = Path(self.tmp.name) / "archive" / "mic"
        folder.mkdir(parents=True)
        from datetime import datetime
        path = folder / (datetime.fromtimestamp(start + 60).astimezone().strftime('%Y%m%d-%H%M%S') + '.flac')
        path.write_bytes(b'audio')
        os.utime(path, (0, 0))
        self.assertEqual(archive.purge(folder.parent, now=2000000000, db_path=Path(self.db)), 0)
        repo.resolve_review(self.mid, self.uid, "original", "", self.db)
        self.assertEqual(archive.purge(folder.parent, now=2000000000, db_path=Path(self.db)), 1)

    def test_audio_and_confirmation_endpoints_require_token_and_preserve_provenance(self):
        samples = Path(self.tmp.name) / "samples"
        samples.mkdir()
        review.write_clip(np.zeros(review.RATE, np.float32), samples / "review.wav")
        server = web_app.make_server(port=0, db_path=self.db, token="t" * 40)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, body=None, token=True):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            headers = {"X-Jarvis-Token": "t" * 40} if token else {}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            data = response.read()
            status = response.status
            conn.close()
            return status, data
        with patch.object(off, "SAMPLES_DIR", samples), patch.object(web_app, "_deferred", side_effect=lambda db, x: x):
            path = f"/api/utterances/{self.uid}/audio"
            self.assertEqual(request("GET", path, token=False)[0], 403)
            code, body = request("GET", path)
            self.assertEqual(code, 200)
            self.assertTrue(body.startswith(b"RIFF"))
            self.assertEqual(request("GET", "/api/utterances/999999/audio")[0], 404)
            path = f"/api/meetings/{self.mid}/review"
            self.assertEqual(request("POST", path, {})[0], 400)
            self.assertEqual(request("POST", path, {"utterance_id": self.uid, "choice": "alternative"})[0], 200)
            self.assertEqual(repo.meeting_utterances(self.mid, self.db)[0]["text"], "contrato")
            self.assertEqual(svc.voice_view(self.mid, self.db)["utterances"][0]["review"]["original_text"], "contato")

    def test_pipeline_keeps_review_audio_and_does_not_filter_uncertain_original(self):
        source_samples = Path(self.tmp.name) / "source"
        destination = Path(self.tmp.name) / "saved"
        source_samples.mkdir()
        review.write_clip(np.zeros(review.RATE, np.float32), source_samples / "review_0.wav")
        r = {**self.review, "sample": "review_0.wav"}
        r.pop("sample_file")
        result = {"mic": {"samples": source_samples, "data": {
            "speakers": {"-1": {"seconds": 1, "embedding": []}},
            "utterances": [{"speaker": -1, "start": 0, "end": 1, "text": "", "review": r}]}}}
        with patch.object(off, "SAMPLES_DIR", destination):
            self.assertEqual(off._store(self.mid, 0, result, self.db), 1)
            u = repo.meeting_utterances(self.mid, self.db)[0]
            self.assertTrue((destination / u["review"]["sample_file"]).exists())
            self.assertEqual(u["text"], "")


if __name__ == "__main__":
    unittest.main()
