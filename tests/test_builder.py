"""Headless tests for builder.py (stdlib unittest, Qt offscreen).

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v

Nothing here runs the real installer, pkexec or a docker command that changes
state: docker, tailscale and pkexec are stub scripts on a private PATH, and
the installer is a stub that only records how it was called.
"""

import dataclasses
import errno
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"      # before any PyQt6 import

# Never touch the developer's real home, settings or app dir.
_SANDBOX = tempfile.TemporaryDirectory(prefix="thebe-test-")
for _name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
    _path = Path(_SANDBOX.name) / _name.lower()
    _path.mkdir(mode=0o700)
    os.environ[_name] = str(_path)
for _name in [n for n in os.environ if n.startswith("JLT_") or n == "JUPYTER_PASSWORD"]:
    del os.environ[_name]

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from PyQt6.QtCore import QEventLoop, QPoint, QPointF, Qt, qInstallMessageHandler  # noqa: E402
from PyQt6.QtGui import QWheelEvent  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox, QWidget  # noqa: E402

import builder  # noqa: E402

# One application for the whole run; it has to outlive every window.
APP = QApplication.instance() or QApplication([])
APP.setStyle("Fusion")


def wait_until(predicate, timeout_ms=15000):
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
    return predicate()


def run_to_completion(runner, argv, wait_ms=15000, **kwargs):
    """Start argv on the runner (kwargs go to Runner.start) and wait for its result."""
    results, lines = [], []
    runner.done.connect(results.append)
    runner.output.connect(lines.extend)
    runner.start(argv, **kwargs)
    if not wait_until(lambda: results, wait_ms):
        raise AssertionError(f"{argv!r} did not finish")
    return results[0], lines


# ---------------------------------------------------------------------------
# Settings file
# ---------------------------------------------------------------------------

class SettingsFileTests(unittest.TestCase):
    def test_parser_accepts_quoting_comments_blank_lines_and_crlf(self):
        text = ("# settings\r\n\r\nJUPYTER_PASSWORD='pa ss#w\"ord'\r\nJUPYTER_PORT=9000\r\n"
                "STATS_PORT=\"9001\"\r\n  STATS_USER = 'jupyter'  \r\nnot an assignment\r\n")
        self.assertEqual(builder.parse_settings(text), {
            "JUPYTER_PASSWORD": 'pa ss#w"ord', "JUPYTER_PORT": "9000",
            "STATS_PORT": "9001", "STATS_USER": "jupyter"})

    def test_parser_matches_the_installer_on_broken_lines(self):
        text = ("export EXTRA=1\nJUPYTER_PASSWORD='abcdefghij' # note\nSTATS_PORT=\"9001\n"
                "JUPYTER_PORT=9000\nSTATS_USER='a'b'\n")
        # Quotes count only around the whole value, exactly like read_env_file.
        self.assertEqual(builder.parse_settings(text), {"JUPYTER_PORT": "9000", "STATS_USER": "a'b"})
        problems = builder.settings_line_problems(text)
        self.assertEqual(problems, ["line 1: expected KEY=value",
                                    "line 2: the value of JUPYTER_PASSWORD has no closing quote",
                                    "line 3: the value of STATS_PORT has no closing quote"])
        self.assertFalse(any("abcdefghij" in p for p in problems), "problems must not echo values")
        # A known key with broken quoting is rewritten in place; other lines stay.
        rendered = builder.render_settings(text, dict(builder.DEFAULTS))
        self.assertEqual(rendered.split("\n")[:5], [
            "export EXTRA=1", f"JUPYTER_PASSWORD='{builder.DEFAULTS['JUPYTER_PASSWORD']}'",
            "STATS_PORT='8889'", "JUPYTER_PORT='8888'", "STATS_USER='jupyter'"])
        self.assertEqual(builder.settings_line_problems(rendered), ["line 1: expected KEY=value"])

    def test_render_rewrites_in_place_keeps_unknown_lines_and_appends_missing(self):
        existing = ("# my comment\nCUSTOM=\"keep me\"\nJUPYTER_PORT=8888\n\n"
                    "JUPYTER_PASSWORD='old-password'\n# trailing comment\n")
        values = dict(builder.DEFAULTS, JUPYTER_PORT="9100", JUPYTER_PASSWORD="new-password-1")
        rendered = builder.render_settings(existing, values)
        lines = rendered.split("\n")
        self.assertEqual(lines[0], "# my comment")
        self.assertEqual(lines[1], 'CUSTOM="keep me"')
        self.assertEqual(lines[2], "JUPYTER_PORT='9100'")
        self.assertEqual(lines[4], "JUPYTER_PASSWORD='new-password-1'")
        self.assertEqual(lines[5], "# trailing comment")
        self.assertIn("THEME='amazing'", lines)
        self.assertTrue(rendered.endswith("\n"))
        self.assertEqual(builder.parse_settings(rendered), dict(values, CUSTOM="keep me"))
        # Every known key is single-quoted.
        for key in builder.DEFAULTS:
            self.assertIn(f"{key}='{values[key]}'", lines)

    def test_render_round_trip_is_stable(self):
        values = dict(builder.DEFAULTS)
        once = builder.render_settings("", values)
        self.assertEqual(builder.render_settings(once, values), once)

    def test_render_refuses_a_quote_without_echoing_the_value(self):
        with self.assertRaises(ValueError) as caught:
            builder.render_settings("", {"JUPYTER_PASSWORD": "it's-secret"})
        self.assertNotIn("it's-secret", str(caught.exception))

    def test_read_text_refuses_bytes_that_are_not_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            self.assertEqual(builder.read_text(path), "")
            path.write_bytes("JUPYTER_PASSWORD='pässwort-123'\n".encode("latin-1"))
            with self.assertRaises(UnicodeDecodeError):
                builder.read_text(path)

    def test_private_file_is_0600_atomic_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("OLD=1\n")
            path.chmod(0o644)
            modes = []
            real_replace = os.replace

            def spy_replace(src, dst):
                modes.append(stat.S_IMODE(os.stat(src).st_mode))
                real_replace(src, dst)

            with mock.patch.object(builder.os, "replace", spy_replace):
                builder.write_private_file(path, "NEW=1\n")
            self.assertEqual(modes, [0o600], "the temp file must be 0600 from creation")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text(), "NEW=1\n")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [".env"])

    def test_failed_replace_keeps_the_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("OLD=1\n")
            with mock.patch.object(builder.os, "replace", side_effect=OSError(errno.EIO, "boom")):
                with self.assertRaises(OSError):
                    builder.write_private_file(path, "NEW=1\n")
            self.assertEqual(path.read_text(), "OLD=1\n")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [".env"])

    def test_relative_override_paths_are_made_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                cwd = Path(os.getcwd())
                paths = builder.Paths.from_environment({"HOME": tmp, "JLT_SETTINGS_FILE": "my.env",
                                                        "JLT_APP_DIR": "app"})
            finally:
                os.chdir(old)
        self.assertEqual(paths.settings, cwd / "my.env")
        self.assertEqual(paths.runtime_env, cwd / "app" / ".env")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    THEMES = ["amazing", "bitter"]

    def errors(self, **changes):
        return builder.validate_settings(dict(builder.DEFAULTS, **changes), self.THEMES)

    def test_defaults_are_valid(self):
        self.assertEqual(self.errors(), [])

    def test_password_rules(self):
        for ok in ("a" * 8, "a" * 128, 'with "double" and spaces inside', "ünïcödé-pass"):
            self.assertEqual(self.errors(JUPYTER_PASSWORD=ok), [], ok)
        for bad, fragment in (("a" * 7, "8 to 128"), ("a" * 129, "8 to 128"), ("", "8 to 128"),
                              ("single'quote", "single quote"), ("back\\slash", "backslash"),
                              ("line\nbreak1", "control"), ("tab\tchar12", "control"),
                              ("del\x7fchar12", "control"), (" leading-space", "whitespace"),
                              ("trailing-space ", "whitespace")):
            errors = self.errors(JUPYTER_PASSWORD=bad)
            self.assertTrue(any(fragment in e for e in errors), f"{bad!r}: {errors}")
            self.assertFalse(any(bad in e for e in errors if len(bad) > 3), "errors must not echo the password")

    def test_password_character_classes_follow_glibc_like_the_installer(self):
        # bash's [[:cntrl:]] / [[:space:]] in a UTF-8 locale: no-break spaces are
        # not whitespace, the line/paragraph separators are control characters.
        for ok in ("password1\u00a0", "\u2007password1", "password1\u202f", "密" * 60):
            self.assertEqual(self.errors(JUPYTER_PASSWORD=ok), [], ascii(ok))
        for bad, fragment in (("pass\u2028word1", "control"), ("pass\u2029word1", "control"),
                              ("password1\u0085", "control"), ("password1\u3000", "whitespace"),
                              ("\u2003password1", "whitespace"), ("password1\u1680", "whitespace")):
            errors = self.errors(JUPYTER_PASSWORD=bad)
            self.assertTrue(any(fragment in e for e in errors), f"{ascii(bad)}: {errors}")

    def test_port_rules(self):
        for ok in ("1024", "8888", "65535"):
            self.assertEqual(self.errors(JUPYTER_PORT=ok), [], ok)
        for bad, fragment in (("80", "privileged"), ("1023", "privileged"), ("0", "whole number"),
                              ("65536", "out of range"), ("99999", "out of range"),
                              ("abc", "whole number"), ("", "whole number"), ("08888", "whole number"),
                              ("-1", "whole number"), ("8888.0", "whole number"), ("１２３４５", "whole number")):
            errors = self.errors(STATS_PORT=bad)
            self.assertTrue(any(fragment in e for e in errors), f"{bad!r}: {errors}")

    def test_equal_ports_rejected_even_with_statistics_disabled(self):
        for enabled in ("1", "0"):
            errors = self.errors(JUPYTER_PORT="9000", STATS_PORT="9000", STATS_ENABLED=enabled)
            self.assertTrue(any("different ports" in e for e in errors), errors)

    def test_statistics_switch_accepts_the_installer_spellings(self):
        for raw, expected in (("1", "1"), ("true", "1"), ("YES", "1"), ("On", "1"),
                              ("0", "0"), ("false", "0"), ("No", "0"), ("OFF", "0")):
            self.assertEqual(builder.normalize_bool(raw), expected, raw)
            self.assertEqual(self.errors(STATS_ENABLED=raw), [], raw)
        for raw in ("maybe", "", "2", "enabled"):
            self.assertIsNone(builder.normalize_bool(raw), raw)
            self.assertTrue(any("STATS_ENABLED" in e for e in self.errors(STATS_ENABLED=raw)), raw)

    def test_other_keys(self):
        self.assertTrue(self.errors(STATS_USER="has space"))
        self.assertTrue(self.errors(STATS_USER="x" * 33))
        self.assertEqual(self.errors(STATS_USER="Jupyter.user_1-2"), [])
        self.assertTrue(any("THEME" in e for e in self.errors(THEME="nope")))


