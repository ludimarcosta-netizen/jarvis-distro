"""No ML/network required: verify the local path has one brain and keeps its safety gates."""
import asyncio
import base64
import io
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import gpu_priority
import local_voice
import plugin_api as pa
import pytest
from local_voice import Segmenter, decode_pcm, speech_parts
from local_voice_session import Session, normalized


def bridge():
    return SimpleNamespace(_state={"input": [], "output": [], "interrupt_seq": 0},
        _audio_clear=Mock(), _audio_publish=Mock(), _audio_total=lambda: 0,
        _listen_only=lambda: False, _capture_paused=False,
        _run_delegation=AsyncMock(), _interrupt_delegation=AsyncMock(), _live_log=Mock())


def test_vad_ignores_silence_keeps_speech_and_endpoints():
    vad = Segmenter()
    silence = bytes(3200)
    voice = struct.pack('<1600h', *([1000] * 1600))
    for _ in range(100):
        assert vad.feed(silence) is None
    assert len(vad.pre) <= 2
    for _ in range(5):
        assert vad.feed(voice) is None
    results = [vad.feed(silence) for _ in range(8)]
    assert sum(item is not None for item in results) == 1
    assert not vad.frames


def test_vad_ignores_short_click_and_caps_long_speech():
    vad = Segmenter()
    assert vad.feed(struct.pack('<1600h', *([2000] * 1600))) is None
    assert not any(vad.feed(bytes(3200)) for _ in range(8))
    results = [vad.feed(struct.pack('<1600h', *([2000] * 1600))) for _ in range(200)]
    assert len([r for r in results if r]) == 1
    assert not vad.frames


@pytest.mark.parametrize('value', ['!', 'AA==', 'a' * 128001, None], ids=['base64', 'odd-pcm', 'oversize', 'not-text'])
def test_pcm_rejects_invalid_or_oversize_payload(value):
    with pytest.raises((ValueError, TypeError)):
        decode_pcm(value)


def test_speech_keeps_content_but_excludes_screen_directives_and_code():
    result = speech_parts('**Decisão:** revisar contrato. ::card{kind="tarefa" title="X"}\n```py\nx=1\n```')
    assert 'revisar contrato.' in ' '.join(result)
    assert '::card' not in ' '.join(result)
    assert 'x=1' not in ' '.join(result)
    assert max(map(len, speech_parts('palavra ' * 200))) <= 180


def test_every_spoken_question_goes_verbatim_to_guarded_hermes_including_meetings():
    async def scenario():
        b = bridge()
        voice = Session(b)
        question = 'Resuma as atas das reuniões que tive hoje'
        await voice.submit(question)
        await asyncio.sleep(0)
        b._run_delegation.assert_awaited_once()
        assert b._run_delegation.call_args.args[0]['args']['request'] == question
        assert b._state['input'] == [question, '—']
        b._audio_clear.assert_called_once()
    asyncio.run(scenario())


def test_stop_is_local_and_cancel_uses_existing_cancellation():
    async def scenario():
        b = bridge()
        voice = Session(b)
        await voice.submit('Jarvis, silêncio')
        b._run_delegation.assert_not_awaited()
        await voice.submit('cancele a tarefa')
        b._interrupt_delegation.assert_awaited_once()
        b._run_delegation.assert_not_awaited()
        assert normalized('Silêncio!') == 'silencio'
    asyncio.run(scenario())


def test_mute_pause_and_closed_session_never_execute_speech():
    async def scenario():
        b = bridge()
        voice = Session(b)
        b._capture_paused = True
        await voice.submit('abra o navegador')
        b._capture_paused = False
        b._listen_only = lambda: True
        await voice.submit('abra o navegador')
        b._listen_only = lambda: False
        await voice.close()
        await voice.submit('abra o navegador')
        b._run_delegation.assert_not_awaited()
    asyncio.run(scenario())


