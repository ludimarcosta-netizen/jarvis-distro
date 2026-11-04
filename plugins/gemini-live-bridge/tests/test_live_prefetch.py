import asyncio
import http.client
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import plugin_api as pa


class PrefetchTestCase(unittest.TestCase):
    def setUp(self):
        pa._candidates_cache.update(names=[], at=0.0)
        pa._token_cache.update(name="", at=0.0, future=None)

    tearDown = setUp


class CandidatesCacheTests(PrefetchTestCase):
    def test_model_list_is_fetched_once_within_ttl(self):
        with patch.object(pa, "_fetch_candidates", return_value=["models/gemini-3.8-live"]) as fetch:
            self.assertEqual(pa._candidates(), ["models/gemini-3.8-live"])
            self.assertEqual(pa._candidates(), ["models/gemini-3.8-live"])
        fetch.assert_called_once()

    def test_failed_listing_is_not_cached(self):
        with patch.object(pa, "_fetch_candidates", side_effect=[[pa.DEFAULT_MODEL], ["models/x-live"]]) as fetch:
            pa._candidates()
            self.assertEqual(pa._candidates(), ["models/x-live"])
        self.assertEqual(fetch.call_count, 2)


class TokenPrefetchTests(PrefetchTestCase):
    def test_prefetched_token_is_used_once(self):
        with patch.object(pa, "_api_key", return_value="k"), \
                patch.object(pa, "_mint_token", side_effect=["pre", "fresh"]) as mint, \
                patch.object(pa, "_fetch_candidates", return_value=["models/x-live"]):
            pa._prefetch_live()
            self.assertEqual(pa._take_token(), "pre")
            self.assertEqual(pa._take_token(), "fresh")  # uso único: o segundo é novo
        self.assertEqual(mint.call_count, 2)

    def test_stale_prefetched_token_is_replaced(self):
        pa._token_cache.update(name="velho", at=time.monotonic() - pa.TOKEN_MAX_AGE_S - 1)
        with patch.object(pa, "_mint_token", return_value="novo"):
            self.assertEqual(pa._take_token(), "novo")

    def test_empty_wake_signal_warms_the_live_session(self):
        async def scenario():
            srv = await asyncio.start_server(pa._bridge_handle, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]

            def post():
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/wake", body=json.dumps({"text": ""}),
                             headers={"Content-Type": "application/json"})
                return conn.getresponse().status

            with patch.object(pa, "_listen_only", return_value=False), \
                    patch.object(pa, "_capture_paused", False), \
                    patch.object(pa, "_prefetch_live") as prefetch:
                status = await asyncio.get_running_loop().run_in_executor(None, post)
            srv.close()
            return status, prefetch.call_count

        status, calls = asyncio.run(scenario())
        self.assertEqual((status, calls), (200, 1))


if __name__ == "__main__":
    unittest.main()
