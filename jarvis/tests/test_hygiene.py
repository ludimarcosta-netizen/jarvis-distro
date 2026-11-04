import queue
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

import ambient_engine
import meetings_repo as repo
import storage
import transcript_hygiene as hy
from transcriber import keep_segment


class HallucinationTests(unittest.TestCase):
    def test_known_hallucinations_and_prompt_echo_are_flagged(self):
        for text in ("Transcrição fiel de conversa falada em português brasileiro.",
                     "J.A.R.V.I.S., __JARVIS_USER__, Hermes, Gemini.",
                     "J.A.R.V.I.S., J.A.R.V.I.S., __JARVIS_USER__, Hermes, Gemini.",
                     "J.A.R.V.I.S.",
                     "Legendas pela comunidade de Amara.org",
                     "Transcrição e Legendas por Olivia Montenegro",
                     "Obrigado por assistir!"):
            self.assertIsNotNone(hy.hallucination_reason(text), text)

    def test_real_speech_is_kept(self):
        for text in ("Jarvis, abre o YouTube.", "Wesley, manda o ofício até sexta.",
                     "Celina, manda as legendas da apresentação.", "Obrigado, Wesley."):
            self.assertIsNone(hy.hallucination_reason(text), text)

    def test_segment_filter_uses_whisper_confidence(self):
        seg = lambda **k: SimpleNamespace(**{"text": "fala", "no_speech_prob": 0.1, "avg_logprob": -0.3,
                                             "compression_ratio": 1.5, **k})
        self.assertTrue(keep_segment(seg()))
        self.assertFalse(keep_segment(seg(no_speech_prob=0.8, avg_logprob=-0.7)))  # ruído
        self.assertTrue(keep_segment(seg(no_speech_prob=0.8, avg_logprob=-0.2)))   # fala baixa, mas confiante
        self.assertFalse(keep_segment(seg(avg_logprob=-1.3)))
        self.assertFalse(keep_segment(seg(compression_ratio=3.0)))                 # repetição em loop
        self.assertFalse(keep_segment(seg(text="Legendas pela comunidade Amara.org")))


class AtaParticipantsTests(unittest.TestCase):
    def test_assistants_never_become_participants(self):
        import context_extractor
        reply = Mock(text='{"title": "t", "participants": ["Wesley", "Hermes", "J.A.R.V.I.S.", "Gemini", "Celina"]}')
        client = Mock()
        client.chats.create.return_value.send_message.return_value = reply
        with patch.object(context_extractor, "get_text_client", return_value=client):
            ata = context_extractor.extract_structured([{"timestamp": "t", "source": "mic", "text": "oi"}])
        self.assertEqual(ata["participants"], ["Wesley", "Celina"])
        prompt = client.chats.create.return_value.send_message.call_args.args[0]
        self.assertIn("NÃO inclua", prompt)


class HotwordLeakTests(unittest.TestCase):
    def test_hotword_text_alone_is_a_leak_but_real_mentions_are_kept(self):
        cidade, curta = "__JARVIS_CIDADE__", "__JARVIS_CIDADE_CURTA__"
        resto = cidade[len(curta):].strip() or cidade  # fim do nome da cidade
        ultima = cidade.split()[-1]
        for text in ("__JARVIS_USER__ __JARVIS_CIDADE__", "__JARVIS_USER__ __JARVIS_CIDADE_CURTA__", resto, f"{ultima}.",
                     f"{ultima}, {ultima}, {ultima}", "Jarvis __JARVIS_USER__ __JARVIS_CIDADE__ Jarvis",
                     f"__JARVIS_USER__ __JARVIS_CIDADE_CURTA__ {resto.split()[0]}"):
            self.assertTrue(hy.hotword_leak(text), text)
            self.assertEqual(hy.hallucination_reason(text), "vazamento das hotwords", text)
            self.assertIsNone(hy.strip_hotword_leak(text), text)
        for text in ("__JARVIS_USER__!", "Para __JARVIS_CIDADE__, preparei um projeto de lei.",
                     "Então eu só coloco para o pessoal da limpeza aqui para __JARVIS_CIDADE_CURTA__?"):
            self.assertFalse(hy.hotword_leak(text), text)
            self.assertEqual(hy.strip_hotword_leak(text), text)
            self.assertIsNone(hy.hallucination_reason(text), text)

    def test_leak_glued_to_real_speech_is_cut_and_junk_rest_is_dropped(self):
        self.assertEqual(hy.strip_hotword_leak("Vamos fechar o contrato amanhã __JARVIS_USER__ __JARVIS_CIDADE__"),
                         "Vamos fechar o contrato amanhã")
        self.assertIsNone(hy.strip_hotword_leak("Jesus __JARVIS_USER__ __JARVIS_CIDADE__"))
        ultima = "__JARVIS_CIDADE__".split()[-1]
        self.assertIsNone(hy.strip_hotword_leak(f"e o {ultima} {ultima} {ultima}"))
        clean, reason = hy.strip_hallucinations("Vamos fechar o contrato amanhã. Jarvis __JARVIS_USER__ __JARVIS_CIDADE__")
        self.assertEqual((clean, reason), ("Vamos fechar o contrato amanhã.", "alucinação do Whisper"))


