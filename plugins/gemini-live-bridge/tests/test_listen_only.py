"""Bloqueio de voz, persistência e transporte no modo Só ouvir."""
import asyncio
import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'dashboard'),
               str(Path(__file__).resolve().parents[3] / 'jarvis')]
import listening_mode as mode
import plugin_api as pa


class ListenOnlyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'mode.json'
        state = dict(pa._state)
        self.addCleanup(lambda: pa._state.update(state))
        pa._state.update(session=None, ws=None, task=None, farewell_task=None, audio=[], input=[], output=[],
                         voice_pcm=bytearray(), manual=True, handle=None)
        module = SimpleNamespace(enabled=lambda: mode.enabled(self.path), save=lambda v: mode.save(v, self.path))
        for target, replacement in [('_jarvis_module', Mock(return_value=module)), ('_ensure_bridge', Mock()),
                                    ('_sentinel_alive', Mock(return_value=True)), ('_sentinel_state', Mock(return_value={'alive': True, 'muted': False})),
                                    ('_send_sentinel_command', Mock(return_value={'muted': False})), ('_live_log', Mock())]:
            mocker = patch.object(pa, target, replacement)
            mocker.start()
            self.addCleanup(mocker.stop)
        mocker = patch.object(pa._voice, 'end', AsyncMock())
        mocker.start()
        self.addCleanup(mocker.stop)

    async def test_activation_clears_speech_closes_live_and_unmutes_only_the_listener(self):
        ws = AsyncMock()
        pa._state.update(session='live', ws=ws, audio=['queued'], wake_text='old command')
        result = await pa.listen_only({'enabled': True})
        self.assertTrue(result['ok'])
        self.assertTrue(mode.enabled(self.path))
        ws.close.assert_awaited_once()
        self.assertIsNone(pa._state['session'])
        self.assertEqual(pa._state['audio'], [])
        self.assertEqual(pa._state['wake_text'], '')
        pa._send_sentinel_command.assert_called_once_with('UNMUTE')

    async def test_saved_mode_blocks_manual_start_audio_and_farewell_without_cloud_calls(self):
        mode.save(True, self.path)
        ws = AsyncMock()
        pa._state.update(session='live', ws=ws)
        with patch.object(pa, '_api_key') as key:
            self.assertFalse((await pa.start())['ok'])
        key.assert_not_called()
        self.assertTrue((await pa.audio({'data': 'pcm'}))['skipped'])
        self.assertTrue((await pa.farewell())['ok'])
        ws.send.assert_not_awaited()

    async def test_invalid_setting_is_rejected_and_disabling_does_not_start_voice(self):
        for value in ['false', 1, None]:
            self.assertFalse((await pa.listen_only({'enabled': value}))['ok'])
        self.assertFalse(self.path.exists())
        mode.save(True, self.path)
        result = await pa.listen_only({'enabled': False})
        self.assertTrue(result['ok'])
        self.assertFalse(mode.enabled(self.path))
        pa._send_sentinel_command.assert_not_called()
        self.assertIsNone(pa._state['session'])

    async def test_late_model_audio_is_not_retained_or_delivered(self):
        mode.save(True, self.path)
        ws = AsyncMock()
        ws.recv.side_effect = [json.dumps({'serverContent': {'modelTurn': {'parts': [{'inlineData': {'data': 'late'}}]}}}),
                               asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await pa._reader(ws)
        self.assertEqual(pa._state['audio'], [])
        pa._state['audio'] = ['stale']
        with patch.object(pa._voice, 'status', AsyncMock(return_value={})), patch.object(pa, '_meeting_prompt', return_value=None):
            result = await pa.poll()
        self.assertTrue(result['listen_only'])
        self.assertEqual(result['audio'], [])

    async def test_wake_does_not_seed_a_command_or_warm_cloud_session(self):
        mode.save(True, self.path)
        before = pa._state['wake_seq']
        server = await asyncio.start_server(pa._bridge_handle, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        def request():
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('POST', '/wake', json.dumps({'text': 'Hey Jarvis'}), {'Content-Type': 'application/json'})
            response = conn.getresponse()
            result = json.loads(response.read())
            conn.close()
            return result
        try:
            with patch.object(pa, '_prefetch_live') as prefetch:
                result = await asyncio.to_thread(request)
            self.assertTrue(result['listen_only'])
            self.assertEqual(pa._state['wake_seq'], before)
            prefetch.assert_not_called()
        finally:
            server.close()
            await server.wait_closed()

    async def test_activation_while_upstream_is_connecting_never_enters_live(self):
        ws = AsyncMock()
        ws.recv.return_value = json.dumps({'setupComplete': {}})
        async def connect(*args, **kwargs):
            mode.save(True, self.path)
            return ws
        with patch.object(pa, '_api_key', return_value='test'), \
                patch.object(pa, '_candidates', return_value=['models/test']), \
                patch.object(pa, '_take_token', return_value='test'), \
                patch('websockets.connect', side_effect=connect), \
                patch.object(pa._voice, 'begin', AsyncMock()) as begin:
            result = await pa.start()
        self.assertFalse(result['ok'])
        self.assertTrue(result['listen_only'])
        self.assertIsNone(pa._state['session'])
        ws.close.assert_awaited_once()
        begin.assert_not_awaited()
