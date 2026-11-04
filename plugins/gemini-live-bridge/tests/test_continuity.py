import asyncio
import threading
from unittest.mock import AsyncMock, patch

import plugin_api as pa


def test_voice_titles_are_stable_within_conversation_and_different_across_conversations():
    with patch.object(pa._voice, "session", "conversation-one"):
        first = pa._voice_session_title()
        assert first == pa._voice_session_title()
    with patch.object(pa._voice, "session", "conversation-two"):
        assert first != pa._voice_session_title()
    assert "conversation-one" not in first


def test_worker_keeps_immutable_context_after_voice_session_changes(isolated_tasks):
    seen = []

    def work(request):
        seen.append(pa._voice_session_title())
        pa._voice.session = "new-conversation"
        seen.append(pa._voice_session_title())
        return "resultado sintético"

    async def scenario():
        with patch.object(pa._voice, "session", "original-conversation"), \
                patch.dict(pa._state, {"session": "live"}), patch.object(pa, "_live_log"), \
                patch.object(pa._voice, "permit", new_callable=AsyncMock, return_value=True), \
                patch.object(pa._voice, "requires_screen", return_value=False), \
                patch.object(pa, "_hermes_turn", side_effect=work), \
                patch.object(pa, "_deliver_result", new_callable=AsyncMock):
            await pa._run_delegation({"id": "one", "args": {"request": "pedido"}})
    asyncio.run(scenario())
    assert seen == [pa._title_for_voice("original-conversation")] * 2
    assert not pa._voice_turn_title.get()


def test_old_file_result_is_not_delivered_into_new_voice_session():
    entered, release = threading.Event(), threading.Event()

    class Workspace:
        def dispatch(self, body):
            if body["action"] == "start":
                return {"session": "fixture"}
            entered.set()
            assert release.wait(5)
            return {"ok": True, "results": [{"name": "old-private-result"}]}

    async def scenario():
        with patch.object(pa._voice, "session", "one"), patch.object(pa, "_voice_files_session", ""), \
                patch.object(pa, "_local_file_workspace", return_value=Workspace()), \
                patch.object(pa, "_send_tool_response", new_callable=AsyncMock) as response:
            task = asyncio.create_task(pa._run_files({"id": "one", "args": {"acao": "buscar", "termo": "fixture"}}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                pa._voice.session = "two"
            finally:
                release.set()
                await task
            response.assert_not_called()
    asyncio.run(scenario())


def test_cleanup_resets_local_references_without_reusing_approval():
    async def scenario():
        with patch.object(pa, "_voice_files_session", "old-files"), patch.object(pa, "_voice_media_session", "old-media"), \
                patch.object(pa._voice, "end", new_callable=AsyncMock), \
                patch.dict(pa._state, {"task": None, "ws": None}):
            await pa._cleanup()
            assert pa._voice_files_session == pa._voice_media_session == ""
    asyncio.run(scenario())
