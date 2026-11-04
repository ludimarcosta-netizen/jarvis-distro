import asyncio
from unittest.mock import patch

import plugin_api as pa


def test_bad_settings_never_enable_checkins():
    async def scenario():
        result = await pa.proactivity_action({"action": "save", "config": {"enabled": True}})
        assert result["ok"] is False
        assert pa._proactivity_view()["config"]["enabled"] is False
    asyncio.run(scenario())


def test_api_save_is_persistent_and_controls_override_enabled_setting():
    from proactivity import DEFAULT

    async def scenario():
        with patch.dict(pa._state, {"session": None, "delegation": None}), \
                patch.object(pa, "_listen_only", return_value=True):
            result = await pa.proactivity_action({"action": "save", "config": {**DEFAULT, "enabled": True}})
            assert result["ok"]
            assert result["config"]["enabled"] is True
            assert result["notice"] is None
            assert "Pausado" in result["status"]
            assert pa._proactivity_store().path.exists()
    asyncio.run(scenario())


def test_client_cannot_override_safety_or_dismiss_another_notice():
    async def scenario():
        assert not (await pa.proactivity_action({"action": "dismiss", "id": "guessed"}))["ok"]
        assert not (await pa.proactivity_action({"action": "save", "config": {}, "blocked": False}))["ok"]
    asyncio.run(scenario())
