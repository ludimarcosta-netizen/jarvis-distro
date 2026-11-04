"""Prewarm resident models independently of conversation lifetime and microphone."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import local_voice
import plugin_api as pa
from test_voice_gpu import worker_module


def test_warmup_loads_workers_sequentially_without_submitting_requests():
    calls = []
    with patch.object(local_voice, 'stt', Mock(call=lambda body: calls.append(('stt', body)))), \
            patch.object(local_voice, 'tts', Mock(call=lambda body: calls.append(('tts', body)))):
        local_voice.warmup()
    assert calls == [('stt', {'op': 'warmup'}), ('tts', {'op': 'warmup'})]


def test_warmup_failure_propagates_and_does_not_load_second_model():
    with patch.object(local_voice, 'stt', Mock(call=Mock(side_effect=RuntimeError('unavailable')))), \
            patch.object(local_voice, 'tts') as tts:
        try:
            local_voice.warmup()
        except RuntimeError:
            pass
        else:
            raise AssertionError('failure was hidden')
    tts.call.assert_not_called()


def test_opening_twice_shares_pending_warmup_without_a_conversation(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        async def warm():
            await release.wait()
        warm = AsyncMock(side_effect=warm)
        monkeypatch.setattr(pa, '_warmup_task', None)
        monkeypatch.setattr(pa, '_local_enabled', lambda: True)
        monkeypatch.setattr(pa, '_warm_local_voice', warm)
        with patch.object(pa, 'start', new_callable=AsyncMock) as start:
            assert await pa.warmup_voice() == {'ok': True}
            first = pa._warmup_task
            await pa.warmup_voice()
            assert pa._warmup_task is first
            await asyncio.sleep(0)
            warm.assert_awaited_once()
            start.assert_not_awaited()
            release.set()
            await first
    asyncio.run(scenario())


def test_remote_voice_mode_never_loads_local_models(monkeypatch):
    async def scenario():
        monkeypatch.setattr(pa, '_warmup_task', None)
        monkeypatch.setattr(pa, '_local_enabled', lambda: False)
        await pa.warmup_voice()
        assert pa._warmup_task is None
    asyncio.run(scenario())


def test_background_failure_is_logged_and_a_later_attempt_can_retry(monkeypatch):
    async def scenario():
        monkeypatch.setattr(pa, '_warmup_task', None)
        monkeypatch.setattr(pa, '_local_enabled', lambda: True)
        models = Mock(warmup=Mock(side_effect=[RuntimeError('failure'), None]))
        monkeypatch.setattr(pa, '_jarvis_module', lambda _: models)
        with patch.object(pa, '_live_log') as log:
            await pa.warmup_voice()
            await pa._warmup_task
            assert 'RuntimeError' in log.call_args.args[0]
            await pa.warmup_voice()
            await pa._warmup_task
            assert 'ready' in log.call_args.args[0]
    asyncio.run(scenario())


def test_stt_warmup_discards_silence_transcript_and_runs_only_once():
    module = worker_module()
    speech = module.Speech('stt')
    speech.model = Mock(model=SimpleNamespace(device='cuda'))
    speech.model.transcribe.return_value = (iter(['must never become a request']), None)
    with patch.object(speech, 'load'), patch.dict(module.sys.modules,
            {'numpy': SimpleNamespace(zeros=Mock(return_value='silence'), float32=object())}):
        for _ in range(2):
            assert speech.run({'op': 'warmup'}) == {'ok': True, 'engine': 'stt', 'device': 'cuda'}
    speech.model.transcribe.assert_called_once()


def test_f5_warmup_discards_audio_and_reuses_reference_once():
    module = worker_module()
    class Probe(module.Speech):
        def run(self, body):
            if 'text' in body:
                samples.append(body['text'])
                return {'ok': True, 'pcm': 'not published'}
            return super().run(body)
    samples = []
    speech = Probe('tts')
    speech.config = {'reference_text': 'Sistemas online, senhor.', 'tts_device': 'cuda'}
    with patch.object(speech, 'load'):
        for _ in range(2):
            assert speech.run({'op': 'warmup'}) == {'ok': True, 'engine': 'tts', 'device': 'cuda'}
    assert samples == ['Sistemas online, senhor.']
