#!/usr/bin/env python3
"""JupyterLab on Tailscale - a small PyQt6 builder.

A thin GUI over setup-jupyterlab-tailscale.sh and its Docker Compose stack.
It edits the settings .env next to the installer, runs the installer's
install / start / restart / stop commands through QProcess, shows container
state from `docker compose ps`, and opens the deployed pages in a browser.
All real work stays in the installer, so the CLI workflow is unchanged.

Start it with ./run-builder.sh, which keeps PyQt6 inside ./.venv.
"""

from __future__ import annotations

import codecs
import errno
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import string
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Iterable, Mapping, NamedTuple

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QGuiApplication, QPalette
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QBoxLayout, QCheckBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QVBoxLayout, QWidget,
)

REPO = Path(__file__).resolve().parent
APP_NAME = "JupyterLab on Tailscale"
PROJECT = "jupyterlab-tailscale"          # Compose project name used by the installer
TAILNET = ipaddress.ip_network("100.64.0.0/10")

# Settings .env keys and their defaults (the installer uses the same ones).
DEFAULTS = {
    "JUPYTER_PASSWORD": "TailLab-7mK9-vQ2x-N4pR!",
    "JUPYTER_PORT": "8888",
    "STATS_ENABLED": "1",
    "STATS_PORT": "8889",
    "STATS_USER": "jupyter",
    "THEME": "amazing",
}

SERVICES = (("jupyterlab", "JupyterLab"), ("stats", "Statistics"))


class Page(NamedTuple):
    label: str
    service: str       # compose service that serves it
    port_key: str      # key in the runtime .env holding the published port
    path: str


# Every page the stack serves, in one table. The first page of a service is
# the one its Open button uses; the others get a link under that service, so
# nobody has to type an address into the tablet's or laptop's browser.
PAGES = (
    Page("JupyterLab", "jupyterlab", "JUPYTER_PORT", "/lab"),
    Page("Statistics", "stats", "STATS_PORT", "/"),
    Page("Dependencies", "stats", "STATS_PORT", "/dependencies"),
    Page("Stats API", "stats", "STATS_PORT", "/api/stats"),
    Page("Health", "stats", "STATS_PORT", "/health"),
)

KILL_GRACE_MS = 10_000
STATUS_INTERVAL_MS = 4_000
TAILSCALE_INTERVAL_MS = 30_000
PROBE_TIMEOUT_MS = 15_000      # a wedged dockerd/tailscaled must not hang the GUI
LOG_MAX_BLOCKS = 4000
LOG_FLUSH_MS = 80              # output reaches the log in batches, not per chunk
MAX_LINE_CHARS = 4096          # longer output lines are cut
MASK = "********"
ACTIVE_STATES = ("running", "restarting", "paused")    # containers Stop still has to stop

# Added to every child environment: plain, uncoloured, unbuffered output.
CHILD_ENV_EXTRA = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PYTHONUNBUFFERED": "1",
    "COMPOSE_ANSI": "never",
    "BUILDKIT_PROGRESS": "plain",
}


def absolute_path(value: str) -> Path:
    return Path(value).expanduser().absolute()


@dataclass(frozen=True)
class Paths:
    installer: Path
    settings: Path       # settings .env the builder edits (0600)
    runtime_env: Path    # APP_DIR/.env written by the installer (read-only here)
    theme_dir: Path
    display_font: Path

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> "Paths":
        home = Path(environ.get("HOME") or Path.home())
        settings = environ.get("JLT_SETTINGS_FILE") or str(REPO / ".env")
        app_dir = environ.get("JLT_APP_DIR") or str(home / ".local/share/jupyterlab-tailscale")
        return cls(
            installer=REPO / "setup-jupyterlab-tailscale.sh",
            # Absolute: the installer runs with REPO as its working directory,
            # so a relative override would name a different file there.
            settings=absolute_path(settings),
            runtime_env=absolute_path(app_dir) / ".env",
            theme_dir=REPO / "stack" / "theme",
            display_font=REPO / "stack" / "stats" / "static" / "orbitron-latin.woff2",
        )


# --------------------------------------------------------------------------
# Settings file
# --------------------------------------------------------------------------

# The installer's bash patterns use glibc's [[:space:]] and [[:cntrl:]]. In a
# UTF-8 locale (C.UTF-8 and en_US.UTF-8 agree) they are exactly these; the
# child environment makes sure the installer runs in one (child_environment).
SPACE_CHARS = "\t\n\v\f\r               　"
_EXTRA_CONTROL = frozenset("  ")     # glibc counts the line/paragraph separators as control

_ASSIGNMENT = re.compile(rf"^[{re.escape(SPACE_CHARS)}]*([A-Za-z_][A-Za-z0-9_]*)[{re.escape(SPACE_CHARS)}]*=(.*)$",
                         re.DOTALL)


class EnvLine(NamedTuple):
    key: str = ""        # '' for blank, comment and unreadable lines
    value: str = ""
    problem: str = ""    # why the installer rejects the line; the value is unusable then


def _parse_line(line: str) -> EnvLine:
    """One .env line, read exactly like the installer's read_env_file."""
    line = line.removesuffix("\n").removesuffix("\r")
    if not line.strip(SPACE_CHARS) or line.lstrip(SPACE_CHARS).startswith("#"):
        return EnvLine()
    match = _ASSIGNMENT.match(line)
    if match is None:
        return EnvLine(problem="expected KEY=value")
    key, raw = match.group(1), match.group(2).strip(SPACE_CHARS)
    # Quotes count only around the whole value: KEY='v' # note is an error there too.
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
        return EnvLine(key, raw[1:-1])
    if raw[:1] in ("'", '"'):
        return EnvLine(key, problem=f"the value of {key} has no closing quote")
    return EnvLine(key, raw)


def parse_settings(text: str) -> dict[str, str]:
    """All readable assignments in an .env text; a later duplicate wins, like the installer."""
    values: dict[str, str] = {}
    for line in text.split("\n"):
        parsed = _parse_line(line)
        if parsed.key and not parsed.problem:
            values[parsed.key] = parsed.value
    return values


def settings_line_problems(text: str) -> list[str]:
    """Lines the installer refuses, by line number only (a value may be the password)."""
    problems = []
    for number, line in enumerate(text.split("\n"), 1):
        problem = _parse_line(line).problem
        if problem:
            problems.append(f"line {number}: {problem}")
    return problems


