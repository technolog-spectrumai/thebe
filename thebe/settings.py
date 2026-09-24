"""Settings, paths and validation shared by the builder GUI and the headless runner.

Kept identical to setup-jupyterlab-tailscale.sh: the same .env keys and defaults, the same parser
and the same validation rules, so the Python side never accepts what the installer refuses.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import re
import socket
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, NamedTuple

REPO = Path(__file__).resolve().parent.parent
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
    "HTTPS": "auto",       # auto: HTTPS by MagicDNS name when the tailnet allows it; off: plain HTTP
}
HTTPS_MODES = ("auto", "off")

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


def absolute_path(value: str) -> Path:
    return Path(value).expanduser().absolute()


@dataclass(frozen=True)
class Paths:
    installer: Path
    settings: Path       # the installer's settings .env (0600) the builder edits
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


def has_control(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" or ch in _EXTRA_CONTROL for ch in text)


def _assignment(key: str, value: str) -> str:
    # Single quotes make the value literal for Compose and the installer's
    # parser alike, so a value must not contain one. (The message never
    # includes the value: it may be the password.)
    if "'" in value or has_control(value):
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


def load_settings_file(path: Path) -> tuple[dict[str, str], list[str]]:
    """The installer's settings (defaults for missing keys) and notes on what could not be used.

    An invalid port falls back to its default and an invalid STATS_ENABLED to off (it opens no
    port nobody clearly asked for), each with a note, so the values are always usable in a form.
    """
    name = path.name
    try:
        text = read_text(path)
    except OSError as exc:
        return dict(DEFAULTS), [f"Could not read {path}: {exc.strerror or exc}."]
    except UnicodeDecodeError:
        return dict(DEFAULTS), [f"{path} is not UTF-8 text, so the defaults are shown and it is not rewritten. "
                                "Convert it to UTF-8 (or delete it) and start again."]
    values = dict(DEFAULTS)
    values.update({k: v for k, v in parse_settings(text).items() if k in DEFAULTS})
    notes = [f"{name} {problem}; the installer rejects this line." for problem in settings_line_problems(text)]
    for key in ("JUPYTER_PORT", "STATS_PORT"):
        if check_port(values[key], key)[1]:
            notes.append(f"{key} in {name} is not a valid port; {DEFAULTS[key]} is used instead.")
            values[key] = DEFAULTS[key]
    enabled = normalize_bool(values["STATS_ENABLED"])
    if enabled is None:
        notes.append(f"STATS_ENABLED in {name} is not 1/0, true/false, yes/no or on/off; statistics are off.")
    values["STATS_ENABLED"] = enabled or "0"
    return values, notes


class SettingsError(Exception):
    """The settings file cannot be written. The message never contains a value (it may be the password).

    `problems` lists lines the installer would refuse; empty when the file could not be read or saved.
    """

    def __init__(self, message: str, problems: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.problems = list(problems)


def save_settings(path: Path, values: Mapping[str, str]) -> None:
    """Write the known keys into the installer's settings file (0600), keeping every other line."""
    try:
        text = render_settings(read_text(path), {k: values[k] for k in DEFAULTS if k in values})
    except UnicodeDecodeError:
        raise SettingsError(f"{path} is not UTF-8 text, so it is not rewritten. "
                            "Convert it to UTF-8 (or delete it) and try again.") from None
    except OSError as exc:
        raise SettingsError(f"Could not read {path}: {exc.strerror or exc}") from None
    except ValueError as exc:         # a value the file cannot hold; the message names only the key
        raise SettingsError(f"Cannot write {path}: {exc}.") from None
    problems = settings_line_problems(text)
    if problems:      # lines nobody here owns, which the installer would refuse
        raise SettingsError(f"{path} has lines the installer refuses.",
                            [f"{path}, {p}: fix or delete that line." for p in problems])
    try:
        write_private_file(path, text)
    except OSError as exc:
        raise SettingsError(f"Could not save {path}: {exc.strerror or exc}") from None


# --------------------------------------------------------------------------
# Validation (kept identical to the installer's rules)
# --------------------------------------------------------------------------

_USER = re.compile(r"[A-Za-z0-9._-]{1,32}")
_PORT_TEXT = re.compile(r"[1-9][0-9]{0,4}")   # no sign, no leading zero (bash would read octal)
# A full MagicDNS name as the installer accepts it: two or more lower-case labels.
_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
GID_TEXT = re.compile(r"[1-9][0-9]{0,9}")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")
MAX_GID = 4294967294                          # 4294967295 is (gid_t)-1, "no group"


def is_valid_hostname(name: str) -> bool:
    return len(name) <= 253 and _HOSTNAME.fullmatch(name) is not None


def is_public_host(host: str) -> bool:
    """A Tailscale IPv4 address or a valid DNS name, as the deployed PUBLIC_HOST may be."""
    if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
        # A browser reads a name ending in a number as an IPv4 address (1.2.3 and 1.0x2 too):
        # only a real address inside the tailnet range counts then.
        try:
            return ipaddress.ip_address(host) in TAILNET
        except ValueError:
            return False
    return is_valid_hostname(host)


def password_problems(password: str) -> list[str]:
    problems = []
    if not 8 <= len(password) <= 128:
        problems.append("The password must be 8 to 128 characters long.")
    if "'" in password:
        problems.append("The password may not contain a single quote (').")
    if "\\" in password:
        problems.append("The password may not contain a backslash (\\).")
    if has_control(password):
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
    # A missing key means auto, like the installer; the builder writes HTTPS='auto' on save.
    if values.get("HTTPS", "auto") not in HTTPS_MODES:
        errors.append(f"HTTPS in the settings file must be one of: {', '.join(HTTPS_MODES)}.")
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
