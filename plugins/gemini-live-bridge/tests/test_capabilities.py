import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import plugin_api as pa
import pytest


@pytest.mark.parametrize("session,key,status", [(None, "fixture-secret", "warn"), ("live", "fixture-secret", "ok"), (None, "", "off")])
def test_voice_readiness_requires_connection_not_key_file(session, key, status):
    async def scenario():
        with patch.dict(pa._state, {"session": session}), \
                patch.object(pa, "_api_key", return_value=key), \
                patch.object(pa, "_jarvis_module", return_value=SimpleNamespace(system_checks=list)), \
                patch.object(pa, "_sentinel_state", return_value={"alive": True, "muted": False}):
            result = await pa.sistemas()
        voice = next(row for row in result["checks"] if row["id"] == "voz")
        assert voice["status"] == status
        assert "fixture-secret" not in str(result)
    asyncio.run(scenario())
