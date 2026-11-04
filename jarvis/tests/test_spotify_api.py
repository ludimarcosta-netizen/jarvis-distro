"""Spotify: escolhe o pedido certo, nunca toca em caixa de som escolhida sozinho, só diz que tocou com confirmação."""
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spotify_api as sp
from local_media import MediaSession
from local_media_commands import try_command

SPEAKERS = [{"id": "casa", "type": "Speaker", "name": "Todo lugar", "is_active": False},
            {"id": "echo", "type": "Speaker", "name": "Echo Dot", "is_active": False}]


class Response(io.BytesIO):
    def __init__(self, status, payload):
        super().__init__(json.dumps(payload).encode() if payload is not None else b"")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSpotify:
    def __init__(self, devices, playing=None, tracks=None):
        self.devices, self.playing, self.calls = devices, playing, []
        self.tracks = tracks or [{"uri": "spotify:track:boate", "name": "Boate Azul", "artists": [{"name": "Edson & Marrone"}]},
                                 {"uri": "spotify:track:evid", "name": "Evidências", "artists": [{"name": "Chitãozinho & Xororó"}],
                                  "album": {"uri": "spotify:album:amor"}}]

    def __call__(self, request, timeout):
        url = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(url.query)
        self.calls.append((request.get_method(), url.path, query, json.loads(request.data) if request.data else None))
        if url.path.endswith("/search"):
            kind = query["type"][0]
            items = self.tracks if kind == "track" else [{"uri": "spotify:playlist:rock", "name": "Rock 80", "owner": {"display_name": "x"}}]
            return Response(200, {kind + "s": {"items": items}})
        if url.path.endswith("/devices"):
            return Response(200, {"devices": self.devices})
        if url.path.endswith("/play"):
            return Response(204, None)
        if url.path.endswith("/volume"):
            for d in self.devices:
                if d.get("is_active") and getattr(self, "obey_volume", True):
                    d["volume_percent"] = int(query["volume_percent"][0])
            return Response(204, None)
        if url.path.endswith("/me/player"):
            return Response(200, self.playing) if self.playing else Response(204, None)
        raise AssertionError(url.path)


class SpotifyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.token = Path(tmp.name) / "token.json"
        self.token.write_text(json.dumps({"access_token": "A", "refresh_token": "R", "expires_at": 9e9}), encoding="utf-8")
        self.launched = []

    def client(self, fake):
        return sp.Client(self.token, opener=fake, sleep=lambda s: None, launch=lambda: self.launched.append(1), local_name="Meu-PC")

    def test_picks_result_matching_the_words_and_uses_market_br(self):
        fake = FakeSpotify([])
        found = self.client(fake).find("evidencias chitaozinho")
        self.assertEqual(found["uri"], "spotify:track:evid")
        self.assertEqual(fake.calls[0][2]["market"], ["BR"])
        self.assertEqual(self.client(fake).find("playlist de rock anos 80")["kind"], "playlist")
        self.assertEqual(self.client(fake).find("sertanejo")["kind"], "playlist")

    def test_never_plays_on_a_speaker_it_picked_itself(self):
        fake = FakeSpotify(SPEAKERS)
        result = sp.Client(self.token, opener=fake, sleep=lambda s: None, launch=lambda: self.launched.append(1))
        with self.assertRaises(sp.SpotifyError):
            result.play("evidencias")
        self.assertEqual(self.launched, [1], "abre o Spotify do PC uma vez")
        self.assertFalse([c for c in fake.calls if c[1].endswith("/play")])

    def test_prefers_active_device_then_this_pc(self):
        active = SPEAKERS + [{"id": "pc", "type": "Computer", "is_active": False},
                             {"id": "cel", "type": "Smartphone", "is_active": True}]
        self.assertEqual(self.client(FakeSpotify(active)).device(), "cel")
        self.assertEqual(self.client(FakeSpotify(SPEAKERS + [{"id": "pc", "type": "Computer", "name": "meu-pc"}])).device(), "pc")
        self.assertEqual(self.launched, [])

    def test_inactive_remote_computer_is_never_chosen(self):
        remote = {"id": "remoto", "type": "Computer", "name": "Outro-PC"}
        fake = FakeSpotify([remote])
        with self.assertRaises(sp.SpotifyError, msg="não confundir Computer com este PC"):
            self.client(fake).play("evidencias")
        self.assertEqual(self.launched, [1])
        self.assertFalse([c for c in fake.calls if c[0] == "PUT"])
        local = {"id": "local", "type": "Computer", "name": " MEU-PC "}
        self.assertEqual(self.client(FakeSpotify([remote, local])).device(), "local")

    def test_ambiguous_local_names_require_selection(self):
        fake = FakeSpotify([{"id": ident, "type": "Computer", "name": "Meu-PC"} for ident in ("a", "b")])
        with self.assertRaisesRegex(sp.SpotifyError, "Selecione"):
            self.client(fake).play("evidencias")
        self.assertFalse([c for c in fake.calls if c[0] == "PUT"])

    def test_default_local_name_comes_from_this_machine(self):
        with patch.object(sp.socket, "gethostname", return_value=" PC-LOCAL "):
            self.assertEqual(sp.Client(self.token).local_name, "pc-local")

    def test_reports_playing_only_when_spotify_confirms_and_never_repeats(self):
        pc = [{"id": "pc", "type": "Computer", "is_active": True}]
        ok = FakeSpotify(pc, playing={"is_playing": True, "item": {"uri": "spotify:track:evid"}})
        self.assertTrue(self.client(ok).play("evidencias chitaozinho")["confirmed"])
        play_call = [c for c in ok.calls if c[1].endswith("/play")]
        self.assertEqual(play_call[0][3], {"context_uri": "spotify:album:amor", "offset": {"uri": "spotify:track:evid"}})
        self.assertEqual(play_call[0][2]["device_id"], ["pc"])
        stuck = FakeSpotify(pc, playing={"is_playing": True, "item": {"uri": "spotify:track:outra"}})
        result = self.client(stuck).play("evidencias")
        self.assertFalse(result["confirmed"])
        self.assertIn("Não repeti", result["message"])
        self.assertEqual(len([c for c in stuck.calls if c[1].endswith("/play")]), 1)

    def test_expired_token_is_refreshed_and_refresh_token_kept(self):
        self.token.write_text(json.dumps({"access_token": "old", "refresh_token": "R", "expires_at": 0}), encoding="utf-8")
        with patch.object(sp, "client_id", return_value="f" * 32), \
                patch.object(sp, "_post_form", return_value={"access_token": "new", "expires_in": 3600}) as post:
            self.assertEqual(sp.Client(self.token, clock=lambda: 1000.0).token(), "new")
        self.assertEqual(post.call_args.args[1]["grant_type"], "refresh_token")
        self.assertEqual(json.loads(self.token.read_text(encoding="utf-8"))["refresh_token"], "R")

    def test_refusal_message_is_safe_and_public_play_never_raises(self):
        def deny(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "x", {}, io.BytesIO(b'{"token":"segredo"}'))
        with self.assertRaises(sp.SpotifyError) as err:
            self.client(deny).find("x")
        self.assertNotIn("segredo", str(err.exception))
        with patch.object(sp, "_client", self.client(deny)):
            self.assertFalse(sp.play("evidencias")["ok"])

    def test_missing_login_is_explained(self):
        self.token.unlink()
        with self.assertRaises(sp.SpotifyError) as err:
            self.client(FakeSpotify([])).token()
        self.assertIn("login", str(err.exception))


    def test_freshly_opened_app_gets_time_to_get_ready(self):
        class Appears(FakeSpotify):
            def __call__(self, request, timeout):
                if request.full_url.endswith("/devices") and len(self.calls) >= 1:
                    self.devices = [{"id": "pc", "type": "Computer", "name": "Meu-PC"}]
                return super().__call__(request, timeout)
        waits = []
        client = sp.Client(self.token, opener=Appears(SPEAKERS), sleep=waits.append, launch=lambda: self.launched.append(1), local_name="Meu-PC")
        self.assertEqual(client.device(), "pc")
        self.assertEqual(self.launched, [1])
        self.assertIn(sp.APP_READY_S, waits)


    def test_volume_sets_spotify_own_control_and_confirms(self):
        pc = [{"id": "pc", "type": "Computer", "is_active": True, "volume_percent": 63, "supports_volume": True}]
        result = self.client(FakeSpotify(pc)).volume(80)
        self.assertEqual((result["ok"], result["volume"]), (True, 80))
        self.assertIn("63% → 80%", result["message"])
        self.assertEqual(self.client(FakeSpotify(pc)).volume(-1)["volume"], 80)

    def test_volume_unconfirmed_is_not_repeated(self):
        fake = FakeSpotify([{"id": "pc", "type": "Computer", "is_active": True, "volume_percent": 63}])
        fake.obey_volume = False
        result = self.client(fake).volume(90)
        self.assertFalse(result["ok"])
        self.assertEqual(len([c for c in fake.calls if c[1].endswith("/volume")]), 1)

    def test_volume_without_active_device_touches_no_speaker(self):
        fake = FakeSpotify(SPEAKERS)
        with self.assertRaises(sp.SpotifyError):
            self.client(fake).volume(50)
        self.assertFalse([c for c in fake.calls if c[1].endswith("/volume")])


