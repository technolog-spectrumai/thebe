"""Headless Thebe: deploy and run the stack from config.yaml, without the GUI.

Start it with ./run.sh, which keeps PyYAML inside ./.venv. The builder GUI and this runner share
config.yaml and everything in the thebe package; both hand the real work to
setup-jupyterlab-tailscale.sh, the same installer the CLI uses.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from thebe.config import (
    Config, ConfigError, config_from_settings, effective_workspace, load_config, make_private, save_config,
)
from thebe.imports import ImportFailed, PlannedImport, default_workspace, plan_imports, run_imports
from thebe.packages import check_requirements, requirements_file
from thebe.settings import DEFAULTS, Paths, SettingsError, absolute_path, save_settings, validate_settings
from thebe.stack import child_environment
from thebe.theme import available_themes

# Commands that write the installer's settings from config.yaml first; the others pass straight through.
APPLYING = ("install", "update", "start", "restart")
PASSING = ("stop", "status", "logs", "uninstall")
EXIT_CONFIG = 2          # config.yaml is missing, unreadable or invalid

USAGE = """\
run.sh [--config FILE] [COMMAND] [ARGS...]

Deploy and run JupyterLab on Tailscale from config.yaml, without the GUI.

Commands:
  install      (default) check config.yaml, write the installer's settings, build and start
  update       the same, and refresh the base image
  start        write the settings, start the containers (follows a new Tailscale IP/certificate)
  restart      stop, then start
  stop         stop the containers
  status       deployment, containers, addresses, firewall
  logs [...]   container logs (--no-follow, service names)
  uninstall [...]  remove the deployment (--yes, --delete-workspace)
  check        check config.yaml and show what would be deployed; changes nothing
  import       copy the import_dirs into <workspace>/imported/ (install and update do it too)
  init         write config.yaml (from the current settings file, else the defaults) if it is missing

