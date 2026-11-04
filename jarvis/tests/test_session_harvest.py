"""Coleta das sessões das IAs para o Cerebro: segredos, leitores e execução incremental."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JARVIS_CEREBRO_DIR"] = ""

import session_harvest as sh


def claude_line(role, text, ts, cwd="__JARVIS_USERPROFILE_FS__/Projects/Lei"):
    return json.dumps({"type": role, "timestamp": ts, "cwd": cwd, "message": {"content": text}})


class RedactTest(unittest.TestCase):
    def test_secrets_and_cpf_are_preserved(self):
        text = "chave sk-abcdefghijklmnopqrstuv e AIza" + "x" * 35 + " senha: hunter22 CPF 123.456.789-09"
        out = sh.redact(text)
        self.assertEqual(out, text)

    def test_normal_text_is_kept(self):
        self.assertEqual(sh.redact("Alterar o art. 12 da Lei 4.320"), "Alterar o art. 12 da Lei 4.320")


class ReadersTest(unittest.TestCase):
    def test_claude_reader_drops_system_noise_and_collects_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "abc.jsonl"
            lines = [claude_line("user", "<system-reminder>x</system-reminder>Ajustar o decreto 10/2026", "2026-10-05T10:00:00Z"),
                     claude_line("assistant", "Feito.", "2026-10-05T10:05:00Z"),
                     json.dumps({"type": "assistant", "timestamp": "2026-10-05T10:06:00Z", "message": {"content": [
                         {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:/x/decreto.md"}}]}})]
            path.write_text("\n".join(lines), encoding="utf-8")
            session = sh.read_claude(path)
            self.assertEqual(session.turns[0], ("user", "Ajustar o decreto 10/2026"))
            self.assertEqual(session.files, ["C:/x/decreto.md"])
            self.assertEqual(session.project, "Lei")

    def test_codex_subagent_sessions_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.jsonl"
            path.write_text(json.dumps({"timestamp": "2026-10-05T10:00:00Z", "type": "session_meta",
                                        "payload": {"thread_source": "subagent", "cwd": "C:\a\b"}}), encoding="utf-8")
            self.assertIsNone(sh.read_codex(path))


class RunTest(unittest.TestCase):
    def test_incremental_run_writes_note_once_and_skips_trivial(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, home = Path(tmp) / "vault", Path(tmp) / "home"
            (vault / "pessoas").mkdir(parents=True)
            (vault / "pessoas" / "Debora Silva.md").write_text("x", encoding="utf-8")
            proj = home / ".claude" / "projects" / "p"
            proj.mkdir(parents=True)
            long_ask = "Preciso revisar o decreto de suprimento de fundos. " * 10
            day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")  # dentro da janela de 14 dias da atividade
            (proj / "s1.jsonl").write_text("\n".join([
                claude_line("user", long_ask, f"{day}T10:00:00Z"), claude_line("assistant", "ok", f"{day}T10:30:00Z")]),
                encoding="utf-8")
            (proj / "s2.jsonl").write_text(claude_line("user", "oi", f"{day}T10:00:00Z"), encoding="utf-8")
            calls = []

            def fake(session):
                calls.append(session.sid)
                return {"titulo": "Revisão do decreto", "resumo": "Resumo.", "uma_linha": "Revisou o decreto",
                        "pessoas": ["Debora Silva"], "preferencias": ["Prefere minutas objetivas"]}
            with patch.object(sh, "HOME", home), patch.object(sh, "STATE_FILE", Path(tmp) / "st.json"), \
                    patch.object(sh.cerebro, "vault_dir", return_value=vault):
                first = sh.run(10, ("claude",), summarizer=fake, now=9e9)
                second = sh.run(10, ("claude",), summarizer=fake, now=9e9)
            self.assertEqual((first["resumidas"], first["puladas"]), (1, 1))
            self.assertEqual(second["pendentes"], 0)
            self.assertEqual(calls, ["s1"])
            note = next((vault / "sessoes").glob("*.md")).read_text(encoding="utf-8")
            self.assertIn("[[Debora Silva]]", note)
            self.assertIn("Revisou o decreto", (vault / "_atividade.md").read_text(encoding="utf-8"))
            self.assertIn("Prefere minutas objetivas", (vault / "perfil" / "Jeito de trabalhar.md").read_text(encoding="utf-8"))


class PersonFactsTest(unittest.TestCase):
    def test_facts_go_only_to_existing_unambiguous_people_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            people = vault / "pessoas"
            people.mkdir()
            for name in ("Debora Silva", "João Souza", "João Lima", "__JARVIS_USER_FULLNAME__"):
                (people / f"{name}.md").write_text("---\nultima_interacao: 2026-01-01\n---\n\n## Histórico\n",
                                                   encoding="utf-8")
            session = sh.Session("claude", "s1", "Lei", sh.datetime(2026, 10, 5, 9), sh.datetime(2026, 10, 5, 10), Path("x"))
            data = {"fatos_pessoas": [
                {"pessoa": "Debora", "fato": "Assumiu a Secretaria de Saúde."},
                {"pessoa": "João", "fato": "Vai revisar o contrato"},       # ambíguo
                {"pessoa": "Pedro Alves", "fato": "Fornecedor de toner"},  # sem ficha
                {"pessoa": "__JARVIS_USER__", "fato": "Prefere minutas curtas"},   # dono do cofre
                {"pessoa": "Debora Silva", "fato": "senha: hunter22abc"}]}
            self.assertEqual(sh.append_person_facts(data, session, "Nota", vault), 2)
            self.assertEqual(sh.append_person_facts(data, session, "Nota", vault), 0)
            debora = (people / "Debora Silva.md").read_text(encoding="utf-8")
            self.assertIn("- 2026-10-05 — Assumiu a Secretaria de Saúde (fonte: [[Nota|sessão Claude Code]])", debora)
            self.assertIn("ultima_interacao: 2026-10-05", debora)
            self.assertIn("senha: hunter22abc", debora)
            self.assertFalse(list(people.glob("Pedro*")))
            for other in ("João Souza", "João Lima", "__JARVIS_USER_FULLNAME__"):
                self.assertNotIn("2026-10-05", (people / f"{other}.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class ProjectLinkTest(unittest.TestCase):
    def test_resolve_by_folder_alias_then_keywords_and_block_refresh(self):
        import project_notes as pn
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "projetos").mkdir()
            (vault / "sessoes").mkdir()
            (vault / "projetos" / "Ranking de Xadrez.md").write_text(
                "---\ntipo: projeto\naliases: [chess_nextjs]\npalavras_chave: [xadrez, torneio]\n---\n# Ranking\n", encoding="utf-8")
            self.assertEqual(pn.resolve(vault, "chess_nextjs"), "Ranking de Xadrez")
            self.assertEqual(pn.resolve(vault, "__JARVIS_USER__", "ajuste do torneio de xadrez"), "Ranking de Xadrez")
            self.assertIsNone(pn.resolve(vault, "__JARVIS_USER__", "assunto qualquer"))
            (vault / "sessoes" / "2026-10-01 Codex — Torneio (abc12345).md").write_text(
                '---\ntipo: sessao-ia\nprojeto_nota: "[[Ranking de Xadrez]]"\n---\n# Torneio\n', encoding="utf-8")
            self.assertEqual(pn.refresh_session_blocks(vault, {"2026-10-01 Codex — Torneio (abc12345)": "ajustou"}), 1)
            text = (vault / "projetos" / "Ranking de Xadrez.md").read_text(encoding="utf-8")
            self.assertIn("ajustou", text)
            self.assertEqual(pn.refresh_session_blocks(vault, {"2026-10-01 Codex — Torneio (abc12345)": "ajustou"}), 0)  # idempotente
