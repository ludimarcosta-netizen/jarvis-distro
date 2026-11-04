import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""  # testes nunca escrevem no Cérebro real
os.environ["JARVIS_NOTIFICACOES"] = "0"  # testes nunca mostram notificação do Windows

import meetings_repo as repo
import meetings_service as svc
import storage
import web_app

T0 = datetime(2026, 9, 30, 13, 0, 0)  # UTC


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def ata(title="Ata", participants=None, **extra):
    return {"title": title, "summary": "resumo", "key_decisions": [], "tasks_owner": [], "tasks_others": [],
            "commitments": [], "topics": [], "participants": participants or [], **extra}


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "transcripts.db")
        storage.init_db(self.db)
        self.extract = Mock(side_effect=lambda rows: ata(f"Ata de {len(rows)} falas"))
        self.register = Mock(return_value=[])
        self.voices = []
        heard = patch.object(svc, "_heard_voices", side_effect=lambda meeting: self.voices)
        heard.start()
        self.addCleanup(heard.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def say(self, seconds, text="fala", source="mic", duration=20.0):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO transcripts (timestamp, source, duration_seconds, text) VALUES (?, ?, ?, ?)",
                         (ts(seconds), source, duration, text))
        conn.close()

    def tick(self, seconds):
        svc.tick(now=T0 + timedelta(seconds=seconds), db_path=self.db, extract=self.extract, register=self.register)

    def kw(self):
        return {"db_path": self.db, "extract": self.extract, "register": self.register}


class MigrationTests(DbCase):
    def test_legacy_atas_become_closed_meetings_with_their_transcripts(self):
        legacy = str(Path(self.tmp.name) / "legacy.db")
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE transcripts (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
                         "timestamp DATETIME, source TEXT, duration_seconds REAL, text TEXT, processed_for_summary INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE meeting_summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
                         "start_time DATETIME, end_time DATETIME, title TEXT, summary TEXT, action_items TEXT, "
                         "key_decisions TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, topics TEXT, participants TEXT)")
            for s in (0, 30, 60, 4000):
                conn.execute("INSERT INTO transcripts (timestamp, source, text, processed_for_summary) VALUES (?, 'mic', 'x', 1)", (ts(s),))
            conn.execute("INSERT INTO meeting_summaries (session_id, start_time, end_time, title) VALUES ('block:a', ?, ?, 'Orçamento')",
                         (ts(0), ts(60)))
        conn.close()
        storage.init_db(legacy)
        storage.init_db(legacy)  # idempotente
        meetings = repo.list_meetings(db_path=legacy)
        self.assertEqual([(m["title"], m["status"], m["chunks"]) for m in meetings], [("Orçamento", "closed", 3)])
        self.assertEqual(repo.get_summary(meetings[0]["id"], legacy)["title"], "Orçamento")


