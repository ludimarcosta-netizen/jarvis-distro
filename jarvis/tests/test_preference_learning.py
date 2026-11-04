"""Aprendizado de preferências: evidência antes de aprender, nada sensível, reversível e sem perder sessões."""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preference_learning as pl

# Embeddings simulados: cada tema é um eixo; frases do mesmo tema ficam idênticas no espaço.
TOPICS = ["diff", "portugu", "emoji", "relatórios longos", "relatórios curtos", "planilha"]


def embed(texts):
    rows = []
    for text in texts:
        v = [1.0 if topic in text.lower() else 0.0 for topic in TOPICS]
        if "relatórios" in text.lower():
            v[3] += 0.8  # temas próximos (cosseno ~0,74): relacionados, mas não a mesma preferência
            v[4] += 0.8
        rows.append(v if any(v) else [0.0] * (len(TOPICS) - 1) + [0.01])
    return rows


class Ask:
    def __init__(self, prefs=None, contradicts=False, fail=False):
        self.prefs, self.contradicts, self.fail, self.prompts = prefs or [], contradicts, fail, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise OSError("ollama fora")
        if "ANTIGA:" in prompt:
            old, new = (prompt.split(label, 1)[1].split("\n", 1)[0].strip() for label in ("ANTIGA:", "NOVA:"))
            return {"relacao": "mesma" if old == new else "contradiz" if self.contradicts else "diferente"}
        return {"preferencias": self.prefs}


def pref(text, explicit=False, sensitive=False):
    return {"texto": text, "explicita": explicit, "sensivel": sensitive, "trecho": "de agora em diante " + text}


# O que o senhor "disse" nas sessões simuladas: contém a citação de cada preferência.
SPOKEN = "\n".join("- de agora em diante " + text for text in ['Prefere planilha da conta.', 'Prefere relatórios curtos.', 'Prefere relatórios longos.', 'Prefere respostas em português.', 'Prefere ver o diff antes de salvar.', 'Quer lembrar do remédio da pressão.', 'Quer o diff mostrado antes de gravar.'])


class LearnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name) / "vault"
        self.vault.mkdir()
        self.state = Path(tmp.name) / "state.json"

    def learner(self, ask, today=date(2026, 10, 16)):
        return pl.Learner(self.vault, self.state, ask=ask, embed=embed, today=today)

    def notes(self):
        return list((self.vault / "perfil" / "aprendido").glob("*.md"))

    def test_explicit_preference_is_learned_at_once_with_source(self):
        learner = self.learner(Ask([pref("Prefere ver o diff antes de salvar.", explicit=True)]))
        learner.observe_session(SPOKEN, "sessao-a", "2026-10-16")
        self.assertEqual(len(learner.consolidate()), 1)
        learner.save()
        [note] = self.notes()
        text = note.read_text(encoding="utf-8")
        self.assertIn("Prefere ver o diff antes de salvar.", text)
        self.assertIn("[[sessao-a]]", text)
        self.assertIn("Declarada pelo senhor", text)
        self.assertIn("aprendi: Prefere ver o diff", (self.vault / "perfil" / "Aprendizado recente.md").read_text(encoding="utf-8"))

    def test_inferred_needs_three_sessions_on_two_days(self):
        ask = Ask([pref("Prefere respostas em português.")])
        learner = self.learner(ask)
        learner.observe_session(SPOKEN, "s1", "2026-10-10")
        learner.observe_session(SPOKEN, "s1", "2026-10-10")  # mesma sessão de novo não conta
        learner.observe_session(SPOKEN, "s2", "2026-10-10")
        self.assertEqual(learner.consolidate(), [])
        learner.observe_session(SPOKEN, "s3", "2026-10-10")
        self.assertEqual(learner.consolidate(), [], "três sessões no mesmo dia ainda não bastam")
        learner.observe_session(SPOKEN, "s4", "2026-10-12")
        self.assertEqual(len(learner.consolidate()), 1)
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("4 sessões em 2 dias", self.notes()[0].read_text(encoding="utf-8"))

    def test_sensitive_is_never_learned_even_when_explicit(self):
        ask = Ask([pref("Quer lembrar do remédio da pressão.", explicit=True, sensitive=False),
                   pref("Prefere planilha da conta.", explicit=True, sensitive=True)])
        learner = self.learner(ask)
        self.assertEqual(learner.observe_session(SPOKEN, "s1", "2026-10-16"), 0)
        self.assertEqual(learner.consolidate(), [])
        self.assertEqual(learner.state["observacoes"], [])

    def test_missing_sensitive_flag_is_treated_as_sensitive(self):
        found = pl.extract("x", lambda prompt: {"preferencias": [{"texto": "Prefere planilha organizada.", "explicita": True}]})
        self.assertTrue(found[0]["sensivel"])

    def test_contradiction_replaces_old_and_keeps_history(self):
        learner = self.learner(Ask([pref("Prefere relatórios longos.", explicit=True)]))
        learner.observe_session(SPOKEN, "s1", "2026-10-01")
        learner.consolidate()
        learner.ask = Ask([pref("Prefere relatórios curtos.", explicit=True)], contradicts=True)
        learner.observe_session(SPOKEN, "s2", "2026-10-16")
        lines = learner.consolidate()
        self.assertTrue(any(line.startswith("substituí: Prefere relatórios longos.") for line in lines))
        states = {o["texto"]: o["estado"] for o in learner.state["observacoes"]}
        self.assertEqual(states, {"Prefere relatórios longos.": "substituida", "Prefere relatórios curtos.": "aprendida"})
        old = next(n for n in self.notes() if "longos" in n.read_text(encoding="utf-8"))
        self.assertIn('estado: "substituida"', old.read_text(encoding="utf-8"))
        self.assertIn("Substituída em 2026-10-16", old.read_text(encoding="utf-8"))

    def test_supersession_removes_indexed_preference_but_keeps_reviewable_history(self):
        from cerebro_memory import Memory
        from local_memory import MemorySession

        learner = self.learner(Ask([pref("Prefere relatórios longos.", explicit=True)]))
        learner.observe_session(SPOKEN, "s1", "2026-10-01")
        learner.consolidate()
        memory = Memory(self.vault)
        self.assertTrue(memory.search("longos", semantic=False))
        old = self.notes()[0]
        learner.ask = Ask([pref("Prefere relatórios curtos.", explicit=True)], contradicts=True)
        learner.observe_session(SPOKEN, "s2", "2026-10-16")
        learner.consolidate()
        self.assertFalse(memory.search("longos", semantic=False))
        self.assertTrue(memory.search("curtos", semantic=False))
        self.assertNotIn("longos", memory.context("relatórios")["contexto"])
        self.assertIn("longos", old.read_text(encoding="utf-8"))
        self.assertIn("longos", (self.vault / "perfil/Aprendizado recente.md").read_text(encoding="utf-8"))
        session = MemorySession(lambda: memory)
        [row] = session.dispatch("memory.search", {"query": "longos", "include_inactive": True})["results"]
        self.assertEqual(row["state"], "disabled")
        prepared = session.dispatch("memory.prepare", {"ref": row["ref"], "change": "restore", "reason": "Restauração explícita no teste"})
        session.dispatch("memory.confirm", {"ticket": prepared["ticket"]})
        self.assertTrue(memory.search("longos", semantic=False))

    def test_existing_cached_superseded_notes_are_reindexed(self):
        from cerebro_memory import Memory
        from cerebro_review import Reviews
        from contextlib import closing
        from unittest.mock import patch

        learner = self.learner(Ask([pref("Prefere relatórios longos.", explicit=True)]))
        learner.observe_session(SPOKEN, "s1", "2026-10-01")
        learner.consolidate()
        note = self.notes()[0]
        note.write_text(note.read_text(encoding="utf-8").replace('estado: "ativa"', 'estado: "substituida"'), encoding="utf-8")
        memory = Memory(self.vault)
        # Simula o cache legado que indexava notas substituídas.
        with patch.object(Reviews, "view", side_effect=lambda relative, raw, latest=None: {"text": raw}):
            memory.sync()
        stat = note.stat()
        with closing(memory.connect()) as conn, conn:
            conn.execute("UPDATE files SET stamp=? WHERE path=?", (f"{stat.st_mtime_ns}:{stat.st_size}", note.relative_to(self.vault).as_posix()))
        self.assertFalse(memory.search("longos", semantic=False))

    def test_related_but_compatible_preferences_coexist(self):
        learner = self.learner(Ask([pref("Prefere relatórios longos.", explicit=True)]))
        learner.observe_session(SPOKEN, "s1", "2026-10-01")
        learner.consolidate()
        learner.ask = Ask([pref("Prefere relatórios curtos.", explicit=True)], contradicts=False)
        learner.observe_session(SPOKEN, "s2", "2026-10-16")
        learner.consolidate()
        self.assertEqual({o["estado"] for o in learner.state["observacoes"]}, {"aprendida"})

    def test_unrepeated_observation_is_forgotten_after_60_days(self):
        learner = self.learner(Ask([pref("Prefere respostas em português.")]), today=date(2026, 12, 31))
        learner.observe_session(SPOKEN, "s1", "2026-10-01")
        learner.consolidate()
        self.assertEqual(learner.state["observacoes"], [])

    def test_model_down_queues_session_and_retries_without_duplicates(self):
        learner = self.learner(Ask(fail=True))
        learner.observe_session(SPOKEN, "s1", "2026-10-16")
        learner.observe_session(SPOKEN, "s1", "2026-10-16")
        learner.save()
        self.assertEqual(len(json.loads(self.state.read_text(encoding="utf-8"))["fila"]), 1)
        again = self.learner(Ask([pref("Prefere ver o diff antes de salvar.", explicit=True)]))
        again.retry_queue()
        self.assertEqual(again.state["fila"], [])
        self.assertEqual(len(again.consolidate()), 1)

    def test_existing_note_is_never_overwritten(self):
        learner = self.learner(Ask([pref("Prefere ver o diff antes de salvar.", explicit=True)]))
        learner.observe_session(SPOKEN, "s1", "2026-10-16")
        obs = learner.state["observacoes"][0]
        path = learner._note_path(obs)
        path.parent.mkdir(parents=True)
        path.write_text("revisada pelo senhor", encoding="utf-8")
        learner.consolidate()
        self.assertEqual(path.read_text(encoding="utf-8"), "revisada pelo senhor")

    def test_learned_preference_gets_new_evidence_without_new_note(self):
        learner = self.learner(Ask([pref("Prefere ver o diff antes de salvar.", explicit=True)]))
        learner.observe_session(SPOKEN, "s1", "2026-10-16")
        learner.consolidate()
        learner.observe_session(SPOKEN, "s2", "2026-10-17")
        self.assertEqual(learner.consolidate(), [])
        self.assertEqual(len(learner.state["observacoes"][0]["evidencias"]), 2)
        self.assertEqual(len(self.notes()), 1)