class LoginTests(unittest.TestCase):
    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def run_login(self, tamper_state=False):
        port = self.free_port()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        token = Path(tmp.name) / "t.json"
        seen = {}

        def browser(url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            seen.update(query)
            state = "outro" if tamper_state else query["state"][0]

            def visit():
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=C&state={state}", timeout=5).read()
                except urllib.error.HTTPError:
                    pass
            threading.Thread(target=visit, daemon=True).start()
        with patch.object(sp, "client_id", return_value="f" * 32), \
                patch.object(sp, "_post_form", return_value={"access_token": "A", "refresh_token": "R", "expires_in": 3600}) as post:
            try:
                sp.login(open_url=browser, timeout=3, path=token, port=port)
            except sp.SpotifyError:
                return seen, None, token
        return seen, post, token

    def test_pkce_login_saves_token_without_client_secret(self):
        seen, post, token = self.run_login()
        self.assertEqual(seen["code_challenge_method"], ["S256"])
        fields = post.call_args.args[1]
        self.assertIn("code_verifier", fields)
        self.assertNotIn("client_secret", fields)
        self.assertEqual(json.loads(token.read_text(encoding="utf-8"))["refresh_token"], "R")

    def test_callback_with_wrong_state_is_rejected(self):
        _, post, token = self.run_login(tamper_state=True)
        self.assertIsNone(post)
        self.assertFalse(token.exists())


class SharedExecutorTests(unittest.TestCase):
    def test_text_shortcut_plays_through_media_session(self):
        calls = []
        media = MediaSession(spotify=lambda q: calls.append(q) or {"ok": True, "message": "Tocando no Spotify: X."})
        self.assertEqual(try_command("Jarvis, toca Evidências de Chitãozinho e Xororó no Spotify", media), "Tocando no Spotify: X.")
        self.assertEqual(try_command("põe sertanejo no spotify", media), "Tocando no Spotify: X.")
        for text in ("não toca rock no spotify", "toca rock, depois manda mensagem no spotify", "toca rock no youtube"):
            self.assertIsNone(try_command(text, media))
        self.assertEqual(calls, ["Evidências de Chitãozinho e Xororó", "sertanejo"])

    def test_media_session_validates_query(self):
        media = MediaSession(spotify=lambda q: {"ok": True})
        for query in (None, "", "x" * 121):
            with self.assertRaises(ValueError):
                media.dispatch("spotify.play", {"query": query})
