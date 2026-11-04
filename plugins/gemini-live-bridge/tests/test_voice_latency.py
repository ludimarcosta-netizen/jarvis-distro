"""Latency changes must preserve speech contents, configuration bounds and interrupt gates."""
import asyncio
import base64
from unittest.mock import patch

import pytest
from local_voice import speech_parts, synthesis_steps
from local_voice_session import Session
from test_local_voice import bridge


@pytest.mark.parametrize('steps', [16, 24, 32])
def test_steps_support_fast_and_original_quality_settings(steps):
    assert synthesis_steps({'tts_steps': steps}, {}) == steps
    assert synthesis_steps({'tts_steps': 32}, {'steps': steps}) == steps


@pytest.mark.parametrize('steps', [True, False, '16', 0, 8, 64, None, 16.0])
def test_steps_reject_invalid_values_before_inference(steps):
    with pytest.raises(ValueError):
        synthesis_steps({}, {'steps': steps})


def test_omitted_steps_preserve_the_original_default():
    assert synthesis_steps({}, {}) == 32


def test_short_response_is_not_split_into_a_broken_one_word_tail():
    text = 'Este é um teste da voz do Jarvis. A resposta precisa ser clara, natural e objetiva, preservando todas as informações.'
    assert speech_parts(text) == [text]


def test_long_response_starts_earlier_and_preserves_every_word():
    text = ('Foram duas reuniões de trabalho, com decisões sobre o calendário e a revisão do relatório. '
            'A primeira reunião tratou dos prazos de entrega. A segunda definiu quem revisará os contratos. '
            'Não envie documentos antes da confirmação e mantenha as duas pendências separadas.')
    parts = speech_parts(text)
    assert 70 <= len(parts[0]) <= 110
    assert all(70 <= len(part) <= 180 for part in parts)
    assert ' '.join(parts) == text


def test_chunk_boundaries_preserve_negation_and_avoid_tiny_final_audio():
    for words in range(25, 100):
        text = 'Não execute esta ação sem confirmação. ' + ' '.join(['verificação'] * words)
        parts = speech_parts(text)
        assert ' '.join(parts) == text
        assert all(70 <= len(part) <= 180 for part in parts)


def test_first_audio_is_published_before_the_next_chunk_finishes():
    async def scenario():
        b = bridge()
        voice = Session(b)
        text = ('Foram duas reuniões de trabalho, com decisões sobre o calendário e a revisão do relatório. '
                'A primeira reunião tratou dos prazos de entrega. A segunda definiu quem revisará os contratos.')
        parts = speech_parts(text)
        seen = []
        def synth(request):
            if seen:
                assert b._audio_publish.call_count > 0
            seen.append(request['text'])
            return {'pcm': base64.b64encode(bytes(4800)).decode()}
        with patch('local_voice_session.tts.call', side_effect=synth):
            await voice.speak(text)
        assert seen == parts
        assert b._state['output'] == [text]
        assert b._state['interaction'] == ''
    asyncio.run(scenario())
