"""Callbacks, privacidade e continuidade entre agentes sem API de IA."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cerebro_capture as capture_module
from cerebro_capture import capture
from cerebro_memory import Memory
from scripts.install_cerebro_hooks import install


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.vault = self.home / "vault"
        (self.vault / "projetos").mkdir(parents=True)
        (self.vault / "projetos/Jarvis.md").write_text('---\naliases: [hermes]\n---\n# Jarvis\n', encoding="utf-8")
        self.memory = Memory(self.vault)
        self.text = "Implementada busca local com fontes e limite de contexto. O resultado ainda precisa ser confirmado pelo usuário."

    def test_codex_and_claude_idempotent_without_promoting_claims(self):
        for source, payload in [("codex", {"type":"agent-turn-complete","thread-id":"c1","cwd":"C:/Jarvis","last-assistant-message":self.text}),
                                ("claude", {"session_id":"a1","cwd":"C:/Jarvis","last_assistant_message":self.text})]:
            self.assertTrue(capture(source, payload, self.memory)["gravado"])
            self.assertFalse(capture(source, payload, self.memory)["gravado"])
        peer = Memory(self.vault)
        self.assertIn("não verificado", peer.search("Implementada")[0]["body"])

    def test_antigravity_only_captures_response_not_thinking_or_tools(self):
        path = self.home / ".gemini/antigravity/brain/abc/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"source":"MODEL","type":"PLANNER_RESPONSE","content":self.text,"thinking":"NÃO CAPTURAR"}) + "\n" + json.dumps({"source":"MODEL","type":"RUN_COMMAND","content":"NÃO CAPTURAR"}), encoding="utf-8")
        payload = {"conversationId":"abc","fullyIdle":True,"workspacePaths":["C:/Jarvis"],"transcriptPath":str(path)}
        with patch.object(capture_module, "HOME", self.home):
            result = capture("antigravity", payload, self.memory)
        text = (self.vault / result["nota"]).read_text(encoding="utf-8")
        self.assertNotIn("NÃO CAPTURAR", text)

    def test_incomplete_and_arbitrary_transcript_are_rejected(self):
        self.assertFalse(capture("codex", {"type":"approval-requested"}, self.memory)["gravado"])
        self.assertFalse(capture("antigravity", {"fullyIdle":False}, self.memory)["gravado"])
        with self.assertRaises(ValueError), patch.object(capture_module, "HOME", self.home):
            capture("antigravity", {"fullyIdle":True,"conversationId":"a","transcriptPath":"C:/outside.jsonl"}, self.memory)

    def test_secret_masked_and_unknown_project_not_guessed(self):
        # Mascaramento mantido por decisão do __JARVIS_USER__ em 2026-10-19 (substitui a de 2026-10-12).
        payload = {"session_id":"a","cwd":"C:/desconhecido","last_assistant_message":self.text + " senha: hunter22abc"}
        result = capture("claude", payload, self.memory)
        self.assertEqual(result["projeto"], "")
        note = (self.vault / result["nota"]).read_text(encoding="utf-8")
        self.assertNotIn("hunter22abc", note)
        self.assertIn("[dado protegido]", note)

    def test_hermes_captures_persisted_turn_and_skips_other_profiles(self):
        db = self.home / "AppData/Local/hermes/state.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        with conn:
            conn.executescript("CREATE TABLE sessions(id TEXT,cwd TEXT,started_at REAL,parent_session_id TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL,active INTEGER,_compressed_summary INTEGER);")
            conn.execute("INSERT INTO sessions VALUES('h1','C:/Jarvis',1800000000,NULL)")
            conn.execute("INSERT INTO messages VALUES(1,'h1','assistant',?,1800000001,1,0)", (self.text,))
        conn.close()
        with patch.object(capture_module, "HOME", self.home):
            result = capture("hermes", {"session_id":"h1","profile":"default","extra":{"completed":True}}, self.memory)
        self.assertTrue(result["gravado"])
        self.assertEqual(result["projeto"], "Jarvis")
        self.assertFalse(capture("hermes", {"profile":"vigia"}, self.memory)["gravado"])

    def test_clinical_details_preserved_in_automatic_memory(self):
        payload = {"session_id":"sensitive","cwd":"C:/Jarvis","last_assistant_message":self.text + " Paciente Debora: medicação confidencial."}
        result = capture("claude", payload, self.memory)
        note = (self.vault / result["nota"]).read_text(encoding="utf-8")
        self.assertIn("Debora", note)
        self.assertIn("medicação confidencial", note)

    def test_original_notify_is_forwarded_without_shell_interpolation(self):
        path = self.home / ".notify_original.json"
        path.write_text(json.dumps(["original.exe", "turn-ended"]), encoding="utf-8")
        raw = '{"type":"agent-turn-complete","last-assistant-message":"$(literal)"}'
        with patch.object(capture_module, "HERE", self.home), patch.object(capture_module.subprocess, "run") as run:
            capture_module.forward_notify(raw)
        self.assertEqual(run.call_args.args[0], ["original.exe", "turn-ended", raw])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_installer_preserves_callbacks_permissions_and_is_idempotent(self):
        runtime = self.home / "AppData/Local/hermes/jarvis"
        runtime.mkdir(parents=True)
        (runtime.parent / "config.yaml").write_text('model: preservado\nhooks:\n  pre_tool_call:\n    - command: "original"\n', encoding="utf-8")
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").write_text('notify = ["original.exe", "turn-ended"]\nmodel = "preservado"\n', encoding="utf-8")
        (self.home / ".claude").mkdir()
        (self.home / ".claude/settings.json").write_text('{"permissions":{"defaultMode":"default"},"hooks":{"Stop":[{"hooks":[{"type":"command","command":"original"}]}]}}', encoding="utf-8")
        backup = self.home / "backup"
        install(self.home, runtime, runtime / "python.exe", backup)
        self.assertEqual(json.loads((runtime / ".notify_original.json").read_text()), ["original.exe","turn-ended"])
        data = json.loads((self.home / ".claude/settings.json").read_text())
        self.assertEqual(data["permissions"]["defaultMode"], "default")
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertEqual(install(self.home, runtime, runtime / "python.exe", backup)["alterados"], [])
