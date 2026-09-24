"""The installer's AI steps (setup-jupyterlab-tailscale.sh): the gateway's secrets and profile.

The script is sourced and ensure_ai_secrets / runtime_env_content are called directly. No Docker.
"""

import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "setup-jupyterlab-tailscale.sh"
AI_JSON = '{\n "default": "claude",\n "providers": {"claude": {"api_key": "sk-ant-api03-Installer-Key-1111"}}\n}\n'


class InstallerAiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-installer-ai-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.app = self.root / "app"
        self.app.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.ai_file = self.repo / ".ai.json"

    def run_step(self, step):
        env = {k: v for k, v in os.environ.items() if not k.startswith("JLT_")}
        env.update(HOME=str(self.root), JLT_APP_DIR=str(self.app), JLT_SETTINGS_FILE=str(self.repo / ".env"))
        script = f"source {shlex.quote(str(INSTALLER))}; {step}"
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)

    def secrets(self):
        step = ('ensure_ai_secrets; printf "%s|%s|%s\\n" "$AI_ENABLED" "$AI_TOKEN_CHANGED" "$AI_CONFIG_HASH"')
        result = self.run_step(step)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().split("|")

    def test_without_the_ai_file_the_secret_holds_no_keys(self):
        enabled, changed, digest = self.secrets()
        self.assertEqual((enabled, changed), ("0", "1"))
        config = self.app / "secrets" / "ai_config"
        self.assertEqual(config.read_text(), "{}\n")
        self.assertEqual(len(digest), 64)
        token = (self.app / "secrets" / "ai_token").read_text()
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        for path in (config, self.app / "secrets" / "ai_token"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_the_ai_file_becomes_the_gateways_secret_and_the_token_is_kept(self):
        first = self.secrets()
        token = (self.app / "secrets" / "ai_token").read_text()
        self.ai_file.write_text(AI_JSON)
        enabled, changed, digest = self.secrets()
        self.assertEqual((enabled, changed), ("1", "0"))                    # same token, nothing recreated for it
        self.assertEqual((self.app / "secrets" / "ai_config").read_text(), AI_JSON)
        self.assertEqual((self.app / "secrets" / "ai_token").read_text(), token)
        self.assertNotEqual(digest, first[2])                               # the ai container is recreated
        self.assertEqual(self.secrets()[2], digest)                         # unchanged settings: same hash
        self.ai_file.unlink()
        self.assertEqual(self.secrets()[:1], ["0"])
        self.assertEqual((self.app / "secrets" / "ai_config").read_text(), "{}\n")   # the keys are gone

    def test_an_unreadable_ai_file_stops_the_deploy(self):
        self.ai_file.mkdir()
        result = self.run_step("ensure_ai_secrets; echo went-on")
        self.assertEqual(result.returncode, 1)
        self.assertIn(".ai.json is not a readable file", result.stderr)
        self.assertNotIn("went-on", result.stdout)

    def runtime_env(self, stats, ai):
        step = (f"STATS_ENABLED={stats}; AI_ENABLED={ai}; AI_CONFIG_HASH={'a' * 64}; USE_TLS=''; TS_IP=100.64.0.1; "
                "JUPYTER_PORT=8888; STATS_PORT=8889; STATS_USER=jupyter; THEME=amazing; WORKSPACE_DIR=/w; "
                "HTTPS_MODE=auto; GPU_MODE=auto; PUBLIC_SCHEME=http; runtime_env_content 0")
        result = self.run_step(step)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line and not line.startswith("#"))

    def test_the_ai_profile_and_the_settings_hash_in_the_runtime_env(self):
        self.assertEqual(self.runtime_env(1, 1)["COMPOSE_PROFILES"], "'stats,ai'")
        self.assertEqual(self.runtime_env(0, 1)["COMPOSE_PROFILES"], "'ai'")
        self.assertEqual(self.runtime_env(1, 0)["COMPOSE_PROFILES"], "'stats'")
        self.assertEqual(self.runtime_env(0, 0)["COMPOSE_PROFILES"], "''")
        self.assertEqual(self.runtime_env(1, 1)["AI_CONFIG_HASH"], f"'{'a' * 64}'")
        self.assertEqual((self.runtime_env(1, 1)["AI_ENABLED"], self.runtime_env(1, 0)["AI_ENABLED"]), ("'1'", "'0'"))

    def test_a_local_node_modules_is_never_copied_into_the_app_dir(self):
        src = self.root / "src"
        (src / "labextension" / "node_modules" / "pkg").mkdir(parents=True)
        (src / "labextension" / "node_modules" / "pkg" / "index.js").write_text("x")
        (src / "labextension" / "package.json").write_text("{}")
        dst = self.root / "dst"
        dst.mkdir()
        result = self.run_step(f"copy_tree {shlex.quote(str(src))} {shlex.quote(str(dst))}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((dst / "labextension" / "package.json").is_file())
        self.assertFalse((dst / "labextension" / "node_modules").exists())

    def test_deploy_order_and_the_other_commands_know_the_ai_profile(self):
        text = INSTALLER.read_text()
        deploy = text[text.index("\ndeploy() {"):text.index("\nload_runtime_env() {")]
        order = [deploy.index(step) for step in ("ensure_runner_token", "ensure_ai_secrets", "write_runtime_env",
                                                 "compose rm -s -f ai", 'compose "${up_args[@]}"')]
        self.assertEqual(order, sorted(order))
        self.assertIn('"$AI_TOKEN_CHANGED" == 1', deploy)
        for command in ("cmd_stop", "show_containers", "remove_project_resources"):
            body = text[text.index(f"\n{command}() {{"):]
            body = body[:body.index("\n}\n")]
            self.assertIn("--profile ai", body, command)


if __name__ == "__main__":
    unittest.main()
