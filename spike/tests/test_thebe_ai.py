"""Spike tests: the %%ai magic and the provider layer, without real API keys.

The real openai and anthropic SDKs talk to local stub servers (base_url) that speak their
streaming formats, so streaming, retries, rate limits and rejected keys run through the SDK
code. Needs ipython, openai and anthropic:

    python -m venv /tmp/ai && /tmp/ai/bin/pip install ipython openai anthropic
    cd spike && /tmp/ai/bin/python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from IPython.core.interactiveshell import InteractiveShell  # noqa: E402

from thebe_ai import config as cfg  # noqa: E402
from thebe_ai.magic import AiMagics, describe_variable, extract_code  # noqa: E402
from thebe_ai.providers import ProviderError, make_provider  # noqa: E402

ANSWER = "Here you go:\n```python\nimport math\nprint(math.pi)\n```\nDone."


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
            payload = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}).encode()
            self.send_response(Stub.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("retry-after", "0")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if not body.get("stream"):
            # Non-streaming replies, as jupyter-ai's magic (through litellm) asks for them.
            if self.path.endswith("/messages"):
                reply = {"id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model", "stub"),
                         "content": [{"type": "text", "text": ANSWER}], "stop_reason": "end_turn",
                         "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 9}}
            else:
                reply = {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": body.get("model", "stub"),
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": ANSWER}}],
                         "usage": {"prompt_tokens": 3, "completion_tokens": 9, "total_tokens": 12}}
            payload = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
                   "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}
        yield "message_start", {"type": "message_start", "message": message}
        yield "content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        for chunk in chunks:
            yield "content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "text_delta", "text": chunk}}
        yield "content_block_stop", {"type": "content_block_stop", "index": 0}
        yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": 9}}
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
        yield "response.completed", {"type": "response.completed", "sequence_number": next(seq),
                                     "response": dict(base, status="completed", output=[done])}


class StubServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Stub.status, Stub.seen = 200, []

    def config(self, retries=0):
        providers = {
            "claude": cfg.ProviderConfig("claude", "anthropic", "claude-sonnet-5", "sk-ant-secret-key-1234", self.url),
            "openai": cfg.ProviderConfig("openai", "openai", "gpt-5.4-mini", "sk-openai-secret-5678", self.url + "/v1"),
        }
        return cfg.AiConfig(providers, "claude", timeout=5, max_retries=retries, max_output_tokens=256)


class ProviderTests(StubServerCase):
    def test_both_sdks_stream_through_one_interface(self):
        config = self.config()
        for name, path, auth in (("claude", "/v1/messages", "x-api-key"), ("openai", "/v1/responses", "authorization")):
            streamed = []
            text = make_provider(config.providers[name], config).generate("system", "prompt", streamed.append)
            self.assertEqual(text, ANSWER, name)
            self.assertGreater(len(streamed), 3, name)                 # arrived in pieces
            request_path, headers, body = Stub.seen[-1]
            self.assertEqual(request_path, path)
            self.assertIn(config.providers[name].api_key, headers.get(auth) or headers.get(auth.title()) or "")
            self.assertTrue(body.get("stream"), name)

    def test_errors_become_one_line_messages_without_the_key(self):
        config = self.config(retries=1)
        for status, text in ((429, "rate limited (still after 1 retry)"), (401, "the API key was rejected")):
            Stub.status = status
            for name in ("claude", "openai"):
                Stub.seen = []
                with self.assertRaises(ProviderError) as caught:
                    make_provider(config.providers[name], config).generate("s", "p")
                self.assertIn(text, str(caught.exception))
                self.assertNotIn(config.providers[name].api_key, str(caught.exception))
                self.assertEqual(len(Stub.seen), 2 if status == 429 else 1, (name, status))   # 429 retried

    def test_unreachable_api(self):
        config = self.config()
        broken = cfg.ProviderConfig("claude", "anthropic", "m", "sk-ant-secret-key-1234", "http://127.0.0.1:9")
        with self.assertRaisesRegex(ProviderError, "cannot reach the API"):
            make_provider(broken, config).generate("s", "p")


class MagicTests(StubServerCase):
    def setUp(self):
        super().setUp()
        self.shell = InteractiveShell.instance()
        self.magics = AiMagics(self.shell, ai_config=self.config())
        self.shell.register_magics(self.magics)
        self.inserted = []
        self.shell.set_next_input = lambda text, replace=False: self.inserted.append((text, replace))

    def run_cell(self, source):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.shell.run_cell(source)
        return out.getvalue(), err.getvalue()

    def test_cell_magic_inserts_code_below_and_never_runs_it(self):
        self.shell.user_ns.pop("math", None)
        out, err = self.run_cell("%%ai\nprint pi")
        self.assertEqual(self.inserted, [("import math\nprint(math.pi)\n", False)])
        self.assertIn("inserted in a new cell below", out)
        self.assertIn("Here you go", out)                             # streamed into the output
        self.assertNotIn("math", self.shell.user_ns)                  # not executed

    def test_replace_print_provider_model_and_variables(self):
        self.shell.user_ns["sales"] = {"north": 1, "south": 2}
        self.run_cell("%%ai openai --model gpt-5.5 --replace --var sales\nsum the values\nand print")
        self.assertEqual(self.inserted[-1], ("# ai: sum the values\n# ai: and print\nimport math\nprint(math.pi)\n", True))
        path, _headers, body = Stub.seen[-1]
        self.assertEqual((path, body["model"]), ("/v1/responses", "gpt-5.5"))
        self.assertIn("- sales: dict (keys='north', 'south')", body["input"])
        self.assertNotIn("1", body["input"].split("keys=")[1].split(")")[0])    # no values sent
        before = len(self.inserted)
        out, err = self.run_cell("%%ai claude --print\nx")
        self.assertEqual(len(self.inserted), before)                  # --print inserts nothing

    def test_line_magic_status_and_errors(self):
        out, err = self.run_cell("%ai status")
        self.assertIn("claude (default): anthropic claude-sonnet-5, key …1234", out)
        self.assertNotIn("sk-ant-secret", out)
        self.run_cell("%ai openai print pi")
        self.assertEqual(Stub.seen[-1][0], "/v1/responses")
        out, err = self.run_cell("%%ai mistral\nx")
        self.assertIn("No AI provider named 'mistral'", err)
        out, err = self.run_cell("%%ai --var nope\nx")
        self.assertIn("--var nope: no such variable", err)
        Stub.status = 429
        out, err = self.run_cell("%%ai\nx")
        self.assertIn("rate limited", err)
        self.assertEqual(len(self.inserted), 1)                        # only the one success

    def test_no_keys(self):
        magics = AiMagics(self.shell, ai_config=cfg.AiConfig({}, ""))
        self.shell.register_magics(magics)
        out, err = self.run_cell("%%ai\nx")
        self.assertIn("No AI provider has an API key", err)


class AskTests(StubServerCase):
    """Option D: the plain function, same request path as the magic."""

    def test_ask_returns_code_and_inserts_only_when_asked(self):
        import thebe_ai
        shell = InteractiveShell.instance()
        thebe_ai._magics = AiMagics(shell, ai_config=self.config())
        inserted = []
        shell.set_next_input = lambda text, replace=False: inserted.append((text, replace))
        self.assertEqual(thebe_ai.ask("print pi"), "import math\nprint(math.pi)\n")
        self.assertEqual(inserted, [])
        thebe_ai.ask("print pi", "openai", insert=True)
        self.assertEqual(inserted, [("import math\nprint(math.pi)\n", False)])
        self.assertEqual(Stub.seen[-1][0], "/v1/responses")


class HelperTests(unittest.TestCase):
    def test_extract_code(self):
        self.assertEqual(extract_code("a\n```python\nx = 1\n```\nb"), "x = 1\n")
        self.assertEqual(extract_code("```\ny = 2\n```"), "y = 2\n")
        self.assertEqual(extract_code("z = 3"), "z = 3\n")
        self.assertEqual(extract_code("```python\nunfinished = True"), "unfinished = True\n")

    def test_describe_variable_has_no_values(self):
        self.assertEqual(describe_variable("xs", [5, 6, 7]), "- xs: list (len=3)")

    def test_config_file_and_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai.json"
            path.write_text(json.dumps({"default": "openai", "timeout": 30, "providers": {
                "claude": {"api": "anthropic", "model": "claude-sonnet-5", "api_key": ""},
                "openai": {"api": "openai", "model": "gpt-5.4-mini", "api_key": "sk-abcdefghijkl"}}}))
            config = cfg.load_config({"THEBE_AI_CONFIG": str(path)})
            self.assertEqual((list(config.providers), config.default, config.timeout), (["openai"], "openai", 30.0))
            self.assertNotIn("sk-abcdefghijkl", repr(config))
            path.write_text("{bad")
            with self.assertRaisesRegex(cfg.ConfigError, "cannot be read as JSON"):
                cfg.load_config({"THEBE_AI_CONFIG": str(path)})
        config = cfg.load_config({"THEBE_AI_CONFIG": "/nonexistent", "ANTHROPIC_API_KEY": "sk-ant-zzzzzzzz1234"})
        self.assertEqual((list(config.providers), config.default), (["claude"], "claude"))


if __name__ == "__main__":
    unittest.main()