class SilenceCycleTests(DbCase):
    def test_two_minutes_of_silence_asks_and_five_more_closes(self):
        for s in (0, 20, 40):
            self.say(s)
        self.tick(50)
        meeting = repo.get_active_meeting(self.db)
        self.assertEqual(meeting["status"], "open")
        self.tick(40 + 119)
        self.assertEqual(repo.get_meeting(meeting["id"], self.db)["status"], "open")
        self.tick(40 + 120)
        self.assertEqual(repo.get_meeting(meeting["id"], self.db)["status"], "prompting")
        self.assertIsNotNone(svc.pending_prompt(self.db, now=T0 + timedelta(seconds=170)))
        self.tick(40 + 120 + 299)
        self.extract.assert_not_called()
        self.tick(40 + 120 + 300)
        closed = repo.get_meeting(meeting["id"], self.db)
        self.assertEqual((closed["status"], closed["closed_by"], closed["ata_status"]), ("closed", "auto", "ok"))
        self.extract.assert_called_once()

    def test_speech_during_the_question_keeps_the_meeting_open(self):
        for s in (0, 20, 40):
            self.say(s)
        self.tick(40 + 120)
        mid = repo.get_active_meeting(self.db)["id"]
        self.say(40 + 150, "voltamos a falar")
        self.tick(40 + 151)
        meeting = repo.get_meeting(mid, self.db)
        self.assertEqual((meeting["status"], meeting["prompt_at"]), ("open", None))
        self.assertEqual(meeting["ended_at"], ts(190))

    def test_continue_postpones_the_next_question(self):
        for s in (0, 20, 40):
            self.say(s)
        self.tick(160)
        mid = repo.get_active_meeting(self.db)["id"]
        svc.continue_meeting(mid, db_path=self.db, now=T0 + timedelta(seconds=170))
        self.tick(170 + 119)
        self.assertEqual(repo.get_meeting(mid, self.db)["status"], "open")
        self.tick(170 + 120)
        self.assertEqual(repo.get_meeting(mid, self.db)["status"], "prompting")

    def test_system_audio_only_closes_without_asking(self):
        for s in (0, 20, 40):
            self.say(s, source="loopback")
        self.tick(40 + 120)
        self.assertIsNone(repo.get_active_meeting(self.db))
        self.assertEqual(repo.list_meetings(db_path=self.db)[0]["closed_by"], "auto")

    def test_isolated_noise_is_discarded(self):
        self.say(0, "tosse", duration=1.0)
        self.tick(130)
        self.assertEqual(repo.list_meetings(db_path=self.db), [])
        self.assertEqual(repo.unassigned_transcripts(self.db), [])  # ficou processada, sem reunião

    def test_speech_after_a_long_gap_starts_a_new_meeting(self):
        for s in (0, 20, 40):
            self.say(s)
        self.tick(41)
        for s in (40 + 421, 40 + 440, 40 + 460):
            self.say(s)
        self.tick(40 + 461)
        meetings = repo.list_meetings(db_path=self.db)
        self.assertEqual(len(meetings), 2)
        self.assertEqual(sorted(m["status"] for m in meetings), ["closed", "open"])

    def test_failed_ata_is_retried_later(self):
        self.extract.side_effect = RuntimeError("503 UNAVAILABLE")
        for s in (0, 20, 40):
            self.say(s, source="loopback")
        self.tick(160)
        mid = repo.list_meetings(db_path=self.db)[0]["id"]
        self.assertEqual(repo.get_meeting(mid, self.db)["ata_status"], "error")
        self.tick(161)
        self.assertEqual(self.extract.call_count, 1)  # ainda na espera
        self.extract.side_effect = lambda rows: ata("ok")
        self.tick(160 + 121)
        self.assertEqual(repo.get_meeting(mid, self.db)["ata_status"], "ok")


