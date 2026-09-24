"""config.yaml: the one configuration of the builder GUI and the headless runner (run.py).

Both load and check it here and write the installer's settings .env from it (thebe.settings)
before they run setup-jupyterlab-tailscale.sh, so the installer and its CLI stay unchanged. The
.env is generated; config.yaml is the file people edit. It holds deployment settings only.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import yaml

from thebe.settings import (
    DEFAULTS, HTTPS_MODES, SPACE_CHARS, absolute_path, check_port, has_control, load_settings_file,
    normalize_bool, write_private_file,
)

MAX_CONFIG_BYTES = 256 * 1024


@dataclass
class Config:
    # The installer's settings (.env keys and text values, as thebe.settings.DEFAULTS).
    settings: dict[str, str] = field(default_factory=lambda: dict(DEFAULTS))
    # The notebook workspace as written in the file ('' = the installer's ~/jupyter-workspace).
    workspace: str = ""


class ConfigError(Exception):
    """The file cannot be used at all: unreadable, not UTF-8, too large or not YAML."""


Converter = Callable[[object, str], "tuple[str | None, str]"]


def _as_text(value: object, where: str) -> tuple[str | None, str]:
    # Unquoted YAML turns 12345678 into a number and 0o17 or 1_000 into other numbers:
    # a password or a name must stay exactly as typed, so only text counts.
    if isinstance(value, str):
        return value, ""
    return None, f"{where} must be text; put it in quotes."


def _as_port(value: object, where: str) -> tuple[str | None, str]:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(text, str) or check_port(text, where)[1]:
        return None, f"{where} must be a port number from 1024 to 65535."
    return text, ""


def _as_switch(value: object, where: str) -> tuple[str | None, str]:
    if isinstance(value, bool):
        return ("1" if value else "0"), ""
    switch = normalize_bool(str(value)) if isinstance(value, (int, str)) else None
    if switch is None:
        return None, f"{where} must be true or false."
    return switch, ""


def _as_https(value: object, where: str) -> tuple[str | None, str]:
    if value is False:          # YAML 1.1 reads a bare off as false
        return "off", ""
    if isinstance(value, str) and value in HTTPS_MODES:
        return value, ""
    return None, f"{where} must be one of: {', '.join(HTTPS_MODES)}."


# YAML path -> installer setting. A missing entry keeps the default.
SETTING_FIELDS: tuple[tuple[tuple[str, ...], str, Converter], ...] = (
    (("jupyter", "password"), "JUPYTER_PASSWORD", _as_text),
    (("jupyter", "port"), "JUPYTER_PORT", _as_port),
    (("stats", "enabled"), "STATS_ENABLED", _as_switch),
    (("stats", "port"), "STATS_PORT", _as_port),
    (("stats", "user"), "STATS_USER", _as_text),
    (("theme",), "THEME", _as_text),
    (("https",), "HTTPS", _as_https),
)
# Top-level keys that are not installer settings.
OTHER_KEYS = ("workspace",)


def _known_keys() -> dict[str, set[str] | None]:
    """Top-level key -> the keys its section allows (None: a plain value)."""
    known: dict[str, set[str] | None] = {key: None for key in OTHER_KEYS}
    for path, _key, _convert in SETTING_FIELDS:
        if len(path) == 1:
            known[path[0]] = None
        else:
            section = known.get(path[0]) or set()
            section.add(path[1])
            known[path[0]] = section
    return known


def resolve_path(text: str, base_dir: Path) -> Path:
    """A path from the file: ~ expanded, relative to the file's directory, not resolved further."""
    path = Path(text).expanduser()
    return path if path.is_absolute() else base_dir / path


def path_problem(text: str, where: str) -> str:
    """Why the installer would refuse a path it writes into its single-quoted .env ('' when fine)."""
    if not text.strip(SPACE_CHARS):
        return f"{where} must not be empty."
    if any(ch in text for ch in "'\"\\") or has_control(text):
        return f"{where} must not contain quotes, backslashes or control characters."
    return ""


