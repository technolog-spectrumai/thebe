"""Tests for the %%ai magic (stack/jupyter/kernel/thebe_ai.py) against an in-process AI gateway.

ClientTests need nothing beyond the standard library. KernelTests start a real ipykernel through
stack/jupyter/kernel_launcher.py (so they also check that every kernel loads the magic) and read
the set_next_input payloads JupyterLab turns into cells; they are skipped where ipykernel and
jupyter_client are missing (the repo's .venv). With the image's pins:

    python3.13 -m venv /tmp/kv && /tmp/kv/bin/pip install ipykernel==7.3.0 jupyter_client==8.10.0
    /tmp/kv/bin/python -m unittest tests.test_ai_magic -v
"""

import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.test_ai_gateway import ANSWER, TOKEN, FakeProviders, GatewayCase  # noqa: E402

_spec = importlib.util.spec_from_file_location("thebe_ai_magic", REPO / "stack" / "jupyter" / "kernel" / "thebe_ai.py")
magic = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = magic
_spec.loader.exec_module(magic)

LAUNCHER = REPO / "stack" / "jupyter" / "kernel_launcher.py"


class MagicCase(GatewayCase):
    def start(self, *args, **kwargs):
        gateway = super().start(*args, **kwargs)
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-magic-")
        self.addCleanup(tmp.cleanup)
        self.token_file = Path(tmp.name) / "ai_token"
        self.token_file.write_text(TOKEN + "\n")
        self.client = magic.GatewayClient(self.url, str(self.token_file))
        return gateway


class ClientTests(MagicCase):
    def test_the_answer_streams_and_the_closing_line_has_the_usage(self):
        self.start(max_tokens=10000)
        pieces = []
        answer, done = self.client.generate("print pi", "openai", None, pieces.append)
        self.assertEqual((answer, "".join(pieces)), (ANSWER, ANSWER))
        self.assertEqual((done["provider"], done["input_tokens"], done["output_tokens"]), ("openai", 100, 40))
        self.assertEqual(magic.budget_text(done["budget"])[:40], "9,860 of 10,000 tokens left this month (")
        self.assertEqual(magic.extract_code(answer), "import math\nprint(math.pi)\n")

    def test_the_gateways_refusals_reach_the_notebook_word_for_word(self):
        self.start(backend=FakeProviders(input_tokens=900, output_tokens=100), max_tokens=1000)
        self.client.generate("x")
        with self.assertRaises(magic.AiError) as caught:
            self.client.generate("x")
        self.assertIn("The AI token budget is used up: 1,000 of 1,000 tokens this month", str(caught.exception))
        with self.assertRaises(magic.AiError) as caught:
            self.client.generate("x", "gemini")
        self.assertIn("No AI provider named 'gemini'", str(caught.exception))

    def test_a_provider_failure_is_one_line(self):
        self.start(backend=FakeProviders(error="claude (claude-sonnet-5): no answer within 60 s."))
        with self.assertRaises(magic.AiError) as caught:
            self.client.generate("x")
        self.assertEqual(str(caught.exception), "claude (claude-sonnet-5): no answer within 60 s.")

    def test_ai_off_and_a_wrong_token(self):
        self.start()
        with socket.socket() as probe:           # a port nobody listens on: the ai container is not running
            probe.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
        for client in (magic.GatewayClient("", str(self.token_file)), magic.GatewayClient(closed, str(self.token_file))):
            with self.assertRaises(magic.AiError) as caught:
                client.status()
            self.assertEqual(str(caught.exception), magic.OFF)
        self.token_file.write_text("f" * 64)
        with self.assertRaises(magic.AiError) as caught:
            self.client.generate("x")
        self.assertIn("refused this JupyterLab's token", str(caught.exception))
        with self.assertRaises(magic.AiError) as caught:
            magic.GatewayClient(self.url, str(self.token_file) + ".missing").status()
        self.assertIn("Cannot read the AI gateway token", str(caught.exception))

    def test_variables_are_described_never_their_values(self):
        namespace = {"df": _Frame(), "cfg": {"secret_value": 42}, "rows": [1, 2, 3]}
        request = magic.build_request("plot it", ["df", "cfg", "rows"], namespace)
        self.assertIn(f"- df: {__name__}._Frame (shape=(3, 2); columns=city (object), temp (float64))", request)
        self.assertIn("- cfg: dict (keys='secret_value')", request)
        self.assertIn("- rows: list (len=3)", request)
        self.assertNotIn("42", request)
        with self.assertRaises(magic.AiError):
            magic.build_request("x", ["nope"], namespace)

    def test_status_texts(self):
        self.assertEqual(magic.times_text({"count": 3, "mean": 2.25, "stdev": 0.5}),
                         "2.2 ± 0.5 s per answer (mean ± standard deviation of 3)")
        self.assertEqual(magic.times_text({"count": 1, "mean": 4.0, "stdev": None}),
                         "4.0 s per answer (mean ± standard deviation of 1)")
        self.assertEqual(magic.times_text({"count": 0}), "no answers yet")
        self.assertEqual(magic.budget_text({"period": "total", "max_tokens": 0, "used_tokens": 1234}),
                         "1,234 tokens used in total (no limit)")

    def test_fences_and_plain_answers(self):
        self.assertEqual(magic.extract_code("```py\nx = 1\n```\nmore\n```python\ny = 2\n```"), "x = 1\n")
        self.assertEqual(magic.extract_code("x = 1\n\n"), "x = 1\n")
        self.assertEqual(magic.extract_code("```python\nunfinished = True"), "unfinished = True\n")