class EditTests(DbCase):
    def _closed_meeting(self, start, texts, title=""):
        mid = repo.create_meeting(started_at=ts(start), title=title, status="closed", db_path=self.db)
        for i, text in enumerate(texts):
            self.say(start + i * 20, text)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE transcripts SET meeting_id = ? WHERE meeting_id IS NULL", (mid,))
        conn.close()
        svc.generate_ata(mid, **self.kw())
        return mid

    def ids(self, mid):
        return [r["id"] for r in repo.meeting_transcripts(mid, self.db)]

    def test_selected_lines_go_to_a_new_meeting_and_both_atas_are_redone(self):
        mid = self._closed_meeting(0, ["a1", "a2", "b1", "b2"])
        self.extract.reset_mock()
        result = svc.move_transcripts(mid, self.ids(mid)[2:], new_title="Reunião B", **self.kw())
        self.assertEqual([r["text"] for r in repo.meeting_transcripts(mid, self.db)], ["a1", "a2"])
        target = result["target"]
        self.assertEqual((target["title"], target["started_at"]), ("Reunião B", ts(40)))
        self.assertEqual(repo.get_meeting(mid, self.db)["ended_at"], ts(20))
        self.assertEqual(self.extract.call_count, 2)

    def test_moving_everything_out_deletes_the_source_and_keeps_pendencias(self):
        a = self._closed_meeting(0, ["a1"])
        b = self._closed_meeting(1000, ["b1"])
        pend = storage.add_pendencia("Enviar ofício", ata_id=repo.get_summary(a, self.db)["id"], db_path=self.db)
        svc.move_transcripts(a, self.ids(a), target_id=b, **self.kw())
        self.assertIsNone(repo.get_meeting(a, self.db))
        row = [p for p in storage.list_pendencias(db_path=self.db) if p["id"] == pend][0]
        self.assertEqual(row["ata_id"], repo.get_summary(b, self.db)["id"])

    def test_merge_brings_lines_and_participants(self):
        a = self._closed_meeting(0, ["a1", "a2"])
        b = self._closed_meeting(300, ["b1"])
        svc.set_participants(a, ["Wesley"], db_path=self.db)
        svc.set_participants(b, ["wesley", "Celina"], db_path=self.db)
        merged = svc.merge_meetings(a, b, **self.kw())
        self.assertEqual(merged["participants"], ["Wesley", "Celina"])
        self.assertEqual(len(self.ids(a)), 3)
        self.assertIsNone(repo.get_meeting(b, self.db))

    def test_moving_lines_of_another_meeting_is_refused(self):
        a = self._closed_meeting(0, ["a1"])
        b = self._closed_meeting(500, ["b1"])
        with self.assertRaises(ValueError):
            svc.move_transcripts(a, self.ids(b), new_title="x", **self.kw())

    def test_deferred_edits_leave_atas_pending_until_generated(self):
        mid = self._closed_meeting(0, ["a1", "a2", "b1"])
        self.extract.reset_mock()
        result = svc.move_transcripts(mid, self.ids(mid)[2:], new_title="B", defer_ata=True, **self.kw())
        self.extract.assert_not_called()
        self.assertEqual({result["source"]["ata_status"], result["target"]["ata_status"]}, {"pending"})
        self.assertEqual(svc.generate_pending_atas(db_path=self.db, extract=self.extract, register=self.register), 2)
        self.assertEqual(repo.get_meeting(mid, self.db)["ata_status"], "ok")

    def test_redoing_an_ata_does_not_create_pendencias_again(self):
        mid = self._closed_meeting(0, ["a1", "a2"])
        self.assertEqual(self.register.call_count, 1)  # primeira ata registra
        svc.generate_ata(mid, **self.kw())
        self.assertEqual(self.register.call_count, 1)  # refeita: não

    def test_meeting_left_with_only_noise_loses_its_old_ata(self):
        mid = self._closed_meeting(0, ["a1"])
        self.assertIsNotNone(repo.get_summary(mid, self.db))
        storage.ignore_transcripts(self.ids(mid), "eco do prompt antigo", db_path=self.db)
        svc.generate_ata(mid, **self.kw())
        self.assertIsNone(repo.get_summary(mid, self.db))
        self.assertEqual(repo.get_meeting(mid, self.db)["ata_status"], "empty")

    def test_confirmed_participants_override_the_ata(self):
        mid = self._closed_meeting(0, ["a1"])
        svc.close_meeting(mid, participants=["Maxwell"], **self.kw())
        self.assertEqual(repo.get_summary(mid, self.db)["participants"], ["Maxwell"])


class SuggestionTests(DbCase):
    def test_suggestions_rank_ata_known_people_and_spoken_hint(self):
        mid = repo.create_meeting(started_at=ts(0), status="closed", db_path=self.db)
        for i, text in enumerate(["Wesley, manda o ofício", "Celina concorda com o Wesley", "a reunião foi com Maxwell."]):
            self.say(i * 10, text)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE transcripts SET meeting_id = ?", (mid,))
        conn.close()
        repo.upsert_summary(mid, ata(participants=["Celina", "J.A.R.V.I.S."]), ts(0), ts(20), self.db)
        names = svc.suggest_participants(mid, db_path=self.db, known_people=["Wesley", "Rogerio"])
        by_name = {s["name"]: s for s in names}
        self.assertIn("Wesley", by_name)
        self.assertIn("Celina", by_name)
        self.assertIn("Maxwell", by_name)
        self.assertNotIn("Rogerio", by_name)       # não citada
        self.assertNotIn("J.A.R.V.I.S.", by_name)  # assistente fora
        svc.set_participants(mid, ["Wesley"], db_path=self.db)
        self.assertNotIn("Wesley", {s["name"] for s in svc.suggest_participants(mid, db_path=self.db, known_people=["Wesley"])})

    def test_mentioned_names_catch_vocatives_and_mid_sentence_names(self):
        found = svc.mentioned_names([
            "Bom dia, Wesley. Vamos ver o edital do distrito industrial.",
            "Celina, você manda o ofício até sexta?",
            "Mando sim. E o Maxwell responde sobre a transferência.",
            "Então, Jarvis, anota isso. Olha, Secretaria de Saúde pediu em Outubro.",
        ])
        self.assertEqual(set(found), {"Wesley", "Celina", "Maxwell"})

    def test_people_are_read_from_user_md(self):
        text = ("Família: Esposa Joaquim Seixas Andrade (Lia, aniversário 31/08). Filhos: Odete e Fausto.\n§\n"
                "Pessoas e contatos recorrentes: Fulano Um (sócio); Beltrano Sete (Prefeito de __JARVIS_CIDADE__); Dr. Zutano.\n§\n"
                "Objetivos e projetos: Desenvolver empresa")
        names = svc.people_from_user_md(text)
        for expected in ("Joaquim Seixas Andrade", "Odete", "Fausto", "Fulano Um", "Beltrano Sete", "Zutano"):
            self.assertIn(expected, names)
        self.assertNotIn("__JARVIS_CIDADE__", names)
        self.assertNotIn("Desenvolver", names)


