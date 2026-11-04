"""Passada offline das reuniões: montagem do áudio, vozes, falas por falante, ata e página."""
import calendar
import http.client
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""
os.environ["JARVIS_NOTIFICACOES"] = "0"

import meetings_offline as off
import meetings_repo as repo
import meetings_service as svc
import offline_transcribe as ot
import storage
import web_app

E0 = float(calendar.timegm((2026, 10, 5, 14, 0, 0)))  # epoch do início do bloco de teste (14:00 UTC)


def utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(E0 + seconds))


def write_block(root: Path, source: str, start_epoch: float, tone_at: float, seconds: float = 20.0) -> Path:
    """Bloco Ogg/Opus de teste: silêncio com 2 s de tom a partir de tone_at."""
    folder = root / source
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.fromtimestamp(start_epoch).strftime("%Y%m%d-%H%M%S") + ".ogg")
    samples = np.zeros(int(seconds * 16000), np.float32)
    t = np.arange(32000) / 16000
    samples[int(tone_at * 16000):int(tone_at * 16000) + 32000] = 0.4 * np.sin(2 * np.pi * 440 * t)
    with av.open(str(path), "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=16000, layout="mono")
        frame = av.AudioFrame.from_ndarray(samples[None, :], format="flt", layout="mono")
        frame.sample_rate = 16000
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return path


def canned(utterances, speakers=None):
    """Runner falso: devolve o resultado do offline_transcribe sem GPU nem modelos."""
    speakers = speakers or {"0": {"seconds": 40.0, "embedding": [0.1], "suggestion": {"name": "__JARVIS_USER__", "score": 0.8},
                                  "sample": "speaker_0.wav"}}

    def runner(wav, out_json, samples_dir):
        assert Path(wav).is_file()
        for info in speakers.values():
            if info.get("sample"):
                (Path(samples_dir) / info["sample"]).write_bytes(b"RIFFfake")
        return {"utterances": utterances, "speakers": speakers, "duration": 60.0}
    return runner


class PureFunctionsTest(unittest.TestCase):
    def test_source_counts_and_database_reach_the_local_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meeting = {"started_at": utc(0), "ended_at": utc(20), "mic_speakers": 2, "loopback_speakers": 4}
            worker = Mock(return_value={})
            with patch.object(off, 'run_cli', worker), patch.object(off, 'assemble', return_value=True), \
                    patch.object(off, 'flush_recordings'), patch.object(off.meeting_gate, 'kept_intervals', return_value=[]):
                off._transcribe_sources(meeting, root, worker, root, 'local.db')
            self.assertEqual([(c.args[0].stem, c.args[3], c.args[4]) for c in worker.call_args_list],
                             [('mic', 2, 'local.db'), ('loopback', 4, 'local.db')])

    def test_short_reply_keeps_its_detected_speaker_in_final_transcript(self):
        words = [{"start": 0, "end": 3, "word": "Pergunta"},
                 {"start": 3.1, "end": 3.5, "word": "Sim"},
                 {"start": 4, "end": 7, "word": "Continuação"}]
        turns = [{"start": 0, "end": 3, "speaker": 0},
                 {"start": 3.1, "end": 3.5, "speaker": 1},
                 {"start": 4, "end": 7, "speaker": 0}]
        self.assertEqual([u["speaker"] for u in ot.build_utterances(words, turns)], [0, 1, 0])

    def test_collapse_loops_reduces_repeated_phrases(self):
        self.assertEqual(ot.collapse_loops("E aí E aí E aí E aí E aí"), "E aí")
        self.assertEqual(ot.collapse_loops("Muito mesmo, então vou pôr no profissional"),
                         "Muito mesmo, então vou pôr no profissional")

    def test_speech_blocks_split_on_real_pauses_and_cap_length(self):
        turns = [{"start": 1, "end": 5, "speaker": 0}, {"start": 6, "end": 9, "speaker": 1},
                 {"start": 20, "end": 25, "speaker": 0}, {"start": 26, "end": 60, "speaker": 0}]
        blocks = ot.speech_blocks(turns, 70)
        self.assertEqual(len(blocks), 3)  # 1-9 | 20-25 | 26-60 (o bloco não passa de BLOCK_MAX_S ao juntar)
        self.assertLess(blocks[0][0], 1)  # folga antes

    def test_short_speaker_switches_are_absorbed(self):
        words = [{"start": i * 0.5, "end": i * 0.5 + 0.4, "speaker": 0} for i in range(10)]
        words[4]["speaker"] = 1  # uma palavra solta de "outra voz"
        ot.smooth_speakers(words)
        self.assertEqual({w["speaker"] for w in words}, {0})

    def test_real_speaker_change_is_kept(self):
        words = [{"start": i * 0.5, "end": i * 0.5 + 0.4, "speaker": 0 if i < 8 else 1} for i in range(16)]
        ot.smooth_speakers(words)
        self.assertEqual([w["speaker"] for w in words], [0] * 8 + [1] * 8)

    def test_minor_voice_folds_into_neighbour(self):
        utterances = [{"speaker": 0}, {"speaker": 9}, {"speaker": 1}]
        ot.fold_minor_speakers(utterances, {0, 1})
        self.assertEqual([u["speaker"] for u in utterances], [0, 0, 1])

    def test_drop_echo_removes_mic_copy_of_system_audio_only(self):
        mic = [{"start": 10, "end": 14, "text": "Vamos fechar o contrato amanhã"},
               {"start": 10, "end": 12, "text": "Concordo plenamente"}]
        system = [{"start": 10, "end": 14, "text": "vamos fechar o contrato amanha"}]
        kept = off.drop_echo(mic, system)
        self.assertEqual([u["text"] for u in kept], ["Concordo plenamente"])


class AssembleTest(unittest.TestCase):
    def test_audio_lands_at_clock_position_and_silent_source_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            write_block(root, "mic", E0, tone_at=5.0)
            out = Path(tmp) / "mic.wav"
            self.assertTrue(off.assemble("mic", E0 - 10, E0 + 30, out, root))
            import wave
            with wave.open(str(out)) as wav:
                data = np.frombuffer(wav.readframes(wav.getnframes()), "<i2").astype(np.float32) / 32768
            tone = np.flatnonzero(np.abs(data) > 0.1)
            self.assertAlmostEqual(tone[0] / 16000, 10 + 5.0, delta=0.15)  # t0 = bloco - 10 s
            self.assertFalse(off.assemble("loopback", E0 - 10, E0 + 30, Path(tmp) / "x.wav", root))  # sem arquivos


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "archive"
        self.db = str(Path(self.tmp.name) / "t.db")
        storage.init_db(self.db)
        patcher = patch.object(off, "SAMPLES_DIR", Path(self.tmp.name) / "samples")
        patcher.start()
        self.addCleanup(patcher.stop)
        write_block(self.root, "mic", E0, tone_at=3.0, seconds=40)
        self.mid = repo.create_meeting(utc(20), utc(40), status="closed", db_path=self.db)
        repo.update_meeting(self.mid, db_path=self.db, offline_status="pending", ata_status="ok")
        repo.assign_transcripts([storage.save_transcript("fala ao vivo", "mic", 5, db_path=self.db)], self.mid,
                                db_path=self.db)
        self.extract = Mock(side_effect=lambda rows: {"title": "Ata", "summary": "s", "participants": [],
                                                      "key_decisions": [], "tasks_owner": [], "tasks_others": [],
                                                      "commitments": [], "topics": []})
        self.register = Mock(return_value=[])

    def run_offline(self, utterances, speakers=None, loopback=None):
        return off.process_meeting(self.mid, self.db, canned(utterances, speakers), self.root)

    def test_success_stores_voices_and_utterances_and_releases_the_ata(self):
        ok = self.run_offline([{"speaker": 0, "start": 70.0, "end": 74.0, "text": "Bom dia a todos"},
                               {"speaker": 0, "start": 75.0, "end": 77.0, "text": "Vamos começar"}])
        self.assertTrue(ok)
        meeting = repo.get_meeting(self.mid, self.db)
        self.assertEqual((meeting["offline_status"], meeting["ata_status"]), ("done", "pending"))
        (voice,) = repo.meeting_voices(self.mid, self.db)
        self.assertEqual((voice["suggested_name"], voice["sample_file"]), ("__JARVIS_USER__", f"m{self.mid}_mic_0.wav"))
        self.assertTrue((off.SAMPLES_DIR / voice["sample_file"]).is_file())
        first = repo.meeting_utterances(self.mid, self.db)[0]
        self.assertEqual(first["start_at"], utc(20 - off.PAD_BEFORE_S + 70.0))  # t0 = started_at - folga

    def test_ata_waits_for_offline_then_uses_speakers_and_names(self):
        self.assertFalse(svc.generate_ata(self.mid, self.db, self.extract, self.register))  # pendente: espera
        self.extract.assert_not_called()
        self.assertEqual(repo.get_meeting(self.mid, self.db)["ata_attempts"], 0)
        self.run_offline([{"speaker": 0, "start": 70.0, "end": 74.0, "text": "Bom dia"},
                          {"speaker": 1, "start": 75.0, "end": 77.0, "text": "Bom dia, doutor"}],
                         {"0": {"seconds": 30, "embedding": [1], "suggestion": None, "sample": "speaker_0.wav"},
                          "1": {"seconds": 12, "embedding": [2], "suggestion": None, "sample": None}})
        voices = repo.meeting_voices(self.mid, self.db)
        by_label = {v["label"]: v for v in voices}
        svc.set_voices(self.mid, [{"id": by_label[0]["id"], "name": "__JARVIS_USER__"},
                                  {"id": by_label[1]["id"], "name": "Dr. Olivia"}], self.db)
        meeting = repo.get_meeting(self.mid, self.db)
        self.assertEqual(meeting["participants"], ["Dr. Olivia"])  # o dono não entra na lista
        self.assertTrue(svc.generate_ata(self.mid, self.db, self.extract, self.register))
        rows = self.extract.call_args.args[0]
        self.assertEqual([(r["speaker"], r["text"]) for r in rows], [("__JARVIS_USER__", "Bom dia"), ("Dr. Olivia", "Bom dia, doutor")])

    def test_merge_and_discard_shape_the_ata_rows(self):
        self.run_offline([{"speaker": 0, "start": 70, "end": 72, "text": "a"}, {"speaker": 1, "start": 73, "end": 75, "text": "b"},
                          {"speaker": 2, "start": 76, "end": 78, "text": "ruído da tv"}],
                         {str(i): {"seconds": 10 - i, "embedding": [i], "suggestion": None, "sample": None} for i in range(3)})
        ids = {v["label"]: v["id"] for v in repo.meeting_voices(self.mid, self.db)}
        svc.set_voices(self.mid, [{"id": ids[0], "name": "Ana"}, {"id": ids[1], "merge_into": ids[0]},
                                  {"id": ids[2], "discarded": True}], self.db)
        rows = svc._ata_rows(repo.get_meeting(self.mid, self.db), self.db)
        self.assertEqual([(r["speaker"], r["text"]) for r in rows], [("Ana", "a b")])
        with self.assertRaises(ValueError):
            svc.set_voices(self.mid, [{"id": ids[0], "merge_into": ids[0]}], self.db)
        with self.assertRaises(ValueError):
            svc.set_voices(self.mid, [{"id": 99999, "name": "x"}], self.db)

    def test_failures_retry_then_fall_back_to_live_transcript(self):
        def boom(*_a):
            raise RuntimeError("sem GPU")
        for attempt in range(1, off.MAX_ATTEMPTS + 1):
            self.assertFalse(off.process_meeting(self.mid, self.db, boom, self.root))
            meeting = repo.get_meeting(self.mid, self.db)
            self.assertEqual(meeting["offline_attempts"], attempt)
            expected = "error" if attempt == off.MAX_ATTEMPTS else "pending"
            self.assertEqual(meeting["offline_status"], expected)
        self.assertTrue(svc.generate_ata(self.mid, self.db, self.extract, self.register))  # ao vivo
        self.assertEqual(self.extract.call_args.args[0][0]["text"], "fala ao vivo")

    def test_meeting_without_audio_keeps_live_flow(self):
        other = repo.create_meeting(utc(9000), utc(9100), status="open", db_path=self.db)
        with patch.object(off, "ARCHIVE_DIR", self.root):
            svc.close_meeting(other, db_path=self.db, extract=self.extract, register=self.register)
        self.assertEqual(repo.get_meeting(other, self.db)["offline_status"], "none")

    def test_split_or_merge_drops_offline_data(self):
        self.run_offline([{"speaker": 0, "start": 70, "end": 72, "text": "a"}])
        sample = repo.meeting_voices(self.mid, self.db)[0]["sample_file"]
        svc._drop_offline(self.mid, self.db)
        self.assertEqual(repo.get_meeting(self.mid, self.db)["offline_status"], "none")
        self.assertEqual(repo.meeting_voices(self.mid, self.db), [])
        self.assertFalse((off.SAMPLES_DIR / sample).exists())

    def test_reprocess_requires_audio(self):
        gone = repo.create_meeting(utc(9000), utc(9100), status="closed", db_path=self.db)
        with patch.object(off, "ARCHIVE_DIR", self.root), self.assertRaises(ValueError):
            svc.reprocess_offline(gone, self.db)

    def test_sample_path_rejects_traversal(self):
        off.SAMPLES_DIR.mkdir(exist_ok=True)
        (off.SAMPLES_DIR / "ok.wav").write_bytes(b"x")
        self.assertTrue(off.sample_path("ok.wav").is_file())
        with self.assertRaises(LookupError):
            off.sample_path("../t.db")


class SampleEndpointTest(unittest.TestCase):
    def test_sample_needs_token_and_serves_only_known_voices(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            storage.init_db(db)
            mid = repo.create_meeting(utc(0), utc(10), status="closed", db_path=db)
            repo.save_offline_result(mid, [{"source": "mic", "label": 0, "seconds": 9, "sample_file": "a.wav"}],
                                     [{"source": "mic", "label": 0, "start_at": utc(0), "end_at": utc(2), "text": "oi"}], db)
            voice_id = repo.meeting_voices(mid, db)[0]["id"]
            samples = Path(tmp) / "samples"
            samples.mkdir()
            (samples / "a.wav").write_bytes(b"RIFFdata")
            server = web_app.make_server(port=0, db_path=db, token="t" * 40)
            threading_thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
            threading_thread.start()
            try:
                port = server.server_address[1]
                with patch.object(off, "SAMPLES_DIR", samples):
                    def get(path, token=True):
                        conn = http.client.HTTPConnection("127.0.0.1", port)
                        conn.request("GET", path, headers={"X-Jarvis-Token": "t" * 40} if token else {})
                        res = conn.getresponse()
                        return res.status, res.getheader("Content-Type"), res.read()
                    self.assertEqual(get(f"/api/voices/{voice_id}/sample", token=False)[0], 403)
                    status, ctype, body = get(f"/api/voices/{voice_id}/sample")
                    self.assertEqual((status, ctype, body), (200, "audio/wav", b"RIFFdata"))
                    self.assertEqual(get("/api/voices/9999/sample")[0], 404)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
