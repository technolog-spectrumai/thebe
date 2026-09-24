"""Tests for the AI gateway (stack/ai/gateway.py): budget, usage figures, HTTP routes, streaming.

The gateway runs in-process on 127.0.0.1 with a fake provider, so these tests need no SDK and no
network. ProviderSdkTests drive the real openai and anthropic SDKs against local stand-ins for
their streaming APIs; they are skipped where the SDKs are not installed (the repo's .venv), and
run with the gateway's own pins:

    python3.13 -m venv /tmp/gw && /tmp/gw/bin/pip install -r stack/ai/requirements.lock.txt
    /tmp/gw/bin/python -m unittest tests.test_ai_gateway -v
"""

import datetime
import importlib.util
import json
import secrets
import statistics
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from thebe import ai as thebe_ai  # noqa: E402

_spec = importlib.util.spec_from_file_location("thebe_ai_gateway", REPO / "stack" / "ai" / "gateway.py")
gw = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gw          # dataclasses look their module up while the class is built
_spec.loader.exec_module(gw)

KEY = "sk-ant-api03-Gateway-Test-Key-4321"
TOKEN = secrets.token_hex(32)
ANSWER = "Here you go:\n```python\nimport math\nprint(math.pi)\n```\nDone."


def utc(*args) -> float:
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc).timestamp()


def settings(**overrides):
    data = {"default": "claude", "max_tokens": 0, "period": "month", "timeout": 30, "max_output_tokens": 500,
            "providers": {"claude": {"api": "anthropic", "model": "claude-sonnet-5", "api_key": KEY, "base_url": None},
                          "openai": {"api": "openai", "model": "gpt-5.4-mini", "api_key": "sk-proj-Other-Key-0000"}}}
    data.update(overrides)
    return gw.parse_settings(data)


class FakeProviders:
    """Streams ANSWER in pieces and reports fixed usage, or fails, or waits for a signal."""

    def __init__(self, input_tokens=100, output_tokens=40, error=None, gate=None, delay=0.0):
        self.input_tokens, self.output_tokens, self.error, self.gate, self.delay = (
            input_tokens, output_tokens, error, gate, delay)
        self.calls = []

    def generate(self, provider, model, prompt, max_output_tokens, on_text):
        self.calls.append((provider.name, model, prompt, max_output_tokens))
        if self.gate is not None:
            self.gate.wait(10)
        if self.error:
            raise gw.ProviderError(self.error)
        for i in range(0, len(ANSWER), 8):
            on_text(ANSWER[i:i + 8])
            time.sleep(self.delay)
        return gw.Answer(self.input_tokens, self.output_tokens, False)