class WebAppTests(DbCase):
    def setUp(self):
        super().setUp()
        self.server = web_app.make_server(port=0, db_path=self.db, token="t" * 48)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def call(self, method, path, body=None, headers=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"X-Jarvis-Token": "t" * 48} if token else {}
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        res = conn.getresponse()
        data = res.read()
        return res.status, (json.loads(data) if res.getheader("Content-Type", "").startswith("application/json") else data), res

    def test_page_embeds_token_and_sends_security_headers(self):
        status, body, res = self.call("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"t" * 48, body)
        self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))

    def test_api_requires_token_local_host_and_origin(self):
        self.assertEqual(self.call("GET", "/api/meetings", token=False)[0], 403)
        self.assertEqual(self.call("GET", "/api/meetings", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/meetings/1/title", {"title": "x"},
                                   headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/meetings")[0], 200)

    def test_post_requires_json(self):
        mid = repo.create_meeting(started_at=ts(0), db_path=self.db)
        status, _b, _r = self.call("POST", f"/api/meetings/{mid}/title", None,
                                   headers={"Content-Type": "text/plain", "Content-Length": "2"})
        self.assertEqual(status, 415)

    def test_detail_and_participants_round_trip_with_utc_iso_times(self):
        mid = repo.create_meeting(started_at=ts(0), db_path=self.db)
        status, body, _ = self.call("POST", f"/api/meetings/{mid}/participants", {"participants": ["Wesley", " "]})
        self.assertEqual((status, body["meeting"]["participants"]), (200, ["Wesley"]))
        status, body, _ = self.call("GET", f"/api/meetings/{mid}")
        self.assertEqual(body["meeting"]["started_at"], "2026-09-30T13:00:00Z")

    def test_unknown_meeting_is_404_and_bad_input_400(self):
        self.assertEqual(self.call("GET", "/api/meetings/999")[0], 404)
        mid = repo.create_meeting(started_at=ts(0), db_path=self.db)
        self.assertEqual(self.call("POST", f"/api/meetings/{mid}/participants", {"participants": "Wesley"})[0], 400)


if __name__ == "__main__":
    unittest.main()


class DeleteMeetingTests(unittest.TestCase):
    def test_delete_hides_meeting_keeps_transcripts_ignored_and_pendencias(self):
        import tempfile
        from pathlib import Path
        import meetings_repo as repo
        import meetings_service as svc
        import storage
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            storage.init_db(db)
            mid = repo.create_meeting("2026-10-06 11:00:00", "2026-10-06 11:10:00", status="closed", db_path=db)
            tid = storage.save_transcript("fala qualquer da sala", "mic", 5, db_path=db)
            repo.assign_transcripts([tid], mid, processed=True, db_path=db)
            svc.delete_meeting(mid, db_path=db)
            self.assertIsNone(repo.get_meeting(mid, db))
            self.assertEqual(repo.unassigned_transcripts(db), [])  # não volta a formar outra reunião
            open_id = repo.create_meeting("2026-10-06 12:00:00", status="open", db_path=db)
            with self.assertRaises(ValueError):
                svc.delete_meeting(open_id, db_path=db)
