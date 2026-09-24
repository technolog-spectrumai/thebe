"""The project's requirements.txt: optional Jupyter packages for the custom packages environment.

It is the source of truth for that baseline; nothing about it is copied into config.yaml or the
.env. The installer hands it to the package runner on install and update (deps_runner.py
baseline), which installs it with the Dependencies page's pip job. Here it is checked on the host
with the runner's own rules (stack/jupyter/deps_runner.py is standard library only), so run.py and
the builder refuse a bad line before a deploy starts, and copy_requirements() puts a file chosen
in the builder in its place.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Mapping

from thebe.settings import REPO, absolute_path

RUNNER_FILE = REPO / "stack" / "jupyter" / "deps_runner.py"
_rules: ModuleType | None = None


def _runner_rules() -> ModuleType:
    global _rules
    if _rules is None:
        spec = importlib.util.spec_from_file_location("thebe_deps_runner_rules", RUNNER_FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _rules = module
    return _rules


def requirements_file(environ: Mapping[str, str]) -> Path:
    """JLT_REQUIREMENTS_FILE, as the installer reads it, else <repo>/requirements.txt."""
    value = environ.get("JLT_REQUIREMENTS_FILE")
    return absolute_path(value) if value else REPO / "requirements.txt"


@dataclass
class RequirementsCheck:
    path: Path
    exists: bool = False
    packages: int = 0                    # lines naming a package
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.exists:
            return f"none ({self.path} does not exist; nothing extra is installed)"
        if not self.packages:
            return f"{self.path}: no packages; nothing extra is installed"
        return f"{self.path}: {self.packages} package line(s), installed on install and update"


def _read(path: Path) -> tuple[RequirementsCheck, bytes | None]:
    """The file's bytes (at most one past the runner's limit), or a check that says why not."""
    check = RequirementsCheck(path)
    try:
        if not path.exists():
            return check, None
        check.exists = True
        if not path.is_file():
            check.problems.append(f"{path} is not a file.")
            return check, None
        with open(path, "rb") as handle:
            return check, handle.read(_runner_rules().MAX_REQUIREMENTS_BYTES + 1)
    except OSError as exc:
        check.problems.append(f"{path} cannot be read: {exc.strerror or exc}.")
        return check, None


def check_requirements(path: Path) -> RequirementsCheck:
    """The file's package count and the problems the runner would refuse it for."""
    check, data = _read(path)
    return check if data is None else _check_data(check, data)


def _check_data(check: RequirementsCheck, data: bytes) -> RequirementsCheck:
    path, rules = check.path, _runner_rules()
    if len(data) > rules.MAX_REQUIREMENTS_BYTES:
        check.problems.append(f"{path} is larger than {rules.MAX_REQUIREMENTS_BYTES // 1024} KB.")
        return check
    try:
        text = rules.normalize_requirements(data.decode("utf-8"))
    except UnicodeDecodeError:
        check.problems.append(f"{path} is not UTF-8 text.")
        return check
    for error in rules.validate_requirements(text):
        where = f" line {error['line']}" if error["line"] else ""
        check.problems.append(f"{path.name}{where}: {error['message']}")
    check.packages = rules.requirement_count(text)
    return check


class CopyRefused(Exception):
    """The chosen file is not copied; the message says why, `problems` lists refused lines."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


def copy_requirements(source: Path, dest: Path) -> RequirementsCheck:
    """Copy `source` (byte for byte) to the project's requirements.txt at `dest`.

    Only a file the package runner accepts is copied, and the old file is replaced atomically, so
    `dest` is always either the old or the new complete file. Returns the check of the new file.
    """
    check, data = _read(source)
    if not check.exists:
        raise CopyRefused(f"{source} does not exist.")
    if data is not None:
        _check_data(check, data)     # the very bytes that are copied
    if check.problems:
        raise CopyRefused(f"{source} is not accepted (the Dependencies page's rules); nothing was copied.",
                          check.problems)
    try:
        if dest.exists() and os.path.samefile(source, dest):
            raise CopyRefused(f"{source} already is the project's requirements.txt.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o644)        # a package list, no secret
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    except OSError as exc:
        raise CopyRefused(f"Could not copy {source} to {dest}: {exc.strerror or exc}.") from None
    return check_requirements(dest)
