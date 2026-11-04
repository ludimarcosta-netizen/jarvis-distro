"""Portuguese TTS input must not leave ordinary numbers to multilingual inference."""
import asyncio
import base64
from unittest.mock import patch

import pytest
from local_voice import speech_parts
from local_voice_session import Session
from test_local_voice import bridge


@pytest.mark.parametrize(('text', 'spoken'), [
    ('Posição 1.', 'Posição um.'),
    ('São 21 pedidos e 100 arquivos.', 'São vinte e um pedidos e cem arquivos.'),
    ('Foram 101, depois 200.', 'Foram cento e um, depois duzentos.'),
    ('Total: 1.234.567.', 'Total: um milhão duzentos e trinta e quatro mil quinhentos e sessenta e sete.'),
    ('Valor 1.234,56 e taxa 2,5%.', 'Valor mil duzentos e trinta e quatro vírgula cinquenta e seis e taxa dois vírgula cinco por cento.'),
    ('Precisão 0,05 e versão v1.2.', 'Precisão zero vírgula zero cinco e versão v1.2.'),
    ('Às 18h45 e às 18h50.', 'Às dezoito horas e quarenta e cinco minutos e às dezoito horas e cinquenta minutos.'),
    ('Às 01:01 e às 9h.', 'Às uma hora e um minuto e às nove horas.'),
    ('Código 001 e ISO2026.', 'Código zero zero um e ISO2026.'),
    ('A reunião é em 30/09.', 'A reunião é em trinta de setembro.'),
    ('Vence em 01/01/2027 às 9h30.', 'Vence em primeiro de janeiro de dois mil e vinte e sete às nove horas e trinta minutos.'),
    ('Datas 29/02/2024 e 31/02/2024.', 'Datas vinte e nove de fevereiro de dois mil e vinte e quatro e 31/02/2024.'),
    ('Frações 1/2 e URL https://example.com/30/09.', 'Frações 1/2 e URL https://example.com/30/09.'),
    ('Consulte https://example.com/123 e use 3 arquivos.', 'Consulte https://example.com/123 e use três arquivos.'),
])
def test_numbers_reach_synthesis_in_portuguese(text, spoken):
    assert ' '.join(speech_parts(text)) == spoken


def test_expanded_numbers_are_segmented_after_conversion_without_changing_screen_text():
    async def scenario():
        b = bridge()
        voice = Session(b)
        text = 'Foram 123456 documentos em 21 lotes no dia 30/09. ' * 8
        parts = speech_parts(text)
        with patch('local_voice_session.tts.call', return_value={
                'pcm': base64.b64encode(bytes(480)).decode()}) as synth:
            await voice.speak(text)
        assert b._state['output'] == [text]
        assert [call.args[0]['text'] for call in synth.call_args_list] == parts
        assert all(len(part) <= 180 for part in parts)
        assert not any(char.isdigit() for part in parts for char in part)
        assert voice.last_spoken == ' '.join(parts)
    asyncio.run(scenario())