def test_exact_hermes_answer_is_published_as_text_and_f5_audio_without_gemini():
    async def scenario():
        b = bridge()
        voice = Session(b)
        with patch('local_voice_session.tts.call', return_value={
                'ok': True, 'pcm': base64.b64encode(bytes(9600)).decode(), 'rate': 24000}) as synth:
            await voice.speak('Foram duas reuniões. A decisão foi revisar o contrato.')
        assert b._state['output'] == ['Foram duas reuniões. A decisão foi revisar o contrato.']
        assert synth.call_args.args[0]['text'] == b._state['output'][0]
        assert b._audio_publish.call_count == 2
    asyncio.run(scenario())


def test_interruption_or_mute_during_synthesis_discards_late_audio():
    async def scenario():
        for action in ('interrupt', 'mute'):
            b = bridge()
            voice = Session(b)
            def synth(_, action=action, voice=voice, b=b):
                if action == 'interrupt':
                    voice.interrupt()
                else:
                    b._capture_paused = True
                return {'ok': True, 'pcm': base64.b64encode(bytes(100)).decode()}
            with patch('local_voice_session.tts.call', side_effect=synth):
                await voice.speak('Resposta antiga')
            b._audio_publish.assert_not_called()
    asyncio.run(scenario())


def test_audio_cursor_survives_interruption_and_delivers_the_next_reply():
    with patch.dict(pa._state, {'audio': ['old-one', 'old-two'], 'audio_base': 0}):
        voice = Session(pa)
        voice.interrupt()
        pa._audio_publish('new-reply')
        assert pa._audio_total() == 3
        assert pa._audio_since(2) == ['new-reply']


def test_f5_priority_survives_ambient_idle_and_releases_its_own_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_priority, 'BUSY_FILE', tmp_path / 'ambient')
    monkeypatch.setattr(gpu_priority, 'TTS_BUSY_FILE', tmp_path / 'tts')
    monkeypatch.setattr(gpu_priority, 'STT_BUSY_FILE', tmp_path / 'stt')
    with gpu_priority.voice_priority():
        gpu_priority.mark_idle()
        assert gpu_priority.busy_until() > 0
    assert gpu_priority.busy_until() == 0


def test_windows_worker_cleanup_terminates_its_launcher_tree(monkeypatch):
    worker = local_voice.Worker('tts')
    process = Mock(pid=12345)
    process.poll.return_value = None
    worker.process = process
    monkeypatch.setenv('SystemRoot', 'C:/Windows')
    with patch.object(local_voice.subprocess, 'run') as kill:
        worker.close()
    assert kill.call_args.args[0][-4:] == ['/PID', '12345', '/T', '/F']
    process.wait.assert_called_once_with(timeout=10)
    assert worker.process is None


def test_worker_isolates_python_abi_from_packaged_parent(tmp_path, monkeypatch):
    python = tmp_path / '.venv311/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(local_voice, 'ROOT', tmp_path)
    monkeypatch.setenv('PYTHONPATH', 'C:/native-python314/site-packages')
    monkeypatch.setenv('PYTHONHOME', 'C:/native-python314')
    worker = local_voice.Worker('stt')
    process = Mock(stdin=io.StringIO(), stdout=io.StringIO())
    process.poll.return_value = 0
    with patch.object(local_voice.subprocess, 'Popen', return_value=process) as spawn:
        worker._start()
    assert spawn.call_args.args[0][1:4] == ['-I', '-X', 'utf8']
    assert 'PYTHONPATH' not in spawn.call_args.kwargs['env']
    assert 'PYTHONHOME' not in spawn.call_args.kwargs['env']
    worker.close()


@pytest.mark.parametrize(('engine', 'device', 'runtime', 'script'), [
    ('f5', 'cpu', '.venv-voice', 'local_voice_worker.py'),
    ('piper', 'cpu', '.venv-piper', 'local_piper_worker.py'),
    ('kokoro', 'cpu', '.venv-kokoro', 'local_kokoro_worker.py'),
    ('kokoro', 'cuda', '.venv-kokoro-gpu', 'local_kokoro_worker.py'),
])
def test_tts_backend_selects_configured_worker(tmp_path, monkeypatch, engine, device, runtime, script):
    python = tmp_path / runtime / 'Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(local_voice, 'ROOT', tmp_path)
    monkeypatch.setattr(local_voice, 'settings', lambda: {'tts_engine': engine, 'kokoro_device': device})
    worker = local_voice.Worker('tts')
    process = Mock(stdin=io.StringIO(), stdout=io.StringIO())
    process.poll.return_value = 0
    with patch.object(local_voice.subprocess, 'Popen', return_value=process) as spawn:
        worker._start()
    command = spawn.call_args.args[0]
    assert command[0] == str(python)
    assert command[-2] == str(tmp_path / script)
    worker.close()


