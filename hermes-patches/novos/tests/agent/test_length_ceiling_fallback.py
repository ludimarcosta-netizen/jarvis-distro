"""A response still truncated after every continuation moves to the fallback chain.

Antigravity's Gemini cut a Centi cron answer at its output cap; continuing on the same model
only re-truncates, and the ceiling exit used to settle for the partial even with a healthy
fallback (Claude Sonnet) configured. The ceiling now asks the fallback chain first; with no
fallback left it keeps the partial as before.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason
from hermes_constants import FINISH_REASON_LENGTH


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.compression_enabled = False
        a.save_trajectories = False
        return a


def _response(content, finish_reason):
    from tests.agent.test_run_agent import _mock_response

    return _mock_response(content=content, finish_reason=finish_reason)


def _run(agent, message):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(message)


def test_ceiling_switches_to_fallback_and_completes(loop_agent):
    loop_agent._fallback_chain = [{"provider": "anthropic", "model": "claude-sonnet-5-5"}]
    loop_agent._fallback_index = 0
    activations = []

    def _activate(reason=None, **_kw):
        activations.append(reason)
        loop_agent._fallback_index = len(loop_agent._fallback_chain)
        return True

    loop_agent.client.chat.completions.create.side_effect = [
        *(_response(f"part {i}. ", FINISH_REASON_LENGTH) for i in range(4)),
        _response("The complete answer from the fallback.", "stop"),
    ]
    with patch.object(loop_agent, "_try_activate_fallback", side_effect=_activate):
        result = _run(loop_agent, "write me a long report")

    assert activations == [FailoverReason.incomplete_response]
    assert result["completed"] is True
    assert result["final_response"] == "The complete answer from the fallback."
    # The fallback answered afresh: no truncated fragment or continuation nudge survives.
    assert not any("part 0." in (m.get("content") or "") for m in result["messages"] if isinstance(m, dict))


def test_ceiling_without_fallback_keeps_the_partial(loop_agent):
    loop_agent._fallback_chain = []
    loop_agent.client.chat.completions.create.side_effect = [
        _response(f"part {i}. ", FINISH_REASON_LENGTH) for i in range(4)
    ]

    result = _run(loop_agent, "write me a long report")

    assert result["completed"] is False
    assert result["partial"] is True
    assert "part 0." in (result["final_response"] or "")