class StripTests(unittest.TestCase):
    def test_real_speech_keeps_and_glued_hallucination_is_cut(self):
        clean, reason = hy.strip_hallucinations("Eu tenho que ver se é fino hoje. Legendas pela comunidade de Amara.org")
        self.assertEqual((clean, reason), ("Eu tenho que ver se é fino hoje.", "alucinação do Whisper"))
        clean, _ = hy.strip_hallucinations("Você fala baixo, né? Vamos ficando aqui. J.A.R.V.I.S., __JARVIS_USER__, Hermes, Gemini.")
        self.assertEqual(clean, "Você fala baixo, né? Vamos ficando aqui.")

    def test_nothing_left_means_ignore_and_clean_text_is_untouched(self):
        self.assertEqual(hy.strip_hallucinations("Agradeço a J.A.R.V.I.S., __JARVIS_USER__, Hermes, Gemini.")[0], None)
        self.assertEqual(hy.strip_hallucinations("Jarvis, abre o YouTube."), ("Jarvis, abre o YouTube.", None))


class EchoGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.guard = hy.EchoGuard(clock=lambda: self.now)

    def test_mic_copy_after_system_is_echo(self):
        self.guard.system_heard("Como sempre desenvolveremos o crédito na plataforma nova")
        self.assertTrue(self.guard.mic_is_echo("como sempre desenvolveremos o credito na plataforma"))
        self.assertFalse(self.guard.mic_is_echo("Wesley manda o ofício do convênio amanhã"))

    def test_mic_copy_before_system_is_returned_for_ignoring(self):
        self.guard.remember_mic(41, "olá Stefan como você está hoje")
        self.guard.remember_mic(42, "Wesley manda o ofício")
        self.assertEqual(self.guard.system_heard("Olá, Stefan. Como você está hoje?"), [41])

    def test_window_and_short_texts(self):
        self.guard.system_heard("sim")
        self.assertFalse(self.guard.mic_is_echo("sim"))  # curto demais para comparar
        self.guard.system_heard("reunião sobre orçamento das emendas")
        self.now += hy.ECHO_WINDOW_S + 1
        self.assertFalse(self.guard.mic_is_echo("reunião sobre orçamento das emendas"))


class LiveFilterTests(unittest.TestCase):
    def _engine(self, texts):
        eng = object.__new__(ambient_engine.JarvisAmbientEngine)
        eng.language = "pt"
        eng.audio_queue = queue.Queue()
        eng.capture_mode = "save"
        eng.on_captured_chunk = None
        eng.on_meeting_command = None
        eng._discard_pending = False
        eng.echo_guard = hy.EchoGuard()
        eng.transcriber = Mock()
        eng.transcriber.transcribe_audio_chunk.side_effect = [t for t, _ in texts]
        for _, source in texts:
            eng.audio_queue.put((np.zeros(160, dtype="float32"), source, 2.0, "ambient", True))
        return eng

    def _run(self, eng):
        stop = threading.Event()
        stop.set()
        saved = []
        ids = iter(range(100, 200))

        def save(**kw):
            saved.append((kw["source"], kw["text"]))
            return next(ids)
        with patch.object(ambient_engine, "save_transcript", side_effect=save), \
                patch.object(ambient_engine, "ignore_transcripts") as ignore, \
                patch.object(ambient_engine._voice_module.client, "mode", return_value="off"):
            eng._transcription_worker(stop)
        return saved, ignore

    def test_mic_echo_of_system_audio_is_not_saved(self):
        eng = self._engine([("O mercado abriu em alta hoje cedo", "loopback"),
                            ("o mercado abriu em alta hoje", "mic"),
                            ("Wesley, vamos fechar a pauta", "mic")])
        saved, _ = self._run(eng)
        self.assertEqual(saved, [("loopback", "O mercado abriu em alta hoje cedo"), ("mic", "Wesley, vamos fechar a pauta")])

    def test_system_audio_outside_meeting_app_is_not_saved_but_still_guards_echo(self):
        eng = self._engine([("O mercado abriu em alta hoje cedo", "loopback"),
                            ("o mercado abriu em alta hoje", "mic")])
        eng.audio_queue = queue.Queue()
        eng.audio_queue.put((np.zeros(160, dtype="float32"), "loopback", 2.0, "ambient", False))
        eng.audio_queue.put((np.zeros(160, dtype="float32"), "mic", 2.0, "ambient", True))
        saved, _ = self._run(eng)
        self.assertEqual(saved, [])  # o vídeo não vira fala de reunião e o eco dele no microfone também não

    def test_system_audio_marks_earlier_mic_copy_as_ignored(self):
        eng = self._engine([("olá Stefan como você está hoje", "mic"), ("Olá, Stefan. Como você está hoje?", "loopback")])
        _saved, ignore = self._run(eng)
        ignore.assert_called_with([100], "eco do áudio do PC")


