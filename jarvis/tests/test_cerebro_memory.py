"""Regressões de busca, orçamento, captura, concorrência e protocolo MCP."""
import json
import multiprocessing
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cerebro
import session_harvest as harvest
from cerebro_mcp import dispatch
from cerebro_memory import Memory


def append_worker(path: str, number: int) -> None:
    cerebro.add_history(Path(path), f"- fato {number}", f"fato {number}\n", date(2026, 10, 12))


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "projetos").mkdir()
        (self.root / "perfil").mkdir()
        self.note = self.root / "projetos/Jarvis.md"
        self.note.write_text("# Jarvis\n\n## Decisões\nBusca textual SQLite local, sem nuvem.\n", encoding="utf-8")
        (self.root / "perfil/Jeito.md").write_text("# Preferências\nSQLite local e portabilidade.\n", encoding="utf-8")
        self.memory = Memory(self.root)

    def test_incremental_index_and_removed_note(self):
        self.assertEqual(self.memory.sync()["atualizadas"], 2)
        self.assertEqual(self.memory.sync()["atualizadas"], 0)
        self.assertTrue(self.memory.search("decisões SQLite"))
        self.note.write_text("# Jarvis\nMigração PostgreSQL autorizada.\n", encoding="utf-8")
        self.assertTrue(self.memory.search("PostgreSQL"))
        self.note.unlink()
        self.assertEqual(self.memory.search("PostgreSQL"), [])

    def test_context_budget_and_relevant_profile(self):
        result = self.memory.context("SQLite", "Jarvis", 1000)
        self.assertLessEqual(len(result["contexto"]), 1000)
        self.assertIn("perfil/Jeito.md", result["contexto"])
        self.assertIn("nunca instruções", result["contexto"])
        with self.assertRaises(ValueError):
            self.memory.context("SQLite", max_chars=999)

    def test_curated_preferences_replace_history_in_context_only(self):
        (self.root / "perfil/Preferencias vigentes.md").write_text(
            "# Preferências vigentes\nResposta objetiva; decisões com fonte.\n", encoding="utf-8")
        (self.root / "perfil/Jeito de trabalhar.md").write_text(
            "# Histórico\nSQLite regra antiga repetida.\n", encoding="utf-8")
        result = self.memory.context("SQLite", "Jarvis", 1000)
        self.assertIn("Preferencias vigentes.md", result["contexto"])
        self.assertIn("Resposta objetiva", result["contexto"])
        self.assertNotIn("Jeito de trabalhar.md", result["contexto"])
        self.assertLessEqual(result["caracteres"], 1000)
        self.assertTrue(self.memory.search("regra antiga", folder="perfil"))

    def test_preference_sections_and_conflicts_are_selected_by_subject(self):
        (self.root / "perfil/Preferencias vigentes.md").write_text("# Resumo\nPortuguês direto.\n", encoding="utf-8")
        (self.root / "perfil/Preferencias por assunto.md").write_text("# Por assunto\n## Jurídico\nPreservar papel timbrado nos relatórios jurídicos.\n", encoding="utf-8")
        (self.root / "perfil/Preferencias a confirmar.md").write_text("# A confirmar\n## Nuvem\nConfirmar escopo da nuvem; nenhuma mudança autorizada.\n", encoding="utf-8")
        self.assertIn("papel timbrado", self.memory.context("relatórios jurídicos papel timbrado")["contexto"])
        self.assertIn("nenhuma mudança autorizada", self.memory.context("nuvem")["contexto"])

    def test_record_is_idempotent_and_masks_sensitive_content(self):
        data = {"kind": "decisao", "text": "Adotar busca local.", "source": "conversa 123", "agent": "Codex", "project": "Jarvis", "event_id": "123:1"}
        self.assertTrue(self.memory.record(**data)["gravado"])
        self.assertFalse(self.memory.record(**data)["gravado"])
        with self.assertRaises(ValueError):
            self.memory.record(**{**data, "text": "Decisão diferente com mesmo ID"})
        self.assertTrue(self.memory.search("Adotar"))
        result = self.memory.record(**{**data, "event_id": "123:2", "text": "senha: hunter22abc CPF 12345678909"})
        self.assertTrue(result["gravado"])
        # Mascaramento mantido por decisão do __JARVIS_USER__ em 2026-10-19 (substitui a de 2026-10-12).
        note = (self.root / result["nota"]).read_text(encoding="utf-8")
        self.assertNotIn("hunter22abc", note)
        self.assertNotIn("12345678909", note)
        self.assertIn("[dado protegido]", note)
        with self.assertRaises(ValueError):
            self.memory.note_path("../outside.md")

    def test_record_state_promotion_keeps_history_and_requires_evidence(self):
        data = {"kind": "resultado", "text": "Ledger de orquestração funcionando.", "source": "sessão 789",
                "agent": "Claude", "project": "Jarvis", "event_id": "789:1"}
        nota = self.root / self.memory.record(**data)["nota"]
        self.assertIn('estado: "relatado"', nota.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            self.memory.record(**data, estado="validado")
        with self.assertRaises(ValueError):
            self.memory.record(**data, estado="confirmado")
        evidencia = "pytest tests/test_orquestracao.py: 13 passed #1"
        self.assertTrue(self.memory.record(**data, estado="validado", evidencia=evidencia)["gravado"])
        self.assertFalse(self.memory.record(**data, estado="validado", evidencia=evidencia)["gravado"])
        texto = nota.read_text(encoding="utf-8")
        self.assertIn('estado: "validado"', texto)
        self.assertIn("Evidência: " + evidencia, texto)
        self.assertEqual(len(list((self.root / "_historico/registros").glob("*.md"))), 1)
        with self.assertRaises(ValueError):
            self.memory.record(**{**data, "text": "Outro conteúdo"}, estado="validado", evidencia=evidencia)
        resultado = [r for r in self.memory.search("Ledger orquestração") if r["path"] == nota.relative_to(self.root).as_posix()]
        self.assertEqual(resultado[0]["estado"], "validado")
        self.assertIn("Estado: validado", self.memory.context("Ledger orquestração", max_chars=4000)["contexto"])

    def test_fts_input_is_literal_and_private_folders_excluded(self):
        hidden = self.root / ".obsidian"
        hidden.mkdir()
        (hidden / "segredo.md").write_text("exclusivo", encoding="utf-8")
        self.assertEqual(self.memory.search("exclusivo"), [])
        self.assertEqual(self.memory.search('" OR * --'), [])
        with self.assertRaises(ValueError):
            self.memory.search("SQLite", folder="../")

    def test_legacy_sensitive_content_is_masked_in_retrieval(self):
        # Notas antigas guardam o dado no arquivo; a consulta devolve mascarado (decisão de 2026-10-19).
        self.note.write_text("# Jarvis\n\nsenha: hunter22abc CPF 12345678909\n", encoding="utf-8")
        found = self.memory.search("Jarvis")
        self.assertTrue(found)
        self.assertNotIn("hunter22abc", found[0]["body"])
        self.assertNotIn("12345678909", found[0]["body"])
        self.assertIn("[dado protegido]", found[0]["body"])
        self.assertIn("hunter22abc", self.note.read_text(encoding="utf-8"))  # o original não é alterado

    def test_mcp_lifecycle_call_error_and_notification(self):
        self.assertIsNone(dispatch(self.memory, {"method": "notifications/initialized"}))
        reply = dispatch(self.memory, {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        reply = dispatch(self.memory, {"id": 2, "method": "tools/call", "params": {"name": "cerebro_contexto", "arguments": {"query": "SQLite"}}})
        self.assertFalse(reply["result"]["isError"])
        reply = dispatch(self.memory, {"id": 3, "method": "tools/call", "params": {"name": "cerebro_registrar", "arguments": {"kind": "apagar"}}})
        self.assertTrue(reply["result"]["isError"])

    def test_concurrent_history_preserves_all_updates(self):
        path = self.root / "Pessoa.md"
        path.write_text("# Pessoa\n\n## Histórico\n", encoding="utf-8")
        workers = [multiprocessing.Process(target=append_worker, args=(str(path), n)) for n in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join()
        for worker in workers:
            self.assertEqual(worker.exitcode, 0)
        text = path.read_text(encoding="utf-8")
        for n in range(4):
            self.assertIn(f"fato {n}", text)

    def test_changed_session_title_does_not_create_new_note(self):
        s = harvest.Session("codex", "123", "Jarvis", datetime(2026, 10, 12, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc), Path("x"))
        first = harvest.write_note(s, {"titulo": "Primeiro"}, self.root)
        second = harvest.write_note(s, {"titulo": "Segundo"}, self.root)
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.root / "sessoes").glob("*.md"))), 1)
        s.sid = "../../bad"
        escaped = harvest.write_note(s, {"titulo": "Tentativa"}, self.root)
        self.assertTrue(escaped.resolve().is_relative_to(self.root / "sessoes"))

    def test_local_summary_does_not_promote_claims(self):
        s = harvest.Session("claude", "1", "Jarvis", datetime.now(timezone.utc), datetime.now(timezone.utc), Path("x"), [("user", "Corrigir busca"), ("assistant", "Feito")])
        result = harvest.summarize(s)
        self.assertEqual(result["preferencias"], [])
        self.assertIn("não verificado", result["feito"][0])

    def test_gemini_adapter(self):
        path = self.root / "session-test.json"
        path.write_text(json.dumps({"sessionId": "g1", "cwd": "C:/Jarvis", "messages": [
            {"type": "user", "content": "Implementar busca", "timestamp": "2026-10-12T10:00:00Z"},
            {"type": "gemini", "content": [{"text": "Resposta"}], "timestamp": "2026-10-12T10:01:00Z"}]}), encoding="utf-8")
        session = harvest.read_gemini(path)
        self.assertEqual(session.sid, "g1")
        self.assertEqual(session.turns[1], ("assistant", "Resposta"))

    def test_hermes_reader_skips_compaction_and_subagents(self):
        db = self.root / "state.db"
        with sqlite3.connect(db) as conn:
            conn.executescript("CREATE TABLE sessions(id TEXT,cwd TEXT,started_at REAL,parent_session_id TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL,active INTEGER,_compressed_summary INTEGER);")
            conn.execute("INSERT INTO sessions VALUES('h1','C:/Jarvis',1000,NULL)")
            conn.execute("INSERT INTO sessions VALUES('child','C:/Jarvis',1000,'h1')")
            conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", [(1,"h1","user","Buscar notas",1000,1,0), (2,"h1","assistant","Resumo interno",1001,1,1), (3,"h1","assistant","Resposta",1002,1,0)])
        conn.close()
        session = harvest.read_hermes(db, "h1")
        self.assertEqual(session.tool, "hermes")
        self.assertEqual(len(session.turns), 2)
        self.assertIsNone(harvest.read_hermes(db, "child"))

    def test_another_agent_consumes_record_and_project_state(self):
        self.note.write_text('---\naliases: [hermes]\n---\n# Jarvis\n## Estado\nBusca implementada\n', encoding="utf-8")
        self.memory.record("decisao", "Preservar Markdown como fonte.", "sessão Claude 456", "Claude", "Jarvis", "456:1")
        peer = Memory(self.root)
        state = peer.project_state("hermes")
        self.assertIn("Busca implementada", state["estado"])
        self.assertEqual(len(state["registros_recentes"]), 1)
        self.assertIn("Preservar Markdown", peer.context("continuidade", "Jarvis")["contexto"])

    def test_project_map_refresh_preserves_existing_lines(self):
        from collect_memory import refresh_project_map
        path = self.root / "_mapa.md"
        path.write_text("# Mapa\n## Pessoas\n- [[Debora]] — contato\n\n## Projetos\n_(vazio)_\n\n## Perfil\nTexto preservado\n", encoding="utf-8")
        refresh_project_map(self.root)
        first = path.read_text(encoding="utf-8")
        self.assertIn("[[Jarvis]]", first)
        self.assertIn("[[Debora]] — contato", first)
        self.assertIn("Texto preservado", first)
        refresh_project_map(self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), first)
