"""Tests for thebe.config (config.yaml). No Qt: they run with PyYAML alone.

    .venv/bin/python -m unittest discover -s tests -v
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from thebe import config as cfg  # noqa: E402
from thebe.settings import DEFAULTS  # noqa: E402


def parse(text, base=Path("/base")):
    return cfg.parse_config(text, base)


class ParseTests(unittest.TestCase):
    def test_an_empty_file_means_the_defaults(self):
        for text in ("", "# only a comment\n", "---\n"):
            config, problems = parse(text)
            self.assertEqual((config.settings, config.workspace, problems), (DEFAULTS, "", []))

    def test_every_setting_maps_to_the_installer_key(self):
        config, problems = parse(
            'jupyter:\n  password: "Pass-word-123"\n  port: 9000\n'
            'stats:\n  enabled: false\n  port: "9001"\n  user: "stats.user"\n'
            'theme: "market"\nhttps: "off"\nworkspace: "~/nb"\n')
        self.assertEqual(problems, [])
        self.assertEqual(config.settings, {
            "JUPYTER_PASSWORD": "Pass-word-123", "JUPYTER_PORT": "9000", "STATS_ENABLED": "0",
            "STATS_PORT": "9001", "STATS_USER": "stats.user", "THEME": "market", "HTTPS": "off"})
        self.assertEqual(config.workspace, "~/nb")

    def test_yaml_spellings_of_switches(self):
        for text, want in (("enabled: yes", "1"), ("enabled: off", "0"), ("enabled: 1", "1"),
                           ("enabled: 'no'", "0"), ("enabled: true", "1")):
            config, problems = parse(f"stats:\n  {text}\n")
            self.assertEqual((config.settings["STATS_ENABLED"], problems), (want, []), text)
        config, problems = parse("https: off\n")         # YAML 1.1 reads a bare off as false
        self.assertEqual((config.settings["HTTPS"], problems), ("off", []))

    def test_wrong_values_keep_the_default_and_are_reported(self):
        config, problems = parse(
            "jupyter:\n  password: 12345678\n  port: 80\nstats:\n  enabled: maybe\n  port: true\n"
            "  user: [a]\nhttps: on\n")
        self.assertEqual(config.settings, DEFAULTS)
        self.assertEqual(problems, [
            "jupyter.password must be text; put it in quotes.",
            "jupyter.port must be a port number from 1024 to 65535.",
            "stats.enabled must be true or false.",
            "stats.port must be a port number from 1024 to 65535.",
            "stats.user must be text; put it in quotes.",
            "https must be one of: auto, off.",
        ])

    def test_unknown_keys_and_misplaced_sections_are_reported(self):
        config, problems = parse("jupyter:\n  pasword: x\nstats: 5\nextra: 1\n")
        self.assertEqual(problems, ["unknown setting 'jupyter.pasword'.",
                                    "stats must be a section (indented key: value lines).",
                                    "unknown setting 'extra'."])
        self.assertEqual(config.settings, DEFAULTS)
        self.assertEqual(parse("stats:\n")[1], [])       # an empty section is fine

    def test_unusable_files_raise(self):
        for text, message in (("- a\n- b\n", "not a mapping of settings"),
                              ("jupyter: [unclosed\n", "not valid YAML (line 2)")):
            with self.assertRaises(cfg.ConfigError) as caught:
                parse(text)
            self.assertIn(message, str(caught.exception))

    def test_workspace_paths(self):
        home = str(Path.home())
        cases = (("~/nb", Path(home) / "nb"), ("nb", Path("/base/nb")), ("/srv/nb", Path("/srv/nb")))
        for text, want in cases:
            config, problems = parse(f"workspace: {text}\n")
            self.assertEqual(problems, [])
            self.assertEqual(cfg.workspace_dir(config, Path("/base/config.yaml")), want)
        self.assertIsNone(cfg.workspace_dir(parse("workspace: ''\n")[0], Path("/base/config.yaml")))
        for text in ('"it\'s"', '"a\\\\b"', "5", '"  "'):
            config, problems = parse(f"workspace: {text}\n")
            self.assertEqual(config.workspace, "")
            self.assertTrue(problems and problems[0].startswith("workspace must"), problems)

    def test_the_environment_wins_over_the_configured_workspace(self):
        config = parse("workspace: /srv/nb\n")[0]
        where = Path("/base/config.yaml")
        self.assertEqual(cfg.effective_workspace(config, where, {}), Path("/srv/nb"))
        self.assertEqual(cfg.effective_workspace(config, where, {"JLT_WORKSPACE_DIR": "/other"}), Path("/other"))


class RenderTests(unittest.TestCase):
    def test_render_reads_back_exactly(self):
        odd = ("Pass: word # 1", " lead-and-trail ", "émoji 😀 \"q\" \\ back", "off", "12345678", "a b")
        for password in odd:
            config = cfg.Config()
            config.settings["JUPYTER_PASSWORD"] = password
            config.workspace = "~/work space"
            back, problems = parse(cfg.render_config(config))
            self.assertEqual((back, problems), (config, []), password)
        config = cfg.Config()
        config.settings.update(STATS_ENABLED="0", HTTPS="off", JUPYTER_PORT="9999")
        self.assertEqual(parse(cfg.render_config(config))[0], config)

    def test_the_example_file_is_the_rendered_default(self):
        self.assertEqual((REPO / "config.example.yaml").read_text(), cfg.render_config(cfg.Config()))


class FileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-config-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "config.yaml"

    def test_save_is_private_and_load_reads_it_back(self):
        config = cfg.Config()
        config.settings["THEME"] = "spectre"
        cfg.save_config(self.path, config)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(cfg.load_config(self.path), (config, []))

    def test_load_errors(self):
        with self.assertRaises(FileNotFoundError):
            cfg.load_config(self.path)
        self.path.write_bytes("theme: 'caf\xe9'\n".encode("latin-1"))
        with self.assertRaisesRegex(cfg.ConfigError, "not UTF-8"):
            cfg.load_config(self.path)
        self.path.write_text("# x\n" * (cfg.MAX_CONFIG_BYTES // 4 + 1))
        with self.assertRaisesRegex(cfg.ConfigError, "larger than"):
            cfg.load_config(self.path)
        self.path.write_text("[1, 2]\n")
        with self.assertRaisesRegex(cfg.ConfigError, "config.yaml is not a mapping"):
            cfg.load_config(self.path)

    def test_make_private(self):
        self.path.write_text("")
        os.chmod(self.path, 0o644)
        self.assertIn("was mode 644", cfg.make_private(self.path))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(cfg.make_private(self.path), "")

    def test_config_from_settings(self):
        settings = self.dir / ".env"
        self.assertEqual(cfg.config_from_settings(settings), (cfg.Config(), []))
        settings.write_text("JUPYTER_PASSWORD='From-Env-1234'\nSTATS_PORT='80'\nOTHER='x'\n")
        config, notes = cfg.config_from_settings(settings)
        self.assertEqual(config.settings["JUPYTER_PASSWORD"], "From-Env-1234")
        self.assertEqual(config.settings["STATS_PORT"], DEFAULTS["STATS_PORT"])
        self.assertEqual(notes, [f"STATS_PORT in .env is not a valid port; {DEFAULTS['STATS_PORT']} is used instead."])


if __name__ == "__main__":
    unittest.main()
