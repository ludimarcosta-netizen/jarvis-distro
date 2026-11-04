"""Delegation admission, recovery and uncertain outcomes; no external effects."""
import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import plugin_api as pa


def test_completed_result_survives_session_close_and_backend_reload(isolated_tasks):
    from delegation_journal import Journal

    async def scenario():
        with patch.dict(pa._state, {"session": "live"}), \
                patch.object(pa._voice, "permit", new_callable=AsyncMock, return_value=True), \
                patch.object(pa._voice, "requires_screen", return_value=False), \
                patch.object(pa, "_hermes_turn", return_value="resultado sintético") as executor, \
                patch.object(pa, "_live_log"), \
                patch.object(pa, "_deliver_result", new_callable=AsyncMock):
            await pa._run_delegation({"id": "one", "args": {"request": "pedido sintético"}})
            await pa._run_delegation({"id": "one", "args": {"request": "pedido sintético"}})
            assert executor.call_count == 1
    asyncio.run(scenario())
    [row] = Journal(isolated_tasks.path).history()
    assert row["reply"] == "resultado sintético"
    assert row["status"] == "done"


def test_journal_failure_never_reaches_executor():
    async def scenario():
        with patch.object(pa, "_task_journal", side_effect=sqlite3.OperationalError("fixture")), \
                patch.object(pa, "_send_tool_response", new_callable=AsyncMock) as response, \
                patch.object(pa, "_hermes_turn") as executor, patch.object(pa, "_live_log"):
            await pa._run_delegation({"id": "one", "args": {"request": "pedido"}})
            assert not executor.called
            assert "Nenhuma delegação" in response.call_args.args[2]
            assert pa._task_history()["error"]
    asyncio.run(scenario())


def test_queued_request_from_closed_session_is_not_executed(isolated_tasks):
    async def scenario():
        with patch.dict(pa._state, {"session": None}), \
                patch.object(pa, "_hermes_turn") as executor, patch.object(pa, "_park_result", new_callable=AsyncMock):
            await pa._guarded_delegation("queued", "pedido", time.time(), "old-session", answered=True)
            assert not executor.called
    asyncio.run(scenario())
    assert isolated_tasks.history()[0]["status"] == "cancelled"


def test_confirmation_is_never_reused_after_restart(isolated_tasks):
    from delegation_journal import Journal
    task_id, _ = isolated_tasks.accept("old", "one", "pedido sensível sintético")
    isolated_tasks.phase(task_id, "confirming")
    recovered = Journal(isolated_tasks.path)
    assert recovered.history()[0]["status"] == "cancelled"
    assert not recovered.accept("old", "one", "pedido sensível sintético")[1]


def test_fifo_queue_survives_as_history_without_running_after_voice_closes(isolated_tasks):
    import threading
    entered, release = threading.Event(), threading.Event()

    def work(request):
        entered.set()
        assert release.wait(10)
        return "resultado do primeiro"

    async def scenario():
        with patch.dict(pa._state, {"session": "live"}), patch.object(pa, "_live_log"), \
                patch.object(pa._voice, "permit", new_callable=AsyncMock, return_value=True), \
                patch.object(pa._voice, "requires_screen", return_value=False), \
                patch.object(pa, "_hermes_turn", side_effect=work) as executor, \
                patch.object(pa, "_send_tool_response", new_callable=AsyncMock), \
                patch.object(pa, "_park_result", new_callable=AsyncMock):
            first = asyncio.create_task(pa._run_delegation({"id": "first", "args": {"request": "primeiro"}}))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                for index in range(4):
                    await pa._run_delegation({"id": str(index), "args": {"request": f"fila {index}"}})
                assert len(pa._delegation_queue) == 3
                pa._state["session"] = None
            finally:
                release.set()
                await first
            executor.assert_called_once()
    asyncio.run(scenario())
    assert sorted(row["status"] for row in isolated_tasks.history()) == ["cancelled"] * 3 + ["done"]


def test_failed_running_reservation_stops_before_executor(isolated_tasks):
    async def scenario():
        original = isolated_tasks.phase

        def phase(task_id, status):
            if status == "running":
                raise sqlite3.OperationalError("fixture")
            original(task_id, status)

        with patch.dict(pa._state, {"session": "live"}), patch.object(pa, "_live_log"), \
                patch.object(isolated_tasks, "phase", side_effect=phase), \
                patch.object(pa._voice, "permit", new_callable=AsyncMock, return_value=True), \
                patch.object(pa._voice, "requires_screen", return_value=False), \
                patch.object(pa, "_hermes_turn") as executor, \
                patch.object(pa, "_send_tool_response", new_callable=AsyncMock):
            await pa._run_delegation({"id": "one", "args": {"request": "pedido"}})
            assert not executor.called
    asyncio.run(scenario())
    assert isolated_tasks.history()[0]["status"] == "error"


def test_result_write_failure_is_uncertain_and_never_retried(isolated_tasks):
    from delegation_journal import Journal

    async def scenario():
        with patch.dict(pa._state, {"session": "live"}), patch.object(pa, "_live_log"), \
                patch.object(isolated_tasks, "finish", side_effect=sqlite3.OperationalError("fixture")), \
                patch.object(pa._voice, "permit", new_callable=AsyncMock, return_value=True), \
                patch.object(pa._voice, "requires_screen", return_value=False), \
                patch.object(pa, "_hermes_turn", return_value="resultado") as executor, \
                patch.object(pa, "_send_tool_response", new_callable=AsyncMock) as response:
            await pa._run_delegation({"id": "one", "args": {"request": "pedido"}})
            assert pa._state["delegation"]["status"] == "uncertain"
            assert "registro local falhou" in response.call_args.args[2]
            executor.assert_called_once()
    asyncio.run(scenario())
    assert Journal(isolated_tasks.path).history()[0]["status"] == "uncertain"
