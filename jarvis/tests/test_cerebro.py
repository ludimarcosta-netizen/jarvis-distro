import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""  # testes nunca escrevem no Cérebro real
os.environ["JARVIS_NOTIFICACOES"] = "0"  # testes nunca mostram notificação do Windows

import cerebro
import meetings_repo as repo
import meetings_service as svc
import storage



def meeting(mid=7, participants=("Wesley",), title="Orçamento da Saúde", started="2026-10-03 13:00:00"):
    return {"id": mid, "title": title, "started_at": started, "ended_at": started, "participants": list(participants)}


def ata(**extra):
    return {"title": "Ata", "summary": "Definiram o cronograma do edital.", "tasks_others": [], **extra}


class VaultCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "pessoas").mkdir()
        (self.vault / "_mapa.md").write_text(
            "# Mapa\n\n## Pessoas (`pessoas/`)\n\n_(vazio)_\n\n## Projetos (`projetos/`)\n\n_(vazio)_\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def note(self, name, frontmatter="", body="## Histórico\n\n- 2026-09-01 — conhecido (fonte: conversa)\n"):
        path = self.vault / "pessoas" / f"{name}.md"
        path.write_text(f"---\nnome: {name}\n{frontmatter}voz_id: \nultima_interacao: 2026-09-01\n---\n\n# {name}\n\n"
                        f"## Quem é\n\nColega.\n\n{body}", encoding="utf-8")
        return path

    def record(self, m=None, a=None, **kw):
        return cerebro.record_meeting(m or meeting(), a or ata(), vault=self.vault, **kw)


class RecordMeetingTests(VaultCase):
    def test_new_participant_gets_note_and_map_line(self):
        done = self.record(a=ata(tasks_others=["Wesley: enviar planilha (sexta)", "Celina: ligar"]))
        self.assertEqual(done["criadas"], ["Wesley"])
        text = (self.vault / "pessoas" / "Wesley.md").read_text(encoding="utf-8")
        self.assertIn("nome: Wesley", text)
        self.assertIn("criado: 2026-10-03", text)
        self.assertIn("_(a completar — participou da reunião “Orçamento da Saúde” em 03/10/2026)_", text)
        self.assertIn("- 2026-10-03 — [[2026-10-03 Orçamento da Saúde (reunião 7)|reunião “Orçamento da Saúde”]]: "
                      "Definiram o cronograma do edital. Tarefa: enviar planilha (sexta). (fonte: reunião #7)", text)
        self.assertNotIn("Celina", text)
        mapa = (self.vault / "_mapa.md").read_text(encoding="utf-8")
        self.assertIn("## Pessoas (`pessoas/`)\n\n- [[Wesley]] — a completar", mapa)
        self.assertEqual(mapa.count("_(vazio)_"), 1)  # só a seção de projetos continua vazia

    def test_same_meeting_is_not_recorded_twice(self):
        self.record()
        done = self.record()
        self.assertEqual(done, {"criadas": [], "atualizadas": [], "ambiguas": []})
        self.assertEqual((self.vault / "pessoas" / "Wesley.md").read_text(encoding="utf-8").count("reunião #7"), 1)

    def test_existing_note_found_by_nickname_is_appended_and_dated(self):
        path = self.note("Wesley Raul Lima", frontmatter="apelidos: [Cadu, Carlão]\n",
                         body="## Histórico\n\n- 2026-09-01 — conhecido (fonte: conversa)\n\n## Anexos\n\nnada\n")
        done = self.record(meeting(participants=["Cadu"]), voices=[{"id": "abc", "name": "Cadu", "count": 3}])
        self.assertEqual(done["atualizadas"], ["Cadu"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("- 2026-09-01 — conhecido (fonte: conversa)\n- 2026-10-03 — [[2026-10-03 Orçamento", text)
        self.assertIn("(fonte: reunião #7)\n\n## Anexos", text)  # entra no fim do histórico, não do arquivo
        self.assertIn("ultima_interacao: 2026-10-03", text)
        self.assertIn("voz_id: abc", text)

    def test_meeting_note_links_resolved_people_and_keeps_ata_details(self):
        self.note("Wesley Raul Lima", frontmatter="apelidos: [Cadu]\n")
        self.record(meeting(participants=["Cadu", "Celina", "__JARVIS_USER__"]),
                    ata(key_decisions=["Edital sai dia 10"], tasks_owner=["Assinar ofício"],
                        tasks_others=["Celina: revisar minuta"]), skip_names={"__JARVIS_USER_LOWER__"})
        note = self.vault / "reunioes" / "2026-10-03 Orçamento da Saúde (reunião 7).md"
        text = note.read_text(encoding="utf-8")
        self.assertIn("reuniao_id: 7", text)
        self.assertIn("## Participantes\n\n- [[Wesley Raul Lima]]\n- [[Celina]]\n", text)  # nome da ficha, não o apelido
        self.assertNotIn("__JARVIS_USER__]]", text)
        self.assertIn("- Edital sai dia 10", text)
        self.assertIn("- __JARVIS_USER__: Assinar ofício\n- Celina: revisar minuta", text)

    def test_participant_confirmed_later_is_added_to_existing_meeting_note(self):
        self.record()
        note = self.vault / "reunioes" / "2026-10-03 Orçamento da Saúde (reunião 7).md"
        note.write_text(note.read_text(encoding="utf-8") + "\nNota manual do senhor.\n", encoding="utf-8")
        self.record(meeting(participants=["Wesley", "Celina"]))
        text = note.read_text(encoding="utf-8")
        self.assertIn("- [[Wesley]]\n- [[Celina]]\n", text)
        self.assertIn("Nota manual do senhor.", text)  # nunca reescreve
        self.assertEqual(len(list((self.vault / "reunioes").iterdir())), 1)

    def test_no_resolved_participant_creates_no_meeting_note(self):
        self.note("Debora Souza")
        self.note("Debora Lima")
        self.record(meeting(participants=["Debora"]))
        self.assertFalse((self.vault / "reunioes").exists())

    def test_unique_first_name_matches_but_ambiguous_one_is_not_written(self):
        self.note("Debora Souza")
        self.assertEqual(self.record(meeting(participants=["Debora"]))["atualizadas"], ["Debora"])
        self.note("Debora Lima")
        done = self.record(meeting(mid=8, participants=["Debora"]))
        self.assertEqual(done["ambiguas"], ["Debora"])
        self.assertNotIn("reunião #8", (self.vault / "pessoas" / "Debora Souza.md").read_text(encoding="utf-8"))

    def test_owner_and_assistant_are_skipped(self):
        done = self.record(meeting(participants=["__JARVIS_USER_FULLNAME__", "Jarvis"]), skip_names={"__JARVIS_USER_LOWER__", "jarvis"})
        self.assertEqual(done, {"criadas": [], "atualizadas": [], "ambiguas": []})
        self.assertEqual(list((self.vault / "pessoas").iterdir()), [])

    def test_older_meeting_does_not_move_last_interaction_back(self):
        path = self.note("Wesley")
        path.write_text(path.read_text(encoding="utf-8").replace("2026-09-01\n---", "2026-12-01\n---"), encoding="utf-8")
        self.record()
        self.assertIn("ultima_interacao: 2026-12-01", path.read_text(encoding="utf-8"))

    def test_missing_vault_disables_everything(self):
        with patch.dict(os.environ, {"JARVIS_CEREBRO_DIR": str(self.vault / "nao-existe")}):
            self.assertIsNone(cerebro.vault_dir())
        with patch.dict(os.environ, {"JARVIS_CEREBRO_DIR": str(self.vault)}):
            self.assertEqual(cerebro.vault_dir(), self.vault)


class MeetingFlowTests(VaultCase):
    def setUp(self):
        super().setUp()
        self.db = str(self.vault / "transcripts.db")
        storage.init_db(self.db)
        self.mid = repo.create_meeting(started_at="2026-10-03 13:00:00", status="closed", db_path=self.db)
        storage.save_transcript("Wesley, manda a planilha", db_path=self.db)
        repo.assign_transcripts([1], self.mid, db_path=self.db)
        env = patch.dict(os.environ, {"JARVIS_CEREBRO_DIR": str(self.vault)})
        heard = patch.object(svc, "_heard_voices", return_value=[])
        for p in (env, heard):
            p.start()
            self.addCleanup(p.stop)

    def test_ata_with_confirmed_participant_writes_the_note(self):
        repo.update_meeting(self.mid, db_path=self.db, participants=["Wesley"])
        svc.generate_ata(self.mid, db_path=self.db, extract=lambda rows: ata(title="Planilha"), register=lambda *a, **k: [])
        self.assertIn(f"(fonte: reunião #{self.mid})",
                      (self.vault / "pessoas" / "Wesley.md").read_text(encoding="utf-8"))

    def test_participants_confirmed_after_the_ata_are_recorded_too(self):
        svc.generate_ata(self.mid, db_path=self.db, extract=lambda rows: ata(title="Planilha"), register=lambda *a, **k: [])
        self.assertEqual(list((self.vault / "pessoas").iterdir()), [])  # palpite da ata não vira ficha
        svc.set_participants(self.mid, ["Wesley"], db_path=self.db)
        self.assertTrue((self.vault / "pessoas" / "Wesley.md").exists())

    def test_cerebro_failure_never_breaks_the_ata(self):
        repo.update_meeting(self.mid, db_path=self.db, participants=["Wesley"])
        with patch.object(cerebro, "record_meeting", side_effect=OSError("OneDrive offline")):
            ok = svc.generate_ata(self.mid, db_path=self.db, extract=lambda rows: ata(), register=lambda *a, **k: [])
        self.assertTrue(ok)
        self.assertEqual(repo.get_meeting(self.mid, self.db)["ata_status"], "ok")

    def test_recognized_voice_becomes_a_suggestion(self):
        svc._heard_voices.return_value = [{"id": "abc", "name": "Celina", "count": 4},
                                          {"id": "def", "name": "Olivia", "count": 1}]
        names = {s["name"]: s["reason"] for s in svc.suggest_participants(self.mid, db_path=self.db, known_people=[])}
        self.assertEqual(names.get("Celina"), "voz reconhecida")
        self.assertNotIn("Olivia", names)  # um trecho só não basta


if __name__ == "__main__":
    unittest.main()


class AskParticipantsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "transcripts.db")
        storage.init_db(self.db)
        self.mid = repo.create_meeting(started_at="2026-10-03 13:00:00", status="closed", db_path=self.db)
        self.voices = []
        self.sent = []
        for p in (patch.object(svc, "_heard_voices", side_effect=lambda m: self.voices),
                  patch.object(svc, "_notify", side_effect=lambda *a: self.sent.append(a) or True)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def talk(self, texts, seconds):
        for text in texts:
            tid = storage.save_transcript(text, source="mic", duration=seconds, db_path=self.db)
            repo.assign_transcripts([tid], self.mid, db_path=self.db)

    def ata(self, **extra):
        svc.generate_ata(self.mid, db_path=self.db, extract=lambda rows: ata(title="Obras", **extra),
                         register=lambda *a, **k: [])

    def test_recognized_voice_triggers_the_question_with_link_and_suggestions(self):
        self.talk(["bom dia"], 20)
        self.voices = [{"id": "f", "name": "Celina", "count": 3}]
        self.ata()
        self.assertEqual(len(self.sent), 1)
        title, body, url, button = self.sent[0]
        self.assertEqual(title, "Com quem foi a reunião “Obras”?")
        self.assertIn("Sugestões: Celina.", body)
        self.assertEqual(url, f"http://127.0.0.1:17850/#{self.mid}")

    def test_long_talk_with_someone_named_triggers_but_monologue_does_not(self):
        self.talk(["Então eu acho que o projeto anda"] * 4, 90)  # 6 min, ninguém citado
        self.ata()
        self.assertEqual(self.sent, [])
        mid2 = repo.create_meeting(started_at="2026-10-03 15:00:00", status="closed", db_path=self.db)
        self.mid = mid2
        self.talk(["Wesley, manda o ofício hoje"] * 4, 90)
        self.ata()
        self.assertEqual(len(self.sent), 1)

    def test_short_talk_already_confirmed_or_redone_ata_does_not_ask(self):
        self.talk(["Wesley, oi"], 30)  # curta e sem voz cadastrada
        self.ata()
        self.assertEqual(self.sent, [])
        self.voices = [{"id": "f", "name": "Celina", "count": 3}]
        self.ata()  # ata refeita: não é a primeira, não pergunta de novo
        self.assertEqual(self.sent, [])
        mid2 = repo.create_meeting(started_at="2026-10-03 15:00:00", status="closed", participants=["Celina"],
                                   db_path=self.db)
        self.mid = mid2
        self.talk(["oi"], 30)
        self.ata()  # participantes já confirmados
        self.assertEqual(self.sent, [])

    def test_notification_failure_never_breaks_the_ata(self):
        self.talk(["oi"], 30)
        self.voices = [{"id": "f", "name": "Celina", "count": 3}]
        with patch.object(svc, "_notify", side_effect=OSError("sem área de trabalho")):
            self.ata()
        self.assertEqual(repo.get_meeting(self.mid, self.db)["ata_status"], "ok")


class ToastTests(unittest.TestCase):
    def test_toast_xml_escapes_text_and_keeps_link(self):
        import notifier
        xml = notifier.toast_xml('Reunião "A&B" <x>', "corpo", "http://127.0.0.1:17850/#5", "Informar")
        self.assertIn('<text>Reunião "A&amp;B" &lt;x&gt;</text>', xml)
        self.assertIn('launch="http://127.0.0.1:17850/#5"', xml)
        self.assertIn('scenario="reminder"', xml)