class GatewayCase(unittest.TestCase):
    def start(self, backend=None, clock=time.time, **overrides):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-gateway-")
        self.addCleanup(tmp.cleanup)
        self.ledger_file = Path(tmp.name) / "usage.jsonl"
        self.settings = settings(**overrides)
        self.ledger = gw.Ledger(self.ledger_file, self.settings, clock)
        self.ledger.load()
        self.providers = backend or FakeProviders()
        self.gateway = gw.Gateway(self.settings, self.ledger, self.providers, TOKEN.encode())
        handler = type("TestHandler", (gw.Handler,), {"gateway": self.gateway, "log_message": lambda *a: None})
        server = gw.Server(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"
        return self.gateway

    def call(self, method, path, body=None, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read().decode()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode()

    def generate(self, prompt="print pi", **extra):
        status, kind, text = self.call("POST", "/generate", {"prompt": prompt, **extra})
        if status != 200:
            return status, json.loads(text)
        self.assertTrue(kind.startswith("application/x-ndjson"), kind)
        return status, [json.loads(line) for line in text.splitlines()]

    def status(self):
        status, _kind, text = self.call("GET", "/status")
        self.assertEqual(status, 200)
        return json.loads(text)


class SettingsTests(unittest.TestCase):
    def test_what_thebe_writes_is_what_the_gateway_reads(self):
        config, problems = thebe_ai.parse_ai({
            "default": "openai",
            "providers": {"claude": {"api": "anthropic", "model": "claude-sonnet-5", "api_key": KEY},
                          "openai": {"api": "openai", "model": "gpt-5.4-mini", "api_key": "sk-proj-Abcdefgh-1234",
                                     "base_url": "https://proxy.example.com/v1"}},
            "budget": {"max_tokens": 5000, "period": "total"}, "timeout": 45, "max_output_tokens": 1000})
        self.assertEqual(problems, [])
        parsed = gw.parse_settings(json.loads(json.dumps(thebe_ai.gateway_settings(config))))
        self.assertEqual((parsed.default, parsed.max_tokens, parsed.period, parsed.timeout, parsed.max_output_tokens),
                         ("openai", 5000, "total", 45.0, 1000))
        self.assertEqual(parsed.providers["claude"].api_key, KEY)
        self.assertEqual(parsed.providers["openai"].base_url, "https://proxy.example.com/v1")
        self.assertNotIn(KEY, repr(parsed))

    def test_problems_are_named_without_the_key(self):
        cases = (
            ({"providers": {}}, "no providers"),
            ({"providers": {"x": {"api": "azure", "model": "m", "api_key": KEY}}}, "api must be one of"),
            ({"providers": {"x": {"api": "openai", "model": "m", "api_key": KEY + " x"}}}, "api_key"),
            ({"default": "nope", "providers": {"x": {"api": "openai", "model": "m", "api_key": KEY}}}, "default"),
            ({"period": "week", "providers": {"x": {"api": "openai", "model": "m", "api_key": KEY}}}, "period"),
            ({"max_tokens": -1, "providers": {"x": {"api": "openai", "model": "m", "api_key": KEY}}}, "max_tokens"),
        )
        for data, want in cases:
            with self.assertRaises(gw.ConfigError) as caught:
                gw.parse_settings(data)
            self.assertIn(want, str(caught.exception))
            self.assertNotIn(KEY, str(caught.exception))


class LedgerTests(unittest.TestCase):
    def ledger(self, clock, **overrides):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-ledger-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "usage.jsonl"
        return gw.Ledger(path, settings(**overrides), clock), path

    def test_monthly_budget_counts_this_month_only_and_names_the_reset_day(self):
        now = utc(2026, 9, 24, 12)
        ledger, _ = self.ledger(lambda: now, max_tokens=1000)
        ledger._records += [gw.Record(utc(2026, 8, 31, 23, 59), "claude", "m", 900, 99, 1.0, True),
                            gw.Record(utc(2026, 9, 1), "claude", "m", 600, 300, 2.0, True)]
        summary = ledger.summary()
        self.assertEqual((summary["used_tokens"], summary["remaining_tokens"], summary["requests"]), (900, 100, 1))
        self.assertEqual((summary["period_start"], summary["period_end"]), ("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z"))
        ledger._records.append(gw.Record(utc(2026, 9, 2), "openai", "m", 60, 40, 1.0, True))
        with self.assertRaises(gw.BudgetError) as caught:
            ledger.reserve(10)
        self.assertIn("used up: 1,000 of 1,000 tokens this month", str(caught.exception))
        self.assertIn("starts again on 2026-10-01 (UTC)", str(caught.exception))

    def test_december_resets_in_january(self):
        ledger, _ = self.ledger(lambda: utc(2026, 12, 31, 23), max_tokens=10)
        self.assertEqual(ledger.summary()["period_end"], "2027-01-01T00:00:00Z")

    def test_total_budget_never_resets(self):
        ledger, _ = self.ledger(lambda: utc(2026, 9, 24), max_tokens=1000, period="total")
        ledger._records.append(gw.Record(utc(2025, 1, 1), "claude", "m", 700, 300, 1.0, True))
        with self.assertRaises(gw.BudgetError) as caught:
            ledger.reserve(10)
        self.assertIn("1,000 of 1,000 tokens in total", str(caught.exception))
        self.assertNotIn("starts again", str(caught.exception))
        self.assertIsNone(ledger.summary()["period_end"])

    def test_the_answer_is_capped_at_what_is_left_and_held_while_it_runs(self):
        ledger, _ = self.ledger(time.time, max_tokens=1000, max_output_tokens=500)
        first = ledger.reserve(300)
        self.assertEqual(first, gw.Reservation(500, 800))           # 500 output + 300 estimated input
        second_cap = ledger.reserve(64)
        self.assertEqual(second_cap.output_cap, 1000 - 800 - 64)    # what the first leaves
        with self.assertRaises(gw.BudgetError) as caught:
            ledger.reserve(10)                                      # everything is held
        self.assertIn("used up", str(caught.exception))
        ledger.finish(first, gw.Record(time.time(), "claude", "m", 250, 100, 1.0, True))
        ledger.finish(second_cap, gw.Record(time.time(), "claude", "m", 50, 20, 1.0, True))
        self.assertEqual(ledger.summary()["in_progress_tokens"], 0)
        self.assertEqual(ledger.summary()["remaining_tokens"], 1000 - 420)

    def test_a_prompt_bigger_than_what_is_left_is_refused(self):
        ledger, _ = self.ledger(time.time, max_tokens=1000)
        with self.assertRaises(gw.BudgetError) as caught:
            ledger.reserve(990)
        self.assertIn("Too little of the AI token budget is left for this prompt", str(caught.exception))

    def test_no_limit(self):
        ledger, _ = self.ledger(time.time, max_tokens=0, max_output_tokens=700)
        self.assertEqual(ledger.reserve(10 ** 9), gw.Reservation(700, 0))
        self.assertIsNone(ledger.summary()["remaining_tokens"])

    def test_mean_and_standard_deviation_of_successful_answers(self):
        ledger, _ = self.ledger(time.time)
        times = [1.5, 2.5, 4.0, 8.0]
        for seconds in times:
            ledger.finish(gw.Reservation(0, 0), gw.Record(time.time(), "claude", "m", 1, 1, seconds, True))
        ledger.finish(gw.Reservation(0, 0), gw.Record(time.time(), "openai", "m", 0, 0, 30.0, False))
        summary = ledger.summary()
        self.assertEqual(summary["seconds"], {"count": 4, "mean": round(statistics.fmean(times), 3),
                                              "stdev": round(statistics.stdev(times), 3)})
        self.assertEqual((summary["requests"], summary["errors"]), (5, 1))
        claude, openai = summary["providers"]
        self.assertEqual((claude["name"], claude["requests"], claude["seconds"]["count"]), ("claude", 4, 4))
        self.assertEqual((openai["errors"], openai["seconds"]), (1, {"count": 0, "mean": None, "stdev": None}))

    def test_one_answer_has_a_mean_but_no_standard_deviation(self):
        ledger, _ = self.ledger(time.time)
        ledger.finish(gw.Reservation(0, 0), gw.Record(time.time(), "claude", "m", 1, 1, 3.0, True))
        self.assertEqual(ledger.summary()["seconds"], {"count": 1, "mean": 3.0, "stdev": None})

    def test_the_file_survives_a_restart_and_bad_lines_are_skipped(self):
        ledger, path = self.ledger(time.time, max_tokens=5000)
        ledger.finish(gw.Reservation(0, 0), gw.Record(time.time(), "claude", "m", 100, 50, 2.0, True))
        with open(path, "a") as handle:
            handle.write('{"t": "broken"\n')           # a line cut by a crash
        ledger.finish(gw.Reservation(0, 0), gw.Record(time.time(), "openai", "m", 10, 5, 4.0, True))
        again = gw.Ledger(path, ledger.settings)
        with self.assertLogs("ai", "WARNING"):
            again.load()
        summary = again.summary()
        self.assertEqual((summary["used_tokens"], summary["requests"], summary["seconds"]["mean"]), (165, 2, 3.0))
        self.assertEqual(set(json.loads(path.read_text().splitlines()[0])), {"t", "provider", "model", "in", "out", "s", "ok"})


class HttpTests(GatewayCase):
    def test_every_route_needs_the_token(self):
        self.start()
        for method, path in (("GET", "/health"), ("GET", "/status"), ("POST", "/generate")):
            for token in (None, "wrong", TOKEN + "x"):
                status, _kind, text = self.call(method, path, {"prompt": "x"} if method == "POST" else None, token=token)
                self.assertEqual(status, 401, (method, path, token))
        self.assertEqual(self.providers.calls, [])
        self.assertEqual(self.call("GET", "/health")[0], 200)

    def test_status_names_providers_but_never_keys(self):
        self.start(max_tokens=2000)
        body = self.status()
        self.assertEqual(body["default"], "claude")
        self.assertEqual([p["name"] for p in body["providers"]], ["claude", "openai"])
        self.assertEqual((body["usage"]["max_tokens"], body["usage"]["remaining_tokens"]), (2000, 2000))
        self.assertNotIn(KEY, json.dumps(body))

    def test_generate_streams_the_answer_then_the_usage(self):
        self.start(max_tokens=10000)
        status, lines = self.generate("print pi", provider="openai")
        self.assertEqual(status, 200)
        self.assertEqual("".join(line.get("text", "") for line in lines), ANSWER)
        done = lines[-1]
        self.assertEqual((done["done"], done["provider"], done["model"], done["input_tokens"], done["output_tokens"]),
                         (True, "openai", "gpt-5.4-mini", 100, 40))
        self.assertEqual((done["budget"]["used_tokens"], done["budget"]["remaining_tokens"]), (140, 9860))
        name, model, prompt, cap = self.providers.calls[0]
        self.assertEqual((name, model, prompt, cap), ("openai", "gpt-5.4-mini", "print pi", 500))
        usage = self.status()["usage"]
        self.assertEqual((usage["requests"], usage["used_tokens"], usage["seconds"]["count"]), (1, 140, 1))
        self.assertEqual(len(self.ledger_file.read_text().splitlines()), 1)
        self.assertNotIn("print pi", self.ledger_file.read_text())      # never the prompt

    def test_model_override_and_the_default_provider(self):
        self.start()
        self.generate("x", model="claude-opus-5")
        self.assertEqual(self.providers.calls[-1][:2], ("claude", "claude-opus-5"))

    def test_budget_used_up_is_refused_before_the_provider_is_asked(self):
        self.start(backend=FakeProviders(input_tokens=600, output_tokens=400), max_tokens=1000)
        self.assertEqual(self.generate()[0], 200)
        status, body = self.generate()
        self.assertEqual(status, 429)
        self.assertIn("The AI token budget is used up: 1,000 of 1,000 tokens this month", body["error"])
        self.assertEqual(len(self.providers.calls), 1)
        self.assertEqual(self.status()["usage"]["remaining_tokens"], 0)

    def test_a_failed_answer_is_counted_as_an_error_not_as_a_time(self):
        self.start(backend=FakeProviders(error="claude (claude-sonnet-5): the API key was rejected."))
        status, lines = self.generate()
        self.assertEqual(status, 200)
        self.assertEqual(lines[-1]["error"], "claude (claude-sonnet-5): the API key was rejected.")
        usage = self.status()["usage"]
        self.assertEqual((usage["requests"], usage["errors"], usage["seconds"]["count"]), (1, 1, 0))

    def test_bad_requests(self):
        self.start()
        cases = (
            ({"prompt": "  "}, 400, "the prompt is empty"),
            ({"prompt": "x", "provider": "gemini"}, 404, "No AI provider named 'gemini' is configured (configured: claude, openai)."),
            ({"prompt": "x", "model": "bad model"}, 400, "the model name is not usable"),
            ({"prompt": "x" * (gw.MAX_PROMPT_CHARS + 1)}, 413, "longer than"),
        )
        for body, want_status, want in cases:
            status, _kind, text = self.call("POST", "/generate", body)
            self.assertEqual(status, want_status, body.get("provider") or body.get("model"))
            self.assertIn(want, json.loads(text)["error"])
        self.assertEqual(self.call("GET", "/generate")[0], 405)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.providers.calls, [])

    def test_too_many_answers_at_once(self):
        gate = threading.Event()
        self.start(backend=FakeProviders(gate=gate))
        self.gateway._slots = threading.BoundedSemaphore(1)
        first = threading.Thread(target=self.generate)
        first.start()
        self.assertTrue(_wait(lambda: self.gateway.active == 1))
        status, body = self.generate()
        self.assertEqual(status, 429)
        self.assertIn("already being written", body["error"])
        gate.set()
        first.join(10)
        self.assertEqual(self.status()["usage"]["requests"], 1)

    def test_a_notebook_that_stops_waiting_still_gets_its_tokens_counted(self):
        self.start(backend=FakeProviders(input_tokens=11, output_tokens=22, delay=0.05), max_tokens=1000)
        request = urllib.request.Request(self.url + "/generate", data=json.dumps({"prompt": "x"}).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        response = opener.open(request, timeout=10)
        response.readline()                          # the first piece, then Kernel -> Interrupt
        response.close()
        self.assertTrue(_wait(lambda: self.gateway.active == 0))
        usage = self.status()["usage"]
        self.assertEqual((usage["used_tokens"], usage["requests"], usage["errors"]), (33, 1, 0))


def _wait(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


# --- the real SDKs against stand-ins for the two APIs -------------------------------------------

class Stub(BaseHTTPRequestHandler):
    """Anthropic /v1/messages and OpenAI /v1/responses, streaming; or an error status."""
    status = 200
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.seen.append((self.path, dict(self.headers), body))
        if Stub.status != 200:
            payload = json.dumps({"type": "error", "error": {"type": "error", "message": "no"}}).encode()
            self.send_response(Stub.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("retry-after", "0")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [ANSWER[i:i + 7] for i in range(0, len(ANSWER), 7)]
        events = self._anthropic(chunks) if self.path.endswith("/messages") else self._openai(chunks, body)
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

    @staticmethod
    def _anthropic(chunks):
        message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "stub", "content": [],
                   "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 31, "output_tokens": 1, "cache_read_input_tokens": 4}}
        yield "message_start", {"type": "message_start", "message": message}
        yield "content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        for chunk in chunks:
            yield "content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "text_delta", "text": chunk}}
        yield "content_block_stop", {"type": "content_block_stop", "index": 0}
        yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": 19}}
        yield "message_stop", {"type": "message_stop"}

    @staticmethod
    def _openai(chunks, body):
        base = {"id": "resp_1", "object": "response", "created_at": 1, "model": body["model"], "output": [],
                "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "status": "in_progress"}
        item = {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress", "content": []}
        part = {"type": "output_text", "text": "", "annotations": []}
        seq = iter(range(1000))
        yield "response.created", {"type": "response.created", "sequence_number": next(seq), "response": base}
        yield "response.output_item.added", {"type": "response.output_item.added", "sequence_number": next(seq),
                                             "output_index": 0, "item": item}
        yield "response.content_part.added", {"type": "response.content_part.added", "sequence_number": next(seq),
                                              "item_id": "msg_1", "output_index": 0, "content_index": 0, "part": part}
        for chunk in chunks:
            yield "response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": next(seq),
                                                 "item_id": "msg_1", "output_index": 0, "content_index": 0,
                                                 "delta": chunk, "logprobs": []}
        done = dict(item, status="completed", content=[dict(part, text=ANSWER)])
        yield "response.output_item.done", {"type": "response.output_item.done", "sequence_number": next(seq),
                                            "output_index": 0, "item": done}
        usage = {"input_tokens": 42, "output_tokens": 17, "total_tokens": 59,
                 "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
        yield "response.completed", {"type": "response.completed", "sequence_number": next(seq),
                                     "response": dict(base, status="completed", output=[done], usage=usage)}


def _have_sdks() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("openai", "anthropic"))


@unittest.skipUnless(_have_sdks(), "needs the openai and anthropic SDKs (stack/ai/requirements.lock.txt)")
class ProviderSdkTests(GatewayCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.api_url = f"http://127.0.0.1:{cls.api.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()

    def setUp(self):
        Stub.status, Stub.seen = 200, []
        providers = {"claude": {"api": "anthropic", "model": "claude-sonnet-5", "api_key": KEY, "base_url": self.api_url},
                     "openai": {"api": "openai", "model": "gpt-5.4-mini", "api_key": "sk-proj-Other-Key-0000",
                                "base_url": self.api_url + "/v1"}}
        overrides = {"providers": providers, "max_tokens": 100000, "timeout": 10}
        self.start(backend=gw.Providers(settings(**overrides)), **overrides)

    def test_both_sdks_stream_and_report_their_usage(self):
        for name, path, header, used in (("claude", "/v1/messages", "x-api-key", 31 + 4 + 19),
                                         ("openai", "/v1/responses", "authorization", 42 + 17)):
            Stub.seen = []
            status, lines = self.generate("print pi", provider=name)
            self.assertEqual(status, 200)
            self.assertEqual("".join(line.get("text", "") for line in lines), ANSWER)
            self.assertEqual(lines[-1]["input_tokens"] + lines[-1]["output_tokens"], used, name)
            request_path, headers, body = Stub.seen[0]
            self.assertEqual(request_path, path)
            self.assertIn(KEY if name == "claude" else "sk-proj-Other-Key-0000", headers.get(header) or headers.get(header.title()))
            self.assertIn("print pi", json.dumps(body))
            self.assertIn("```python", json.dumps(body))                      # the gateway's system prompt
            self.assertEqual(body.get("max_tokens") or body.get("max_output_tokens"), 500)
        self.assertEqual(self.status()["usage"]["used_tokens"], 54 + 59)

    def test_errors_become_one_line_without_the_key(self):
        for status, want in ((401, "the API key was rejected"), (429, "rate limited"), (404, "model is not available")):
            Stub.status = status
            _, lines = self.generate()
            self.assertIn(want, lines[-1]["error"])
            self.assertNotIn(KEY, json.dumps(lines))
        usage = self.status()["usage"]
        self.assertEqual((usage["errors"], usage["used_tokens"]), (3, 0))


if __name__ == "__main__":
    unittest.main()