def test_same_preference_in_other_words_merges_by_model_decision():
    with tempfile.TemporaryDirectory() as tmp:
        replies = iter([{"preferencias": [pref("Prefere ver o diff antes de salvar.")]},
                        {"preferencias": [pref("Quer o diff mostrado antes de gravar.")]}, {"relacao": "mesma"}])
        learner = pl.Learner(Path(tmp), Path(tmp) / "s.json", ask=lambda prompt: next(replies), embed=embed)
        learner.observe_session(SPOKEN, "s1", "2026-10-10")
        learner.observe_session(SPOKEN, "s2", "2026-10-11")
        [obs] = learner.state["observacoes"]
        assert [e["note"] for e in obs["evidencias"]] == ["s1", "s2"]


def test_explicit_requires_real_quote_with_general_rule():
    spoken = "- tá prolixo, enxuga\n- de agora em diante nunca use emojis"
    def reply(prompt):
        return {"preferencias": [
            {"texto": "Prefere textos enxutos.", "explicita": True, "sensivel": False, "trecho": "tá prolixo, enxuga"},
            {"texto": "Não quer emojis.", "explicita": True, "sensivel": False, "trecho": "de agora em diante nunca use emojis"},
            {"texto": "Quer relatórios curtos.", "explicita": True, "sensivel": False, "trecho": "sempre relatórios curtos"}]}
    found = {p["texto"]: p["explicita"] for p in pl.extract(spoken, reply)}
    assert found == {"Prefere textos enxutos.": False, "Não quer emojis.": True, "Quer relatórios curtos.": False}


