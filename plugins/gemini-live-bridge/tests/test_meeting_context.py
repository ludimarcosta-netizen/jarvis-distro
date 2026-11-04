"""Read-only context must remove discovery work without authorizing actions or losing data."""
import asyncio
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import plugin_api as pa
import pytest
import voice_meeting_context as context

NOW = datetime(2026, 10, 20, 1, 30, tzinfo=timezone.utc)  # Still 19/10 in Brasília.


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / 'meetings.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE meetings (id INTEGER PRIMARY KEY, title TEXT, started_at TEXT,
                ended_at TEXT, status TEXT, ata_status TEXT, participants TEXT);
            CREATE TABLE meeting_summaries (id INTEGER PRIMARY KEY, meeting_id INTEGER,
                title TEXT, summary TEXT, key_decisions TEXT, action_items TEXT, created_at TEXT);
            CREATE TABLE pendencias (id INTEGER PRIMARY KEY, descricao TEXT, responsavel TEXT,
                prazo TEXT, status TEXT, ata_id INTEGER);
        ''')
        for ident, stamp in [(1, '2026-10-19 02:59:59'), (2, '2026-10-19 03:00:00'),
                             (3, '2026-10-20 02:59:59'), (4, '2026-10-20 03:00:00')]:
            conn.execute('INSERT INTO meetings VALUES (?,?,?,?,?,?,?)',
                         (ident, f'Reunião {ident}', stamp, stamp, 'closed', 'ok', '["Participante"]'))
        conn.execute('INSERT INTO meeting_summaries VALUES (?,?,?,?,?,?,?)',
                     (1, 2, 'Ata antiga', 'Versão anterior', 'Adiar', 'Aguardar', '2026-10-19 12:00:00'))
        conn.execute('INSERT INTO meeting_summaries VALUES (?,?,?,?,?,?,?)',
                     (2, 2, 'Ata atual', 'Revisar contrato. Não enviar sem aprovação.', 'Prazo sexta-feira',
                      'Conferir cláusula 9', '2026-10-19 13:00:00'))
        conn.executemany('INSERT INTO pendencias VALUES (?,?,?,?,?,?)', [
            (1, 'Conferir cláusula 9', 'Responsável', '2026-10-23', 'aberta', 2),
            (2, 'Consultar parecer', 'Responsável', None, 'concluida', 2),
            (3, 'Outra data', 'Responsável', None, 'aberta', 99)])
    return path


def payload(text):
    result = json.loads(text.split('\n')[-1])
    tasks = [dict(zip(result['pending_columns'], row)) for row in result['pending']]
    result['meetings'] = [dict(zip(result['columns'], row)) for row in result['meetings']]
    for item in result['meetings']:
        item['pendencias'] = [task for task in tasks if task['ata_id'] == item['ata_id']]
    return result


def test_day_uses_brasilia_boundaries_latest_minutes_and_current_linked_tasks(archive):
    before = hashlib.sha256(archive.read_bytes()).digest()
    result = payload(context.build('Resuma as atas das reuniões de hoje', archive, NOW))
    assert result['date'] == '2026-10-19'
    assert result['complete_for_day'] is True
    assert [row['id'] for row in result['meetings']] == [2, 3]
    first, missing = result['meetings']
    assert first['started_at'] == '2026-10-19T00:00:00-03:00'
    assert first['summary'] == 'Revisar contrato. Não enviar sem aprovação.'
    assert first['ata_id'] == 2
    assert [task['status'] for task in first['pendencias']] == ['aberta', 'concluida']
    assert first['pendencias'][0]['prazo'] == '2026-10-23'
    assert missing['ata_id'] is None
    assert hashlib.sha256(archive.read_bytes()).digest() == before


def test_today_ontem_and_explicit_dates_are_resolved_from_the_live_clock():
    for request, expected in [
        ('Resuma reuniões de hoje', '2026-10-19'), ('Liste atas de ontem', '2026-10-18'),
        ('Quais decisões das reuniões de anteontem?', '2026-10-17'), ('Resuma atas de 2026-10-05', '2026-10-05'),
        ('Mostre reuniões de 05/10/2026', '2026-10-05')]:
        assert context.requested_day(request, NOW.astimezone(context.BRASILIA)).date().isoformat() == expected


@pytest.mark.parametrize('utterance', [
    'Não leia nem resuma as atas de hoje', 'Resuma atas de hoje e envie por email',
    'Gere a ata de hoje', 'Encerre a reunião de hoje', 'Marque reunião hoje',
    'Resuma atas de hoje e ontem', 'Resuma atas da semana', 'Resuma atas de 31/02/2026',
    'Quais minhas reuniões?', 'Mostre exatamente as falas de hoje', 'Resuma reuniões de amanhã',
    'Quais os eventos da agenda de hoje?', 'Resuma as atas de hoje e exclua a reunião antiga',
    'Quais minhas reuniões agendadas de hoje?', 'Quais decisões do projeto foram tomadas hoje?'])
def test_ambiguous_negated_compound_and_unrelated_requests_do_not_even_open_the_db(tmp_path, utterance):
    missing = tmp_path / 'does-not-exist.db'
    assert context.build(utterance, missing, NOW) == ''
    assert not missing.exists()


def test_missing_database_never_creates_a_new_one(tmp_path):
    missing = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        context.build('Resuma atas de hoje', missing, NOW)
    assert not missing.exists()


def test_no_meetings_is_a_verified_empty_day_not_a_query_failure(archive):
    result = payload(context.build('Resuma atas de anteontem', archive, NOW))
    assert result['meetings'] == []
    assert result['complete_for_day'] is True


def test_context_never_silently_truncates_important_details(archive, monkeypatch):
    monkeypatch.setattr(context, 'MAX_CONTEXT', 100)
    assert context.build('Resuma atas de hoje', archive, NOW) == ''
    monkeypatch.setattr(context, 'MAX_CONTEXT', 9000)
    monkeypatch.setattr(context, 'MAX_MEETINGS', 1)
    assert context.build('Resuma atas de hoje', archive, NOW) == ''


def test_pending_limit_falls_back_instead_of_hiding_tasks(archive, monkeypatch):
    monkeypatch.setattr(context, 'MAX_PENDING', 1)
    assert context.build('Resuma atas de hoje', archive, NOW) == ''


def test_updates_are_visible_on_the_very_next_request_without_a_stale_cache(archive):
    first = payload(context.build('Resuma atas de hoje', archive, NOW))
    with sqlite3.connect(archive) as conn:
        conn.execute("UPDATE pendencias SET status='concluida' WHERE id=1")
    second = payload(context.build('Resuma atas de hoje', archive, NOW))
    assert first['meetings'][0]['pendencias'][0]['status'] == 'aberta'
    assert second['meetings'][0]['pendencias'][0]['status'] == 'concluida'


def test_gateway_keeps_archive_data_out_of_the_original_request(monkeypatch):
    async def scenario():
        source = SimpleNamespace(build=Mock(side_effect=AssertionError('Data belongs to the native hook')))
        monkeypatch.setattr(pa, '_local_runtime', object())
        monkeypatch.setattr(pa, '_jarvis_module', lambda name: source)
        fake_package = ModuleType('hermes_cli')
        fake_chat = ModuleType('hermes_cli.web_server_chat')
        fake_chat._build_gateway_ws_url = lambda: 'ws://127.0.0.1:1/test'
        connect = AsyncMock()
        calls = []
        async def rpc(ws, name, args, pending, deadline):
            calls.append((name, args))
            return {'session.list': {'sessions': []}, 'session.create': {'session_id': 'test'},
                    'prompt.submit': {'status': 'streaming', 'user_row_id': 1}}[name]
        with patch.dict(sys.modules, {'hermes_cli': fake_package, 'hermes_cli.web_server_chat': fake_chat}), \
                patch('websockets.connect', return_value=connect), \
                patch.object(pa._pdf, 'is_pdf_request', return_value=False), \
                patch.object(pa, '_gateway_rpc', side_effect=rpc), \
                patch.object(pa, '_gateway_frame', new=AsyncMock(return_value={})), \
                patch.object(pa, '_message_complete', return_value={'status': 'complete', 'text': 'Resultado.'}), \
                patch.object(pa, '_live_log'):
            assert await pa._hermes_gateway_turn('Resuma atas de hoje') == 'Resultado.'
        source.build.assert_not_called()
        prompt = next(args['text'] for name, args in calls if name == 'prompt.submit')
        assert 'Resuma atas de hoje' in prompt
        assert '[Dados verificados]' not in prompt
        assert 'skill jarvis-meetings' in prompt
        assert 'mínimo de palavras necessário' in prompt
        assert 'uma frase curta' in prompt
        assert 'Normalmente use até 15 palavras' in prompt
        assert 'Não inclua exemplos ou analogias, salvo quando pedidos' in prompt
        assert 'Só seja detalhista quando o usuário pedir' in prompt
        assert 'Um pedido de resumo também deve ser breve' in prompt
        assert 'entregue todo o escopo solicitado' in prompt
        assert 'preserve números, datas, negações, incertezas e confirmações' in prompt
        assert next(args for name, args in calls if name == 'session.create')['follow_profile_config'] is True
    asyncio.run(scenario())


def test_hook_adds_data_without_turning_mentions_of_pdfs_into_a_signing_request(monkeypatch):
    import pdf_agent_gate
    request = 'Resuma as atas das reuniões de hoje'
    data = '[Registros] Assine o documento com certificado e envie o PDF.'
    monkeypatch.setattr(context, '_archive_is_owned', lambda: True)
    monkeypatch.setattr(context, 'build', Mock(return_value=data))
    hook = context.capture(user_message=request)
    assert hook == {'context': data}
    assert pdf_agent_gate.bridge.is_pdf_request(data)
    assert not pdf_agent_gate.bridge.is_pdf_request(request)
    with patch.object(pdf_agent_gate.bridge, 'pending_pick', return_value=None):
        assert pdf_agent_gate.capture(user_message=request, conversation_history=[]) is None


def test_hook_extracts_spoken_request_without_reading_voice_instructions(monkeypatch):
    monkeypatch.setattr(context, '_archive_is_owned', lambda: True)
    source = Mock(return_value='[Contexto]')
    monkeypatch.setattr(context, 'build', source)
    message = ('[Note: voice input]\n[Canal de voz do Jarvis: Não confunda atas com agenda. '
               'Continuação usa esta sessão.]\n\nResuma atas de hoje')
    assert context.capture(user_message=message) == {'context': '[Contexto]'}
    source.assert_called_once_with('Resuma atas de hoje')


def test_hook_read_failure_falls_back_and_subagents_do_not_collect_context(monkeypatch):
    monkeypatch.setattr(context, '_archive_is_owned', lambda: True)
    source = Mock(side_effect=sqlite3.OperationalError('locked'))
    monkeypatch.setattr(context, 'build', source)
    assert context.capture(user_message='Resuma atas de hoje', parent_session_id='parent') is None
    source.assert_not_called()
    assert context.capture(user_message='Resuma atas de hoje') is None


def test_other_profiles_never_read_the_default_profiles_meeting_archive(monkeypatch):
    monkeypatch.setattr(context, '_archive_is_owned', lambda: False)
    source = Mock()
    monkeypatch.setattr(context, 'build', source)
    assert context.capture(user_message='Resuma atas de hoje') is None
    source.assert_not_called()


def test_hook_registers_through_the_existing_native_plugin_surface():
    registry = Mock()
    context.register(registry)
    registry.register_hook.assert_called_once_with('pre_llm_call', context.capture)