class HistoryCleanupTests(unittest.TestCase):
    def test_scan_marks_noise_keeps_speech_and_flags_atas_to_redo(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            storage.init_db(db)
            mid = repo.create_meeting(started_at="2026-09-30 13:00:00", status="closed", db_path=db)
            rows = [("2026-09-30 13:00:00", "mic", "Transcrição fiel de conversa falada em português brasileiro."),
                    ("2026-09-30 13:00:05", "loopback", "Como sempre desenvolveremos o crédito na plataforma"),
                    ("2026-09-30 13:00:09", "mic", "como sempre desenvolveremos o crédito na plataforma"),
                    ("2026-09-30 13:00:20", "mic", "Wesley, manda o ofício do convênio até sexta.")]
            with sqlite3.connect(db) as conn:
                for ts, src, text in rows:
                    conn.execute("INSERT INTO transcripts (timestamp, source, text, meeting_id, processed_for_summary) "
                                 "VALUES (?, ?, ?, ?, 1)", (ts, src, text, mid))
            conn.close()
            preview = hy.scan_history(db, apply=False)
            self.assertEqual(preview, {"eco do prompt antigo": 1, "eco do áudio do PC": 1})
            self.assertEqual(len(repo.meeting_transcripts(mid, db)), 4)  # simulação não altera
            done = hy.scan_history(db, apply=True)
            self.assertEqual(done["reuniões com ata a refazer"], 1)
            kept = [r["text"] for r in repo.meeting_transcripts(mid, db)]
            self.assertEqual(kept, ["Como sempre desenvolveremos o crédito na plataforma",
                                    "Wesley, manda o ofício do convênio até sexta."])
            self.assertEqual(repo.ignored_count(mid, db), 2)
            self.assertEqual(repo.get_meeting(mid, db)["ata_status"], "pending")
            self.assertEqual(hy.scan_history(db, apply=True).get("eco do prompt antigo"), None)  # idempotente

    def test_scan_rewrites_mixed_rows_and_keeps_the_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            storage.init_db(db)
            storage.save_transcript("Wesley fecha a pauta hoje. Legendas pela comunidade de Amara.org", db_path=db)
            self.assertEqual(hy.scan_history(db, apply=True)["fala real com trecho alucinado recortado"], 1)
            with sqlite3.connect(db) as conn:
                text, original, ignored = conn.execute("SELECT text, original_text, ignored FROM transcripts").fetchone()
            conn.close()
            self.assertEqual((text, ignored), ("Wesley fecha a pauta hoje.", 0))
            self.assertIn("Amara.org", original)


if __name__ == "__main__":
    unittest.main()


class NoiseTests(unittest.TestCase):
    def test_standalone_filler_and_caption_noise_is_dropped(self):
        for text in ("Obrigado.", "Tchau, tchau.", "Um abraço.", "Ehehehehehehe", "hahaha", "kkkk",
                     "A CIDADE NO BRASIL", "[Música]", "..."):
            self.assertTrue(hy.hallucination_reason(text), text)

    def test_real_speech_is_kept(self):
        for text in ("Obrigado pelo retorno, Edson, vamos fechar na terça.", "Tchau, até amanhã então, Debora.",
                     "Vamos testar ela.", "O que?", "Fechou? Fechou.", "Ata da reunião de SESAU"):
            self.assertIsNone(hy.hallucination_reason(text), text)