def _has_control(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" or ch in _EXTRA_CONTROL for ch in text)


def _assignment(key: str, value: str) -> str:
    # Single quotes make the value literal for Compose and the installer's
    # parser alike, so a value must not contain one. (The message never
    # includes the value: it may be the password.)
    if "'" in value or _has_control(value):
        raise ValueError(f"{key} contains a single quote or a control character")
    return f"{key}='{value}'"


def render_settings(existing: str, values: Mapping[str, str]) -> str:
    """Rewrite known keys in place, keep every other line, append missing keys."""
    lines = existing.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out, seen = [], set()
    for line in lines:
        key = _parse_line(line).key
        if key in values:        # a known key with broken quoting is rewritten too
            out.append(_assignment(key, str(values[key])))
            seen.add(key)
        else:
            out.append(line.rstrip("\r"))
    out.extend(_assignment(k, str(v)) for k, v in values.items() if k not in seen)
    return "\n".join(out) + "\n"


def write_private_file(path: Path, text: str) -> None:
    """Atomically replace `path` with `text`, mode 0600 from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file 0600, so the secret is never world-readable,
    # not even for the instant before a chmod.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    os.chmod(path, 0o600)


def read_text(path: Path) -> str:
    """The file as UTF-8 ('' when missing).

    Raises UnicodeDecodeError instead of replacing bytes: a rewrite would
    otherwise change a Latin-1 password behind the masked field.
    """
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""


def available_themes(theme_dir: Path) -> list[str]:
    try:
        return sorted(p.stem for p in theme_dir.glob("*.json"))
    except OSError:
        return []


# --------------------------------------------------------------------------
# Validation (kept identical to the installer's rules)
# --------------------------------------------------------------------------

_USER = re.compile(r"[A-Za-z0-9._-]{1,32}")
_PORT_TEXT = re.compile(r"[1-9][0-9]{0,4}")   # no sign, no leading zero (bash would read octal)


def password_problems(password: str) -> list[str]:
    problems = []
    if not 8 <= len(password) <= 128:
        problems.append("The password must be 8 to 128 characters long.")
    if "'" in password:
        problems.append("The password may not contain a single quote (').")
    if "\\" in password:
        problems.append("The password may not contain a backslash (\\).")
    if _has_control(password):
        problems.append("The password may not contain control characters such as tabs or line breaks.")
    if password != password.strip(SPACE_CHARS):
        problems.append("The password may not start or end with whitespace.")
    return problems


_BOOLEANS = {"1": "1", "true": "1", "yes": "1", "on": "1", "0": "0", "false": "0", "no": "0", "off": "0"}


def normalize_bool(value: str) -> str | None:
    """'1' or '0' for the spellings the installer accepts (1/0, true/false, yes/no, on/off)."""
    return _BOOLEANS.get(str(value).lower())


def check_port(value: object, label: str) -> tuple[int | None, str | None]:
    text = str(value).strip()
    if not _PORT_TEXT.fullmatch(text):
        return None, f"The {label} port must be a whole number from 1024 to 65535."
    port = int(text)
    if port < 1024:
        return None, f"The {label} port {port} is privileged; use 1024 to 65535."
    if port > 65535:
        return None, f"The {label} port {port} is out of range; use 1024 to 65535."
    return port, None


def validate_settings(values: Mapping[str, str], themes: Iterable[str] | None = None) -> list[str]:
    """Static checks of a settings mapping; returns human-readable problems."""
    errors = password_problems(values.get("JUPYTER_PASSWORD", ""))
    ports = {}
    for key, label in (("JUPYTER_PORT", "JupyterLab"), ("STATS_PORT", "statistics")):
        port, problem = check_port(values.get(key, ""), label)
        if problem:
            errors.append(problem)
        else:
            ports[key] = port
    # Different even when statistics are off: enabling them later must not
    # silently collide with JupyterLab.
    if len(ports) == 2 and ports["JUPYTER_PORT"] == ports["STATS_PORT"]:
        errors.append(f"JupyterLab and statistics need different ports (both are {ports['STATS_PORT']}).")
    if normalize_bool(values.get("STATS_ENABLED", "1")) is None:
        errors.append("STATS_ENABLED must be 1 or 0 (true/false, yes/no, on/off also work).")
    if not _USER.fullmatch(values.get("STATS_USER", "")):
        errors.append("STATS_USER must be 1 to 32 letters, digits, dots, underscores or hyphens.")
    theme_names = list(themes) if themes is not None else None
    if theme_names and values.get("THEME", "") not in theme_names:
        errors.append(f"THEME in the settings file must be one of: {', '.join(theme_names)}.")
    return errors


BindProbe = Callable[[str, int], None]


def probe_bind(ip: str, port: int) -> None:
    """Raise OSError when (ip, port) cannot be bound right now."""
    # Deliberately without SO_REUSEADDR, so any socket holding the port counts.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((ip, port))


def occupied_port_errors(ip: str, wanted: Iterable[tuple[str, int]],
                         published: Mapping[str, Iterable[int]] | None = None,
                         bind: BindProbe = probe_bind) -> list[str]:
    """Problems for wanted (service, port) pairs that are taken on the Tailscale address.

    `published` maps each of this project's services to the ports it
    publishes now. A service may keep its own port (an Update re-publishes
    it), but not take over another service's: Compose recreates one service
    at a time, so the old holder would still have the port.
    """
    names = dict(SERVICES)
    holders = {port: service for service, ports in (published or {}).items() for port in ports}
    errors = []
    for service, port in wanted:
        label = names.get(service, service)
        try:
            bind(ip, port)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                holder = holders.get(port)
                if holder is None:
                    errors.append(f"Port {port} ({label}) is already in use on {ip}.")
                elif holder != service:
                    errors.append(f"Port {port} ({label}) is still published by the {names.get(holder, holder)} "
                                  "container. Press Stop first, then Deploy.")
            elif exc.errno == errno.EADDRNOTAVAIL:
                return [f"The Tailscale address {ip} is not assigned to this machine."]
            else:
                errors.append(f"Port {port} ({label}) cannot be checked on {ip}: {exc.strerror or exc}.")
    return errors


# --------------------------------------------------------------------------
# Parsing command output
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TailscaleStatus:
    ip: str = ""
    problem: str = ""


_TAILSCALE_STATES = {
    "NeedsLogin": "Tailscale is logged out (run: sudo tailscale up)",
    "NeedsMachineAuth": "Tailscale is waiting for this machine to be approved",
    "Stopped": "Tailscale is stopped (run: tailscale up)",
    "Starting": "Tailscale is still starting",
    "NoState": "Tailscale has no state yet",
}


def _load_json(text: str) -> object:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        # Merged stderr can put a warning in front of the document.
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
    return None


def parse_tailscale_status(text: str) -> TailscaleStatus:
    data = _load_json(text)
    if not isinstance(data, dict) or "BackendState" not in data:
        return TailscaleStatus(problem="Tailscale status could not be read")
    state = str(data.get("BackendState") or "")
    if state != "Running":
        return TailscaleStatus(problem=_TAILSCALE_STATES.get(state, f"Tailscale is not connected ({state or 'unknown'})"))
    node = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    for address in node.get("TailscaleIPs") or []:
        try:
            ip = ipaddress.ip_address(str(address))
        except ValueError:
            continue
        if ip.version == 4 and ip in TAILNET:
            return TailscaleStatus(ip=str(ip))
    return TailscaleStatus(problem="Tailscale runs but has no IPv4 address in 100.64.0.0/10")


def parse_compose_ps(text: str, project: str = PROJECT) -> dict[str, dict]:
    """`docker compose ps -a --format json` (JSON Lines or an array) -> per-service info."""
    loaded = _load_json(text) if text.lstrip().startswith("[") else None
    if isinstance(loaded, list):
        records = loaded
    else:
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith(("{", "[")):    # skip warnings sharing the channel
                continue
            try:
                record = json.loads(line)
            except ValueError:                     # e.g. "[+] Running 2/2"
                continue
            records.extend(record if isinstance(record, list) else [record])
    services: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("Service"):
            continue
        if record.get("Project") not in (None, "", project):
            continue
        ports = set()
        for publisher in record.get("Publishers") or []:
            try:
                if int(publisher.get("PublishedPort") or 0) > 0:
                    ports.add(int(publisher["PublishedPort"]))
            except (AttributeError, TypeError, ValueError):
                continue
        info = {"state": str(record.get("State", "")).lower(),
                "health": str(record.get("Health", "")).lower(), "ports": ports}
        previous = services.get(record["Service"])
        if previous is None or previous["state"] != "running":
            services[record["Service"]] = info
    return services


def service_state(entry: Mapping | None, *, disabled: bool = False) -> tuple[str, str]:
    """(label, kind) for a service; kind is success, caution, warn or muted."""
    if entry is None:
        return ("Disabled", "muted") if disabled else ("Not deployed", "muted")
    state, health = entry.get("state", ""), entry.get("health", "")
    if state == "running":
        if health == "healthy":
            return "Running", "success"
        if health == "unhealthy":
            return "Unhealthy", "warn"
        return "Starting", "caution"
    if state == "restarting":
        return "Restarting", "warn"
    if state == "paused":
        return "Paused", "caution"
    return "Stopped", "muted"


_ROOT_STEP = re.compile(
    r"ROOT_STEP_REQUIRED: (host-setup 100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?: [1-9][0-9]{3,4}){1,2}|host-teardown)")


def root_step_from_line(line: str) -> list[str] | None:
    """Installer arguments for pkexec when `line` is a valid ROOT_STEP_REQUIRED line."""
    match = _ROOT_STEP.fullmatch(line.strip())
    if match is None:
        return None
    args = match.group(1).split(" ")
    if args[0] == "host-setup":
        try:
            if ipaddress.ip_address(args[1]) not in TAILNET:
                return None
        except ValueError:
            return None
        ports = args[2:]
        if not all(1024 <= int(port) <= 65535 for port in ports) or len(set(ports)) != len(ports):
            return None
    return args


def page_url(runtime: Mapping[str, str], page: Page) -> str:
    """URL of a page from the deployed runtime .env, or '' when it is not usable."""
    ip, port = runtime.get("TS_IP", ""), runtime.get(page.port_key, "")
    try:
        if ipaddress.ip_address(ip) not in TAILNET:
            return ""
    except ValueError:
        return ""
    if check_port(port, page.label)[1]:
        return ""
    return f"http://{ip}:{int(port)}{page.path}"


def service_url(runtime: Mapping[str, str], service: str) -> str:
    return next((page_url(runtime, p) for p in PAGES if p.service == service), "")


def mask_secrets(text: str, secrets: Iterable[str]) -> str:
    # Very short strings are skipped: masking every "a" would wreck the log,
    # and a valid password is at least 8 characters anyway.
    for secret in sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


# --------------------------------------------------------------------------
# Theme: zenobia's oya tokens (stack/theme/<THEME>.json) mapped onto Qt
# --------------------------------------------------------------------------

# amazing.json's colours, used when the theme file is missing or incomplete.
BUILTIN_COLORS = {
    "primary-bg-light": "#f2f3f5", "header-bg-light": "#1d2333", "appbar-bg-light": "#252c3e",
    "appbar-text-light": "#e4e7ef", "bubble-bg-light": "#e8e9ec", "footer-bg-light": "#1d2333",
    "footer-text-light": "#e4e7ef", "text-main-light": "#0f1114", "accent-light": "#2f3d63",
    "warn-light": "#d94a4a", "success-light": "#4a8f7a", "sunken-light": "#e1e2e5",
    "link-light": "#2a4b8f", "primary-bg-dark": "#0a0c11", "header-bg-dark": "#0f1420",
    "appbar-bg-dark": "#151b29", "appbar-text-dark": "#cfd4df", "bubble-bg-dark": "#191f2d",
    "footer-bg-dark": "#0f1420", "footer-text-dark": "#cfd4df", "text-main-dark": "#d3d7e0",
    "accent-dark": "#4f5fa1", "warn-dark": "#ff4455", "caution-light": "#a07800",
    "caution-dark": "#f0a820", "success-dark": "#5fa38c", "sunken-dark": "#141821",
    "link-dark": "#5f7fc9", "accent-1": "#4f5fa1", "accent-2": "#2f3d63",
}

TOKEN_NAMES = (
    "window_bg", "card_bg", "card_border", "input_bg", "text", "muted", "accent", "on_accent",
    "header_bg", "header_text", "success", "caution", "warn", "log_bg", "link",
    # derived
    "accent_text", "accent_hover", "input_border", "divider", "button_hover", "disabled_text",
    "disabled_bg", "success_bg", "caution_bg", "warn_bg", "accent_bg", "header_muted",
    "header_ok", "header_warn", "header_caution", "scroll_handle",
)

_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}")
_THEME_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def mix(a: str, b: str, amount: float) -> str:
    """`a` moved `amount` (0..1) of the way towards `b`."""
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * amount) for x, y in zip(_rgb(a), _rgb(b)))


def contrast(a: str, b: str) -> float:
    """WCAG 2.1 contrast ratio."""
    def luminance(color: str) -> float:
        channels = [c / 255 for c in _rgb(color)]
        r, g, b_ = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def load_theme_colors(theme_dir: Path, name: str) -> dict[str, str]:
    colors = dict(BUILTIN_COLORS)
    if not _THEME_NAME.fullmatch(name or ""):
        return colors
    try:
        data = json.loads((theme_dir / f"{name}.json").read_text(encoding="utf-8"))
        loaded = data.get("colors", {})
        colors.update({k: v for k, v in loaded.items() if isinstance(v, str) and _HEX.fullmatch(v)})
    except (OSError, ValueError, AttributeError):
        pass
    return colors


def theme_tokens(colors: Mapping[str, str], mode: str) -> dict[str, str]:
    """Qt colour tokens for 'light' or 'dark' from an oya colour table."""
    c = {**BUILTIN_COLORS, **colors}
    m = "dark" if mode == "dark" else "light"
    window_bg, card_bg = c[f"primary-bg-{m}"], c[f"bubble-bg-{m}"]
    text, accent, link = c[f"text-main-{m}"], c[f"accent-{m}"], c[f"link-{m}"]
    header_bg, header_text = c[f"appbar-bg-{m}"], c[f"appbar-text-{m}"]
    success, warn = c[f"success-{m}"], c[f"warn-{m}"]
    caution = colors.get(f"caution-{m}") or ("#f0a820" if m == "dark" else "#a07800")
    # oya borders cards with accent-2 in light mode and accent-1 in dark mode.
    card_border = c["accent-1"] if m == "dark" else c["accent-2"]

    def best(options: Iterable[str], background: str) -> str:
        return max(options, key=lambda color: contrast(color, background))

    return {
        "window_bg": window_bg, "card_bg": card_bg, "card_border": card_border,
        "input_bg": window_bg, "text": text, "muted": mix(text, window_bg, 0.40),
        "accent": accent, "on_accent": best((window_bg, "#ffffff"), accent),
        "header_bg": header_bg, "header_text": header_text,
        "success": success, "caution": caution, "warn": warn,
        "log_bg": c[f"sunken-{m}"], "link": link,
        # Small accent text: oya's dark accent is only ~2.7:1 on cards, so the
        # (brighter) link colour takes over where it reads better.
        "accent_text": best((accent, link), card_bg),
        "accent_hover": mix(accent, "#ffffff", 0.12),
        "input_border": mix(card_border, card_bg, 0.45),
        "divider": mix(card_border, card_bg, 0.65),
        "button_hover": mix(card_bg, text, 0.08),
        "disabled_text": mix(text, card_bg, 0.60),
        "disabled_bg": mix(card_bg, window_bg, 0.50),
        "success_bg": mix(card_bg, success, 0.15),
        "caution_bg": mix(card_bg, caution, 0.15),
        "warn_bg": mix(card_bg, warn, 0.15),
        "accent_bg": mix(card_bg, accent, 0.12),
        "header_muted": mix(header_text, header_bg, 0.32),
        "header_ok": best((c["success-light"], c["success-dark"]), header_bg),
        "header_warn": best((c["warn-light"], c["warn-dark"]), header_bg),
        "header_caution": best((c["caution-light"], c["caution-dark"]), header_bg),
        "scroll_handle": mix(text, card_bg, 0.70),
    }


QSS_TEMPLATE = """
QMainWindow, QWidget#root, QScrollArea#page { background: $window_bg; border: none; }
QWidget { color: $text; font-size: 14px; }
QLabel, QCheckBox { background: transparent; }
QToolTip { background: $card_bg; color: $text; border: 1px solid $card_border; padding: 4px 8px; }

QFrame#header { background: $header_bg; border: none; }
QLabel#title { color: $header_text; font-size: 22px; font-weight: 700; $display_font }
QLabel#headerEyebrow { color: $header_muted; font-size: 10px; font-weight: 700; letter-spacing: 2px; $display_font }
QLabel#headerMeta { color: $header_muted; font-size: 13px; }
QLabel#headerMeta[state="ok"] { color: $header_text; }
QLabel#headerMeta[state="problem"] { color: $header_warn; }
QLabel#headerDot { border-radius: 4px; background: $header_muted; }
QLabel#headerDot[state="ok"] { background: $header_ok; }
QLabel#headerDot[state="problem"] { background: $header_warn; }
QLabel#headerDot[state="pending"] { background: $header_caution; }

QFrame#card { background: $card_bg; border: 1px solid $card_border; border-radius: 16px; }
QFrame#divider { background: $divider; border: none; min-height: 1px; max-height: 1px; }
QLabel#eyebrow { color: $accent_text; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; $display_font }
QLabel#fieldLabel { color: $muted; font-size: 13px; font-weight: 600; }
QLabel#hint { color: $muted; font-size: 12px; }
QLabel#errorText { color: $warn; font-size: 13px; font-weight: 600; }
QLabel#serviceName { font-size: 15px; font-weight: 700; }
QLabel#url { color: $link; font-size: 13px; }
QLabel#url[kind="muted"] { color: $muted; }

QLineEdit, QSpinBox {
    background: $input_bg; color: $text; border: 1px solid $input_border; border-radius: 10px;
    padding: 7px 10px; selection-background-color: $accent; selection-color: $on_accent;
}
QLineEdit { lineedit-password-character: 8226; }
QLineEdit:focus, QSpinBox:focus { border: 1px solid $accent_text; }
QLineEdit:disabled, QSpinBox:disabled { color: $disabled_text; background: $disabled_bg; border: 1px solid $divider; }

QCheckBox { spacing: 10px; padding: 2px 0; }
QCheckBox:disabled { color: $disabled_text; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px; border: 1px solid $input_border; background: $input_bg; }
QCheckBox::indicator:hover { border: 1px solid $accent_text; }
QCheckBox::indicator:checked { background: $accent; border: 1px solid $accent; }
QCheckBox::indicator:disabled { background: $disabled_bg; border: 1px solid $divider; }
QCheckBox::indicator:checked:disabled { background: $disabled_text; border: 1px solid $disabled_text; }

QPushButton {
    background: transparent; color: $text; border: 1px solid $input_border; border-radius: 10px;
    padding: 8px 18px; font-weight: 600;
}
QPushButton:hover { background: $button_hover; border: 1px solid $card_border; }
QPushButton:pressed { background: $accent_bg; }
QPushButton:disabled { color: $disabled_text; background: transparent; border: 1px solid $divider; }
QPushButton#primaryButton { background: $accent; color: $on_accent; border: 1px solid $accent; padding: 8px 26px; }
QPushButton#primaryButton:hover { background: $accent_hover; border: 1px solid $accent_hover; }
QPushButton#primaryButton:disabled { background: $disabled_bg; color: $disabled_text; border: 1px solid $divider; }
QPushButton#smallButton { padding: 5px 14px; font-size: 13px; }
QPushButton#linkButton { background: transparent; border: none; color: $accent_text; padding: 6px 8px; }
QPushButton#linkButton:hover { color: $text; }
QPushButton#linkButton:disabled { color: $disabled_text; }

QLabel#dot { border-radius: 5px; background: $disabled_text; }
QLabel#dot[kind="success"] { background: $success; }
QLabel#dot[kind="caution"] { background: $caution; }
QLabel#dot[kind="warn"] { background: $warn; }
QLabel#badge {
    border-radius: 9px; padding: 3px 10px; font-size: 12px; font-weight: 700;
    color: $muted; background: $log_bg; border: 1px solid $divider;
}
QLabel#badge[kind="success"] { color: $success; background: $success_bg; border: 1px solid $success; }
QLabel#badge[kind="caution"] { color: $caution; background: $caution_bg; border: 1px solid $caution; }
QLabel#badge[kind="warn"] { color: $warn; background: $warn_bg; border: 1px solid $warn; }

