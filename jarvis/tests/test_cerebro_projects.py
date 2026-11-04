"""Regressões de cronologia, escopo e preservação nas visões de projeto."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cerebro_io import atomic_write
from cerebro_memory import Memory
from cerebro_projects import publish_states


class ProjectStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        (self.vault / "projetos").mkdir()
        self.original = self.vault / "projetos/Jarvis.md"
        self.original.write_text('---\naliases: [hermes]\n---\n# Jarvis\n\nAssistente de voz local.\n\n## Estado\nEstado antigo.\n\n## Próximos passos\n- Conferir microfone\n', encoding="utf-8")
        self.memory = Memory(self.vault)

    def record(self, kind, text, project="Jarvis", date="2026-10-12T10:00:00-03:00"):
        result = self.memory.record(kind, text, "teste de contrato", "Codex", project)
        path = self.vault / result["nota"]
        body = path.read_text(encoding="utf-8")
        body = body.replace(next(l for l in body.splitlines() if l.startswith("data:")), "data: " + json.dumps(date))
        atomic_write(path, body)
        return path

    def test_chronology_uses_event_date_not_file_modified_time(self):
        old = self.record("resultado", "Resultado antigo", date="2026-10-01T10:00:00-03:00")
        self.record("resultado", "Resultado recente", project="hermes")
        os.utime(old, (9e9, 9e9))
        state = self.memory.project_state("Jarvis")
        self.assertEqual(state["resultados"][0]["texto"], "Resultado recente")
        self.assertIn("referência histórica", state["estado"])
        self.assertEqual(state["objetivo"], "Assistente de voz local.")

    def test_pending_is_preserved_despite_later_success_report(self):
        self.record("pendencia", "Revisar voz com __JARVIS_USER__")
        self.record("resultado", "O agente relatou sucesso na revisão", date="2026-10-12T11:00:00-03:00")
        state = self.memory.project_state("hermes")
        self.assertEqual(state["pendencias"][0]["texto"], "Revisar voz com __JARVIS_USER__")
        self.assertIn("Conferir microfone", state["pendencias_da_nota_original"])
        self.assertIn("Fonte declarada não equivale a validação independente", state["aviso"])

    def test_fiction_and_other_project_are_not_operational_decisions(self):
        self.record("decisao", "A cor fictícia é violeta")
        self.record("decisao", "Outro projeto escolheu PostgreSQL", project="Outro")
        self.record("decisao", "Manter Markdown")
        self.record("resultado", "Integração validada por meio de um código fictício")
        state = self.memory.project_state("Jarvis")
        self.assertEqual([r["texto"] for r in state["decisoes"]], ["Manter Markdown"])
        self.assertEqual(state["registros_ficticios_separados"], 1)
        self.assertEqual(len(state["resultados"]), 1)

    def test_session_duplicates_remain_reports_and_sources_are_linked(self):
        folder = self.vault / "sessoes"
        folder.mkdir()
        for n in range(2):
            (folder / f"Relato {n}.md").write_text('---\nprojeto_nota: "[[Jarvis]]"\nferramenta: Codex\nsessao_id: abc\nfim: 2026-10-12 12:00\n---\n# Relato\n\n## Pendências\n- Validar voz\n', encoding="utf-8")
        state = self.memory.project_state("Jarvis")
        self.assertEqual(len(state["sessoes_recentes"]), 1)
        self.assertIn("não validado", state["sessoes_recentes"][0]["verificacao"])
        self.assertEqual(state["pendencias"], [])

    def test_generated_views_are_idempotent_and_preserve_manual_notes(self):
        before = self.original.read_bytes()
        self.memory.sync()
        self.assertEqual(publish_states(self.vault, self.memory.db)["atualizados"], 1)
        self.assertEqual(publish_states(self.vault, self.memory.db)["atualizados"], 0)
        self.assertEqual(self.original.read_bytes(), before)
        generated = self.vault / "projetos/estados/Estado — Jarvis.md"
        generated.write_text("Nota manual que deve ser preservada", encoding="utf-8")
        self.assertEqual(publish_states(self.vault, self.memory.db)["arquivos_nao_gerados_preservados"], 1)
        self.assertEqual(generated.read_text(encoding="utf-8"), "Nota manual que deve ser preservada")

    def test_project_records_work_with_separate_v2_metadata(self):
        from contextlib import closing
        from cerebro_projects import build_state

        note = self.record("resultado", "Integração preservada no índice v2")
        self.memory.sync()
        with closing(self.memory.connect()) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS meta(path TEXT PRIMARY KEY, front TEXT, auto INTEGER)")
            conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?,0)",
                         (note.relative_to(self.vault).as_posix(), note.read_text(encoding="utf-8")))
            conn.execute("DELETE FROM chunks WHERE section='Cabeçalho'")
        state = build_state(self.vault, self.memory.db, "Jarvis", ["hermes"])
        self.assertEqual(state["resultados"][0]["texto"], "Integração preservada no índice v2")

    def test_project_resolution_can_reuse_existing_index(self):
        import project_notes
        from unittest.mock import patch

        index = project_notes.project_index(self.vault)
        with patch.object(project_notes, "project_index", side_effect=AssertionError("não reler o índice")):
            self.assertEqual(project_notes.resolve(self.vault, "hermes", index=index), "Jarvis")
