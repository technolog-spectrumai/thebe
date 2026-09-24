"""The dashboard's view of the AI gateway (stack/stats/aiapi.py): budget, times, off, unreachable.

aiapi.py is standard library only, so this runs without the dashboard's FastAPI stack, against the
in-process gateway of test_ai_gateway.
"""

import importlib.util
import json
import socket
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.test_ai_gateway import KEY, TOKEN, FakeProviders, GatewayCase  # noqa: E402

_spec = importlib.util.spec_from_file_location("thebe_stats_aiapi", REPO / "stack" / "stats" / "aiapi.py")
aiapi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aiapi)


class AiClientTests(GatewayCase):
    def test_budget_and_times_for_the_statistics_page(self):
        self.start(backend=FakeProviders(input_tokens=300, output_tokens=200), max_tokens=10000)
        for _ in range(3):
            self.generate()
        status = aiapi.AiClient(self.url, TOKEN.encode(), enabled=True).status()
        self.assertTrue(status["available"])
        usage = status["usage"]
        self.assertEqual((usage["used_tokens"], usage["remaining_tokens"], usage["max_tokens"]), (1500, 8500, 10000))
        self.assertEqual((usage["requests"], usage["seconds"]["count"]), (3, 3))
        self.assertIsNotNone(usage["seconds"]["mean"])
        self.assertIsNotNone(usage["seconds"]["stdev"])
        self.assertEqual([p["name"] for p in usage["providers"]], ["claude", "openai"])
        self.assertNotIn(KEY, json.dumps(status))

    def test_ai_off_is_not_an_error(self):
        status = aiapi.AiClient("http://ai:8891", b"", enabled=False).status()
        self.assertEqual((status["available"], status["enabled"]), (False, False))
        self.assertIn("config.yaml has no ai: section", status["error"])

    def test_a_wrong_token_and_a_stopped_gateway(self):
        self.start()
        status = aiapi.AiClient(self.url, b"0" * 64, enabled=True).status()
        self.assertEqual((status["available"], status["enabled"]), (False, True))
        self.assertIn("rejected the dashboard's token", status["error"])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
        client = aiapi.AiClient(closed, TOKEN.encode(), enabled=True, timeout=2)
        started = time.monotonic()
        with self.assertLogs("stats.ai", "WARNING"):
            status = client.status()
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("not reachable", status["error"])

    def test_a_missing_token_file_names_the_problem(self):
        status = aiapi.AiClient("http://ai:8891", b"", enabled=True,
                                unavailable_reason="AI gateway token: password file x does not exist").status()
        self.assertEqual(status["error"], "AI gateway token: password file x does not exist")


if __name__ == "__main__":
    unittest.main()
