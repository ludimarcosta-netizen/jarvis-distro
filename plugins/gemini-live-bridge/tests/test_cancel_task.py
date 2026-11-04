"""Cancelar tarefa: interrompe só o turno atual do Hermes e nunca relata cancelamento sem confirmação."""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import plugin_api as pa


class FakeGateway:
    def __init__(self, status="interrupted"):
        self.status = status
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return json.dumps({"id": self.sent[-1]["id"], "result": {"status": self.status}})


def connect(gateway):
    return patch("websockets.connect", lambda *a, **k: gateway)


class CancelTaskTests(unittest.TestCase):
    def setUp(self):
        pa._state.update(delegation={"status": "running", "request": "tarefa"}, delegation_sid="sid-1",
                         delegation_cancelled="")
        self.url = patch("hermes_cli.web_server_chat._build_gateway_ws_url", return_value="ws://127.0.0.1:1/x")
        self.url.start()
        self.log = patch.object(pa, "_live_log")
        self.log.start()

    def tearDown(self):
        self.url.stop()
        self.log.stop()
        pa._state.update(delegation=None, delegation_sid="", delegation_cancelled="")

    def test_interrupts_exact_running_session(self):
        gateway = FakeGateway()
        with connect(gateway):
            result = asyncio.run(pa._interrupt_delegation())
        self.assertTrue(result["ok"])
        self.assertIn("não são desfeitas", result["message"])
        self.assertEqual(gateway.sent[0]["method"], "session.interrupt")
        self.assertEqual(gateway.sent[0]["params"]["session_id"], "sid-1")
        self.assertEqual(pa._state["delegation_cancelled"], "sid-1")

    def test_nothing_running_sends_nothing(self):
        pa._state.update(delegation={"status": "done"})
        gateway = FakeGateway()
        with connect(gateway):
            result = asyncio.run(pa._interrupt_delegation())
        self.assertFalse(result["ok"])
        self.assertEqual(gateway.sent, [])

    def test_unconfirmed_interrupt_is_reported_and_not_marked(self):
        with connect(FakeGateway(status="not_interrupted")):
            result = asyncio.run(pa._interrupt_delegation())
        self.assertFalse(result["ok"])
        self.assertEqual(pa._state["delegation_cancelled"], "")

    def test_cancelled_turn_reports_cancelled_not_error(self):
        async def scenario():
            pa._state["session"] = "live"

            def work(request):
                pa._state["delegation_sid"] = "sid-2"
                pa._state["delegation_cancelled"] = "sid-2"
                raise RuntimeError("Hermes turn ended with status interrupted")
            with patch.object(pa._voice, "permit", new_callable=AsyncMock, return_value=True), \
                    patch.object(pa._voice, "requires_screen", return_value=False), \
                    patch.object(pa, "_hermes_turn", side_effect=work), \
                    patch.object(pa._pdf, "is_pdf_request", return_value=False), \
                    patch.object(pa, "_send_tool_response", new_callable=AsyncMock) as reply:
                await pa._guarded_delegation("one", "tarefa longa", time.time())
            self.assertEqual(pa._state["delegation"]["status"], "cancelled")
            self.assertIn("não foi desfeito", reply.call_args.args[2])
            self.assertEqual(pa._state["delegation_sid"], "")
        asyncio.run(scenario())

    def test_failure_of_other_turn_is_not_called_cancelled(self):
        async def scenario():
            pa._state["session"] = "live"
            pa._state["delegation_cancelled"] = "old"
            with patch.object(pa._voice, "permit", new_callable=AsyncMock, return_value=True), \
                    patch.object(pa._voice, "requires_screen", return_value=False), \
                    patch.object(pa, "_hermes_turn", side_effect=RuntimeError("x")), \
                    patch.object(pa._pdf, "is_pdf_request", return_value=False), \
                    patch.object(pa, "_send_tool_response", new_callable=AsyncMock):
                await pa._guarded_delegation("one", "tarefa", time.time())
            self.assertEqual(pa._state["delegation"]["status"], "uncertain")
        asyncio.run(scenario())
