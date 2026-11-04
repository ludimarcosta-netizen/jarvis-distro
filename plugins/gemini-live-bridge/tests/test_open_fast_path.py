import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import plugin_api as pa

abrir = pa._abrir
CATALOG = [{"name": n, "id": f"id-{i}"} for i, n in enumerate([
    "Spotify", "Adobe Photoshop 2026", "Adobe Illustrator 2026", "Fotos", "WhatsApp", "WhatsApp Web",
    "Vincular ao Celular", "Word", "Calculadora"])]


class FindTests(unittest.TestCase):
    def names(self, query):
        return [a["name"] for a in abrir.find(query, CATALOG)]

    def test_exact_word_prefix_and_misheard_names(self):
        self.assertEqual(self.names("spotify"), ["Spotify"])
        self.assertEqual(self.names("o photoshop"), ["Adobe Photoshop 2026"])
        self.assertEqual(self.names("whatsapp"), ["WhatsApp"])  # nome exato ganha do "WhatsApp Web"
        self.assertEqual(self.names("fotoshop"), ["Adobe Photoshop 2026"])  # erro de transcrição
        self.assertEqual(self.names("spotfy"), ["Spotify"])
        self.assertEqual(self.names("adobe"), ["Adobe Photoshop 2026", "Adobe Illustrator 2026"])
        self.assertEqual(self.names("xyzabc"), [])
        self.assertEqual(self.names("o aplicativo"), [])  # só palavras de enchimento


class OpenAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.atalhos = Path(self.tmp.name) / "atalhos.json"
        self.atalhos.write_text(json.dumps({"fone": "Vincular ao Celular", "config": "ms-settings:"}), encoding="utf-8")
        self.launched = []
        for p in (patch.object(abrir, "ATALHOS", self.atalhos), patch.object(abrir, "apps", return_value=CATALOG),
                  patch.object(abrir, "_launch", side_effect=lambda t, app: self.launched.append((t, app))),
                  patch.object(abrir, "refresh_in_background")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_opens_unique_program_by_app_id(self):
        result = abrir.open_app("Spotify")
        self.assertEqual(result["mensagem"], "Abri Spotify.")
        self.assertEqual(self.launched, [("id-0", True)])

    def test_spoken_alias_and_uri_alias(self):
        self.assertTrue(abrir.open_app("o fone")["ok"])
        self.assertEqual(self.launched[-1], ("id-6", True))
        self.assertTrue(abrir.open_app("config")["ok"])
        self.assertEqual(self.launched[-1], ("ms-settings:", False))

    def test_ambiguous_lists_options_and_opens_nothing(self):
        result = abrir.open_app("adobe")
        self.assertFalse(result["ok"])
        self.assertEqual(result["opcoes"], ["Adobe Photoshop 2026", "Adobe Illustrator 2026"])
        self.assertEqual(self.launched, [])

    def test_unknown_never_waits_for_the_slow_list(self):
        result = abrir.open_app("zzqq")
        self.assertFalse(result["ok"])
        abrir.refresh_in_background.assert_called_once()
        self.assertEqual(self.launched, [])

    def test_teaching_a_new_alias(self):
        abrir.ensinar("folha", "Word")
        self.assertTrue(abrir.open_app("folha")["ok"])
        self.assertEqual(self.launched[-1], ("id-7", True))


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.launched = []
        p = patch.object(abrir, "_launch", side_effect=lambda t, app: self.launched.append((t, app)))
        p.start()
        self.addCleanup(p.stop)

    def test_search_urls_are_encoded_per_site(self):
        self.assertTrue(abrir.search("youtube", "receita de pão")["ok"])
        self.assertTrue(abrir.search("Google Maps", "prefeitura de __JARVIS_CIDADE__")["ok"])
        self.assertTrue(abrir.search("spotify", "sertanejo raiz")["ok"])
        self.assertEqual([t for t, _ in self.launched], [
            "https://www.youtube.com/results?search_query=receita+de+p%C3%A3o",
            "https://www.google.com/maps/search/prefeitura%20de%20__JARVIS_CIDADE_URL__",
            "spotify:search:sertanejo%20raiz"])

    def test_unknown_site_or_empty_term_opens_nothing(self):
        self.assertFalse(abrir.search("tiktok", "x")["ok"])
        self.assertFalse(abrir.search("youtube", "  ")["ok"])
        self.assertEqual(self.launched, [])


class LiveToolTests(unittest.TestCase):
    def test_search_tool_call_answers_without_hermes(self):
        sent = []

        async def fake_send(call_id, name, reply):
            sent.append((call_id, name, reply))

        with patch.object(pa, "_send_tool_response", side_effect=fake_send), \
             patch.object(pa._abrir, "_launch"), \
             patch.object(pa, "_hermes_turn", side_effect=AssertionError("não deve chamar o Hermes")):
            asyncio.run(pa._run_open({"id": "c2", "name": "buscar_rapido",
                                      "args": {"onde": "youtube", "termo": "jornal nacional"}}))
        self.assertEqual(sent, [("c2", "buscar_rapido", "Abri a busca por “jornal nacional” no YouTube.")])

    def test_voice_session_declares_both_functions(self):
        self.assertEqual(pa.OPEN_FUNCTION["name"], "abrir_programa")
        self.assertIn("abrir_programa", pa.LIVE_PERSONA)

    def test_open_tool_call_answers_without_hermes(self):
        sent = []

        async def fake_send(call_id, name, reply):
            sent.append((call_id, name, reply))

        with patch.object(pa, "_send_tool_response", side_effect=fake_send), \
             patch.object(pa._abrir, "open_app", return_value={"ok": True, "mensagem": "Abri Spotify.", "opcoes": []}), \
             patch.object(pa, "_hermes_turn", side_effect=AssertionError("não deve chamar o Hermes")):
            asyncio.run(pa._run_open({"id": "c1", "args": {"nome": "spotify"}}))
        self.assertEqual(sent, [("c1", "abrir_programa", "Abri Spotify.")])
        self.assertEqual(pa._state["delegation"]["status"], "done")

    def test_agenda_tool_call_answers_without_hermes(self):
        sent, calls = [], []

        async def fake_send(call_id, name, reply):
            sent.append((call_id, name, reply))

        def fake_consultar(desde=0, dias=0):
            calls.append((desde, dias))
            return "Agenda Google (08/10/2026):\n- Nenhum evento."

        with patch.object(pa, "_send_tool_response", side_effect=fake_send), \
             patch.object(pa._agenda, "consultar", side_effect=fake_consultar), \
             patch.object(pa, "_hermes_turn", side_effect=AssertionError("não deve chamar o Hermes")):
            asyncio.run(pa._run_open({"id": "c3", "name": "agenda_compromissos", "args": {"quando": "amanha"}}))
        self.assertEqual(calls, [(1, 0)])
        self.assertEqual(sent, [("c3", "agenda_compromissos", "Agenda Google (08/10/2026):\n- Nenhum evento.")])
        self.assertIn("agenda_compromissos", pa.LIVE_PERSONA)

    def test_agenda_failure_is_spoken_not_raised(self):
        sent = []

        async def fake_send(call_id, name, reply):
            sent.append((call_id, name, reply))

        with patch.object(pa, "_send_tool_response", side_effect=fake_send), \
             patch.object(pa._agenda, "consultar", side_effect=RuntimeError("venv sumiu")):
            asyncio.run(pa._run_open({"id": "c4", "name": "agenda_compromissos", "args": {}}))
        self.assertEqual(sent, [("c4", "agenda_compromissos", "Não consegui: RuntimeError.")])
        self.assertEqual(pa._state["delegation"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