class _Frame:
    """Looks like a pandas DataFrame to describe_variable, without pandas."""
    shape = (3, 2)
    columns = ["city", "temp"]
    dtypes = {"city": "object", "temp": "float64"}


def _have_kernel() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("ipykernel", "jupyter_client"))


@unittest.skipUnless(_have_kernel(), "needs ipykernel and jupyter_client (the JupyterLab image's pins)")
class KernelTests(MagicCase):
    """A real kernel, started like the image starts it (kernel_launcher.py), runs the magic."""

    def setUp(self):
        from jupyter_client.manager import KernelManager
        self.start(max_tokens=100000)
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-kernel-")
        self.addCleanup(tmp.cleanup)
        spec_dir = Path(tmp.name) / "kernels" / "thebe"
        spec_dir.mkdir(parents=True)
        (spec_dir / "kernel.json").write_text(json.dumps({
            "argv": [sys.executable, "-Xfrozen_modules=off", str(LAUNCHER), "-f", "{connection_file}"],
            "display_name": "thebe", "language": "python"}))
        env = dict(os.environ, AI_GATEWAY_URL=self.url, AI_TOKEN_FILE=str(self.token_file),
                   IPYTHONDIR=str(Path(tmp.name) / "ipython"), JUPYTER_PATH=tmp.name)
        env.pop("PYTHONPATH", None)
        self.km = KernelManager(kernel_name="thebe", kernel_spec_manager=_spec_manager(tmp.name))
        self.km.start_kernel(env=env, cwd=tmp.name)
        self.kc = self.km.client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=60)
        self.addCleanup(self._stop)

    def _stop(self):
        self.kc.stop_channels()
        self.km.shutdown_kernel(now=True)

    def run_cell(self, code):
        out, err = [], []

        def hook(message):
            if message["msg_type"] == "stream":
                (out if message["content"]["name"] == "stdout" else err).append(message["content"]["text"])

        reply = self.kc.execute_interactive(code, timeout=60, output_hook=hook)["content"]
        return reply, "".join(out), "".join(err)

    def test_every_kernel_has_the_magic_and_code_lands_in_a_new_cell_unrun(self):
        reply, out, err = self.run_cell("%%ai\nprint pi")
        self.assertEqual(reply["status"], "ok", err)
        self.assertEqual(reply["payload"], [{"source": "set_next_input", "text": "import math\nprint(math.pi)\n",
                                             "replace": False}])
        self.assertIn(ANSWER, out)
        self.assertIn("code inserted in a new cell below. It has not run", out)
        self.assertIn("99,860 of 100,000 tokens left this month", out)
        reply, out, _ = self.run_cell("print('math' in globals())")
        self.assertEqual(out.strip(), "False")                       # the generated code did not run

    def test_the_cell_button_path_replaces_the_cell_and_describes_variables(self):
        self.run_cell("measurements = {'station': 'secret-station-7'}")
        reply, out, err = self.run_cell("%%ai openai --replace --var measurements\nprint pi\nnicely")
        self.assertEqual(reply["payload"][0]["text"], "# ai: print pi\n# ai: nicely\nimport math\nprint(math.pi)\n")
        self.assertTrue(reply["payload"][0]["replace"])
        name, _model, prompt, _cap = self.providers.calls[-1]
        self.assertEqual(name, "openai")
        self.assertIn("- measurements: dict (keys='station')", prompt)
        self.assertNotIn("secret-station-7", prompt)

    def test_status_and_refusals_are_shown_without_inserting_anything(self):
        reply, out, _ = self.run_cell("%ai status")
        self.assertIn("claude (default): anthropic claude-sonnet-5", out)
        self.assertIn("Budget: 100,000 of 100,000 tokens left this month", out)
        self.assertIn("Answers: 0 (0 failed); no answers yet", out)
        reply, out, err = self.run_cell("%%ai gemini\nx")
        self.assertEqual(reply.get("payload"), [])
        self.assertIn("No AI provider named 'gemini'", err)
        reply, out, err = self.run_cell("%%ai --replace --print\nx")
        self.assertIn("not allowed with argument", err)
        self.assertEqual(reply.get("payload"), [])

    def test_the_helper_function(self):
        reply, out, err = self.run_cell("from thebe_ai import ask\nprint(repr(ask('print pi')))")
        self.assertEqual(out.strip(), repr("import math\nprint(math.pi)\n"), err)
        self.assertEqual(reply.get("payload"), [])


def _spec_manager(path):
    from jupyter_client.kernelspec import KernelSpecManager
    return KernelSpecManager(kernel_dirs=[str(Path(path) / "kernels")])


if __name__ == "__main__":
    unittest.main()