config.yaml: JLT_CONFIG_FILE, else next to the settings file (<repo>/config.yaml).
"""


class Output:
    """Messages in the installer's style: '==>' on stdout, warnings and errors on stderr."""

    def __init__(self, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> None:
        self.out, self.err = out, err

    def info(self, text: str) -> None:
        print(f"==> {text}", file=self.out, flush=True)

    def warn(self, text: str) -> None:
        print(f"WARNING: {text}", file=self.err, flush=True)

    def error(self, text: str) -> None:
        print(f"ERROR: {text}", file=self.err, flush=True)


def ensure_config(paths: Paths, say: Output) -> None:
    """Create config.yaml from the current settings file (or the defaults) when it is missing."""
    path = paths.config_file
    if path.exists() or path.is_symlink():
        return
    config, notes = config_from_settings(paths.settings)
    save_config(path, config)
    source = paths.settings if paths.settings.is_file() else "the defaults"
    say.info(f"Created {path} from {source} (mode 600). Edit it to change the deployment.")
    for note in notes:
        say.warn(note)


def read_config(paths: Paths, say: Output, *, strict: bool = True) -> Config | None:
    """The checked configuration, or None after reporting why it cannot be used."""
    path = paths.config_file
    try:
        config, problems = load_config(path)
    except FileNotFoundError:
        say.error(f"{path} does not exist. Create it with: run.sh init")
        return None
    except ConfigError as exc:
        say.error(f"{exc}.")
        return None
    note = make_private(path)
    if note:
        say.info(f"Note: {note}")
    themes = available_themes(paths.theme_dir)
    problems += validate_settings(config.settings, themes or None)
    if problems and strict:
        say.error(f"Invalid configuration in {path}:\n" + "\n".join(f"  - {p}" for p in problems))
        return None
    for problem in problems:
        say.warn(f"{path}: {problem}")
    return config


def installer_environment(paths: Paths, config: Config | None, environ: Mapping[str, str]) -> dict[str, str]:
    """The installer's environment: this settings file and workspace, never the password."""
    overrides = {"JLT_SETTINGS_FILE": str(paths.settings)}
    if config is not None:
        workspace = effective_workspace(config, paths.config_file, environ)
        if workspace is not None:
            overrides["JLT_WORKSPACE_DIR"] = str(workspace)
        secrets = [config.settings["JUPYTER_PASSWORD"]]
    else:
        secrets = []
    env = child_environment(environ, secrets, overrides, plain=False)
    env.pop("JLT_GPU", None)          # config.yaml's nvidia decides, not a stray export
    return env


def run_installer(paths: Paths, command: str, args: Sequence[str], env: Mapping[str, str]) -> int:
    """Run the installer attached to this terminal (sudo can ask for a password); its exit code."""
    bash = shutil.which("bash", path=env.get("PATH", os.defpath)) or "/bin/bash"
    proc = subprocess.Popen([bash, str(paths.installer), command, *args], cwd=str(paths.installer.parent),
                            env=dict(env))
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            # Ctrl+C reached the installer from the terminal as well; it decides how to stop.
            continue
    return 128 - code if code < 0 else code


def import_workspace(paths: Paths, config: Config, environ: Mapping[str, str]) -> Path:
    """The workspace the installer will mount, which the imports are copied into."""
    return effective_workspace(config, paths.config_file, environ) or default_workspace()


def plan_config_imports(paths: Paths, config: Config, environ: Mapping[str, str],
                        say: Output) -> list[PlannedImport] | None:
    """The checked import plan, or None after reporting why nothing can be copied."""
    plans, problems = plan_imports(config.imports, paths.config_file.parent, import_workspace(paths, config, environ))
    if problems:
        say.error("Cannot copy the import_dirs of {}:\n{}".format(
            paths.config_file, "\n".join(f"  - {p}" for p in problems)))
        return None
    return plans


def copy_imports(paths: Paths, config: Config, plans: list[PlannedImport], environ: Mapping[str, str],
                 say: Output) -> bool:
    """Copy the planned directories into the workspace; False after reporting a failure."""
    try:
        run_imports(plans, import_workspace(paths, config, environ), say.info)
    except ImportFailed as exc:
        say.error(str(exc))
        return False
    return True


def packages_ok(environ: Mapping[str, str], say: Output) -> bool:
    """requirements.txt passes the package runner's rules (the installer checks it again)."""
    check = check_requirements(requirements_file(environ))
    if check.problems:
        say.error("requirements.txt is not accepted (the Dependencies page's rules):\n"
                  + "\n".join(f"  - {p}" for p in check.problems))
    return not check.problems


def show_config(paths: Paths, config: Config, environ: Mapping[str, str], say: Output) -> None:
    s = config.settings
    workspace = effective_workspace(config, paths.config_file, environ)
    stats = f"on, port {s['STATS_PORT']}, user {s['STATS_USER']}" if s["STATS_ENABLED"] == "1" else "off"
    rows = (
        ("Config", str(paths.config_file)),
        ("Settings", f"{paths.settings} (written by install, update, start and restart)"),
        ("JupyterLab", f"port {s['JUPYTER_PORT']}"),
        ("Statistics", stats),
        ("Theme", s["THEME"]),
        ("HTTPS", s["HTTPS"]),
        ("NVIDIA GPU", {"1": "expected", "0": "off", "auto": "auto (used when it works)"}.get(s["NVIDIA"], s["NVIDIA"])),
        ("Workspace", str(workspace) if workspace else "~/jupyter-workspace (the installer's default)"),
    )
    for label, value in rows:
        print(f"  {label + ':':<12} {value}", file=say.out)
    for item in config.imports:
        print(f"  {'Import:':<12} {item.path} -> imported/{item.name or Path(item.path).expanduser().name}/",
              file=say.out)
    print(f"  {'Packages:':<12} {check_requirements(requirements_file(environ)).summary()}", file=say.out)
    if s["JUPYTER_PASSWORD"] == DEFAULTS["JUPYTER_PASSWORD"]:
        say.warn(f"The default password is in use. Change jupyter.password in {paths.config_file}.")


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         paths: Paths | None = None, say: Output | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    say = say or Output()
    parser = argparse.ArgumentParser(prog="run.sh", usage=USAGE, add_help=False)
    parser.add_argument("--config")
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("command", nargs="?", default="install")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    if options.help or options.command in ("help", "-h", "--help"):
        print(USAGE, end="", file=say.out)
        return 0
    paths = paths or Paths.from_environment(environ)
    if options.config:
        paths = dataclasses.replace(paths, config=absolute_path(options.config))
    command, args = options.command, list(options.args)

    if command == "init":
        if paths.config_file.exists():
            say.info(f"{paths.config_file} already exists; nothing to do.")
            return 0
        ensure_config(paths, say)
        return 0
    if command == "check":
        config = read_config(paths, say)
        if config is None:
            return EXIT_CONFIG
        show_config(paths, config, environ, say)
        if not packages_ok(environ, say):
            return EXIT_CONFIG
        say.info("The configuration is valid.")
        return 0
    if command == "import":
        config = read_config(paths, say)
        if config is None:
            return EXIT_CONFIG
        plans = plan_config_imports(paths, config, environ, say)
        if plans is None:
            return EXIT_CONFIG
        if not plans:
            say.info(f"No import_dirs in {paths.config_file}; nothing to copy.")
            return 0
        return 0 if copy_imports(paths, config, plans, environ, say) else 1
    if command in PASSING:
        # The workspace still matters here (uninstall --delete-workspace), so a readable config counts.
        config = read_config(paths, say, strict=False) if paths.config_file.exists() else None
        return run_installer(paths, command, args, installer_environment(paths, config, environ))
    if command not in APPLYING:
        say.error(f"Unknown command: {command}. See: run.sh help")
        return EXIT_CONFIG
    if args:
        say.error(f"'{command}' takes no arguments. See: run.sh help")
        return EXIT_CONFIG

    ensure_config(paths, say)
    config = read_config(paths, say)
    if config is None:
        return EXIT_CONFIG
    show_config(paths, config, environ, say)
    deploying = command in ("install", "update")
    # Checked before anything is written; copied before the installer starts JupyterLab.
    plans = plan_config_imports(paths, config, environ, say) if deploying else []
    if plans is None or (deploying and not packages_ok(environ, say)):
        return EXIT_CONFIG
    try:
        save_settings(paths.settings, config.settings)
    except SettingsError as exc:
        say.error("\n".join([str(exc), *(f"  - {p}" for p in exc.problems)]))
        return EXIT_CONFIG
    say.info(f"Settings written to {paths.settings} (mode 600)")
    if plans and not copy_imports(paths, config, plans, environ, say):
        return 1
    return run_installer(paths, command, args, installer_environment(paths, config, environ))


if __name__ == "__main__":
    sys.exit(main())