class HarvestIntegrationTests(unittest.TestCase):
    def test_history_is_learned_oldest_first_and_resumes_without_repeating(self):
        import session_harvest as sh
        from unittest.mock import patch
        when = __import__("datetime").datetime(2026, 10, 16)

        def reader(text):
            session = sh.Session(tool="codex", sid=text, project="p", started=when, ended=when, source=Path("x"))
            session.turns = [("user", text)]
            return lambda: session
        found = [("codex", Path("b"), reader("nova")), ("codex", Path("a"), reader("antiga")),
                 ("codex", Path("c"), reader("sem nota"))]
        state = {"codex:b": {"note": "n-b", "date": "2026-10-10"}, "codex:a": {"note": "n-a", "date": "2026-08-01"},
                 "codex:c": {"skipped": True}}
        seen = []

        class FakeLearner:
            def __init__(self, vault):
                self.state = {"historico": list(seen)}

            def observe_session(self, text, note, day):
                seen.append(note)
                return 1

            def consolidate(self):
                return []

            def save(self):
                pass
        with tempfile.TemporaryDirectory() as vault, patch.object(sh.cerebro, "vault_dir", return_value=Path(vault)), \
                patch.object(sh, "load_state", return_value=state), \
                patch.object(sh, "discover", side_effect=lambda tools, hermes_days: iter(found)):
            first = sh.learn_history(FakeLearner)
            again = sh.learn_history(FakeLearner)
        self.assertEqual(seen, ["n-a", "n-b"])
        self.assertEqual((first["lidas"], again["sessoes"]), (2, 0))

    def test_history_relearns_only_sessions_changed_since_learned(self):
        import session_harvest as sh
        from unittest.mock import patch
        when = __import__("datetime").datetime(2026, 10, 16)

        def reader():
            session = sh.Session(tool="claude", sid="s", project="p", started=when, ended=when, source=Path("x"))
            session.turns = [("user", "texto")]
            return session
        found = [("claude", Path("a"), reader), ("claude", Path("b"), reader)]
        state = {"claude:a": {"note": "n-a", "date": "2026-10-16", "mtime": 2.0},
                 "claude:b": {"note": "n-b", "date": "2026-10-16", "mtime": 5.0}}
        learned = {"historico": ["n-a", "n-b"]}  # formato antigo: lista de notas
        seen = []

        class FakeLearner:
            def __init__(self, vault):
                self.state = learned

            def observe_session(self, text, note, day):
                seen.append(note)
                return 0

            def consolidate(self):
                return []

            def save(self):
                pass
        with tempfile.TemporaryDirectory() as vault, patch.object(sh.cerebro, "vault_dir", return_value=Path(vault)), \
                patch.object(sh, "load_state", return_value=state), \
                patch.object(sh, "discover", side_effect=lambda tools, hermes_days: iter(found)):
            self.assertEqual(sh.learn_history(FakeLearner)["sessoes"], 0)  # migração: versão atual = aprendida
            state["claude:a"]["mtime"] = 3.0  # a sessão continuou depois de aprendida
            self.assertEqual(sh.learn_history(FakeLearner)["sessoes"], 1)
        self.assertEqual(seen, ["n-a"])
        self.assertEqual(learned["historico"], {"n-a": 3.0, "n-b": 5.0})

    def test_only_user_messages_reach_the_learner(self):
        import session_harvest as sh
        when = __import__("datetime").datetime(2026, 10, 16)
        session = sh.Session(tool="claude", sid="s", project="p", started=when, ended=when, source=Path("s.jsonl"))
        session.turns = [("user", "prefiro   respostas curtas"), ("assistant", "SEGREDO DA IA"), ("user", "ok")]
        text = sh.user_messages(session)
        self.assertIn("prefiro respostas curtas", text)
        self.assertNotIn("SEGREDO DA IA", text)


if __name__ == "__main__":
    unittest.main()