def test_local_config_identifies_kokoro_voice(monkeypatch):
    monkeypatch.setattr(pa, '_local_enabled', lambda: True)
    monkeypatch.setattr(pa, '_jarvis_module', lambda _: SimpleNamespace(settings=lambda: {'tts_engine': 'kokoro'}))
    assert asyncio.run(pa.config()) == {
        'model': 'hermes-kokoro-local', 'candidates': ['hermes-kokoro-local'],
        'resumable': False, 'engine': 'local'}
    assert pa._local_tts_name() == 'Kokoro Santa'


def test_local_start_requires_no_google_key_and_passes_readiness_before_activation(monkeypatch):
    async def scenario():
        runtime = SimpleNamespace(prepare=AsyncMock(), start=Mock(), close=AsyncMock())
        monkeypatch.setattr(pa, '_local_enabled', lambda: True)
        with patch.object(pa, '_listen_only', return_value=False), patch.object(pa, '_capture_paused', False), \
                patch.object(pa, '_cleanup', new_callable=AsyncMock), patch.object(pa, '_ensure_bridge'), \
                patch.object(pa, '_api_key') as google, patch.object(pa._voice, 'begin', new_callable=AsyncMock), \
                patch('local_voice_session.Session', return_value=runtime), patch.dict(pa._state):
            assert (await pa.start())['engine'] == 'local'
            google.assert_not_called()
            runtime.prepare.assert_awaited_once()
            runtime.start.assert_called_once()
    asyncio.run(scenario())


def test_local_readiness_failure_never_falls_back_to_gemini(monkeypatch):
    async def scenario():
        runtime = SimpleNamespace(prepare=AsyncMock(side_effect=RuntimeError('F5 indisponível')),
                                  close=AsyncMock())
        monkeypatch.setattr(pa, '_local_enabled', lambda: True)
        with patch.object(pa, '_listen_only', return_value=False), patch.object(pa, '_capture_paused', False), \
                patch.object(pa, '_cleanup', new_callable=AsyncMock), patch.object(pa, '_ensure_bridge'), \
                patch.object(pa, '_api_key') as google, patch('local_voice_session.Session', return_value=runtime), \
                patch.dict(pa._state):
            assert not (await pa.start())['ok']
            assert pa._state['session'] is None
            google.assert_not_called()
    asyncio.run(scenario())


def test_partial_is_display_only_and_late_hypothesis_is_discarded():
    async def scenario():
        b = bridge()
        voice = Session(b)
        with patch('local_voice_session.stt.call', return_value={'text': 'Organize minha agenda'}):
            await voice.preview(bytes(48000), 0)
        assert voice.preview_text == 'Organize minha agenda'
        b._run_delegation.assert_not_awaited()
        voice.preview_epoch += 1
        with patch('local_voice_session.stt.call', return_value={'text': 'resultado antigo'}):
            await voice.preview(bytes(48000), 0)
        assert voice.preview_text == 'Organize minha agenda'
        voice.closed = True
        with patch('local_voice_session.stt.call', return_value={'text': 'depois de fechar'}):
            await voice.preview(bytes(48000), voice.preview_epoch)
        assert voice.preview_text == 'Organize minha agenda'
    asyncio.run(scenario())


def test_speech_cues_mark_audio_cursor_duration_and_new_reply():
    b = bridge()
    voice = Session(b)
    b._audio_total = lambda: b._audio_publish.call_count
    voice.publish_speech('Primeiro trecho', bytes(9600), True)
    voice.publish_speech('continuação', bytes(4800), False)
    assert voice.speech_cues == [
        {'offset': 0, 'text': 'Primeiro trecho', 'duration': .2, 'first': True},
        {'offset': 2, 'text': 'continuação', 'duration': .1, 'first': False}]
