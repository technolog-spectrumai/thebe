"""Tests for the project requirements.txt baseline in the deps runner (stack/jupyter/deps_runner.py).

They run the real runner code and real pip, offline: tiny wheels are written into a local
directory and requirements.txt points pip there with --no-index / --find-links. No Qt, no Docker.
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "stack" / "jupyter" / "deps_runner.py"
TOKEN = "t" * 40


def make_wheel(directory: Path, name: str, version: str, requires=()) -> None:
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": f"VERSION = {version!r}\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                            + "".join(f"Requires-Dist: {item}\n" for item in requires),
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: thebe-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{path},,\n" for path in files) + f"{dist}/RECORD,,\n"
    with zipfile.ZipFile(directory / f"{name}-{version}-py3-none-any.whl", "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
        archive.writestr(f"{dist}/RECORD", record)


def pip_works_in_a_system_site_venv() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "v"
        made = subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", "--without-pip", str(venv)],
                              capture_output=True)
        return made.returncode == 0 and subprocess.run(
            [str(venv / "bin" / "python"), "-m", "pip", "--version"], capture_output=True).returncode == 0


PIP_OK = pip_works_in_a_system_site_venv()


class RunnerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-preinstall-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        for name, version in (("alpha", "1.0"), ("alpha", "2.0"), ("beta", "1.0"), ("gamma", "1.0")):
            make_wheel(self.wheels, name, version)
        self.constraints = self.root / "constraints.txt"
        self.constraints.write_text("alpha==1.0\n")          # as the image pins its own packages
        self.token_file = self.root / "token"
        self.token_file.write_text(TOKEN)
        self.runner = self.load()

    def load(self):
        """A fresh copy of the runner module: what a recreated container starts with."""
        env = {"CUSTOM_DIR": str(self.root / "custom"), "PIP_CACHE_DIR": str(self.root / "cache"),
               "CONSTRAINTS_FILE": str(self.constraints), "DEPS_TOKEN_FILE": str(self.token_file),
               "STATS_USER": "jupyter"}
        with mock.patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location(f"deps_runner_test_{id(self)}_{len(os.listdir(self.root))}",
                                                          RUNNER)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        module.prepare_dirs()
        self.env = env
        return module

    def requirements(self, *lines: str) -> str:
        return "\n".join(["--no-index", f"--find-links {self.wheels}", *lines]) + "\n"

    def run_cli(self, *args: str, stdin: str | bytes = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        fake_stdin = io.TextIOWrapper(io.BytesIO(stdin if isinstance(stdin, bytes) else stdin.encode("utf-8")))
        with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "stdin", fake_stdin), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.runner.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def installed(self) -> dict:
        return {item["name"]: item["version"] for item in self.runner.JobRunner(self.runner.Sizes()).installed()}

    def job_id(self) -> str | None:
        path = self.root / "custom" / "job.json"
        return json.loads(path.read_text())["id"] if path.exists() else None


class CheckTests(RunnerCase):
    def test_valid_and_invalid_files(self):
        code, out, err = self.run_cli("check", stdin="requests\nopencv-python\n--extra-index-url https://x/simple\n"
                                                      "torch==2.14.0+cpu\n")
        self.assertEqual(code, 0, err)
        self.assertIn("3 package line(s), accepted", out)
        code, out, err = self.run_cli("check", stdin="ok\n-e .\n--target /x\n-r other.txt\nfoo ${HOME}\n"
                                                      "rich --index-url https://x\n")
        self.assertEqual(code, 2)
        for text in ("line 2: --editable is not allowed", "line 3: --target is not allowed",
                     "line 4: --requirement is not allowed", "line 5: Environment variables",
                     "line 6: pip ignores --index-url next to a package"):
            self.assertIn(text, err)
        self.assertEqual(self.run_cli("check", stdin="")[0], 0)
        code, out, err = self.run_cli("check", stdin="caf\xe9\n".encode("latin-1"))
        self.assertEqual(code, 2)
        self.assertIn("not UTF-8", err)
        code, out, err = self.run_cli("check", stdin=b"x" * (64 * 1024 + 1))
        self.assertEqual(code, 2)
        self.assertIn("larger than 64 KB", err)


@unittest.skipUnless(PIP_OK, "pip is not importable in a --system-site-packages venv here")
class BaselineTests(RunnerCase):
    def test_a_missing_or_empty_file_installs_nothing(self):
        for text in ("", "# only comments\n\n", "--no-index\n"):
            code, out, err = self.run_cli("baseline", "--local", stdin=text)
            self.assertEqual(code, 0, err)
            self.assertIn("lists no packages; nothing extra to install", out)
        self.assertFalse((self.root / "custom" / "venv").exists())
        self.assertIsNone(self.job_id())

    def test_first_install_unchanged_redeploy_and_changed_requirements(self):
        text = self.requirements("alpha", "beta==1.0")
        code, out, err = self.run_cli("baseline", "--local", stdin=text)
        self.assertEqual(code, 0, out + err)
        self.assertIn("first install of requirements.txt", out)
        self.assertIn("Packages from requirements.txt are installed", out)
        self.assertIn("Custom packages", out)                              # sizes and free disk
        # alpha at the version the "image" pins (2.0 exists); the real image already has its pins.
        self.assertEqual(self.installed(), {"alpha": "1.0", "beta": "1.0"})
        first = self.job_id()
        state = json.loads((self.root / "custom" / "baseline.json").read_text())
        self.assertTrue(state["ok"])

        code, out, err = self.run_cli("baseline", "--local", stdin=text)
        self.assertEqual(code, 0, err)
        self.assertIn("unchanged and already installed", out)
        self.assertEqual(self.job_id(), first)                             # no pip run

        code, out, err = self.run_cli("baseline", "--local", stdin=text + "gamma\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("requirements.txt changed", out)
        self.assertNotEqual(self.job_id(), first)
        self.assertEqual(self.installed(), {"alpha": "1.0", "beta": "1.0", "gamma": "1.0"})

    def test_invalid_lines_change_nothing(self):
        self.run_cli("baseline", "--local", stdin=self.requirements("beta"))
        before = (self.root / "custom" / "baseline.txt").read_text()
        code, out, err = self.run_cli("baseline", "--local", stdin="beta\n-e ./src\n")
        self.assertEqual(code, 2)
        self.assertIn("line 2: --editable is not allowed", err)
        self.assertEqual((self.root / "custom" / "baseline.txt").read_text(), before)

    def test_a_pinned_image_package_is_never_replaced(self):
        self.run_cli("baseline", "--local", stdin=self.requirements("beta"))
        code, out, err = self.run_cli("baseline", "--local", stdin=self.requirements("beta", "alpha==2.0"))
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", out)
        self.assertIn("alpha==2.0", out.split("Installing requirements.txt ended")[1])   # named in the summary
        self.assertIn("keeps the packages installed before", out)
        self.assertEqual(self.installed(), {"beta": "1.0"})                # the working environment stays
        self.assertFalse(json.loads((self.root / "custom" / "baseline.json").read_text())["ok"])
        # A deploy after the fix installs again, even though the file's hash matches an older one.
        code, out, err = self.run_cli("baseline", "--local", stdin=self.requirements("beta"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("already installed", out)

    def test_an_interrupted_install_is_redone(self):
        text = self.requirements("beta")
        self.run_cli("baseline", "--local", stdin=text)
        # What a container killed in the middle of the next job leaves: job.json "running" and
        # baseline.json marked unfinished when the job started.
        job = json.loads((self.root / "custom" / "job.json").read_text())
        (self.root / "custom" / "job.json").write_text(json.dumps(dict(job, status="running")))
        state = json.loads((self.root / "custom" / "baseline.json").read_text())
        (self.root / "custom" / "baseline.json").write_text(json.dumps(dict(state, ok=False)))
        self.runner = self.load()
        self.assertEqual(self.runner.JobRunner(self.runner.Sizes()).job["status"], "interrupted")
        code, out, err = self.run_cli("baseline", "--local", stdin=text)
        self.assertEqual(code, 0, out)
        self.assertIn("the last install did not finish", out)

    def test_start_marks_the_baseline_unfinished_before_pip_runs(self):
        runner = self.runner
        (self.root / "custom" / "baseline.txt").write_text(self.requirements("beta"))
        job_runner = runner.JobRunner(runner.Sizes())
        with mock.patch.object(runner.JobRunner, "_run", lambda self, action: None):
            job_runner.start("install")
        self.addCleanup(job_runner._log_handle.close)
        state = json.loads((self.root / "custom" / "baseline.json").read_text())
        self.assertEqual((state["ok"], state["sha256"]), (False, runner.text_digest(self.requirements("beta"))))
        with self.assertRaises(runner.Busy):             # the file lock is held for the job
            runner.JobRunner(runner.Sizes()).start("install")

    def test_the_environment_persists_and_drift_is_repaired(self):
        text = self.requirements("beta", "gamma")
        self.run_cli("baseline", "--local", stdin=text)
        self.runner = self.load()                           # a recreated container, same volume
        code, out, err = self.run_cli("baseline", "--local", stdin=text)
        self.assertIn("unchanged and already installed", out)
        site = next((self.root / "custom" / "venv" / "lib").glob("python*/site-packages"))
        for path in site.glob("gamma*"):                   # a package lost from the volume
            subprocess.run(["rm", "-rf", str(path)], check=True)
        code, out, err = self.run_cli("baseline", "--local", stdin=text)
        self.assertEqual(code, 0, out)
        self.assertIn("missing from the environment", out)
        self.assertEqual(self.installed(), {"beta": "1.0", "gamma": "1.0"})

    def test_the_page_list_and_the_baseline_install_together_with_the_shared_cache(self):
        (self.root / "custom" / "requirements.txt").write_text(self.requirements("gamma"))
        (self.root / "cache" / "keep").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "cache" / "keep").write_text("cached download")
        seen = []
        original = self.runner.JobRunner._command

        def record(job_runner, argv, env, cwd):
            seen.append((argv, env))
            return original(job_runner, argv, env, cwd)

        with mock.patch.object(self.runner.JobRunner, "_command", record):
            code, out, err = self.run_cli("baseline", "--local", stdin=self.requirements("beta"))
        self.assertEqual(code, 0, out)
        pip_argv, pip_env = seen[-1]
        self.assertEqual([pip_argv[i + 1] for i, a in enumerate(pip_argv) if a == "-r"],
                         [str(self.root / "custom" / "baseline.txt"), str(self.root / "custom" / "requirements.txt")])
        self.assertIn("--constraint", pip_argv)
        self.assertEqual(pip_env["PIP_CACHE_DIR"], str(self.root / "cache"))
        self.assertEqual((self.root / "cache" / "keep").read_text(), "cached download")    # never cleared
        self.assertEqual(self.installed(), {"beta": "1.0", "gamma": "1.0"})
        state = self.runner.Handler.state(mock.Mock(runner=self.runner.JobRunner(self.runner.Sizes()),
                                                    sizes=self.runner.Sizes()), {})[1]
        self.assertEqual((state["baseline"]["packages"], state["baseline"]["installed"]), (1, True))


@unittest.skipUnless(PIP_OK, "pip is not importable in a --system-site-packages venv here")
class ServerTests(RunnerCase):
    """The deploy path with statistics on: deps_runner.py baseline talks to the running server."""

    def setUp(self):
        super().setUp()
        runner = self.runner
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            runner.PORT = probe.getsockname()[1]
        sizes = runner.Sizes()
        runner.Handler.username, runner.Handler.password = b"jupyter", TOKEN.encode()
        runner.Handler.runner, runner.Handler.sizes = runner.JobRunner(sizes), sizes
        server = runner.Server(("127.0.0.1", runner.PORT), runner.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server_runner = runner.Handler.runner

    def test_install_through_the_server_and_the_page_state(self):
        text = self.requirements("beta")
        code, out, err = self.run_cli("baseline", stdin=text)
        self.assertEqual(code, 0, out + err)
        self.assertIn("first install", out)
        self.assertIn("$ ", out)                                        # the job log, streamed
        self.assertEqual(self.server_runner.job_copy()["status"], "succeeded")
        code, out, err = self.run_cli("baseline", stdin=text)
        self.assertIn("unchanged and already installed", out)
        # The page sees the baseline, read-only (the dashboard proxies no PUT /baseline).
        auth = base64.b64encode(f"jupyter:{TOKEN}".encode()).decode()
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.runner.PORT, timeout=10)
        connection.request("GET", "/state", headers={"Authorization": f"Basic {auth}"})
        state = json.loads(connection.getresponse().read())
        self.assertEqual(state["baseline"]["text"], text)
        self.assertTrue(state["baseline"]["installed"])
        code, out, err = self.run_cli("baseline", stdin="beta\n-e .\n")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
