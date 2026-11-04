"""Concurrent speech must preserve whole replies and discard interrupted waiters."""
import asyncio
import base64
from unittest.mock import patch

from local_voice_session import Session
from test_local_voice import bridge


def test_default_turn_has_no_filler_but_still_delegates():
    async def scenario():
        b = bridge()
        with patch('local_voice_session.settings', return_value={}):
            voice = Session(b)
        voice.phrases.acknowledgement = lambda: (_ for _ in ()).throw(AssertionError('filler selected'))
        await voice.submit('Qual é o próximo passo?')
        await asyncio.sleep(0)
        b._run_delegation.assert_awaited_once()
        b._audio_publish.assert_not_called()
        assert b._state['output'] == []
    asyncio.run(scenario())


def test_concurrent_replies_never_interleave_chunks():
    async def scenario():
        voice = Session(bridge())
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        text = 'Primeira resposta com informações importantes. ' * 8

        async def synth(_, request):
            calls.append(request['text'])
            if len(calls) == 1:
                started.set()
                await release.wait()
            return {'pcm': base64.b64encode(bytes(480)).decode()}

        with patch('local_voice_session.asyncio.to_thread', side_effect=synth):
            first = asyncio.create_task(voice.speak(text))
            await started.wait()
            second = asyncio.create_task(voice.speak('Segunda resposta completa.'))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
        assert calls[-1] == 'Segunda resposta completa.'
        assert ' '.join(calls[:-1]) == text.strip()
    asyncio.run(scenario())


def test_interrupt_discards_reply_already_waiting_for_speech():
    async def scenario():
        b = bridge()
        voice = Session(b)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def synth(_, request):
            calls.append(request['text'])
            started.set()
            await release.wait()
            return {'pcm': base64.b64encode(bytes(480)).decode()}

        with patch('local_voice_session.asyncio.to_thread', side_effect=synth):
            first = asyncio.create_task(voice.speak('Primeira resposta.'))
            await started.wait()
            second = asyncio.create_task(voice.speak('Resposta na espera.'))
            await asyncio.sleep(0)
            voice.interrupt()
            release.set()
            await asyncio.gather(first, second)
        assert calls == ['Primeira resposta.']
        b._audio_publish.assert_not_called()
    asyncio.run(scenario())
