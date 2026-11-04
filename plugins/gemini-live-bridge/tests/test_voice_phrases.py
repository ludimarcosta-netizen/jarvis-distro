"""Receipt must be instant, truthful, interruptible, and independent of inference."""
import asyncio
import base64
import json
import time
import wave
from unittest.mock import AsyncMock, patch

import pytest
from local_voice_phrases import Bank, Clip
from local_voice_session import Session
from test_local_voice import bridge


def bank_at(root, *, channels=1, rate=24000, frames=2400, file="phrase.wav"):
    root.mkdir(exist_ok=True)
    with wave.open(str(root / "phrase.wav"), "wb") as audio:
        audio.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(bytes(frames * 2 * channels))
    (root / "manifest.json").write_text(json.dumps({"version": 1, "clips": [
        {"file": file, "text": "Um momento, senhor.", "bridge": True}]}), encoding="utf-8")
    bank = Bank(root)
    bank.load()
    return bank


def test_bank_loads_pcm_and_matches_only_the_complete_utterance(tmp_path):
    bank = bank_at(tmp_path)
    assert len(bank.acknowledgement().pcm) == 4800
    assert bank.match("UM MOMENTO, SENHOR!") is not None
    assert bank.match("Um momento, senhor. Foram duas reuniões.") is None
    assert bank.match("Não diga um momento, senhor.") is None


@pytest.mark.parametrize('options', [{'channels': 2}, {'rate': 16000},
    {'frames': 24000 * 9}, {'frames': 0}, {'file': '../outside.wav'}])
def test_bank_rejects_unsafe_paths_invalid_audio_and_unbounded_clips(tmp_path, options):
    with pytest.raises(ValueError):
        bank_at(tmp_path, **options)


def test_corrupt_manifest_does_not_discard_a_previously_loaded_bank(tmp_path):
    bank = bank_at(tmp_path)
    (tmp_path / 'manifest.json').write_text('{broken', encoding='utf-8')
    with pytest.raises(ValueError):
        bank.load()
    assert bank.acknowledgement().text == 'Um momento, senhor.'


def test_bridges_rotate_but_never_select_a_greeting():
    bank = Bank()
    bank.clips = [Clip('Certo.', bytes(480), True), Clip('À disposição, senhor.', bytes(480), False),
                  Clip('Pedido recebido, senhor.', bytes(480), True)]
    assert [bank.acknowledgement().text for _ in range(3)] == [
        'Certo.', 'Pedido recebido, senhor.', 'Certo.']


def test_receipt_audio_is_published_before_hermes_finishes_without_calling_f5(tmp_path):
    async def scenario():
        b = bridge()
        finished = asyncio.Event()
        async def execute(_):
            await finished.wait()
        b._run_delegation = AsyncMock(side_effect=execute)
        voice = Session(b)
        voice.acknowledgements_enabled = True
        voice.phrases = bank_at(tmp_path)
        with patch('local_voice_session.tts.call') as synth:
            await voice.submit('Resuma as atas de hoje')
            assert b._state['output'] == ['Um momento, senhor.']
            assert base64.b64decode(b._audio_publish.call_args.args[0]) == bytes(4800)
            await asyncio.sleep(0)
            b._run_delegation.assert_awaited_once()
            assert b._state['interaction'] == 'IN_PROGRESS'
            assert b._run_delegation.call_args.args[0]['args']['request'] == 'Resuma as atas de hoje'
            synth.assert_not_called()
            finished.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert b._state['interaction'] == ''
    asyncio.run(scenario())


def test_receipt_cooldown_keeps_rapid_followups_quiet_without_dropping_them(tmp_path):
    async def scenario():
        b = bridge()
        voice = Session(b)
        voice.acknowledgements_enabled = True
        voice.phrases = bank_at(tmp_path)
        await voice.submit('Resuma a reunião')
        await voice.submit('Inclua também as decisões')
        await asyncio.sleep(0)
        assert b._state['output'] == ['Um momento, senhor.']
        assert b._run_delegation.await_count == 2
    asyncio.run(scenario())


def test_failed_hermes_turn_releases_busy_state_and_reports_the_failure(tmp_path):
    async def scenario():
        b = bridge()
        b._run_delegation = AsyncMock(side_effect=RuntimeError('gateway unavailable'))
        voice = Session(b)
        voice.phrases = bank_at(tmp_path)
        await voice.submit('Resuma a reunião')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert b._state['interaction'] == ''
        assert 'Não consegui processar' in b._state['error']
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['silêncio', 'pare', 'cancele a tarefa', 'bom dia', 'obrigado'])
def test_controls_and_social_turns_do_not_emit_a_bridge(tmp_path, text):
    async def scenario():
        b = bridge()
        voice = Session(b)
        voice.phrases = bank_at(tmp_path)
        await voice.submit(text)
        await asyncio.sleep(0)
        b._audio_publish.assert_not_called()
    asyncio.run(scenario())


def test_exact_cached_reply_bypasses_synthesis_and_respects_mute_and_close(tmp_path):
    async def scenario():
        b = bridge()
        voice = Session(b)
        voice.phrases = bank_at(tmp_path)
        with patch('local_voice_session.tts.call') as synth:
            await voice.speak('Um momento, senhor.')
            synth.assert_not_called()
        assert b._audio_publish.call_count == 1
        b._capture_paused = True
        await voice.speak('Um momento, senhor.')
        b._capture_paused = False
        b._listen_only = lambda: True
        await voice.speak('Um momento, senhor.')
        b._listen_only = lambda: False
        await voice.close()
        await voice.speak('Um momento, senhor.')
        assert b._audio_publish.call_count == 1
        assert b._state['interrupt_seq'] == 1
    asyncio.run(scenario())


def test_mixed_reply_preserves_facts_and_uses_dynamic_voice(tmp_path):
    async def scenario():
        b = bridge()
        voice = Session(b)
        voice.phrases = bank_at(tmp_path)
        answer = 'Um momento, senhor. Foram duas reuniões, às dez e às onze.'
        with patch('local_voice_session.tts.call', return_value={'pcm': base64.b64encode(bytes(480)).decode()}) as synth:
            await voice.speak(answer)
        assert synth.call_args.args[0]['text'] == answer
    asyncio.run(scenario())


def test_missing_optional_bank_keeps_stt_and_tts_readiness(tmp_path):
    async def scenario():
        b = bridge()
        voice = Session(b)
        voice.phrases = Bank(tmp_path)
        with patch('local_voice_session.stt.call', return_value={}), patch('local_voice_session.tts.call', return_value={}):
            await voice.prepare()
        b._live_log.assert_called_once()
        await voice.submit('Resuma a reunião')
        await asyncio.sleep(0)
        b._run_delegation.assert_awaited_once()
    asyncio.run(scenario())


def test_ack_echo_is_ignored_even_after_the_full_reply_has_started(tmp_path):
    async def scenario():
        b = bridge()
        voice = Session(b)
        voice.last_ack_at = time.monotonic()
        voice.last_ack_text = 'Um momento, senhor.'
        voice.last_spoken = 'Foram duas reuniões de trabalho hoje.'
        voice.submit = AsyncMock()
        voice.queue.put_nowait((bytes(480), True))
        consumer = asyncio.create_task(voice.consume())
        with patch('local_voice_session.stt.call', return_value={'text': 'Um momento, senhor.'}):
            await voice.queue.join()
        voice.submit.assert_not_awaited()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    asyncio.run(scenario())
