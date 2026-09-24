"""Tests for the ai: section of config.yaml (thebe.ai) and the .ai.json it becomes. No Qt.

    .venv/bin/python -m unittest discover -s tests -v
"""

import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from thebe import ai, cli  # noqa: E402
from thebe import config as cfg  # noqa: E402
from thebe.settings import Paths  # noqa: E402

CLAUDE_KEY = "sk-ant-api03-Secret-Claude-Key-1234"
OPENAI_KEY = "sk-proj-Secret-OpenAI-Key-5678"

FULL = f"""\
ai:
  default: "openai"
  providers:
    claude:
      api: "anthropic"
      model: "claude-sonnet-5"
      api_key: "{CLAUDE_KEY}"
    openai:
      api: openai
      model: gpt-5.4-mini
      api_key: "{OPENAI_KEY}"
      base_url: "https://proxy.example.com/v1/"
  budget:
    max_tokens: 250000
    period: total
  timeout: 90
  max_output_tokens: 4096
"""


def parse(text):
    return cfg.parse_config(text, Path("/base"))


class ParseTests(unittest.TestCase):
    def test_no_section_means_ai_off(self):
        config, problems = parse("theme: market\n")
        self.assertEqual((config.ai, config.ai_problems, problems), (None, [], []))

    def test_a_full_section(self):
        config, problems = parse(FULL)
        self.assertEqual(problems, [])
        self.assertEqual(config.ai, ai.AiConfig(
            enabled=True, default="openai",
            providers=(ai.Provider("claude", "anthropic", "claude-sonnet-5", CLAUDE_KEY),
                       ai.Provider("openai", "openai", "gpt-5.4-mini", OPENAI_KEY, "https://proxy.example.com/v1")),
            max_tokens=250000, period="total", timeout=90, max_output_tokens=4096))
        self.assertTrue(config.ai.active)

    def test_defaults_of_a_minimal_section(self):
        config, problems = parse(f'ai:\n  providers:\n    claude:\n      api: anthropic\n'
                                 f'      model: claude-sonnet-5\n      api_key: "{CLAUDE_KEY}"\n')
        self.assertEqual(problems, [])
        self.assertEqual((config.ai.default_provider, config.ai.max_tokens, config.ai.period, config.ai.timeout,
                          config.ai.max_output_tokens), ("claude", 0, "month", 60, 2048))

    def test_the_key_never_shows_in_a_repr(self):
        config, _ = parse(FULL)
        self.assertNotIn(CLAUDE_KEY, repr(config))
        self.assertNotIn(OPENAI_KEY, repr(config.ai))

    def test_switched_off(self):
        config, problems = parse("ai:\n  enabled: false\n")
        self.assertEqual(problems, [])
        self.assertFalse(config.ai.active)
        config, problems = parse(FULL.replace("ai:\n", "ai:\n  enabled: false\n", 1))
        self.assertEqual(problems, [])
        self.assertFalse(config.ai.active)

    def test_problems_turn_ai_off_and_are_all_listed(self):
        cases = {
            "ai: [1]\n": "ai must be a section",
            "ai:\n  model: x\n": "unknown setting 'ai.model'",
            "ai:\n  enabled: maybe\n": "ai.enabled must be true or false",
            "ai:\n  providers: {}\n": "ai.providers is empty",
            "ai:\n  providers:\n    Claude:\n      api: anthropic\n": "must start with a lowercase letter",
            "ai:\n  providers:\n    claude: x\n": "must be a section with api, model and api_key",
        }
        for text, want in cases.items():
            config, problems = parse(text)
            self.assertIsNone(config.ai, text)
            self.assertTrue(any(want in p for p in problems), (text, problems))
            self.assertEqual(config.ai_problems, problems)

    def test_provider_problems(self):
        base = FULL
        cases = (
            ('api: "anthropic"', 'api: "azure"', "ai.providers.claude.api must be one of: anthropic, openai"),
            ('model: "claude-sonnet-5"', 'model: "claude sonnet"', "ai.providers.claude.model must be"),
            (f'api_key: "{CLAUDE_KEY}"', 'api_key: "sk-ant-..."', "not the example's '...'"),
            (f'api_key: "{CLAUDE_KEY}"', "api_key: 12345678", "api_key must be set, as text"),
            (f'api_key: "{CLAUDE_KEY}"', 'api_key: "short"', "not a usable key"),
            (f'api_key: "{CLAUDE_KEY}"', 'api_key: "has a space in it"', "not a usable key"),
            ('base_url: "https://proxy.example.com/v1/"', 'base_url: "ftp://x.example.com"', "must be an http(s) address"),
            ('base_url: "https://proxy.example.com/v1/"', 'base_url: "https://user:pw@x.example.com"',
             "no user name or password"),
            ('default: "openai"', 'default: "gemini"', "ai.default names 'gemini'"),
            ("max_tokens: 250000", "max_tokens: -1", "ai.budget.max_tokens must be a whole number"),
            ("max_tokens: 250000", "max_tokens: true", "ai.budget.max_tokens must be a whole number"),
            ("max_tokens: 250000", 'max_tokens: "1000"', "ai.budget.max_tokens must be a whole number"),
            ("period: total", "period: week", "ai.budget.period must be one of: month, total"),
            ("timeout: 90", "timeout: 1", "ai.timeout must be a whole number from 5 to 600"),
            ("max_output_tokens: 4096", "max_output_tokens: 0", "ai.max_output_tokens must be"),
            ("  budget:\n", "  budget:\n    per_user: 5\n", "unknown setting 'ai.budget.per_user'"),
            ("      base_url:", "      organization: x\n      base_url:", "unknown setting 'ai.providers.openai.organization'"),
        )
        for old, new, want in cases:
            self.assertIn(old, base)
            config, problems = parse(base.replace(old, new, 1))
            self.assertIsNone(config.ai, new)
            self.assertTrue(any(want in p for p in problems), (new, problems))
            for problem in problems:
                self.assertNotIn(CLAUDE_KEY, problem)
                self.assertNotIn(OPENAI_KEY, problem)

    def test_at_most_eight_providers(self):
        entries = "".join(f"    p{i}:\n      api: openai\n      model: gpt-5.4-mini\n      api_key: \"{OPENAI_KEY}\"\n"
                          for i in range(9))
        config, problems = parse(f"ai:\n  providers:\n{entries}")
        self.assertIsNone(config.ai)
        self.assertIn("ai.providers lists 9 providers; at most 8 are allowed.", problems)


