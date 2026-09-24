"""The installer's requirements.txt steps (setup-jupyterlab-tailscale.sh), with a stub docker.

The script is sourced and check_requirements / apply_requirements are called directly: which
docker command runs, with which stdin, and what a failure does to the deploy.
"""

import os
import shlex
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "setup-jupyterlab-tailscale.sh"


class InstallerPackagesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-installer-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "app").mkdir()
        self.requirements = self.root / "requirements.txt"
        docker = self.bin / "docker"
        docker.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> {self.root}/calls
            cat > {self.root}/stdin.$(wc -l < {self.root}/calls)
            exit "$(cat {self.root}/rc 2>/dev/null || echo 0)"
        """))
        docker.chmod(0o755)

    def run_step(self, step, stats="1"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("JLT_")}
        env.update(HOME=str(self.root), JLT_APP_DIR=str(self.root / "app"),
                   JLT_SETTINGS_FILE=str(self.root / ".env"), JLT_REQUIREMENTS_FILE=str(self.requirements))
        # Sourced as root the script resets PATH, so the stub goes in front afterwards.
        script = (f"source {shlex.quote(str(INSTALLER))}; export PATH={shlex.quote(str(self.bin))}:\"$PATH\"; "
                  f"STATS_ENABLED={stats}; {step}")
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        path = self.root / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def stdin(self, number):
        return (self.root / f"stdin.{number}").read_text()

    def test_check_runs_the_runners_rules_in_the_image(self):
        result = self.run_step("check_requirements")
        self.assertEqual((result.returncode, self.calls()), (0, []))          # no file: nothing to check
        self.requirements.write_text("requests\n")
        result = self.run_step("check_requirements")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["run --rm -i --pull never --network none --entrypoint python "
                                        "jupyterlab-tailscale/jupyterlab:local /srv/jupyter/deps_runner.py check"])
        self.assertEqual(self.stdin(1), "requests\n")
        (self.root / "rc").write_text("2")
        result = self.run_step("check_requirements")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not accepted (see above); nothing was changed", result.stderr)

    def test_apply_with_statistics_on_uses_the_running_runner(self):
        self.requirements.write_text("opencv-python\npytesseract\n")
        result = self.run_step("apply_requirements")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["compose --progress plain exec -T deps python /srv/jupyter/deps_runner.py baseline"])
        self.assertEqual(self.stdin(1), "opencv-python\npytesseract\n")
        self.requirements.unlink()                     # removed: an empty list is handed over
        self.run_step("apply_requirements")
        self.assertEqual(self.stdin(2), "")

    def test_apply_with_statistics_off_uses_a_one_off_runner(self):
        result = self.run_step("apply_requirements", stats="0")
        self.assertEqual((result.returncode, self.calls()), (0, []))          # no file, no runner: nothing
        self.requirements.write_text("requests\n")
        result = self.run_step("apply_requirements", stats="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["compose --progress plain --profile stats run --rm --no-deps -T deps "
                                        "python /srv/jupyter/deps_runner.py baseline --local"])
        self.assertEqual(self.stdin(1), "requests\n")

    def test_a_failed_install_fails_the_deploy_before_it_reports_ready(self):
        self.requirements.write_text("torch==99\n")
        (self.root / "rc").write_text("1")
        result = self.run_step("apply_requirements; echo reported-ready")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("reported-ready", result.stdout)
        self.assertIn("were not installed (see above). JupyterLab runs, with the custom packages installed before",
                      result.stderr)

    def test_deploy_checks_after_the_build_and_installs_before_the_summary(self):
        text = INSTALLER.read_text()
        deploy = text[text.index("\ndeploy() {"):text.index("\nload_runtime_env() {")]
        order = [deploy.index(step) for step in ("compose \"${build_args[@]}\"", "check_requirements",
                                                 "compose \"${up_args[@]}\"", "apply_requirements", "print_summary")]
        self.assertEqual(order, sorted(order))
        start = text[text.index("\ncmd_start() {"):text.index("\ncmd_stop() {")]
        self.assertNotIn("apply_requirements", start)   # start does not install


if __name__ == "__main__":
    unittest.main()
