"""Tests for the headless runner (run.py / thebe.cli) and run.sh. No Qt.

The installer is a stub that records its arguments and environment; nothing touches Docker.
"""

import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from thebe import cli  # noqa: E402
from thebe.config import load_config  # noqa: E402
from thebe.settings import DEFAULTS, Paths, parse_settings  # noqa: E402


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-cli-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.installer = self.root / "installer.sh"
        self.installer.write_text(textwrap.dedent(f"""\
            printf '%s\\n' "$*" >> {self.state}/calls
            env -0 > {self.state}/env
            exit "$(cat {self.state}/rc 2>/dev/null || echo 0)"
        """))
        self.settings = self.root / "repo" / ".env"
        self.settings.parent.mkdir()
        self.config = self.settings.with_name("config.yaml")
        self.paths = Paths(self.installer, self.settings, self.root / "app" / ".env", REPO / "stack" / "theme",
                           REPO / "stack" / "stats" / "static" / "orbitron-latin.woff2")
        self.environ = {k: v for k, v in os.environ.items() if not k.startswith("JLT_")}
        self.environ["JUPYTER_PASSWORD"] = "leaked-from-the-shell"
        self.environ["JLT_GPU"] = "on"                  # config.yaml's nvidia decides, not this

    def run_cli(self, *argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), environ=environ or self.environ, paths=self.paths, say=cli.Output(out, err))
        return code, out.getvalue(), err.getvalue()

    def calls(self):
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def installer_env(self):
        text = (self.state / "env").read_text()
        return dict(item.split("=", 1) for item in text.split("\0") if "=" in item)

    def test_install_creates_config_writes_settings_and_runs_the_installer(self):
        code, out, err = self.run_cli()                 # install is the default command
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls(), ["install"])
        self.assertIn(f"Created {self.config} from the defaults", out)
        self.assertIn("default password", err)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)
        self.assertEqual(parse_settings(self.settings.read_text()), DEFAULTS)
        env = self.installer_env()
        self.assertEqual(env["JLT_SETTINGS_FILE"], str(self.settings))
        self.assertNotIn("JUPYTER_PASSWORD", env)
        self.assertNotIn("JLT_WORKSPACE_DIR", env)
        self.assertNotIn("NO_COLOR", env)               # a terminal, not a log panel
        self.assertNotIn("JLT_GPU", env)

    def test_nvidia_from_the_config(self):
        self.config.write_text("nvidia: false\n")
        code, out, err = self.run_cli("install")
        self.assertEqual(code, 0, err)
        self.assertEqual(parse_settings(self.settings.read_text())["NVIDIA"], "0")
        self.assertIn("NVIDIA GPU:  off", out)

    def test_the_first_config_comes_from_the_settings_file(self):
        self.settings.write_text("JUPYTER_PASSWORD='From-Env-1234'\nTHEME='bitter'\n# mine\n")
        code, out, err = self.run_cli("update")
        self.assertEqual(code, 0, err)
        self.assertIn(f"from {self.settings}", out)
        config = load_config(self.config)[0]
        self.assertEqual((config.settings["JUPYTER_PASSWORD"], config.settings["THEME"]), ("From-Env-1234", "bitter"))
        self.assertIn("# mine", self.settings.read_text())
        self.assertEqual(self.calls(), ["update"])

    def test_the_config_is_the_source_of_the_settings(self):
        self.config.write_text('jupyter:\n  password: "From-Yaml-123"\n  port: 9000\nstats:\n  enabled: no\n'
                               'workspace: "nb"\n')
        self.settings.write_text("JUPYTER_PASSWORD='From-Env-1234'\n")
        code, out, err = self.run_cli("start")
        self.assertEqual(code, 0, err)
        values = parse_settings(self.settings.read_text())
        self.assertEqual((values["JUPYTER_PASSWORD"], values["JUPYTER_PORT"], values["STATS_ENABLED"]),
                         ("From-Yaml-123", "9000", "0"))
        self.assertEqual(self.installer_env()["JLT_WORKSPACE_DIR"], str(self.config.parent / "nb"))
        self.assertIn("Statistics:  off", out)
        # An exported JLT_WORKSPACE_DIR wins, as it does for the installer.
        self.run_cli("restart", environ=dict(self.environ, JLT_WORKSPACE_DIR="/srv/nb"))
        self.assertEqual(self.installer_env()["JLT_WORKSPACE_DIR"], "/srv/nb")
        self.assertEqual(self.calls(), ["start", "restart"])

    def test_an_invalid_config_changes_nothing(self):
        self.config.write_text('jupyter:\n  password: "short"\n  port: 80\ntheme: "nope"\n')
        self.settings.write_text("JUPYTER_PASSWORD='From-Env-1234'\n")
        before = self.settings.read_text()
        code, out, err = self.run_cli("install")
        self.assertEqual(code, cli.EXIT_CONFIG)
        for text in ("jupyter.port must be", "8 to 128 characters", "THEME in the settings file must be one of"):
            self.assertIn(text, err)
        self.assertEqual(self.settings.read_text(), before)
        self.assertEqual(self.calls(), [])
        self.config.write_text("jupyter: [\n")
        code, out, err = self.run_cli("install")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("not valid YAML", err)
        self.assertEqual(self.calls(), [])

    def test_a_settings_file_the_installer_refuses_is_not_rewritten(self):
        self.settings.write_text("export OTHER=1\n")
        code, out, err = self.run_cli("install")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("line 1: expected KEY=value: fix or delete that line.", err)
        self.assertEqual(self.settings.read_text(), "export OTHER=1\n")
        self.assertEqual(self.calls(), [])

    def test_check_reports_and_changes_nothing(self):
        code, out, err = self.run_cli("check")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("run.sh init", err)
        self.assertFalse(self.config.exists())
        self.config.write_text("theme: market\n")
        os.chmod(self.config, 0o644)
        code, out, err = self.run_cli("check")
        self.assertEqual(code, 0, err)
        self.assertIn("Theme:       market", out)
        self.assertIn("The configuration is valid.", out)
        self.assertIn("changed to 600", out)
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.calls(), [])

    def test_init(self):
        code, out, err = self.run_cli("init")
        self.assertEqual((code, self.config.exists()), (0, True))
        code, out, err = self.run_cli("init")
        self.assertIn("already exists", out)
        self.assertEqual(self.calls(), [])

    def test_other_commands_pass_through_without_writing(self):
        self.config.write_text('workspace: "/srv/nb"\n')
        for argv in (("stop",), ("status",), ("logs", "--no-follow", "stats"), ("uninstall", "--yes")):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.installer_env()["JLT_WORKSPACE_DIR"], "/srv/nb")
        self.assertEqual(self.calls(), ["stop", "status", "logs --no-follow stats", "uninstall --yes"])
        self.assertFalse(self.settings.exists())

    def test_exit_codes_and_argument_errors(self):
        (self.state / "rc").write_text("3")
        self.assertEqual(self.run_cli("status")[0], 3)
        self.assertEqual(self.run_cli("start", "extra")[0], cli.EXIT_CONFIG)
        self.assertEqual(self.run_cli("bogus")[0], cli.EXIT_CONFIG)
        code, out, err = self.run_cli("help")
        self.assertEqual(code, 0)
        self.assertIn("Commands:", out)
        self.assertEqual(self.calls(), ["status"])

    def test_config_option(self):
        other = self.root / "other.yaml"
        other.write_text("theme: spectre\n")
        code, out, err = self.run_cli("--config", str(other), "install")
        self.assertEqual(code, 0, err)
        self.assertEqual(parse_settings(self.settings.read_text())["THEME"], "spectre")
        self.assertFalse(self.config.exists())