class RenderTests(unittest.TestCase):
    def test_round_trip_keeps_every_value_and_key(self):
        config, problems = parse(FULL)
        self.assertEqual(problems, [])
        again, problems = parse(cfg.render_config(config))
        self.assertEqual(problems, [])
        self.assertEqual(again.ai, config.ai)

    def test_round_trip_of_a_switched_off_section(self):
        config, _ = parse(FULL.replace("ai:\n", "ai:\n  enabled: false\n", 1))
        again, problems = parse(cfg.render_config(config))
        self.assertEqual((again.ai, problems), (config.ai, []))

    def test_no_section_renders_the_commented_example(self):
        text = cfg.render_config(cfg.Config())
        self.assertIn('# ai:\n#   default: "claude"', text)
        self.assertEqual(parse(text)[0].ai, None)

    def test_the_example_parses_once_real_keys_are_filled_in(self):
        text = (REPO / "config.example.yaml").read_text()
        section = text[text.index("# ai:"):]
        lines = [line[2:].split("   #")[0].rstrip() for line in section.splitlines()]
        uncommented = "\n".join(lines).replace("sk-ant-...", CLAUDE_KEY).replace('"sk-..."', f'"{OPENAI_KEY}"')
        config, problems = parse(uncommented)
        self.assertEqual(problems, [])
        self.assertEqual((config.ai.default_provider, config.ai.max_tokens, config.ai.period),
                         ("claude", 1000000, "month"))

    def test_the_file_is_never_rewritten_without_a_broken_section(self):
        config, problems = parse('ai:\n  timeout: "soon"\n')
        self.assertTrue(problems)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            with self.assertRaises(ValueError):
                cfg.save_config(path, config)
            self.assertFalse(path.exists())


class AiFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-ai-")
        self.addCleanup(tmp.cleanup)
        self.path = ai.ai_file(Path(tmp.name) / ".env")

    def test_next_to_the_settings_file(self):
        self.assertEqual(ai.ai_file(Path("/repo/.env")), Path("/repo/.ai.json"))

    def test_written_private_with_the_keys_for_the_gateway(self):
        config, _ = parse(FULL)
        self.assertTrue(ai.save_ai_file(self.path, config.ai))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text()), {
            "default": "openai",
            "providers": {
                "claude": {"api": "anthropic", "model": "claude-sonnet-5", "api_key": CLAUDE_KEY, "base_url": None},
                "openai": {"api": "openai", "model": "gpt-5.4-mini", "api_key": OPENAI_KEY,
                           "base_url": "https://proxy.example.com/v1"},
            },
            "max_tokens": 250000, "period": "total", "timeout": 90, "max_output_tokens": 4096,
        })

    def test_removed_when_ai_is_off(self):
        config, _ = parse(FULL)
        ai.save_ai_file(self.path, config.ai)
        self.assertFalse(ai.save_ai_file(self.path, None))
        self.assertFalse(self.path.exists())
        self.assertFalse(ai.save_ai_file(self.path, ai.AiConfig(enabled=False)))      # nothing to remove

    def test_describe_masks_the_keys(self):
        config, _ = parse(FULL)
        text = ai.describe(config.ai)
        self.assertEqual(text, "on, default openai (claude = anthropic claude-sonnet-5, key …1234, "
                               "openai = openai gpt-5.4-mini, key …5678); 250,000 tokens in total (never reset); "
                               "timeout 90 s")
        self.assertEqual(ai.describe(None), "off (no ai: section)")
        self.assertEqual(ai.describe(ai.AiConfig(enabled=False)), "off (ai.enabled: false)")


class CliTests(unittest.TestCase):
    """run.sh writes .ai.json next to the settings before the installer runs, and check shows it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-ai-cli-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.installer = self.root / "installer.sh"
        self.installer.write_text(textwrap.dedent(f"""\
            printf '%s\\n' "$*" >> {self.root}/calls
            env -0 > {self.root}/env
            if [ -f {self.root}/repo/.ai.json ]; then cp {self.root}/repo/.ai.json {self.root}/seen.json; fi
        """))
        self.settings = self.root / "repo" / ".env"
        self.settings.parent.mkdir()
        self.config = self.settings.with_name("config.yaml")
        self.paths = Paths(self.installer, self.settings, self.root / "app" / ".env", REPO / "stack" / "theme",
                           REPO / "stack" / "stats" / "static" / "orbitron-latin.woff2")
        self.environ = {k: v for k, v in os.environ.items() if not k.startswith("JLT_")}
        self.environ["JLT_REQUIREMENTS_FILE"] = str(self.root / "requirements.txt")

    def run_cli(self, *argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), environ=environ or self.environ, paths=self.paths, say=cli.Output(out, err))
        return code, out.getvalue(), err.getvalue()

    def test_install_writes_the_ai_file_before_the_installer_runs(self):
        self.config.write_text(FULL)
        code, out, err = self.run_cli("install")
        self.assertEqual(code, 0, err)
        seen = json.loads((self.root / "seen.json").read_text())
        self.assertEqual(seen["providers"]["claude"]["api_key"], CLAUDE_KEY)
        self.assertIn("AI settings written to", out)
        self.assertIn("AI:          on, default openai", out)
        self.assertNotIn(CLAUDE_KEY, out + err)
        env_text = (self.root / "env").read_text()
        self.assertNotIn(CLAUDE_KEY, env_text)

    def test_the_ai_file_goes_when_the_section_does(self):
        self.config.write_text(FULL)
        self.assertEqual(self.run_cli("install")[0], 0)
        self.config.write_text("theme: amazing\n")
        (self.root / "seen.json").unlink()
        code, out, err = self.run_cli("update")
        self.assertEqual(code, 0, err)
        self.assertFalse(ai.ai_file(self.settings).exists())
        self.assertFalse((self.root / "seen.json").exists())
        self.assertIn("AI:          off (no ai: section)", out)

    def test_a_broken_section_stops_install_and_names_the_problem(self):
        self.config.write_text(FULL.replace("period: total", "period: weekly"))
        code, out, err = self.run_cli("install")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("ai.budget.period must be one of: month, total", err)
        self.assertFalse((self.root / "calls").exists())
        self.assertFalse(ai.ai_file(self.settings).exists())

    def test_check_shows_the_section_without_keys(self):
        self.config.write_text(FULL)
        code, out, err = self.run_cli("check")
        self.assertEqual(code, 0, err)
        self.assertIn("key …1234", out)
        self.assertNotIn(CLAUDE_KEY, out + err)
        self.assertFalse(ai.ai_file(self.settings).exists())      # check changes nothing


if __name__ == "__main__":
    unittest.main()
