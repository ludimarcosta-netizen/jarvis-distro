"""Routine local queue status stays in the journal; actual results remain spoken."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import plugin_api as pa
import pytest


@pytest.mark.parametrize('local', [True, False])
def test_queue_keeps_fifo_and_results_without_local_spoken_positions(isolated_tasks, local):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        requests = []

        def work(request):
            requests.append(request)
            if len(requests) == 1:
                entered.set()
                assert release.wait(10)
            return f'Resultado de {request}.'

        runtime = SimpleNamespace(speak=AsyncMock()) if local else None
        with patch.object(pa, '_local_runtime', runtime), \
                patch.dict(pa._state, {'session': 'live'}), patch.object(pa, '_live_log'), \
                patch.object(pa._voice, 'permit', new_callable=AsyncMock, return_value=True), \
                patch.object(pa._voice, 'requires_screen', return_value=False), \
                patch.object(pa, '_hermes_turn', side_effect=work), \
                patch.object(pa, '_send_tool_response', new_callable=AsyncMock) as response, \
                patch.object(pa, '_send_voice_turn', new_callable=AsyncMock, return_value=True):
            first = asyncio.create_task(pa._run_delegation({'id': 'one', 'args': {'request': 'primeiro'}}))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                await pa._run_delegation({'id': 'two', 'args': {'request': 'segundo'}})
                await pa._run_delegation({'id': 'three', 'args': {'request': 'terceiro'}})
                assert len(pa._delegation_queue) == 2
                assert len(isolated_tasks.history()) == 3
                assert response.await_count == (0 if local else 2)
                if local:
                    runtime.speak.assert_not_awaited()
            finally:
                release.set()
                await first
            assert requests == ['primeiro', 'segundo', 'terceiro']
            if local:
                assert [call.args[0] for call in runtime.speak.await_args_list] == [
                    'Resultado de primeiro.', 'Resultado de segundo.', 'Resultado de terceiro.']
            assert all(row['status'] == 'done' for row in isolated_tasks.history())
    asyncio.run(scenario())
