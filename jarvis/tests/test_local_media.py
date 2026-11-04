"""Media privacy, bounded lookup, deterministic selection and failure contracts."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_media as lm
from local_media_commands import try_command
from local_workspace import Workspace

ROW = {"id": "tRsQsTMvPNg", "title": "<script>música</script>", "channel": "Anthropic", "duration": "1:00"}


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.open = Mock()
        self.fetch = Mock(return_value=[ROW])
        self.control = Mock(return_value={"ok": True, "sessions": [{"app": "Spotify.exe", "status": "Paused"}]})
        # spotify_volume=False: estes testes cobrem o mixer do Windows, sem chamar o Spotify real.
        self.media = lm.MediaSession(fetch=self.fetch, opener=self.open, clock=lambda: self.now, controller=self.control,
                                     spotify_volume=False)

    def test_search_never_opens_and_choice_opens_only_canonical_video(self):
        rows = self.media.dispatch("videos.search", {"query": "música & foco"})
        self.open.assert_not_called()
        self.assertIn('m%C3%BAsica+%26+foco', rows["fallback"])
        result = self.media.dispatch("videos.open", {"ref": rows["results"][0]["ref"]})
        self.open.assert_called_once_with("https://www.youtube.com/watch?v=tRsQsTMvPNg")
        self.assertNotIn("reproduzindo", result["message"])

    def test_stale_cross_session_and_arbitrary_urls_cannot_open(self):
        ref = self.media.dispatch("videos.search", {"query": "música"})["results"][0]["ref"]
        with self.assertRaises(ValueError):
            lm.MediaSession(opener=self.open).dispatch("videos.open", {"ref": ref})
        self.now += lm.TTL + 1
        with self.assertRaises(ValueError):
            self.media.dispatch("videos.open", {"ref": ref})
        with self.assertRaises(ValueError):
            self.media.dispatch("videos.open", {"ref": "javascript:alert(1)"})
        self.open.assert_not_called()

    def test_new_search_invalidates_previous_choice_even_when_fetch_fails(self):
        old = self.media.dispatch("videos.search", {"query": "primeira"})["results"][0]["ref"]
        self.fetch.side_effect = TimeoutError()
        result = self.media.dispatch("videos.search", {"query": "outra"})
        self.assertTrue(result["fallback"].startswith("https://www.youtube.com/results?"))
        self.assertFalse(result["results"])
        with self.assertRaises(ValueError):
            self.media.dispatch("videos.open", {"ref": old})

    def test_rejects_bad_number_and_query(self):
        for number in (True, 0, -1, 100, "1"):
            with self.assertRaises(ValueError):
                self.media.dispatch("videos.open", {"number": number})
        for query in (None, "", " " * 5, "x" * 181):
            with self.assertRaises(ValueError):
                self.media.dispatch("videos.search", {"query": query})

    def test_commands_need_returned_session_and_do_not_retry(self):
        with self.assertRaises(ValueError):
            self.media.dispatch("media.command", {"ref": "Spotify.exe", "command": "play"})
        ref = self.media.dispatch("media.status", {})["sessions"][0]["ref"]
        self.control.return_value = {"ok": False, "error": "Não confirmado"}
        result = self.media.dispatch("media.command", {"ref": ref, "command": "play"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.control.call_count, 2)
        self.control.assert_called_with("play", "Spotify.exe")

    def test_status_refresh_invalidates_old_app_reference(self):
        old = self.media.dispatch("media.status", {})["sessions"][0]["ref"]
        self.media.dispatch("media.status", {})
        with self.assertRaises(ValueError):
            self.media.dispatch("media.command", {"ref": old, "command": "next"})

    def test_busy_commands_are_not_queued_or_duplicated(self):
        self.media.lock.acquire()
        try:
            with self.assertRaises(ValueError):
                self.media.dispatch("videos.search", {"query": "música"})
        finally:
            self.media.lock.release()
        self.fetch.assert_not_called()

    def test_three_text_turns_keep_choice_and_control_without_model(self):
        self.assertIn("Anthropic", try_command("procura música no YouTube", self.media))
        self.assertIn("vídeo escolhido", try_command("abra o primeiro vídeo", self.media))
        self.control.side_effect = [{"ok": True, "sessions": [{"app": "Spotify.exe", "status": "Playing"}]}, {"ok": True, "status": "Paused"}]
        self.assertIn("confirmado", try_command("pause o Spotify", self.media))

    def test_negative_and_compound_commands_have_no_effect(self):
        for text in ("não pause o Spotify", "pause o Spotify e mande mensagem", "abra o primeiro vídeo e apague o arquivo",
                     "ele disse pause o Spotify", "procura música e abra outra no YouTube"):
            self.assertIsNone(try_command(text, self.media))
        self.control.assert_not_called()
        self.fetch.assert_not_called()
        self.open.assert_not_called()

    def test_ambiguous_spotify_does_not_control_another_player(self):
        self.control.return_value = {"ok": True, "sessions": [{"app": "brave.exe", "status": "Playing"}]}
        self.assertIn("única sessão", try_command("pause o Spotify", self.media))
        self.control.assert_called_once_with("status")

    def test_workspace_uses_same_executor_and_profile_session_boundary(self):
        workspace = Workspace(media_factory=lambda: self.media)
        session = workspace.dispatch({"action": "start"})["session"]
        result = workspace.dispatch({"action": "videos.search", "session": session, "query": "teste"})
        self.assertTrue(result["ok"])
        with self.assertRaises(ValueError):
            Workspace().dispatch({"action": "videos.open", "session": session, "ref": result["results"][0]["ref"]})

    def test_parser_ignores_ads_duplicates_and_non_video_urls(self):
        video = {"videoRenderer": {"videoId": ROW["id"], "title": {"runs": [{"text": "música"}]}}}
        data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {"contents": [
            {"itemSectionRenderer": {"contents": [video, video, {"adSlotRenderer": video},
             {"videoRenderer": {"videoId": "javascript:alert(1)"}}]}}
        ]}}}}}
        html = 'var ytInitialData = ' + json.dumps(data) + '; another = {"unrelated": true};'
        result = lm.parse_videos(html)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "música")
        with self.assertRaises(ValueError):
            lm.parse_videos('var ytInitialData = <html>')

    def test_volume_needs_returned_session_valid_level_and_reports_mixer_value(self):
        with self.assertRaises(ValueError):
            self.media.dispatch("media.volume", {"ref": "Spotify.exe", "level": 30})
        ref = self.media.dispatch("media.status", {})["sessions"][0]["ref"]
        for level in (101, -2, True, "30", 3.5):
            with self.assertRaises(ValueError):
                self.media.dispatch("media.volume", {"ref": ref, "level": level})
        self.control.return_value = {"ok": True, "volume": 30}
        self.assertIn("30%", self.media.dispatch("media.volume", {"ref": ref, "level": 30})["message"])
        self.control.assert_called_with("volume", "Spotify.exe", 30)
        self.control.return_value = {"ok": False, "error": "O mixer não confirmou o novo volume."}
        self.assertFalse(self.media.dispatch("media.volume", {"ref": ref, "level": 40})["ok"])

    def test_text_volume_shortcut_controls_only_spotify(self):
        self.control.side_effect = [{"ok": True, "sessions": [{"app": "Spotify.exe", "status": "Playing"}]},
                                    {"ok": True, "volume": 25}]
        self.assertIn("25%", try_command("Jarvis, coloca o volume do Spotify em 25%", self.media))
        self.control.assert_called_with("volume", "Spotify.exe", 25)
        self.control.reset_mock(side_effect=True)
        self.assertEqual(try_command("volume do spotify 300", self.media), "O volume vai de 0 a 100.")
        self.assertIsNone(try_command("volume do spotify em 30 e pause o youtube", self.media))
        self.control.assert_not_called()

    def test_spotify_volume_uses_spotify_own_control_when_authorized(self):
        levels = []
        media = lm.MediaSession(controller=self.control, spotify_volume=lambda level: levels.append(level) or
                                {"ok": True, "volume": level, "message": f"Volume do Spotify: 63% → {level}%."})
        ref = media.dispatch("media.status", {})["sessions"][0]["ref"]
        self.assertIn("63% → 80%", media.dispatch("media.volume", {"ref": ref, "level": 80})["message"])
        self.assertEqual(levels, [80])
        self.assertNotIn("volume", [c.args[0] for c in self.control.call_args_list])

    def test_relative_volume_starts_from_the_real_value_and_is_clamped(self):
        state = {"volume": 80}

        def spotify(level):
            if level >= 0:
                state["volume"] = level
            return {"ok": True, "volume": state["volume"], "message": f"Volume do Spotify: {state['volume']}%."}
        media = lm.MediaSession(controller=self.control, spotify_volume=spotify)
        ref = media.dispatch("media.status", {})["sessions"][0]["ref"]
        self.assertEqual(media.dispatch("media.volume", {"ref": ref, "step": 15})["volume"], 95)
        self.assertEqual(media.dispatch("media.volume", {"ref": ref, "step": 15})["volume"], 100)
        self.assertEqual(media.dispatch("media.volume", {"ref": ref, "step": -15})["volume"], 85)
        for step in (0, True, "15", 500):
            with self.assertRaises(ValueError):
                media.dispatch("media.volume", {"ref": ref, "step": step})

    def test_text_relative_volume_shortcut(self):
        steps = []
        media = lm.MediaSession(controller=self.control, spotify_volume=lambda level: steps.append(level) or {"ok": True, "volume": 50, "message": "ok"})
        self.assertEqual(try_command("Jarvis, sobe o volume do Spotify", media), "ok")
        self.assertEqual(try_command("abaixa um pouco o volume do spotify", media), "ok")
        self.assertEqual(steps, [-1, 65, -1, 35])

    def test_other_players_keep_the_windows_mixer(self):
        self.control.return_value = {"ok": True, "sessions": [{"app": "brave.exe", "status": "Playing"}]}
        media = lm.MediaSession(controller=self.control, spotify_volume=lambda level: self.fail("não é Spotify"))
        ref = media.dispatch("media.status", {})["sessions"][0]["ref"]
        self.control.return_value = {"ok": True, "volume": 40}
        self.assertTrue(media.dispatch("media.volume", {"ref": ref, "level": 40})["ok"])
        self.control.assert_called_with("volume", "brave.exe", 40)

    def test_system_bridge_validates_volume_before_spawning(self):
        with patch.object(lm.subprocess, "run") as run:
            for level in (101, -5, True):
                with self.assertRaises(ValueError):
                    lm.control("volume", "Spotify.exe", level)
            run.assert_not_called()

    def test_system_bridge_sends_arguments_without_shell_and_sanitizes_failure(self):
        with patch.object(lm.subprocess, "run", return_value=Mock(returncode=0, stdout=b'{"ok":true,"sessions":[]}')) as run:
            self.assertTrue(lm.control("status")["ok"])
            self.assertNotIn("shell", run.call_args.kwargs)
        with patch.object(lm.subprocess, "run", side_effect=OSError("secret")):
            self.assertNotIn("secret", lm.control("status")["error"])
