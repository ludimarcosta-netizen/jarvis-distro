"""Nomeação automática de vozes pelos perfis cadastrados."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""
os.environ["JARVIS_NOTIFICACOES"] = "0"

import meetings_repo as repo
import storage
import voice_autoname as va


class AutonameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "t.db")
        storage.init_db(self.db)
        patcher = patch.object(va, "STATE", Path(self.tmp.name) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mid = repo.create_meeting("2026-10-05 14:00:00", "2026-10-05 14:10:00", status="closed", db_path=self.db)
        repo.update_meeting(self.mid, db_path=self.db, offline_status="done", ata_status="ok")
        repo.save_offline_result(self.mid, [
            {"source": "mic", "label": 0, "seconds": 30, "embedding": [1.0, 0.0]},
            {"source": "mic", "label": 1, "seconds": 20, "embedding": [0.0, 1.0]}], [], self.db)

    def voices(self):
        return {v["label"]: v for v in repo.meeting_voices(self.mid, self.db)}

    def test_names_confident_voices_updates_participants_and_requeues_ata(self):
        ids = {v["label"]: v["id"] for v in repo.meeting_voices(self.mid, self.db)}
        match = lambda vectors: {str(ids[0]): {"name": "Debora", "score": 0.8, "automatic": True},  # noqa: E731
                                 str(ids[1]): {"name": "João", "score": 0.55}}
        self.assertEqual(va.run(self.db, matcher=match), 1)
        voices = self.voices()
        self.assertEqual(voices[0]["name"], "Debora")
        self.assertFalse(voices[1]["name"])  # confiança baixa: só fica como sugestão
        self.assertEqual(voices[1]["suggested_name"], "João")
        meeting = repo.get_meeting(self.mid, self.db)
        self.assertIn("Debora", meeting["participants"])
        self.assertEqual(meeting["ata_status"], "pending")

    def test_never_touches_voices_the_user_named(self):
        repo.update_voice(self.voices()[0]["id"], "Wesley", None, False, self.db)
        seen = []
        va.run(self.db, matcher=lambda vectors: seen.append(set(vectors)) or {}, force=True)
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(self.voices()[0]["name"], "Wesley")

    def test_skips_when_nothing_changed(self):
        calls = []
        va.run(self.db, matcher=lambda v: calls.append(1) or {})
        va.run(self.db, matcher=lambda v: calls.append(1) or {})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