QLabel#banner {
    border-radius: 10px; padding: 8px 14px; font-size: 13px; font-weight: 600;
    color: $text; background: $accent_bg; border: 1px solid $divider;
}
QLabel#banner[kind="success"] { color: $success; background: $success_bg; border: 1px solid $success; }
QLabel#banner[kind="error"] { color: $warn; background: $warn_bg; border: 1px solid $warn; }
QLabel#banner[kind="busy"] { color: $caution; background: $caution_bg; border: 1px solid $caution; }

QPlainTextEdit#log {
    background: $log_bg; color: $text; border: 1px solid $divider; border-radius: 12px; padding: 8px;
    font-family: "$mono_family"; font-size: 12px;
    selection-background-color: $accent; selection-color: $on_accent;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px 4px 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0 4px 2px 4px; }
QScrollBar::handle:vertical { background: $scroll_handle; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:horizontal { background: $scroll_handle; border-radius: 4px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


def build_stylesheet(tokens: Mapping[str, str], display_family: str = "", mono_family: str = "monospace") -> str:
    # substitute() raises KeyError on a missing token, which the tests rely on.
    display = f'font-family: "{display_family}";' if display_family else ""
    return string.Template(QSS_TEMPLATE).substitute(tokens, display_font=display, mono_family=mono_family)


def build_palette(tokens: Mapping[str, str]) -> QPalette:
    """Fusion paints some parts (placeholders, selections, dialogs) from the palette."""
    role = QPalette.ColorRole
    palette = QPalette()
    for r, key in ((role.Window, "window_bg"), (role.WindowText, "text"), (role.Base, "input_bg"),
                   (role.AlternateBase, "card_bg"), (role.Text, "text"), (role.Button, "card_bg"),
                   (role.ButtonText, "text"), (role.PlaceholderText, "muted"), (role.Highlight, "accent"),
                   (role.HighlightedText, "on_accent"), (role.ToolTipBase, "card_bg"),
                   (role.ToolTipText, "text"), (role.Link, "link"), (role.BrightText, "warn")):
        palette.setColor(r, QColor(tokens[key]))
    for r in (role.Text, role.WindowText, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, r, QColor(tokens["disabled_text"]))
    return palette


_display_families: dict[str, str] = {}


def load_display_family(font_file: Path) -> str:
    """Register Orbitron once; '' when the file is missing or Qt cannot read it."""
    key = str(font_file)
    if key not in _display_families:
        family = ""
        if font_file.is_file():
            font_id = QFontDatabase.addApplicationFont(key)
            families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            family = families[0] if families else ""
        _display_families[key] = family
    return _display_families[key]


# --------------------------------------------------------------------------
# Running commands
# --------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def clean_line(text: str) -> str:
    """Strip ANSI sequences and keep only the last state of a '\\r' progress line."""
    text = text.rstrip("\r\n")
    if "\r" in text:
        text = text.rsplit("\r", 1)[-1]
    return _ANSI.sub("", text)


def child_environment(base: Mapping[str, str], secrets: Iterable[str] = (),
                      overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for child processes. It never carries the password."""
    env = dict(base)
    env.update(CHILD_ENV_EXTRA)
    env.update(overrides or {})
    env.pop("JUPYTER_PASSWORD", None)
    # The installer checks the password with bash, which counts bytes and
    # classifies only ASCII in the C locale. A UTF-8 locale makes its rules
    # the ones password_problems() mirrors.
    ctype = (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or "").lower()
    if "utf-8" not in ctype and "utf8" not in ctype:
        env["LC_ALL"] = "C.UTF-8"
    secrets = [s for s in secrets if len(s) >= 4]
    return {k: v for k, v in env.items() if not any(s in v or s in k for s in secrets)}


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    outcome: str               # ok | exit (non-zero) | crashed | failed (never started) | cancelled | timeout
    code: int
    error: str = ""
    lines: tuple[str, ...] = ()  # only filled when the Runner captures output


class Runner(QObject):
    """One command at a time through QProcess: argv only, never a shell."""

    output = pyqtSignal(list)    # cleaned lines, in batches as they arrive
    done = pyqtSignal(object)    # RunResult

    def __init__(self, parent: QObject | None = None, *, echo: bool = True, capture: bool = False,
                 kill_grace_ms: int = KILL_GRACE_MS) -> None:
        super().__init__(parent)
        self.echo, self.capture, self.kill_grace_ms = echo, capture, kill_grace_ms
        self._proc: QProcess | None = None
        self._argv: tuple[str, ...] = ()
        self._lines: list[str] = []
        self._pending = ""           # the unfinished last line, at most MAX_LINE_CHARS
        self._truncated = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._cancelled = self._timed_out = False
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._kill)
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)

    def is_running(self) -> bool:
        return self._proc is not None

    def start(self, argv: Iterable[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
              timeout_ms: int = 0) -> None:
        """Run argv; with timeout_ms, cancel it after that long (outcome 'timeout')."""
        if self._proc is not None:
            raise RuntimeError("the runner is already running a command")
        argv = [str(arg) for arg in argv]
        env = child_environment(os.environ) if env is None else dict(env)
        self._argv, self._lines, self._pending, self._truncated = tuple(argv), [], "", False
        self._cancelled = self._timed_out = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        # Resolve the program with the child's PATH. Left to itself QProcess
        # would search the builder's own PATH, which may differ.
        program = argv[0] if "/" in argv[0] else shutil.which(argv[0], path=env.get("PATH", os.defpath))
        if program is None:
            if self.echo:
                self.output.emit(["$ " + shlex.join(argv)])
            self.done.emit(RunResult(self._argv, "failed", -1, f"{argv[0]}: command not found"))
            return
        proc = QProcess(self)    # parented, so it is never collected mid-run
        proc.setProgram(program)
        proc.setArguments(argv[1:])
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        # No stdin: a prompt (sudo, docker login) fails instead of hanging a GUI.
        proc.setStandardInputFile(QProcess.nullDevice())
        if hasattr(QProcess, "UnixProcessFlag") and hasattr(QProcess.UnixProcessFlag, "CreateNewSession"):
            # Own session and process group: cancel can signal the whole tree,
            # and without a controlling terminal sudo cannot prompt either.
            proc.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
        environment = QProcessEnvironment()
        for key, value in env.items():
            environment.insert(key, value)
        proc.setProcessEnvironment(environment)
        if cwd:
            proc.setWorkingDirectory(cwd)
        proc.readyReadStandardOutput.connect(self._drain)
        proc.errorOccurred.connect(self._on_error)
        proc.finished.connect(self._on_finished)
        self._proc = proc
        if self.echo:
            self.output.emit(["$ " + shlex.join(argv)])
        if timeout_ms > 0:
            self._timeout_timer.start(timeout_ms)
        proc.start()

    def cancel(self) -> None:
        """SIGTERM the process group now, SIGKILL it after the grace period."""
        proc = self._proc
        if proc is None or self._cancelled:
            return
        self._cancelled = True
        if proc.state() != QProcess.ProcessState.NotRunning:
            self._signal(proc, signal.SIGTERM)
            self._kill_timer.start(self.kill_grace_ms)

    def terminate_and_wait(self) -> bool:
        """Cancel and block until the process is gone (used when closing)."""
        proc = self._proc
        if proc is None:
            return True
        self.cancel()
        # The QTimer cannot fire while we block, so escalate by hand.
        if proc.waitForFinished(self.kill_grace_ms):
            return True
        if self._proc is proc:
            self._signal(proc, signal.SIGKILL)
        return proc.waitForFinished(3000)

    def _on_timeout(self) -> None:
        if self._proc is not None and not self._cancelled:
            self._timed_out = True
            self.cancel()

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            self._signal(proc, signal.SIGKILL)

    @staticmethod
    def _signal(proc: QProcess, sig: int) -> None:
        pid = int(proc.processId())
        if pid > 0:     # never killpg(0): that would be our own group
            try:
                os.killpg(pid, sig)   # CreateNewSession: the group id is the pid
                return
            except (ProcessLookupError, PermissionError):
                pass
        (proc.kill if sig == signal.SIGKILL else proc.terminate)()

    def _drain(self, final: bool = False) -> None:
        proc = self._proc
        if proc is None:
            return
        data = self._decoder.decode(bytes(proc.readAllStandardOutput()), final)
        # Only the new data is split, and the unfinished line is capped, so a
        # flood of output (or one endless line) costs linear time.
        lines = []
        for index, piece in enumerate(data.split("\n")):
            if index:
                lines.append(self._take_pending())
            self._add_pending(piece)
        if final and self._pending:
            lines.append(self._take_pending())
        if lines:
            if self.capture:
                self._lines.extend(lines)
            self.output.emit(lines)

    def _add_pending(self, piece: str) -> None:
        text = self._pending + piece
        if "\r" in piece:
            # A carriage return redraws the line (progress output): keep only
            # the latest state. A trailing '\r' may be half of a CRLF, so it stays.
            cut = text.rstrip("\r").rfind("\r")
            if cut >= 0:
                text, self._truncated = text[cut + 1:], False
        if len(text) > MAX_LINE_CHARS:
            text, self._truncated = text[:MAX_LINE_CHARS], True
        self._pending = text

    def _take_pending(self) -> str:
        line = clean_line(self._pending) + (" […line cut]" if self._truncated else "")
        self._pending, self._truncated = "", False
        return line

    def _on_error(self, error: QProcess.ProcessError) -> None:
        # finished() does not follow a failed start; every other error does.
        if error == QProcess.ProcessError.FailedToStart and self._proc is not None:
            self._complete("failed", -1, self._proc.errorString())

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        if self._proc is None:
            return
        self._drain(final=True)
        if self._timed_out:
            outcome = "timeout"
        elif self._cancelled:
            outcome = "cancelled"
        elif status == QProcess.ExitStatus.CrashExit:
            outcome = "crashed"
        else:
            outcome = "ok" if code == 0 else "exit"
        self._complete(outcome, code)

    def _complete(self, outcome: str, code: int, error: str = "") -> None:
        proc, self._proc = self._proc, None   # cleared first: a done() slot may start the next command
        self._kill_timer.stop()
        self._timeout_timer.stop()
        if proc is not None:
            proc.deleteLater()
        self.done.emit(RunResult(self._argv, outcome, int(code), error, tuple(self._lines)))


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class PortSpinBox(QSpinBox):
    """A port field that the mouse wheel changes only once it has focus.

    Otherwise scrolling the page over the field would edit a port.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)     # the wheel does not grab focus
        # The whole port range, not 1024+: a narrower range makes Qt silently put
        # back the previous value when someone types 80, and Deploy would then
        # ship a port nobody asked for. validate_settings rejects it with a message.
        self.setRange(1, 65535)
        self.setGroupSeparatorShown(False)    # 8888, not 8,888
        self.setKeyboardTracking(False)       # no clamping or validation mid-typing
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setMinimumWidth(120)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()                    # let the scroll area have it


@dataclass
class ServiceRow:
    dot: QLabel
    badge: QLabel
    url: QLabel
    open_button: QPushButton


def _repolish(widget: QWidget, **properties: str) -> None:
    """Set dynamic properties; Qt only re-reads the stylesheet after unpolish/polish."""
    for name, value in properties.items():
        widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


BUSY_TEXT = {
    "install": "Deploying: building images and starting containers…",
    "start": "Starting containers…",
    "restart": "Restarting containers…",
    "stop": "Stopping containers…",
}
JOB_TITLES = {"install": "Deploy", "start": "Start", "restart": "Restart", "stop": "Stop"}


class MainWindow(QMainWindow):
    def __init__(self, paths: Paths | None = None, *, environ: Mapping[str, str] | None = None,
                 bind: BindProbe = probe_bind, open_url: Callable[[QUrl], bool] = QDesktopServices.openUrl,
                 kill_grace_ms: int = KILL_GRACE_MS, poll: bool = True) -> None:
        super().__init__()
        self._environ = dict(os.environ if environ is None else environ)
        self.paths = paths or Paths.from_environment(self._environ)
        self._bind = bind
        self._open_url = open_url
        self._bash = shutil.which("bash", path=self._environ.get("PATH", os.defpath)) or "/bin/bash"
        self.themes = available_themes(self.paths.theme_dir)
        self.ts = TailscaleStatus()                 # neither ip nor problem: not checked yet
        self.docker_problem: str | None = None      # None: not checked yet, '': reachable
        self.services: dict[str, dict] = {}
        self.runtime = self._read_runtime()
        self.tokens: dict[str, str] = {}
        self.mode = "light"
        self._busy = False
        self._closing = False
        self._job = ""               # install | start | restart | stop | pkexec
        self._command = ""           # the installer command a pkexec step belongs to
        self._root_step: list[str] | None = None
        self._tail: list[str] = []   # last output lines, for failure messages
        self._after_tailscale: Callable[[], None] | None = None
        self._after_status: Callable[[], None] | None = None
        self._status_again = False
        self._log_queue: list[str] = []
        self._log_timer = QTimer(self)
        self._log_timer.setSingleShot(True)
        self._log_timer.setInterval(LOG_FLUSH_MS)
        self._log_timer.timeout.connect(self._flush_log)

        self.settings_values, load_notes = self._load_settings()
        self._secrets = {self.settings_values["JUPYTER_PASSWORD"]}

        self.job_runner = Runner(self, kill_grace_ms=kill_grace_ms)
        self.job_runner.output.connect(self._on_job_output)
        self.job_runner.done.connect(self._on_job_done)
        self.status_runner = Runner(self, echo=False, capture=True, kill_grace_ms=2000)
        self.status_runner.done.connect(self._on_status_done)
        self.tailscale_runner = Runner(self, echo=False, capture=True, kill_grace_ms=2000)
        self.tailscale_runner.done.connect(self._on_tailscale_done)

        self._display_family = load_display_family(self.paths.display_font)
        self._build_ui()
        self._fill_form(self.settings_values)
        self.apply_theme()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_color_scheme_changed)

        self.status_timer = QTimer(self)
        self.status_timer.setInterval(STATUS_INTERVAL_MS)
        self.status_timer.timeout.connect(self._poll_status)
        self.tailscale_timer = QTimer(self)
        self.tailscale_timer.setInterval(TAILSCALE_INTERVAL_MS)
        self.tailscale_timer.timeout.connect(self._poll_tailscale)

        self._refresh_view()
        if load_notes:
            self._banner("error", " ".join(load_notes))
        else:
            self._banner("info", "Ready. Deploy saves the settings, builds the images and starts the containers.")
        if poll:
            self.status_timer.start()
            self.tailscale_timer.start()
            self.refresh_status()
            self.refresh_tailscale()

    # -- construction -------------------------------------------------------

    def _load_settings(self) -> tuple[dict[str, str], list[str]]:
        name = self.paths.settings.name
        try:
            text = read_text(self.paths.settings)
        except OSError as exc:
            return dict(DEFAULTS), [f"Could not read {self.paths.settings}: {exc.strerror or exc}."]
        except UnicodeDecodeError:
            return dict(DEFAULTS), [f"{self.paths.settings} is not UTF-8 text, so the form shows the defaults and "
                                    "Deploy will not rewrite it. Convert it to UTF-8 (or delete it) and restart the builder."]
        values = dict(DEFAULTS)
        values.update({k: v for k, v in parse_settings(text).items() if k in DEFAULTS})
        notes = [f"{name} {problem}; the installer rejects this line." for problem in settings_line_problems(text)]
        # The form cannot show an invalid port or switch; say so instead of
        # silently replacing it.
        for key in ("JUPYTER_PORT", "STATS_PORT"):
            if check_port(values[key], key)[1]:
                notes.append(f"{key} in {name} is not a valid port; the form shows {DEFAULTS[key]}.")
                values[key] = DEFAULTS[key]
        enabled = normalize_bool(values["STATS_ENABLED"])
        if enabled is None:
            # Off is the safe reading: it opens no port the user did not clearly ask for.
            notes.append(f"STATS_ENABLED in {name} is not 1/0, true/false, yes/no or on/off; the form shows statistics off.")
        values["STATS_ENABLED"] = enabled or "0"
        return values, notes

    @staticmethod
    def _label(text: str = "", name: str = "", *, wrap: bool = False, selectable: bool = False) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(wrap)
        if selectable:
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _button(text: str, name: str = "", slot: Callable | None = None, tooltip: str = "") -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        if slot is not None:
            button.clicked.connect(slot)
        return button

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(self._label(title.upper(), "eyebrow"))
        return card, layout

    def _build_ui(self) -> None:
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(640, 480)
        self.resize(900, 820)
        page = QWidget()
        page.setObjectName("root")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        body = QVBoxLayout()
        body.setContentsMargins(20, 18, 20, 18)
        body.setSpacing(14)
        outer.addLayout(body, 1)
        # Side by side on a wide window, stacked below ~860 px (see resizeEvent).
        self.cards_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.cards_layout.setSpacing(14)
        self.cards_layout.addWidget(self._build_settings_card(), 1)
        self.cards_layout.addWidget(self._build_services_card(), 1)
        body.addLayout(self.cards_layout)
        body.addLayout(self._build_actions())
        body.addWidget(self._build_output_card(), 1)
        # A short screen scrolls the page instead of squeezing the inputs.
        scroll = QScrollArea()
        scroll.setObjectName("page")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self.setCentralWidget(scroll)

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("header")
        layout = QVBoxLayout(header)
        layout.setContentsMargins(24, 14, 24, 14)
        layout.setSpacing(2)
        layout.addWidget(self._label("DOCKER · TAILSCALE · BUILDER", "headerEyebrow"))
        layout.addWidget(self._label(APP_NAME, "title"))
        meta = QHBoxLayout()
        meta.setContentsMargins(0, 6, 0, 0)
        meta.setSpacing(8)
        self.tailscale_dot, self.docker_dot = self._label(name="headerDot"), self._label(name="headerDot")
        self.tailscale_label = self._label(name="headerMeta", selectable=True)
        self.docker_label = self._label(name="headerMeta", selectable=True)
        for dot, label, stretch in ((self.tailscale_dot, self.tailscale_label, 0),
                                    (self.docker_dot, self.docker_label, 1)):
            dot.setFixedSize(8, 8)
            # A long problem text is clipped rather than widening the window;
            # the tooltip carries the full text.
            label.setMinimumWidth(40)
            meta.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
            meta.addWidget(label, stretch)
            meta.addSpacing(12)
        layout.addLayout(meta)
        return header

    def _build_settings_card(self) -> QFrame:
        card, layout = self._card("Settings")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("8 to 128 characters")
        self.password_toggle = self._button("Show", "smallButton", tooltip="Show or hide the password")
        self.password_toggle.setCheckable(True)
        self.password_toggle.toggled.connect(self._toggle_password)
        self.jupyter_port = PortSpinBox()
        self.stats_check = QCheckBox("Enable FastAPI statistics")
        self.stats_port = PortSpinBox()
        self.stats_user_label = self._label(name="hint")
        self.stats_user_label.setToolTip("The dashboard asks for this username and the password above (HTTP Basic).")

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        rows = (("Password", (self.password_edit, 1), self.password_toggle),
                ("JupyterLab port", self.jupyter_port, 1),
                ("Statistics", self.stats_check, 1),
                ("Statistics port", self.stats_port, self.stats_user_label, 1))
        for index, (caption, *items) in enumerate(rows):
            grid.addWidget(self._label(caption, "fieldLabel"), index, 0)
            row = QHBoxLayout()
            row.setSpacing(10)
            for item in items:     # widget, (widget, stretch) or a trailing stretch
                if isinstance(item, int):
                    row.addStretch(item)
                else:
                    row.addWidget(*(item if isinstance(item, tuple) else (item,)))
            grid.addLayout(row, index, 1)
        layout.addLayout(grid)

        self.error_label = self._label(name="errorText", wrap=True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        layout.addStretch(1)
        for changed in (self.password_edit.textChanged, self.jupyter_port.valueChanged,
                        self.stats_port.valueChanged, self.stats_check.toggled):
            changed.connect(self._on_form_changed)
        return card

    def _build_services_card(self) -> QFrame:
        card, layout = self._card("Services")
        self.rows: dict[str, ServiceRow] = {}
        self.page_buttons: dict[Page, QPushButton] = {}
        for index, (service, name) in enumerate(SERVICES):
            if index:
                divider = QFrame()
                divider.setObjectName("divider")
                layout.addWidget(divider)
            row = ServiceRow(self._label(name="dot"), self._label(name="badge"),
                             self._label(name="url", selectable=True),
                             self._button("Open", "smallButton", partial(self.open_page, service),
                                          f"Open {name} in the browser"))
            row.dot.setFixedSize(10, 10)
            text = QVBoxLayout()
            text.setSpacing(2)
            text.addWidget(self._label(name, "serviceName"))
            text.addWidget(row.url)
            line = QHBoxLayout()
            line.setSpacing(12)
            line.addWidget(row.dot, 0, Qt.AlignmentFlag.AlignVCenter)
            line.addLayout(text, 1)
            line.addWidget(row.badge, 0, Qt.AlignmentFlag.AlignVCenter)
            line.addWidget(row.open_button, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addLayout(line)
            self.rows[service] = row
            # The service's other pages, as links under its row (indented past the dot).
            extra = [page for page in PAGES if page.service == service][1:]
            if extra:
                links = QHBoxLayout()
                links.setContentsMargins(16, 0, 0, 0)
                links.setSpacing(0)
                for page in extra:
                    button = self._button(page.label, "linkButton", partial(self.open_link, page),
                                          f"Open {name} · {page.label} in the browser")
                    links.addWidget(button)
                    self.page_buttons[page] = button
                links.addStretch(1)
                layout.addLayout(links)
        layout.addStretch(1)
        self.services_hint = self._label(
            "State refreshes every 4 seconds. Open uses the deployed Tailscale address.", "hint", wrap=True)
        layout.addWidget(self.services_hint)
        return card

    def _build_actions(self) -> QHBoxLayout:
        self.banner = self._label(name="banner", wrap=True, selectable=True)
        self.banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.stop_button = self._button("Stop", "", self.stop, "docker compose stop: containers and data are kept")
        self.start_button = self._button("Start", "", self.start_or_restart,
                                         "Start the containers, or restart them when they run")
        self.deploy_button = self._button("Deploy", "primaryButton", self.deploy,
                                          "Save the settings, build the images and (re)create the containers")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.banner, 1)
        for button in (self.stop_button, self.start_button, self.deploy_button):
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _build_output_card(self) -> QFrame:
        card, layout = self._card("Output")
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setPlaceholderText("Output of Deploy, Start/Restart and Stop appears here.")
        self.log_view.setMinimumHeight(80)
        layout.addWidget(self.log_view, 1)
        return card

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        wide = self.width() >= 860
        direction = QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        if self.cards_layout.direction() != direction:
            self.cards_layout.setDirection(direction)
        self.services_hint.setVisible(wide)     # stacked cards need the height
        super().resizeEvent(event)

    # -- theme ----------------------------------------------------------------

    def apply_theme(self, mode: str | None = None) -> None:
        """Apply the oya theme; the mode follows the OS unless given."""
        if mode is None:
            dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
            mode = "dark" if dark else "light"
        self.mode = mode
        colors = load_theme_colors(self.paths.theme_dir, self.settings_values.get("THEME", "amazing"))
        self.tokens = theme_tokens(colors, mode)
        palette = build_palette(self.tokens)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(palette)
        self.setPalette(palette)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        self.setStyleSheet(build_stylesheet(self.tokens, self._display_family, mono))

    def _on_color_scheme_changed(self, _scheme: object) -> None:
        if not self._closing:
            self.apply_theme()

    # -- form -----------------------------------------------------------------

    def _fill_form(self, values: Mapping[str, str]) -> None:
        self.password_edit.setText(values["JUPYTER_PASSWORD"])
        self.password_edit.setCursorPosition(0)     # show the start, not a scrolled-off first dot
        self.jupyter_port.setValue(int(values["JUPYTER_PORT"]))
        self.stats_port.setValue(int(values["STATS_PORT"]))
        self.stats_check.setChecked(normalize_bool(values.get("STATS_ENABLED", "1")) == "1")
        user = values.get("STATS_USER") or "jupyter"
        self.stats_user_label.setText(f"Username: {user}")

    def form_values(self, *, commit: bool = False) -> dict[str, str]:
        """The settings the form shows. Only Deploy commits digits typed without Enter.

        Committing on a status poll would snap a half-typed port ("12" on the
        way to 12000) back to the old value under the user's fingers.
        """
        if commit:
            for spin in (self.jupyter_port, self.stats_port):
                spin.interpretText()
        values = dict(self.settings_values)    # keeps STATS_USER and THEME
        values.update(
            JUPYTER_PASSWORD=self.password_edit.text(),
            JUPYTER_PORT=str(self.jupyter_port.value()),
            STATS_ENABLED="1" if self.stats_check.isChecked() else "0",
            STATS_PORT=str(self.stats_port.value()),
        )
        return values

    def _toggle_password(self, shown: bool) -> None:
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.password_toggle.setText("Hide" if shown else "Show")

    def _on_form_changed(self, *_args: object) -> None:
        self._validate_live()
        self._refresh_view()

    def published_ports(self) -> dict[str, set[int]]:
        """Ports each of this project's services publishes, from the last `compose ps`."""
        return {service: set(entry["ports"]) for service, entry in self.services.items()}

    def current_errors(self) -> list[str]:
        """Static validation plus port occupancy once Tailscale and Docker have answered."""
        values = self.form_values()
        errors = validate_settings(values, self.themes or None)
        # Before the first `compose ps` the project's own containers would look
        # like strangers holding the ports.
        ready = self.ts.ip and self.docker_problem is not None
        if ready and not any(check_port(values[k], k)[1] for k in ("JUPYTER_PORT", "STATS_PORT")):
            wanted = [("jupyterlab", int(values["JUPYTER_PORT"]))]
            if values["STATS_ENABLED"] == "1" and values["STATS_PORT"] != values["JUPYTER_PORT"]:
                wanted.append(("stats", int(values["STATS_PORT"])))
            errors += occupied_port_errors(self.ts.ip, wanted, self.published_ports(), bind=self._bind)
        return errors

    def _validate_live(self) -> list[str]:
        errors = self.current_errors()
        self.error_label.setText("\n".join(errors))
        self.error_label.setVisible(bool(errors))
        return errors

    # -- view -----------------------------------------------------------------

    def _refresh_view(self) -> None:
        self._refresh_header()
        self._refresh_services()
        self._refresh_controls()

    def _refresh_header(self) -> None:
        if self.ts.ip:
            ts = ("ok", f"Tailscale {self.ts.ip}")
        else:
            ts = ("problem", self.ts.problem) if self.ts.problem else ("pending", "Checking Tailscale…")
        if self.docker_problem is None:
            docker = ("pending", "Checking Docker…")
        else:
            docker = ("problem", self.docker_problem) if self.docker_problem else ("ok", "Docker ready")
        for dot, label, (state, text) in ((self.tailscale_dot, self.tailscale_label, ts),
                                          (self.docker_dot, self.docker_label, docker)):
            label.setText(text)
            label.setToolTip(text)
            if label.property("state") != state:
                _repolish(dot, state=state)
                _repolish(label, state=state)

    def _refresh_services(self) -> None:
        for service, row in self.rows.items():
            entry = self.services.get(service)
            if self.docker_problem:
                label, kind = "Unknown", "muted"
            else:
                label, kind = service_state(entry, disabled=service == "stats" and not self.stats_check.isChecked())
            url = service_url(self.runtime, service)
            if entry is not None and url:
                url_text, url_kind = url, "link"
            else:
                url_text = {"Disabled": "Statistics are off", "Not deployed": "Not deployed yet"}.get(label, "—")
                url_kind = "muted"
            row.badge.setText(label)
            row.url.setText(url_text)
            for widget, value in ((row.dot, kind), (row.badge, kind), (row.url, url_kind)):
                if widget.property("kind") != value:
                    _repolish(widget, kind=value)
            running = entry is not None and entry["state"] == "running"
            row.open_button.setEnabled(running and bool(url) and not self._busy)
            for page, button in self.page_buttons.items():
                if page.service == service:
                    link = page_url(self.runtime, page)
                    button.setEnabled(running and bool(link) and not self._busy)
                    button.setToolTip(link or f"{page.label} is not deployed")

    def _any_active(self) -> bool:
        """A container runs, restarts (a crash loop) or is paused: Stop and Restart apply."""
        return any(entry["state"] in ACTIVE_STATES for entry in self.services.values())

    def _refresh_controls(self) -> None:
        idle = not self._busy
        for widget in (self.password_edit, self.password_toggle, self.jupyter_port, self.stats_check,
                       self.deploy_button):
            widget.setEnabled(idle)
        self.stats_port.setEnabled(idle and self.stats_check.isChecked())
        active = self._any_active()
        known = self.docker_problem == ""
        self.start_button.setText("Restart" if active else "Start")
        self.deploy_button.setText("Update" if self.services else "Deploy")
        # While the state is unknown the installer gets the final word. Start
        # also works for an installed app whose containers were removed.
        self.stop_button.setEnabled(idle and (active or not known))
        self.start_button.setEnabled(idle and (bool(self.services) or bool(self.runtime) or not known))

    def _banner(self, kind: str, text: str) -> None:
        self.banner.setText(self._mask(text))
        if self.banner.property("kind") != kind:
            _repolish(self.banner, kind=kind)

    def _mask(self, text: str) -> str:
        return mask_secrets(text, {self.password_edit.text(), *self._secrets})

    def log(self, *lines: str) -> None:
        """Append lines now, after any queued command output."""
        self._log_queue.extend(lines)
        self._flush_log()

    def _queue_log(self, lines: list[str]) -> None:
        # One append per LOG_FLUSH_MS: a flood of output cannot starve the event loop.
        self._log_queue.extend(lines)
        if not self._log_timer.isActive():
            self._log_timer.start()

    def _flush_log(self) -> None:
        self._log_timer.stop()
        # Older lines would fall out of the block limit straight away anyway.
        lines, self._log_queue = self._log_queue[-LOG_MAX_BLOCKS:], []
        if not lines:
            return
        bar = self.log_view.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2     # do not yank a reader scrolled up
        self.log_view.appendPlainText(self._mask("\n".join(lines)))
        if follow:
            bar.setValue(bar.maximum())

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        if busy:
            self._banner("busy", text)
        else:
            self._job = ""
        self._refresh_view()

    def _read_runtime(self) -> dict[str, str]:
        try:
            return parse_settings(read_text(self.paths.runtime_env))
        except (OSError, ValueError):
            return {}

    def _child_env(self) -> dict[str, str]:
        overrides = {}
        defaults = Paths.from_environment({"HOME": self._environ.get("HOME", str(Path.home()))})
        # Tell the installer about non-default locations, so it edits what we edit.
        if "JLT_SETTINGS_FILE" in self._environ or self.paths.settings != defaults.settings:
            overrides["JLT_SETTINGS_FILE"] = str(self.paths.settings)
        if "JLT_APP_DIR" in self._environ or self.paths.runtime_env != defaults.runtime_env:
            overrides["JLT_APP_DIR"] = str(self.paths.runtime_env.parent)
        if self._environ.get("JLT_WORKSPACE_DIR"):     # relative to where the builder was started
            overrides["JLT_WORKSPACE_DIR"] = str(absolute_path(self._environ["JLT_WORKSPACE_DIR"]))
        return child_environment(self._environ, {self.password_edit.text(), *self._secrets}, overrides)

    # -- status polling -------------------------------------------------------

    def _poll_status(self) -> None:
        if not self.status_runner.is_running():    # never overlapping
            self.refresh_status()

    def refresh_status(self) -> None:
        if self._closing:
            return
        if self.status_runner.is_running():
            self._status_again = True
            return
        self.status_runner.start(["docker", "compose", "-p", PROJECT, "ps", "-a", "--format", "json"],
                                 env=self._child_env(), timeout_ms=PROBE_TIMEOUT_MS)

    @staticmethod
    def _docker_problem(result: RunResult) -> str:
        if result.outcome == "failed":
            return "Docker is not installed (no docker command on PATH)"
        if result.outcome == "timeout":
            return f"docker compose ps did not answer within {PROBE_TIMEOUT_MS // 1000} s (is the Docker daemon stuck?)"
        text = "\n".join(result.lines).lower()
        if "permission denied" in text:
            return "Docker refused access: add your user to the docker group and log in again"
        if "is not a docker command" in text or "unknown command" in text:
            return "Docker Compose v2 is missing (the docker compose plugin)"
        if "cannot connect" in text or "daemon running" in text:
            return "The Docker daemon is not running"
        return f"docker compose ps failed (exit {result.code})"

    def _on_status_done(self, result: RunResult) -> None:
        if self._closing:
            return
        if result.outcome == "cancelled":
            if self._after_status is not None:
                self._after_status = None
                self._set_busy(False)
                self._banner("error", "Deploy cancelled.")
            return
        if result.outcome == "ok":
            self.docker_problem = ""
            self.services = parse_compose_ps("\n".join(result.lines))
        else:
            self.docker_problem = self._docker_problem(result)
            self.services = {}
        self.runtime = self._read_runtime()
        if not self._busy:
            self._validate_live()      # the own-port whitelist may have changed
        self._refresh_view()
        if self._status_again:
            # Someone asked while this ps ran; a waiting callback wants that newer answer.
            self._status_again = False
            self.refresh_status()
            return
        callback, self._after_status = self._after_status, None
        if callback is not None:
            callback()

    def _poll_tailscale(self) -> None:
        if not self._busy:
            self.refresh_tailscale()

    def refresh_tailscale(self) -> None:
        if not self._closing and not self.tailscale_runner.is_running():
            self.tailscale_runner.start(["tailscale", "status", "--json"], env=self._child_env(),
                                        timeout_ms=PROBE_TIMEOUT_MS)

    def _on_tailscale_done(self, result: RunResult) -> None:
        callback, self._after_tailscale = self._after_tailscale, None
        if self._closing:
            return
        if result.outcome == "cancelled":
            if callback is not None:
                self._set_busy(False)
                self._banner("error", "Deploy cancelled.")
            return
        if result.outcome == "failed":
            self.ts = TailscaleStatus(problem="Tailscale is not installed (no tailscale command on PATH)")
        elif result.outcome == "timeout":
            self.ts = TailscaleStatus(
                problem=f"tailscale status did not answer within {PROBE_TIMEOUT_MS // 1000} s (is tailscaled stuck?)")
        else:
            status = parse_tailscale_status("\n".join(result.lines))
            if result.outcome != "ok" and not status.ip and "could not be read" in status.problem:
                status = TailscaleStatus(problem="tailscaled is not reachable (sudo systemctl start tailscaled)")
            self.ts = status
        if not self._busy:
            self._validate_live()
        self._refresh_view()
        if callback is not None:
            callback()

    # -- actions --------------------------------------------------------------

    def deploy(self, *_args: object) -> None:
        """Validate, re-detect Tailscale, refresh `compose ps`, check ports, save .env, run `install`."""
        if self._busy:
            return
        errors = validate_settings(self.form_values(commit=True), self.themes or None)
        if errors:
            self._refuse(errors)
            return
        if not self._installer_present():
            return
        self._set_busy(True, "Checking Tailscale…")
        self._after_tailscale = self._deploy_tailscale_checked
        self.refresh_tailscale()     # if a probe is already running, its result is used

    def _deploy_tailscale_checked(self) -> None:
        if not self.ts.ip:
            self._set_busy(False)
            self._refuse([f"No usable Tailscale IPv4 address: {self.ts.problem or 'unknown problem'}."])
            return
        # A fresh `compose ps`: the port check must know which ports our own
        # containers hold right now.
        self._banner("busy", "Checking Docker and the ports…")
        self._after_status = self._deploy_checked
        self.refresh_status()

    def _deploy_checked(self) -> None:
        if self.docker_problem:      # the installer needs a working `docker compose` as well
            self._set_busy(False)
            self._refuse([f"Docker is not usable: {self.docker_problem}."])
            return
        errors = self.current_errors()
        if errors:
            self._set_busy(False)
            self._refuse(errors)
            return
        values = self.form_values()
        try:
            text = render_settings(read_text(self.paths.settings), {k: values[k] for k in DEFAULTS})
        except UnicodeDecodeError:
            self._set_busy(False)
            self._fail(f"{self.paths.settings} is not UTF-8 text; the builder will not rewrite it. "
                       "Convert it to UTF-8 (or delete it), then restart the builder.")
            return
        except (OSError, ValueError) as exc:
            self._set_busy(False)
            self._fail(f"Could not read {self.paths.settings}: {getattr(exc, 'strerror', None) or exc}")
            return
        problems = settings_line_problems(text)
        if problems:          # lines the builder does not own, which the installer would refuse
            self._set_busy(False)
            self._refuse([f"{self.paths.settings}, {p}: fix or delete that line." for p in problems])
            return
        try:
            write_private_file(self.paths.settings, text)
        except OSError as exc:
            self._set_busy(False)
            self._fail(f"Could not save {self.paths.settings}: {getattr(exc, 'strerror', None) or exc}")
            return
        self.settings_values = values
        self._secrets.add(values["JUPYTER_PASSWORD"])
        self.log(f"# Settings saved to {self.paths.settings} (mode 0600)")
        self._run_installer("install")

    def stop(self, *_args: object) -> None:
        if not self._busy and self._installer_present():
            self._run_installer("stop")

    def start_or_restart(self, *_args: object) -> None:
        if not self._busy and self._installer_present():
            self._run_installer("restart" if self._any_active() else "start")

    def open_page(self, service: str, *_args: object) -> None:
        self._open(service_url(self.runtime, service))

    def open_link(self, page: Page, *_args: object) -> None:
        self._open(page_url(self.runtime, page))

    def _open(self, url: str) -> None:
        if not url:
            return
        self.log(f"# Opening {url}")
        if not self._open_url(QUrl(url)):
            QMessageBox.critical(self, APP_NAME, f"Could not open a browser. Open this address yourself:\n\n{url}")

    def _refuse(self, errors: list[str]) -> None:
        self.error_label.setText("\n".join(errors))
        self.error_label.show()
        self._banner("error", "Deploy refused: fix the problems listed under the settings.")
        QMessageBox.critical(self, APP_NAME, "Deploy refused:\n\n" + "\n".join(f"• {e}" for e in errors))

    def _fail(self, detail: str) -> None:
        self.log(f"# FAILED: {detail}")
        self._banner("error", detail)
        QMessageBox.critical(self, APP_NAME, detail)

    def _installer_present(self) -> bool:
        if self.paths.installer.is_file():
            return True
        self._fail(f"The installer was not found at {self.paths.installer}.")
        return False

    def _run_installer(self, command: str) -> None:
        self._job = self._command = command
        self._root_step = None
        self._tail = []
        self._set_busy(True, BUSY_TEXT[command])
        self.job_runner.start([self._bash, str(self.paths.installer), command],
                              cwd=str(self.paths.installer.parent), env=self._child_env())

    def _on_job_output(self, lines: list[str]) -> None:
        for line in lines:
            if self._job != "pkexec" and line.startswith("ROOT_STEP_REQUIRED"):
                step = root_step_from_line(line)
                if step is not None:
                    self._root_step = step
        self._tail.extend(line for line in lines[-20:] if line.strip() and not line.startswith("$ "))
        del self._tail[:-20]
        self._queue_log(lines)

    def _on_job_done(self, result: RunResult) -> None:
        if self._closing:
            return
        if self._job == "pkexec":
            self._on_root_step_done(result)
            return
        command = self._job
        if result.outcome != "ok":
            self._job_failed(command, result)
        elif self._root_step is not None:
            # sudo could not prompt without a terminal; polkit asks graphically.
            self.log(f"# The host step needs root: {shlex.join(self._root_step)} (asking through polkit)")
            self._job = "pkexec"
            self._banner("busy", "Waiting for authorisation of the host step (sysctl/firewall)…")
            self.job_runner.start(["pkexec", "/bin/bash", str(self.paths.installer), *self._root_step],
                                  cwd=str(self.paths.installer.parent), env=self._child_env())
        else:
            self._job_succeeded(command)

    def _on_root_step_done(self, result: RunResult) -> None:
        command, args = self._command, self._root_step or []
        if result.outcome == "ok":
            self._job_succeeded(command, "The host step (sysctl/firewall) was applied.")
            return
        if result.outcome == "failed":
            reason = "pkexec is not available"
        elif result.outcome == "cancelled":
            reason = "cancelled"
        elif result.code == 126:
            reason = "authorisation dismissed"
        elif result.code == 127:
            reason = "not authorised"
        else:
            reason = f"it failed with exit code {result.code}"
        terminal = shlex.join(["sudo", str(self.paths.installer), *args])
        message = (f"{JOB_TITLES[command]} finished and the services run, but the host step "
                   f"(sysctl/firewall) was not applied: {reason}.")
        self.log(f"# {message}", f"# Run it in a terminal: {terminal}")
        self._set_busy(False)
        self._banner("error", f"{message} Run in a terminal: {terminal}")
        QMessageBox.critical(self, APP_NAME, f"{message}\n\nRun it in a terminal:\n\n    {terminal}")
        self.refresh_status()

    def _job_succeeded(self, command: str, note: str = "") -> None:
        self._set_busy(False)
        self.runtime = self._read_runtime()
        if command == "stop":
            text = "Stopped. Containers and data are kept; Start brings them back."
        else:
            verb = {"install": "Deployed", "start": "Started", "restart": "Restarted"}[command]
            stats_on = "stats" in self.runtime.get("COMPOSE_PROFILES", "").split(",")
            urls = [f"{name} {service_url(self.runtime, service)}" for service, name in SERVICES
                    if service_url(self.runtime, service) and (service != "stats" or stats_on)]
            text = f"{verb}. " + "  ·  ".join(urls)
        text = " ".join(part for part in (text.strip(), note) if part)
        self.log(f"# {text}")
        self._banner("success", text.replace("  ·  ", "\n"))    # one URL per line
        self.refresh_status()

    def _job_failed(self, command: str, result: RunResult) -> None:
        self._set_busy(False)
        title = JOB_TITLES.get(command, command)
        if result.outcome == "failed":
            detail = f"Could not start {result.argv[0] if result.argv else 'the command'}: {result.error}"
        elif result.outcome == "cancelled":
            detail = "Cancelled. Containers that already started keep running."
        elif result.outcome == "crashed":
            detail = "The installer was killed before it finished."
        else:
            detail = f"The installer exited with code {result.code}."
        self.log(f"# FAILED: {title}: {detail}")
        self._banner("error", f"{title} failed. {detail}")
        if result.outcome != "cancelled":
            last = self._mask(self._tail[-1]) if self._tail else ""
            QMessageBox.critical(self, APP_NAME, f"{title} failed.\n\n{detail}"
                                 + (f"\n\nLast output line:\n{last}" if last else "")
                                 + "\n\nThe Output panel shows the full log.")
        self.refresh_status()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if self.job_runner.is_running():
            if self._job == "pkexec":
                # Once authorised, the child runs as root and ignores our signals.
                text = ("The host step (sysctl/firewall) is waiting for authorisation or running as root.\n\n"
                        "Close anyway? A step that already runs as root cannot be interrupted, so closing "
                        "waits for it to finish.")
            else:
                text = "A command is still running.\n\nCancel it and close? Containers that already started keep running."
            answer = QMessageBox.question(
                self, APP_NAME, text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._closing = True
        self.status_timer.stop()
        self.tailscale_timer.stop()
        # Blocks up to the kill grace period: the child must not outlive the window.
        for runner in (self.job_runner, self.status_runner, self.tailscale_runner):
            runner.terminate_and_wait()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    # Ctrl+C in the terminal closes the window the normal way; the idle timer
    # gives the Python interpreter a chance to run the signal handler.
    signal.signal(signal.SIGINT, lambda *_: window.close())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(250)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
