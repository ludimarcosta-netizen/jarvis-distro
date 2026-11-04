"""Consenso, calibração e aprendizagem de vozes de reuniões."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""
os.environ["JARVIS_NOTIFICACOES"] = "0"

import meetings_repo as repo
import meetings_service as service
import storage
import voice_autoname as auto
import web_app
from voice_matching import calibrate, match_voice, valid_vectors

PROFILES = {"Ana": [[1, 0.01], [1, 0.02], [1, 0.03]],
            "Bia": [[0.01, 1], [0.02, 1], [0.03, 1]]}
EVIDENCE = {"model": "test", "embeddings": [[1, 0], [1, 0.02], [1, 0.04]], "clean_seconds": 14, "windows": 3}


class MatchingTest(unittest.TestCase):
    def test_separable_profiles_and_consistent_windows_allow_automatic_name(self):
        calibration = calibrate(PROFILES)
        self.assertTrue(calibration["ready"])
        self.assertTrue(calibration["separable"])
        hit = match_voice(EVIDENCE, PROFILES)
        self.assertEqual(hit["name"], "Ana")
        self.assertTrue(hit["automatic"])

    def test_single_window_and_old_embeddings_only_suggest(self):
        hit = match_voice([1, 0], PROFILES)
        self.assertEqual(hit["name"], "Ana")
        self.assertFalse(hit["automatic"])
        short = {**EVIDENCE, "clean_seconds": 4}
        self.assertFalse(match_voice(short, PROFILES)["automatic"])

    def test_disagreeing_windows_never_name_automatically(self):
        mixed = {**EVIDENCE, "embeddings": [[1, 0], [1, 0], [0, 1]]}
        hit = match_voice(mixed, PROFILES)
        self.assertIsNotNone(hit)
        self.assertFalse(hit["automatic"])

    def test_similar_people_disable_automatic_matching(self):
        similar = {"Ana": [[1, 0]] * 3, "Bia": [[1, 0.01]] * 3}
        self.assertFalse(calibrate(similar)["separable"])
        self.assertIsNone(match_voice(EVIDENCE, similar))

    def test_conflicting_reference_does_not_block_three_mutually_supported_references(self):
        profiles = {**PROFILES, 'Ana': PROFILES['Ana'] + [[0, 1]]}
        measured = calibrate(profiles)['profiles']['Ana']
        self.assertTrue(measured['separable'])
        self.assertEqual(measured['reference_indices'], [0, 1, 2])
        self.assertTrue(match_voice(EVIDENCE, profiles)['automatic'])

    def test_two_supported_references_are_insufficient_despite_an_outlier(self):
        profiles = {**PROFILES, 'Ana': PROFILES['Ana'][:2] + [[0, 1]]}
        self.assertFalse(calibrate(profiles)['profiles']['Ana']['separable'])
        self.assertFalse(match_voice(EVIDENCE, profiles)['automatic'])

    def test_conflicting_query_window_still_blocks_automatic_name(self):
        profiles = {**PROFILES, 'Ana': PROFILES['Ana'] + [[0, 1]]}
        mixed = {**EVIDENCE, 'embeddings': [[1, 0], [1, 0], [0, 1]]}
        self.assertFalse(match_voice(mixed, profiles)['automatic'])

    def test_query_matching_only_excluded_reference_cannot_be_named(self):
        profiles = {'Ana': [[1, 0, 0]] * 3 + [[0, 1, 0]],
                    'Bia': [[0, .8, .6]] * 3}
        query = {**EVIDENCE, 'embeddings': [[0, 1, 0]] * 3}
        self.assertTrue(calibrate(profiles)['profiles']['Ana']['separable'])
        hit = match_voice(query, profiles)
        self.assertEqual(hit['name'], 'Ana')
        self.assertFalse(hit['automatic'])

    def test_one_registered_person_does_not_prove_a_match(self):
        self.assertFalse(match_voice(EVIDENCE, {"Ana": PROFILES["Ana"]})["automatic"])

    def test_empty_zero_and_nonfinite_vectors_are_rejected(self):
        self.assertEqual(valid_vectors([[0, 0], [float('nan'), 0], []]), [])
        self.assertIsNone(match_voice({"embeddings": []}, PROFILES))

    def test_identical_profiles_cannot_name_someone_else(self):
        identical = {name: [[1, 0]] * 3 for name in ['Ana', 'Bia', 'Cris']}
        self.assertIsNone(match_voice([1, 0.1], identical))

    def test_stable_reference_center_separates_shared_model_bias(self):
        profiles = {'Ana': [[1, 0.1, 0], [1, 0.11, 0], [1, 0.09, 0]],
                    'Bia': [[1, 0, 0.1], [1, 0, 0.11], [1, 0, 0.09]],
                    'Cris': [[1, -0.1, -0.1], [1, -0.11, -0.1], [1, -0.09, -0.1]]}
        evidence = {**EVIDENCE, 'embeddings': [[1, 0.1, 0]] * 3}
        hit = match_voice(evidence, profiles)
        self.assertEqual(hit['name'], 'Ana')
        self.assertTrue(hit['automatic'])


class LearningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "voices.db")
        storage.init_db(self.db)
        self.mid = repo.create_meeting("2026-10-08 12:00:00", "2026-10-08 12:10:00", status="closed", db_path=self.db)
        repo.update_meeting(self.mid, db_path=self.db, offline_status="done")
        repo.save_offline_result(self.mid, [{"source": "mic", "label": 0, "seconds": 14,
                                           "embedding": [1, 0], "evidence": EVIDENCE}], [], self.db)
        self.uid = repo.meeting_voices(self.mid, self.db)[0]["id"]

    def references(self):
        with closing(repo._connect(self.db)) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM meeting_voice_references")]

    def test_only_human_confirmation_learns_and_correction_replaces_name(self):
        with patch.object(service, "_record_in_cerebro"):
            service.set_voices(self.mid, [{"id": self.uid, "name": "Ana"}], self.db, confirmed=False)
            self.assertEqual(self.references(), [])
            service.set_voices(self.mid, [{"id": self.uid, "name": "Ana"}], self.db)
            self.assertEqual(self.references()[0]["name"], "Ana")
            service.set_voices(self.mid, [{"id": self.uid, "name": "Bia"}], self.db)
            self.assertEqual([r["name"] for r in self.references()], ["Bia"])

    def test_discarding_a_voice_removes_its_learned_reference(self):
        with patch.object(service, "_record_in_cerebro"):
            service.set_voices(self.mid, [{"id": self.uid, "name": "Ana"}], self.db)
            service.set_voices(self.mid, [{"id": self.uid, "name": "Ana", "discarded": True}], self.db)
        self.assertEqual(self.references(), [])

    def test_frontend_receives_quality_but_not_voice_vectors(self):
        voice = service.voice_view(self.mid, self.db)["voices"][0]
        self.assertNotIn("evidence", voice)
        self.assertEqual(voice["quality"], {"clean_seconds": 14, "windows": 3})

    def test_reprocessing_or_deleting_does_not_leave_stale_voice_references(self):
        with patch.object(service, "_record_in_cerebro"):
            service.set_voices(self.mid, [{"id": self.uid, "name": "Ana"}], self.db)
        repo.clear_offline(self.mid, self.db)
        self.assertEqual(self.references(), [])
        repo.save_offline_result(self.mid, [{"source": "mic", "label": 0, "evidence": EVIDENCE}], [], self.db)
        uid = repo.meeting_voices(self.mid, self.db)[0]["id"]
        with patch.object(service, "_record_in_cerebro"):
            service.set_voices(self.mid, [{"id": uid, "name": "Bia"}], self.db)
        repo.save_offline_result(self.mid, [], [], self.db)
        self.assertEqual(self.references(), [])

    def test_speaker_count_endpoint_requires_token_and_validates_inputs(self):
        server = web_app.make_server(port=0, db_path=self.db, token="t" * 40)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(body, token=True):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Jarvis-Token"] = "t" * 40
            conn.request("POST", f"/api/meetings/{self.mid}/speaker-counts", json.dumps(body), headers)
            response = conn.getresponse()
            status = response.status
            response.read()
            conn.close()
            return status
        self.assertEqual(request({"mic_speakers": 2}, token=False), 403)
        self.assertEqual(request({"mic_speakers": True}), 400)
        self.assertEqual(request({"unknown": 2}), 400)
        self.assertEqual(request({"mic_speakers": 2, "loopback_speakers": None}), 200)
        repo.update_meeting(self.mid, db_path=self.db, offline_status="running")
        self.assertEqual(request({"mic_speakers": 3}), 400)
        self.assertEqual(repo.get_meeting(self.mid, self.db)["mic_speakers"], 2)

    def test_autoname_requires_explicit_consensus_even_with_high_score(self):
        with patch.object(auto, "STATE", Path(self.tmp.name) / "state.json"):
            hit = {str(self.uid): {"name": "Ana", "score": 0.99}}
            self.assertEqual(auto.run(self.db, matcher=lambda v: hit), 0)
        self.assertIsNone(repo.meeting_voices(self.mid, self.db)[0]["name"])

    def test_speaker_counts_validate_per_source_and_migrations_are_repeatable(self):
        result = service.set_speaker_counts(self.mid, {"mic_speakers": 2, "loopback_speakers": 3}, self.db)
        self.assertEqual((result["mic_speakers"], result["loopback_speakers"]), (2, 3))
        for invalid in [0, 21, True, '2', 2.5]:
            with self.assertRaises(ValueError):
                service.set_speaker_counts(self.mid, {"mic_speakers": invalid}, self.db)
        storage.init_db(self.db)
        self.assertEqual(repo.get_meeting(self.mid, self.db)["mic_speakers"], 2)


if __name__ == "__main__":
    unittest.main()
