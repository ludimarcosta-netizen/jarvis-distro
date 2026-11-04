import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import plugin_api as pa


class MeetingPromptTests(unittest.TestCase):
    def setUp(self):
        pa._meeting_prompt_cache.update(at=0.0, value=None)

    tearDown = setUp

    def test_prompt_is_cached_between_polls(self):
        with patch.object(pa, "_meetings_call", return_value={"prompt": {"meeting": {"id": 3}}}) as call:
            self.assertEqual(pa._meeting_prompt()["meeting"]["id"], 3)
            self.assertEqual(pa._meeting_prompt()["meeting"]["id"], 3)
        call.assert_called_once_with("GET", "/api/prompt", timeout=3.0)

    def test_page_offline_means_no_prompt(self):
        with patch.object(pa, "_meetings_call", side_effect=ConnectionRefusedError()):
            self.assertIsNone(pa._meeting_prompt())

    def test_close_forwards_participants_and_clears_the_cache(self):
        pa._meeting_prompt_cache.update(at=10**9, value={"meeting": {"id": 3}})
        with patch.object(pa, "_meetings_call", return_value={"meeting": {"id": 3}}) as call:
            result = asyncio.run(pa.meeting_action({"action": "close", "meeting_id": 3, "participants": ["Wesley"]}))
        self.assertTrue(result["ok"])
        call.assert_called_once_with("POST", "/api/meetings/3/close", {"participants": ["Wesley"]}, 90.0)
        self.assertEqual(pa._meeting_prompt_cache["at"], 0.0)

    def test_continue_and_unknown_actions(self):
        with patch.object(pa, "_meetings_call", return_value={"ok": True}) as call:
            self.assertTrue(asyncio.run(pa.meeting_action({"action": "continue", "meeting_id": 3}))["ok"])
            self.assertFalse(asyncio.run(pa.meeting_action({"action": "apagar", "meeting_id": 3}))["ok"])
            self.assertFalse(asyncio.run(pa.meeting_action({"action": "close", "meeting_id": "x"}))["ok"])
        call.assert_called_once_with("POST", "/api/meetings/3/continue", {}, 5.0)


if __name__ == "__main__":
    unittest.main()