class PortOccupancyTests(unittest.TestCase):
    def test_a_listening_socket_is_reported_unless_its_service_holds_it(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            errors = builder.occupied_port_errors("127.0.0.1", [("jupyterlab", port)])
            self.assertEqual(errors, [f"Port {port} (JupyterLab) is already in use on 127.0.0.1."])
            self.assertEqual(builder.occupied_port_errors("127.0.0.1", [("jupyterlab", port)],
                                                          {"jupyterlab": {port}}), [])

    def test_a_port_held_by_the_other_service_is_not_whitelisted(self):
        def taken(ip, port):
            raise OSError(errno.EADDRINUSE, "Address already in use")

        # Swapping the ports: Compose would recreate jupyterlab while stats still holds 8889.
        errors = builder.occupied_port_errors("100.82.217.101", [("jupyterlab", 8889), ("stats", 8888)],
                                              {"jupyterlab": {8888}, "stats": {8889}}, bind=taken)
        self.assertEqual(len(errors), 2)
        self.assertIn("Port 8889 (JupyterLab) is still published by the Statistics container", errors[0])
        self.assertIn("Press Stop first", errors[0])
        self.assertIn("Port 8888 (Statistics) is still published by the JupyterLab container", errors[1])

    def test_a_free_port_passes(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertEqual(builder.occupied_port_errors("127.0.0.1", [("jupyterlab", port)]), [])

    def test_unassigned_address_and_other_errors(self):
        def not_here(ip, port):
            raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")

        errors = builder.occupied_port_errors("100.100.1.1", [("jupyterlab", 9000), ("stats", 9001)], bind=not_here)
        self.assertEqual(errors, ["The Tailscale address 100.100.1.1 is not assigned to this machine."])

        def denied(ip, port):
            raise OSError(errno.EACCES, "Permission denied")

        self.assertIn("cannot be checked",
                      builder.occupied_port_errors("100.100.1.1", [("jupyterlab", 9000)], bind=denied)[0])


# ---------------------------------------------------------------------------
# Parsing command output
# ---------------------------------------------------------------------------

def tailscale_json(state="Running", ips=("100.82.217.101", "fd7a:115c:a1e0::b801:d9b5")):
    return json.dumps({"BackendState": state, "Self": {"TailscaleIPs": list(ips)}, "TailscaleIPs": list(ips)})


class ParsingTests(unittest.TestCase):
    def test_tailscale_status(self):
        parse = builder.parse_tailscale_status
        self.assertEqual(parse(tailscale_json()).ip, "100.82.217.101")
        self.assertEqual(parse(tailscale_json(ips=("fd7a::1", "100.64.0.7"))).ip, "100.64.0.7")
        self.assertEqual(parse("warning: client/daemon version mismatch\n" + tailscale_json()).ip, "100.82.217.101")
        needs_login = parse(tailscale_json("NeedsLogin"))
        self.assertEqual(needs_login.ip, "")
        self.assertIn("logged out", needs_login.problem)
        self.assertIn("stopped", parse(tailscale_json("Stopped")).problem)
        no_ipv4 = parse(tailscale_json(ips=("fd7a::1", "192.168.1.5", "100.128.0.1")))
        self.assertEqual(no_ipv4.ip, "")
        self.assertIn("no IPv4", no_ipv4.problem)
        self.assertIn("could not be read", parse("not json at all").problem)
        self.assertIn("could not be read", parse("[]").problem)

    PS_LINES = "\n".join([
        "WARN[0000] some compose warning",
        json.dumps({"Project": "jupyterlab-tailscale", "Service": "jupyterlab", "State": "running",
                    "Health": "healthy", "Publishers": [{"URL": "100.82.217.101", "TargetPort": 8888,
                                                         "PublishedPort": 8888, "Protocol": "tcp"}]}),
        json.dumps({"Project": "jupyterlab-tailscale", "Service": "stats", "State": "exited",
                    "Health": "", "Publishers": []}),
        json.dumps({"Project": "someone-else", "Service": "db", "State": "running"}),
    ])

    def test_compose_ps_json_lines_and_array(self):
        services = builder.parse_compose_ps(self.PS_LINES)
        self.assertEqual(set(services), {"jupyterlab", "stats"})
        self.assertEqual(services["jupyterlab"], {"state": "running", "health": "healthy", "ports": {8888}})
        array = json.dumps([json.loads(line) for line in self.PS_LINES.splitlines()[1:]])
        self.assertEqual(builder.parse_compose_ps(array), services)
        # Older Compose prints an array, possibly after a warning line.
        self.assertEqual(builder.parse_compose_ps("WARN[0000] old compose\n[+] Running 2/2\n" + array), services)
        self.assertEqual(builder.parse_compose_ps(""), {})
        self.assertEqual(builder.parse_compose_ps("[]"), {})

    def test_service_state_mapping(self):
        state = builder.service_state
        self.assertEqual(state({"state": "running", "health": "healthy"}), ("Running", "success"))
        self.assertEqual(state({"state": "running", "health": "starting"}), ("Starting", "caution"))
        self.assertEqual(state({"state": "running", "health": ""}), ("Starting", "caution"))
        self.assertEqual(state({"state": "running", "health": "unhealthy"}), ("Unhealthy", "warn"))
        self.assertEqual(state({"state": "restarting", "health": ""}), ("Restarting", "warn"))
        self.assertEqual(state({"state": "paused", "health": ""}), ("Paused", "caution"))
        self.assertEqual(state({"state": "exited", "health": ""}), ("Stopped", "muted"))
        self.assertEqual(state({"state": "created", "health": ""}), ("Stopped", "muted"))
        self.assertEqual(state(None), ("Not deployed", "muted"))
        self.assertEqual(state(None, disabled=True), ("Disabled", "muted"))

    def test_root_step_lines(self):
        step = builder.root_step_from_line
        self.assertEqual(step("ROOT_STEP_REQUIRED: host-setup 100.82.217.101 8888 8889"),
                         ["host-setup", "100.82.217.101", "8888", "8889"])
        self.assertEqual(step("ROOT_STEP_REQUIRED: host-setup 100.82.217.101 8888"),
                         ["host-setup", "100.82.217.101", "8888"])
        self.assertEqual(step("ROOT_STEP_REQUIRED: host-teardown"), ["host-teardown"])
        for bad in ("ROOT_STEP_REQUIRED: host-setup 100.82.217.101",
                    "ROOT_STEP_REQUIRED: host-setup 100.82.217.101 8888 8889 9000",
                    "ROOT_STEP_REQUIRED: host-setup 10.0.0.1 8888",
                    "ROOT_STEP_REQUIRED: host-setup 100.300.1.1 8888",
                    "ROOT_STEP_REQUIRED: host-setup 100.200.1.1 8888",      # outside 100.64.0.0/10
                    "ROOT_STEP_REQUIRED: host-setup 100.82.217.101 80",
                    "ROOT_STEP_REQUIRED: host-setup 100.82.217.101 08888",  # bash would read octal
                    "ROOT_STEP_REQUIRED: host-setup 100.82.217.101 8888 8888",
                    "ROOT_STEP_REQUIRED: host-setup 100.82.217.101 8888;reboot",
                    "ROOT_STEP_REQUIRED: host-setup 100.82.217.101 ８８８８",
                    "ROOT_STEP_REQUIRED: host-teardown now",
                    "ROOT_STEP_REQUIRED: uninstall",
                    "echo ROOT_STEP_REQUIRED: host-teardown",
                    "ROOT_STEP_REQUIRED:host-teardown", ""):
            self.assertIsNone(step(bad), bad)

    def test_urls_come_from_the_page_table(self):
        runtime = {"TS_IP": "100.82.217.101", "JUPYTER_PORT": "8888", "STATS_PORT": "8889"}
        self.assertEqual(builder.service_url(runtime, "jupyterlab"), "http://100.82.217.101:8888/lab")
        self.assertEqual(builder.service_url(runtime, "stats"), "http://100.82.217.101:8889/")
        self.assertEqual(builder.service_url(dict(runtime, TS_IP="0.0.0.0"), "jupyterlab"), "")
        self.assertEqual(builder.service_url(dict(runtime, STATS_PORT=""), "stats"), "")
        self.assertEqual(builder.service_url({}, "jupyterlab"), "")

    def test_clean_line_mask_and_child_environment(self):
        self.assertEqual(builder.clean_line("\x1b[1;31mred\x1b[0m text\r\n"), "red text")
        self.assertEqual(builder.clean_line("10%\r50%\r100%"), "100%")
        self.assertEqual(builder.mask_secrets("pw is Secret-123 and Secret-123", ["Secret-123", "ab"]),
                         "pw is ******** and ********")
        env = builder.child_environment({"PATH": "/bin", "JUPYTER_PASSWORD": "x", "LEAK": "has Secret-123",
                                         "JLT_APP_DIR": "/tmp/app"}, ["Secret-123"], {"JLT_SETTINGS_FILE": "/s"})
        self.assertEqual(env["NO_COLOR"], "1")
        self.assertEqual(env["TERM"], "dumb")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(env["JLT_APP_DIR"], "/tmp/app")
        self.assertEqual(env["JLT_SETTINGS_FILE"], "/s")
        self.assertNotIn("JUPYTER_PASSWORD", env)
        self.assertNotIn("LEAK", env)

    def test_child_environment_uses_a_utf8_locale(self):
        # The installer's password checks match password_problems() only in UTF-8.
        env = builder.child_environment
        self.assertEqual(env({"PATH": "/bin"})["LC_ALL"], "C.UTF-8")
        self.assertEqual(env({"LANG": "C"})["LC_ALL"], "C.UTF-8")
        self.assertEqual(env({"LANG": "en_US.UTF-8", "LC_ALL": "POSIX"})["LC_ALL"], "C.UTF-8")
        self.assertNotIn("LC_ALL", env({"LANG": "en_US.UTF-8"}))
        self.assertNotIn("LC_ALL", env({"LC_CTYPE": "de_DE.utf8"}))


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

class ThemeTests(unittest.TestCase):
    def test_every_theme_file_gives_complete_tokens_and_clean_qss(self):
        messages = []
        previous = qInstallMessageHandler(lambda kind, context, text: messages.append(text))
        try:
            files = sorted((REPO / "stack" / "theme").glob("*.json"))
            self.assertTrue(files, "stack/theme/*.json missing")
            for path in files:
                for mode in ("light", "dark"):
                    with self.subTest(theme=path.stem, mode=mode):
                        tokens = builder.theme_tokens(builder.load_theme_colors(path.parent, path.stem), mode)
                        self.assertLessEqual(set(builder.TOKEN_NAMES), set(tokens))
                        for name, value in tokens.items():
                            self.assertRegex(value, r"^#[0-9a-f]{6}$|^#[0-9a-fA-F]{3,6}$", name)
                        qss = builder.build_stylesheet(tokens, "Orbitron", "monospace")
                        self.assertNotIn("$", qss)
                        widget = QWidget()
                        widget.setStyleSheet(qss)
                        widget.setPalette(builder.build_palette(tokens))
                        widget.ensurePolished()
                        widget.deleteLater()
        finally:
            qInstallMessageHandler(previous)
        self.assertFalse([m for m in messages if "stylesheet" in m.lower() or "parse" in m.lower()], messages)

    def test_missing_or_hostile_theme_falls_back_to_amazing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(builder.load_theme_colors(Path(tmp), "amazing"), builder.BUILTIN_COLORS)
        self.assertEqual(builder.load_theme_colors(REPO / "stack" / "theme", "../theme/amazing"),
                         builder.BUILTIN_COLORS)

    def test_mapping_follows_the_oya_rules(self):
        colors = builder.BUILTIN_COLORS
        light, dark = builder.theme_tokens(colors, "light"), builder.theme_tokens(colors, "dark")
        self.assertEqual(light["window_bg"], colors["primary-bg-light"])
        self.assertEqual(dark["card_bg"], colors["bubble-bg-dark"])
        self.assertEqual(light["card_border"], colors["accent-2"])
        self.assertEqual(dark["card_border"], colors["accent-1"])
        self.assertEqual(dark["header_bg"], colors["appbar-bg-dark"])
        self.assertEqual(light["log_bg"], colors["sunken-light"])
        for tokens in (light, dark):
            options = (tokens["window_bg"], "#ffffff")
            best = max(options, key=lambda c: builder.contrast(c, tokens["accent"]))
            self.assertEqual(tokens["on_accent"], best)


# ---------------------------------------------------------------------------
# Runner (real child processes)
# ---------------------------------------------------------------------------

class RunnerTests(unittest.TestCase):
    def test_exit_code_output_cleaning_and_echo(self):
        runner = builder.Runner(capture=True)
        code = ("import sys\nprint('one')\nprint('\\x1b[31mtwo\\x1b[0m')\n"
                "sys.stdout.write('10%\\r100%\\n')\nsys.stdout.buffer.write(b'bad \\xff byte\\n')\nsys.exit(3)")
        argv = [sys.executable, "-c", code]
        result, lines = run_to_completion(runner, argv)
        self.assertEqual((result.outcome, result.code), ("exit", 3))
        self.assertEqual(lines[0], "$ " + shlex.join(argv))
        self.assertEqual(list(result.lines[:4]), ["one", "two", "100%", "bad � byte"])

    def test_success_closed_stdin_new_session_and_environment(self):
        runner = builder.Runner(capture=True)
        code = ("import os, sys\nprint(len(sys.stdin.read()))\nprint(os.getsid(0) == os.getpid())\n"
                "print(os.environ.get('NO_COLOR'), os.environ.get('TERM'))")
        result, _ = run_to_completion(runner, [sys.executable, "-c", code],
                                      env=builder.child_environment(os.environ))
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(list(result.lines), ["0", "True", "1 dumb"])

    def test_failed_to_start(self):
        result, _ = run_to_completion(builder.Runner(), ["thebe-test-no-such-program"])
        self.assertEqual(result.outcome, "failed")
        self.assertTrue(result.error)

    def test_a_child_killed_by_a_signal_is_crashed(self):
        code = "import os, signal\nprint('bye', flush=True)\nos.kill(os.getpid(), signal.SIGKILL)"
        result, _ = run_to_completion(builder.Runner(), [sys.executable, "-c", code])
        self.assertEqual(result.outcome, "crashed")

    def test_timeout_cancels_a_hanging_command(self):
        runner = builder.Runner(kill_grace_ms=500)
        started = time.monotonic()
        result, _ = run_to_completion(runner, [sys.executable, "-c", "import time; time.sleep(60)"], timeout_ms=300)
        self.assertEqual(result.outcome, "timeout")
        self.assertLess(time.monotonic() - started, 8)

    def test_long_lines_are_cut_and_a_flood_of_output_stays_fast(self):
        code = ("import sys\nw = sys.stdout.write\nw('x' * 3_000_000 + '\\n')\n"
                "w('a' * 10_000 + '\\rshort\\n')\n"
                "for i in range(200_000):\n    w(f'{i}\\n')\n")
        started = time.monotonic()
        result, _ = run_to_completion(builder.Runner(capture=True), [sys.executable, "-c", code], 30000)
        elapsed = time.monotonic() - started
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.lines[0], "x" * builder.MAX_LINE_CHARS + " […line cut]")
        self.assertEqual(result.lines[1], "short")        # the carriage return started the line over
        self.assertEqual(len(result.lines), 200_002)
        self.assertEqual(result.lines[-1], "199999")
        self.assertLess(elapsed, 15)

    def test_cancel_escalates_to_sigkill(self):
        runner = builder.Runner(kill_grace_ms=300)
        seen, results = [], []
        runner.output.connect(seen.extend)
        runner.done.connect(results.append)
        code = ("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('ready', flush=True)\ntime.sleep(60)")
        started = time.monotonic()
        runner.start([sys.executable, "-c", code])
        self.assertTrue(wait_until(lambda: "ready" in seen))
        runner.cancel()
        self.assertTrue(wait_until(lambda: results, 10000))
        self.assertEqual(results[0].outcome, "cancelled")
        self.assertLess(time.monotonic() - started, 8)
        self.assertFalse(runner.is_running())

    def test_terminate_and_wait(self):
        runner = builder.Runner(kill_grace_ms=2000)
        results = []
        runner.done.connect(results.append)
        runner.start([sys.executable, "-c", "import time; time.sleep(60)"])
        self.assertTrue(wait_until(lambda: runner._proc.processId() > 0))
        self.assertTrue(runner.terminate_and_wait())
        self.assertEqual(results[0].outcome, "cancelled")


# ---------------------------------------------------------------------------
# run-builder.sh (in a throw-away copy, with a requirement that is already met)
# ---------------------------------------------------------------------------

class LauncherTests(unittest.TestCase):
    PYTHON = "/usr/bin/python3"

    def test_a_venv_without_pip_is_rebuilt_and_a_bare_python_name_works(self):
        if not os.access(self.PYTHON, os.X_OK) or subprocess.run(
                [self.PYTHON, "-c", "import venv, ensurepip"], capture_output=True).returncode:
            self.skipTest(f"{self.PYTHON} cannot create venvs")
        with tempfile.TemporaryDirectory(prefix="thebe-test-launcher-") as tmp:
            root = Path(tmp)
            shutil.copy2(REPO / "run-builder.sh", root / "run-builder.sh")
            (root / "requirements-builder.txt").write_text("pip\n")      # already installed: no download
            (root / "builder.py").write_text("import sys\nprint('launched', sys.argv[1:])\n")
            # What Ctrl+C during ensurepip leaves behind: a python, but no pip.
            subprocess.run([self.PYTHON, "-m", "venv", "--without-pip", str(root / ".venv")], check=True)
            env = {k: v for k, v in os.environ.items() if k != "PYTHON"}
            env.update(QT_QPA_PLATFORM="offscreen", PATH="/usr/bin:/bin")

            first = subprocess.run(["bash", str(root / "run-builder.sh"), "--flag"], env=env,
                                   capture_output=True, text=True, timeout=300)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("no working pip", first.stderr)
            self.assertIn("launched ['--flag']", first.stdout)

            second = subprocess.run(["bash", str(root / "run-builder.sh")], env=dict(env, PYTHON="python3"),
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("Installing", second.stderr)
            self.assertIn("launched []", second.stdout)


# ---------------------------------------------------------------------------
# Main window, with stub docker / tailscale / pkexec and a stub installer
# ---------------------------------------------------------------------------

SECRET = "Very-Secret-Pass-42"
TS_IP = "100.82.217.101"

RUNNING_PS = "\n".join(json.dumps({
    "Project": "jupyterlab-tailscale", "Service": service, "State": "running", "Health": "healthy",
    "Publishers": [{"URL": TS_IP, "TargetPort": target, "PublishedPort": port, "Protocol": "tcp"}],
}) for service, target, port in (("jupyterlab", 8888, 8888), ("stats", 8889, 8889)))


class WindowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-window-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.state = root / "state"
        self.bin = root / "bin"
        for directory in (self.state, self.bin, root / "repo", root / "app"):
            directory.mkdir()
        state = shlex.quote(str(self.state))
        record = 'printf "%s\\037" "$@" >> {log}; printf "\\n" >> {log}'
        self._script(self.bin / "docker", f"""
            printf '%s\\n' "$*" >> {state}/docker.calls
            if [ "$*" = "compose -p jupyterlab-tailscale ps -a --format json" ]; then
              if [ -f {state}/docker.fail ]; then cat {state}/docker.fail; exit 1; fi
              cat {state}/ps.json 2>/dev/null
              exit 0
            fi
            echo "stub docker refuses: $*"; exit 99
        """)
        self._script(self.bin / "tailscale", f"""
            [ -f {state}/tailscale.sleep ] && sleep 30
            cat {state}/tailscale.json
        """)
        self._script(self.bin / "pkexec", f"""
            {record.format(log=f'{state}/pkexec.calls')}
            env -0 >> {state}/pkexec.env
            [ -f {state}/pkexec.sleep ] && sleep 30
            exit "$(cat {state}/pkexec.rc 2>/dev/null || echo 0)"
        """)
        # The installer is deliberately not executable: the builder runs it with bash.
        self.installer = root / "repo" / "setup-jupyterlab-tailscale.sh"
        self.installer.write_text("#!/bin/bash\n" + textwrap.dedent(f"""
            printf '%s\\037' "$0" "$@" >> {state}/installer.calls; printf '\\n' >> {state}/installer.calls
            env -0 >> {state}/installer.env
            echo "stub installer: $1"
            [ -f {state}/sleep ] && sleep 30
            # A careless installer might print the settings, password included.
            [ "$1" = install ] && cat "$JLT_SETTINGS_FILE"
            [ -f {state}/root-step ] && cat {state}/root-step
            exit "$(cat {state}/installer.rc 2>/dev/null || echo 0)"
        """))
        self.installer.chmod(0o644)
        self.settings = root / "repo" / ".env"
        self.runtime = root / "app" / ".env"
        self.runtime.write_text(f"TS_IP='{TS_IP}'\nJUPYTER_PORT='8888'\nSTATS_PORT='8889'\nCOMPOSE_PROFILES='stats'\n")
        (self.state / "tailscale.json").write_text(tailscale_json())
        self.paths = builder.Paths(self.installer, self.settings, self.runtime, REPO / "stack" / "theme",
                                   REPO / "stack" / "stats" / "static" / "orbitron-latin.woff2")

        path_patch = mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})
        path_patch.start()
        self.addCleanup(path_patch.stop)
        self.critical = self._patch_box("critical", QMessageBox.StandardButton.Ok)
        self._patch_box("warning", QMessageBox.StandardButton.Ok)
        self._patch_box("information", QMessageBox.StandardButton.Ok)
        self.question = self._patch_box("question", QMessageBox.StandardButton.Yes)

    def _script(self, path, body):
        path.write_text("#!/bin/bash\n" + textwrap.dedent(body))
        path.chmod(0o755)

    def _patch_box(self, name, answer):
        patcher = mock.patch.object(builder.QMessageBox, name, return_value=answer)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def window(self, **kwargs):
        kwargs.setdefault("bind", lambda ip, port: None)
        kwargs.setdefault("kill_grace_ms", 1000)
        paths = kwargs.pop("paths", self.paths)
        window = builder.MainWindow(paths, poll=False, **kwargs)
        results = []
        window.job_runner.done.connect(results.append)
        window.results = results
        self.addCleanup(self._dispose, window)
        return window

    @staticmethod
    def _dispose(window):
        window.close()
        window.deleteLater()
        APP.processEvents()

    def calls(self, name):
        path = self.state / f"{name}.calls"
        text = path.read_text() if path.exists() else ""
        return [line.rstrip("\x1f").split("\x1f") for line in text.splitlines() if line]

    def wait_idle(self, window, jobs):
        self.assertTrue(wait_until(lambda: len(window.results) >= jobs and not window._busy),
                        window.log_view.toPlainText())

    def wait_refused(self, window):
        self.assertTrue(wait_until(lambda: self.critical.called and not window._busy))
        return self.critical.call_args.args[2]

    def refresh_status(self, window):
        window.refresh_status()
        self.assertTrue(wait_until(lambda: window.docker_problem is not None and not window.status_runner.is_running()))

    def assert_no_secret_anywhere(self, window):
        for name in ("installer.calls", "installer.env", "pkexec.calls", "pkexec.env", "docker.calls"):
            path = self.state / name
            if path.exists():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), name)
        for result in window.results:
            self.assertNotIn(SECRET, " ".join(result.argv))
        self.assertNotIn(SECRET, window.log_view.toPlainText())
        self.assertNotIn(SECRET, window.banner.text())

    # -- form ---------------------------------------------------------------

    def test_password_is_masked_until_shown(self):
        window = self.window()
        self.assertEqual(window.password_edit.echoMode(), QLineEdit.EchoMode.Password)
        window.password_toggle.click()
        self.assertEqual(window.password_edit.echoMode(), QLineEdit.EchoMode.Normal)
        self.assertEqual(window.password_toggle.text(), "Hide")
        window.password_toggle.click()
        self.assertEqual(window.password_edit.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(window.password_toggle.text(), "Show")

    def test_defaults_and_statistics_checkbox(self):
        window = self.window()
        self.assertEqual((window.jupyter_port.value(), window.stats_port.value()), (8888, 8889))
        # The whole range, so a typed privileged port reaches validation instead of being reverted.
        self.assertEqual((window.jupyter_port.minimum(), window.jupyter_port.maximum()), (1, 65535))
        self.assertFalse(window.jupyter_port.isGroupSeparatorShown())
        self.assertTrue(window.stats_check.isChecked())
        self.assertIn("Username: jupyter", window.stats_user_label.text())
        window.stats_check.setChecked(False)
        self.assertFalse(window.stats_port.isEnabled())
        self.assertEqual(window.rows["stats"].badge.text(), "Disabled")

    def test_existing_settings_are_loaded(self):
        self.settings.write_text("# mine\nJUPYTER_PASSWORD='Loaded-Pass-123'\nJUPYTER_PORT=9200\n"
                                 "STATS_ENABLED='0'\nSTATS_PORT=\"9201\"\n")
        window = self.window()
        self.assertEqual(window.password_edit.text(), "Loaded-Pass-123")
        self.assertEqual(window.password_edit.cursorPosition(), 0)
        self.assertEqual((window.jupyter_port.value(), window.stats_port.value()), (9200, 9201))
        self.assertFalse(window.stats_check.isChecked())
        self.assertEqual(window.banner.property("kind"), "info")

    def test_statistics_switch_loads_the_installer_spellings(self):
        for raw, checked in (("false", False), ("No", False), ("off", False), ("On", True), ("yes", True)):
            with self.subTest(raw=raw):
                self.settings.write_text(f"STATS_ENABLED='{raw}'\n")
                window = self.window()
                self.assertEqual(window.stats_check.isChecked(), checked)
                self.assertEqual(window.banner.property("kind"), "info")
        self.settings.write_text("STATS_ENABLED='maybe'\n")
        window = self.window()
        self.assertFalse(window.stats_check.isChecked())     # unclear: open no port
        self.assertEqual(window.banner.property("kind"), "error")
        self.assertIn("STATS_ENABLED", window.banner.text())

    def test_a_status_poll_does_not_disturb_a_port_being_typed(self):
        window = self.window()
        window._on_tailscale_done(builder.RunResult(("tailscale",), "ok", 0, lines=(tailscale_json(),)))
        spin = window.jupyter_port
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin, "12")                      # on the way to 12000
        self.assertEqual(spin.lineEdit().text(), "12")
        window._on_status_done(builder.RunResult(("docker",), "ok", 0, lines=tuple(RUNNING_PS.splitlines())))
        window._on_tailscale_done(builder.RunResult(("tailscale",), "ok", 0, lines=(tailscale_json(),)))
        self.assertEqual(spin.lineEdit().text(), "12")
        QTest.keyClicks(spin, "000")
        QTest.keyClick(spin, Qt.Key.Key_Return)
        self.assertEqual(spin.value(), 12000)
        self.assertEqual(window.form_values()["JUPYTER_PORT"], "12000")

    def test_the_mouse_wheel_does_not_change_an_unfocused_port(self):
        window = self.window()
        spin = window.jupyter_port
        event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(spin, event)
        self.assertEqual(spin.value(), 8888)
        self.assertFalse(event.isAccepted(), "the scroll area should get the wheel")

    def test_occupied_port_detection_with_a_real_socket_and_own_whitelist(self):
        window = self.window(bind=builder.probe_bind)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            window.ts = builder.TailscaleStatus(ip="127.0.0.1")
            window.docker_problem = ""            # compose ps has answered
            window.stats_check.setChecked(False)
            window.jupyter_port.setValue(port)
            self.assertTrue(window.error_label.isVisibleTo(window))
            self.assertIn(f"Port {port} (JupyterLab) is already in use", window.error_label.text())
            window.services = {"jupyterlab": {"state": "running", "health": "healthy", "ports": {port}}}
            self.assertEqual(window._validate_live(), [])
            self.assertFalse(window.error_label.isVisibleTo(window))
            # The same port held by the other service is not ours to take.
            window.services = {"stats": {"state": "running", "health": "healthy", "ports": {port}}}
            self.assertIn("still published by the Statistics container", window._validate_live()[0])

    def test_own_ports_are_not_reported_before_compose_ps_answers(self):
        def taken(ip, port):
            if port in (8888, 8889):
                raise OSError(errno.EADDRINUSE, "Address already in use")

        (self.state / "ps.json").write_text(RUNNING_PS)
        window = self.window(bind=taken)
        window._on_tailscale_done(builder.RunResult(("tailscale",), "ok", 0, lines=(tailscale_json(),)))
        self.assertEqual(window.ts.ip, TS_IP)
        self.assertFalse(window.error_label.isVisibleTo(window), window.error_label.text())
        # Deploy asks compose ps itself before judging the ports.
        window.deploy()
        self.wait_idle(window, 1)
        self.critical.assert_not_called()
        self.assertEqual(self.calls("installer"), [[str(self.installer), "install"]])

    # -- deploy -------------------------------------------------------------

    def test_deploy_saves_runs_install_then_pkexec_and_never_leaks_the_password(self):
        (self.state / "root-step").write_text(f"ROOT_STEP_REQUIRED: host-setup {TS_IP} 9100 9101\n")
        window = self.window()
        window.password_edit.setText(SECRET)
        window.jupyter_port.setValue(9100)
        window.stats_port.setValue(9101)
        window.deploy()
        self.wait_idle(window, 2)

        bash = shutil.which("bash")
        self.assertEqual([list(r.argv) for r in window.results], [
            [bash, str(self.installer), "install"],
            ["pkexec", "/bin/bash", str(self.installer), "host-setup", TS_IP, "9100", "9101"],
        ])
        self.assertEqual(self.calls("installer"), [[str(self.installer), "install"]])
        self.assertEqual(self.calls("pkexec"), [["/bin/bash", str(self.installer), "host-setup", TS_IP, "9100", "9101"]])
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)
        saved = builder.parse_settings(self.settings.read_text())
        self.assertEqual((saved["JUPYTER_PASSWORD"], saved["JUPYTER_PORT"], saved["STATS_PORT"]), (SECRET, "9100", "9101"))
        env = dict(item.split("=", 1) for item in (self.state / "installer.env").read_text().split("\0") if "=" in item)
        self.assertEqual(env["JLT_SETTINGS_FILE"], str(self.settings))
        self.assertEqual(env["JLT_APP_DIR"], str(self.runtime.parent))
        self.assertEqual((env["NO_COLOR"], env["TERM"], env["PYTHONUNBUFFERED"]), ("1", "dumb", "1"))
        self.assertIn("$ " + shlex.join([bash, str(self.installer), "install"]), window.log_view.toPlainText())
        self.assertIn(builder.MASK, window.log_view.toPlainText())   # the stub printed the settings file
        self.assert_no_secret_anywhere(window)
        self.critical.assert_not_called()
        self.assertEqual(window.banner.property("kind"), "success")
        self.assertIn(f"http://{TS_IP}:8888/lab", window.banner.text())

    def test_pkexec_refusals_explain_the_terminal_command(self):
        (self.state / "root-step").write_text(f"ROOT_STEP_REQUIRED: host-setup {TS_IP} 8888\n")
        for code, reason in ((126, "authorisation dismissed"), (127, "not authorised")):
            with self.subTest(code=code):
                self.critical.reset_mock()
                (self.state / "pkexec.rc").write_text(str(code))
                window = self.window()
                window.deploy()
                self.wait_idle(window, 2)
                self.critical.assert_called_once()
                message = self.critical.call_args.args[2]
                self.assertIn(reason, message)
                self.assertIn("services run", message)
                self.assertIn(shlex.join(["sudo", str(self.installer), "host-setup", TS_IP, "8888"]), message)
                self.assertEqual(window.banner.property("kind"), "error")

    def test_statistics_disabled_is_saved(self):
        window = self.window()
        window.stats_check.setChecked(False)
        window.deploy()
        self.wait_idle(window, 1)
        self.assertEqual(builder.parse_settings(self.settings.read_text())["STATS_ENABLED"], "0")
        self.assertEqual(len(self.calls("pkexec")), 0)

    def test_invalid_settings_refuse_deploy(self):
        window = self.window()
        window.password_edit.setText("short")
        window.deploy()
        APP.processEvents()
        self.critical.assert_called_once()
        self.assertIn("Deploy refused", self.critical.call_args.args[2])
        self.assertTrue(window.error_label.isVisibleTo(window))
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.calls("installer"), [])

    def test_a_typed_privileged_port_is_refused_not_reverted(self):
        window = self.window()
        window.jupyter_port.lineEdit().setText("80")
        window.deploy()
        APP.processEvents()
        self.critical.assert_called_once()
        self.assertIn("privileged", self.critical.call_args.args[2])
        self.assertEqual(window.jupyter_port.value(), 80)
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.calls("installer"), [])

    def test_tailscale_problem_refuses_deploy(self):
        (self.state / "tailscale.json").write_text(tailscale_json("NeedsLogin"))
        window = self.window()
        window.deploy()
        self.assertIn("logged out", self.wait_refused(window))
        self.assertEqual(self.calls("installer"), [])

    def test_docker_problem_refuses_deploy(self):
        (self.state / "docker.fail").write_text("permission denied while trying to connect to the Docker daemon\n")
        window = self.window()
        window.deploy()
        self.assertIn("Docker is not usable", self.wait_refused(window))
        self.assertEqual(self.calls("installer"), [])

    def test_occupied_port_refuses_deploy(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            window = self.window(bind=builder.probe_bind)
            window.stats_check.setChecked(False)
            window.jupyter_port.setValue(port)
            with mock.patch.object(builder, "parse_tailscale_status",
                                   return_value=builder.TailscaleStatus(ip="127.0.0.1")):
                window.deploy()
                message = self.wait_refused(window)
        self.assertIn("already in use", message)
        self.assertEqual(self.calls("installer"), [])

    def test_a_settings_file_that_is_not_utf8_is_never_rewritten(self):
        raw = "# café\nJUPYTER_PASSWORD='pässwort-123'\n".encode("latin-1")
        self.settings.write_bytes(raw)
        window = self.window()
        self.assertEqual(window.banner.property("kind"), "error")
        self.assertIn("not UTF-8", window.banner.text())
        window.deploy()
        self.assertIn("not UTF-8", self.wait_refused(window))
        self.assertEqual(self.settings.read_bytes(), raw)
        self.assertEqual(self.calls("installer"), [])

    def test_lines_the_installer_rejects_block_deploy(self):
        text = "export EXTRA=1\nJUPYTER_PASSWORD='Loaded-Pass-123' # note\n"
        self.settings.write_text(text)
        window = self.window()
        self.assertEqual(window.banner.property("kind"), "error")
        self.assertIn("line 1: expected KEY=value", window.banner.text())
        self.assertIn("line 2", window.banner.text())
        window.deploy()
        message = self.wait_refused(window)
        self.assertIn("line 1: expected KEY=value", message)
        self.assertNotIn("line 2", message)       # the builder rewrites its own key's line
        self.assertEqual(self.settings.read_text(), text)
        self.assertEqual(self.calls("installer"), [])

    def test_relative_overrides_reach_the_installer_as_absolute_paths(self):
        workdir = self.state / "cwd"
        workdir.mkdir()
        old = os.getcwd()
        os.chdir(workdir)
        self.addCleanup(os.chdir, old)
        cwd = Path(os.getcwd())
        environ = dict(os.environ, JLT_SETTINGS_FILE="my.env", JLT_APP_DIR="app", JLT_WORKSPACE_DIR="ws")
        paths = dataclasses.replace(builder.Paths.from_environment(environ), installer=self.installer)
        window = self.window(paths=paths, environ=environ)
        self.assertEqual(window.paths.settings, cwd / "my.env")
        env = window._child_env()
        self.assertEqual(env["JLT_SETTINGS_FILE"], str(cwd / "my.env"))
        self.assertEqual(env["JLT_APP_DIR"], str(cwd / "app"))
        self.assertEqual(env["JLT_WORKSPACE_DIR"], str(cwd / "ws"))

    # -- stop / start / status ---------------------------------------------

    def test_stop_runs_stop(self):
        window = self.window()
        window.stop()
        self.wait_idle(window, 1)
        self.assertEqual(list(window.results[0].argv), [shutil.which("bash"), str(self.installer), "stop"])
        self.assertEqual(window.banner.property("kind"), "success")

    def test_start_or_restart_follows_compose_ps(self):
        opened = []
        window = self.window(open_url=lambda url: opened.append(url.toString()) or True)
        (self.state / "ps.json").write_text("")
        self.refresh_status(window)
        self.assertEqual(window.start_button.text(), "Start")
        self.assertEqual(window.rows["jupyterlab"].badge.text(), "Not deployed")

        (self.state / "ps.json").write_text(RUNNING_PS)
        window.refresh_status()
        self.assertTrue(wait_until(lambda: window.services))
        self.assertEqual(window.start_button.text(), "Restart")
        self.assertEqual(window.deploy_button.text(), "Update")
        self.assertEqual(window.rows["jupyterlab"].badge.text(), "Running")
        self.assertEqual(window.published_ports(), {"jupyterlab": {8888}, "stats": {8889}})
        self.assertTrue(window.rows["stats"].open_button.isEnabled())
        window.rows["jupyterlab"].open_button.click()
        self.assertEqual(opened, [f"http://{TS_IP}:8888/lab"])
        window.start_or_restart()
        self.wait_idle(window, 1)
        self.assertEqual(window.results[-1].argv[-1], "restart")

        (self.state / "ps.json").write_text(RUNNING_PS.replace('"running"', '"exited"'))
        window.refresh_status()
        self.assertTrue(wait_until(lambda: not window._any_active() and not window.status_runner.is_running()))
        self.assertEqual(window.start_button.text(), "Start")
        self.assertFalse(window.rows["jupyterlab"].open_button.isEnabled())
        window.start_or_restart()
        self.wait_idle(window, 2)
        self.assertEqual(window.results[-1].argv[-1], "start")

    def test_stop_and_restart_apply_to_a_crash_looping_container(self):
        (self.state / "ps.json").write_text(RUNNING_PS.replace('"running"', '"restarting"'))
        window = self.window()
        self.refresh_status(window)
        self.assertEqual(window.rows["jupyterlab"].badge.text(), "Restarting")
        self.assertTrue(window.stop_button.isEnabled())
        self.assertEqual(window.start_button.text(), "Restart")
        self.assertFalse(window.rows["jupyterlab"].open_button.isEnabled())
        window.stop_button.click()
        self.wait_idle(window, 1)
        self.assertEqual(window.results[-1].argv[-1], "stop")
        window.start_or_restart()
        self.wait_idle(window, 2)
        self.assertEqual(window.results[-1].argv[-1], "restart")
        # Paused containers count as well.
        window.services = {"jupyterlab": {"state": "paused", "health": "", "ports": set()}}
        window._refresh_view()
        self.assertTrue(window.stop_button.isEnabled())
        self.assertEqual(window.start_button.text(), "Restart")

    def test_start_is_offered_for_an_installed_app_without_containers(self):
        (self.state / "ps.json").write_text("")
        window = self.window()
        self.refresh_status(window)
        self.assertTrue(window.start_button.isEnabled())       # e.g. after a manual compose down
        self.assertFalse(window.stop_button.isEnabled())
        self.runtime.unlink()                                  # never installed
        self.refresh_status(window)
        self.assertFalse(window.runtime)
        self.assertFalse(window.start_button.isEnabled())

    def test_docker_problems_are_shown_not_raised(self):
        (self.state / "docker.fail").write_text(
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n")
        window = self.window()
        window.refresh_status()
        self.assertTrue(wait_until(lambda: window.docker_problem))
        self.assertIn("not running", window.docker_label.text())
        self.assertEqual(window.rows["jupyterlab"].badge.text(), "Unknown")

        empty_bin = self.state / "empty-bin"
        empty_bin.mkdir()
        lonely = builder.MainWindow(self.paths, poll=False, environ=dict(os.environ, PATH=str(empty_bin)))
        self.addCleanup(self._dispose, lonely)
        lonely.refresh_status()
        lonely.refresh_tailscale()
        self.assertTrue(wait_until(lambda: lonely.docker_problem and lonely.ts.problem))
        self.assertIn("not installed", lonely.docker_label.text())
        self.assertIn("not installed", lonely.tailscale_label.text())

    def test_a_hanging_probe_times_out(self):
        (self.state / "tailscale.sleep").write_text("")
        window = self.window()
        with mock.patch.object(builder, "PROBE_TIMEOUT_MS", 300):
            window.refresh_tailscale()
            self.assertTrue(wait_until(lambda: window.ts.problem, 10000))
        self.assertIn("did not answer", window.ts.problem)
        self.assertIn("did not answer", window.tailscale_label.text())
        self.assertFalse(window.tailscale_runner.is_running())

    def test_installer_failure_is_reported(self):
        (self.state / "installer.rc").write_text("2")
        window = self.window()
        window.stop()
        self.wait_idle(window, 1)
        self.critical.assert_called_once()
        self.assertIn("exited with code 2", self.critical.call_args.args[2])
        self.assertEqual(window.banner.property("kind"), "error")

    def test_command_output_reaches_the_log_in_batches(self):
        window = self.window()
        window._on_job_output([f"line {i}" for i in range(10_000)])
        self.assertEqual(window.log_view.toPlainText(), "")        # queued, not painted per chunk
        self.assertTrue(wait_until(lambda: window.log_view.blockCount() >= builder.LOG_MAX_BLOCKS, 3000))
        self.assertTrue(window.log_view.toPlainText().endswith("line 9999"))
        window.log("# after")                                      # direct lines keep their order
        self.assertTrue(window.log_view.toPlainText().endswith("line 9999\n# after"))

    def test_everything_is_disabled_while_a_job_runs_and_close_cancels_it(self):
        (self.state / "sleep").write_text("")
        window = self.window()
        window.stop()
        self.assertTrue(wait_until(lambda: self.calls("installer")))
        for widget in (window.password_edit, window.password_toggle, window.jupyter_port, window.stats_check,
                       window.stats_port, window.stop_button, window.start_button, window.deploy_button,
                       window.rows["jupyterlab"].open_button):
            self.assertFalse(widget.isEnabled(), widget)
        window.deploy()          # ignored while busy
        started = time.monotonic()
        window.close()
        self.assertFalse(window.job_runner.is_running())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(window.results[-1].outcome, "cancelled")
        self.assertEqual(len(self.calls("installer")), 1)

    def test_closing_during_the_root_step_warns_that_it_cannot_be_interrupted(self):
        (self.state / "root-step").write_text(f"ROOT_STEP_REQUIRED: host-setup {TS_IP} 8888\n")
        (self.state / "pkexec.sleep").write_text("")
        window = self.window()
        window.deploy()
        self.assertTrue(wait_until(lambda: self.calls("pkexec")))
        window.close()
        self.assertIn("running as root", self.question.call_args.args[2])
        self.assertFalse(window.job_runner.is_running())

    def test_theme_applies_in_both_modes(self):
        window = self.window()
        for mode in ("light", "dark"):
            window.apply_theme(mode)
            self.assertEqual(window.mode, mode)
            self.assertIn(window.tokens["card_bg"], window.styleSheet())


if __name__ == "__main__":
    unittest.main()