def parse_config(text: str, base_dir: Path) -> tuple[Config, list[str]]:
    """The configuration in `text` plus problems; a setting with a problem keeps its default."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise ConfigError(f"not valid YAML{where}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("not a mapping of settings (key: value lines)")
    config, problems = Config(), []
    known = _known_keys()
    for key, value in data.items():
        if key not in known:
            problems.append(f"unknown setting {key!r}.")
        elif known[key] is not None and value is not None:
            if not isinstance(value, dict):
                problems.append(f"{key} must be a section (indented key: value lines).")
                continue
            problems += [f"unknown setting '{key}.{sub}'." for sub in value if sub not in known[key]]
    for path, key, convert in SETTING_FIELDS:
        node: object = data
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            continue
        value, problem = convert(node, ".".join(path))
        if problem:
            problems.append(problem)
        else:
            config.settings[key] = value
    workspace = data.get("workspace")
    if workspace not in (None, ""):
        if not isinstance(workspace, str):
            problems.append("workspace must be a directory path (text).")
        else:
            problem = (path_problem(workspace, "workspace")
                       or path_problem(str(resolve_path(workspace, base_dir)), "workspace"))
            if problem:
                problems.append(problem)
            else:
                config.workspace = workspace
    return config, problems


def load_config(path: Path) -> tuple[Config, list[str]]:
    """The configuration in `path` and its problems. FileNotFoundError when it does not exist."""
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigError(f"{path} is larger than {MAX_CONFIG_BYTES // 1024} KiB")
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise
    except UnicodeDecodeError:
        raise ConfigError(f"{path} is not UTF-8 text") from None
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc.strerror or exc}") from None
    try:
        return parse_config(text, path.parent)
    except ConfigError as exc:
        raise ConfigError(f"{path} is {exc}") from None


def workspace_dir(config: Config, config_file: Path) -> Path | None:
    """The configured workspace as an absolute path, or None for the installer's default."""
    return resolve_path(config.workspace, config_file.parent) if config.workspace else None


def _scalar(value: str) -> str:
    """`value` as a double-quoted YAML scalar that reads back exactly."""
    return yaml.safe_dump(value, default_style='"', allow_unicode=True, width=float("inf")).rstrip("\n")


def _number(value: str) -> str:
    return value if value.isdigit() and value.isascii() else _scalar(value)


def render_config(config: Config) -> str:
    """config.yaml for `config`, with the explanations of the template."""
    s = config.settings
    workspace = (f"workspace: {_scalar(config.workspace)}" if config.workspace
                 else f"# workspace: {_scalar('~/jupyter-workspace')}")
    lines = [
        "# Thebe configuration, read by the builder (./run-builder.sh) and the headless runner (./run.sh).",
        "# Both check it and write the installer's settings file (.env) from it: edit this file, not the .env.",
        "# Deploy in the builder rewrites it (comments of your own are not kept). Keep it private (mode 600).",
        "",
        "jupyter:",
        "  # Password of JupyterLab and the dashboard: 8 to 128 characters, no single quote or backslash.",
        f"  password: {_scalar(s['JUPYTER_PASSWORD'])}",
        f"  port: {_number(s['JUPYTER_PORT'])}",
        "",
        "stats:",
        "  # The statistics dashboard and the Dependencies page (HTTP Basic: this user and the password).",
        f"  enabled: {'true' if s['STATS_ENABLED'] == '1' else 'false'}",
        f"  port: {_number(s['STATS_PORT'])}",
        f"  user: {_scalar(s['STATS_USER'])}",
        "",
        "# Colours of the dashboard and the builder: a file name from stack/theme without .json.",
        f"theme: {_scalar(s['THEME'])}",
        "",
        "# auto: HTTPS on the Tailscale name when the tailnet allows it; off: plain HTTP.",
        f"https: {_scalar(s['HTTPS'])}",
        "",
        "# The notebook workspace on this machine (/workspace in JupyterLab). Default: ~/jupyter-workspace.",
        workspace,
    ]
    return "\n".join(lines) + "\n"


def save_config(path: Path, config: Config) -> None:
    write_private_file(path, render_config(config))


def make_private(path: Path) -> str:
    """chmod 600 a config that others may read (it holds the password); a note when it changed."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            os.chmod(path, 0o600)
            return f"{path} was mode {mode:o}; changed to 600 because it holds the password."
    except OSError:
        pass
    return ""


def config_from_settings(settings_file: Path) -> tuple[Config, list[str]]:
    """A first config.yaml from the installer's settings file (the defaults when it is missing)."""
    values, notes = load_settings_file(settings_file)
    return Config(settings=values), notes


def effective_workspace(config: Config, config_file: Path, environ: Mapping[str, str]) -> Path | None:
    """JLT_WORKSPACE_DIR when set (it wins, as it does for the installer), else the configured one."""
    if environ.get("JLT_WORKSPACE_DIR"):
        return absolute_path(environ["JLT_WORKSPACE_DIR"])
    return workspace_dir(config, config_file)