class RunShTests(unittest.TestCase):
    PYTHON = "/usr/bin/python3"

    def test_run_sh_builds_the_venv_once_and_passes_the_arguments(self):
        if not os.access(self.PYTHON, os.X_OK) or subprocess.run(
                [self.PYTHON, "-c", "import venv, ensurepip"], capture_output=True).returncode:
            self.skipTest(f"{self.PYTHON} cannot create venvs")
        with tempfile.TemporaryDirectory(prefix="thebe-test-run-sh-") as tmp:
            root = Path(tmp)
            shutil.copy2(REPO / "run.sh", root / "run.sh")
            (root / "lib").mkdir()
            shutil.copy2(REPO / "lib" / "venv.sh", root / "lib" / "venv.sh")
            (root / "requirements-run.txt").write_text("pip\n")      # already installed: no download
            (root / "run.py").write_text("import sys\nprint('ran', sys.argv[1:])\n")
            env = {k: v for k, v in os.environ.items() if k != "PYTHON"}
            env.update(PATH="/usr/bin:/bin", DISPLAY="")
            first = subprocess.run(["bash", str(root / "run.sh"), "status"], env=env,
                                   capture_output=True, text=True, timeout=300)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("Installing dependencies", first.stderr)
            self.assertIn("ran ['status']", first.stdout)
            second = subprocess.run(["bash", str(root / "run.sh")], env=env, capture_output=True, text=True,
                                    timeout=120)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("Installing", second.stderr)
            self.assertIn("ran []", second.stdout)


if __name__ == "__main__":
    unittest.main()
