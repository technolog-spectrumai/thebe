"""The project's requirements.txt: optional Jupyter packages for the custom packages environment.

It is the source of truth for that baseline; nothing about it is copied into config.yaml or the
.env. The installer hands it to the package runner on install and update (deps_runner.py
baseline), which installs it with the Dependencies page's pip job. Here it is only checked, on the
host, with the runner's own rules (stack/jupyter/deps_runner.py is standard library only), so
run.py and the builder refuse a bad line before a deploy starts.
"""

from __future__ import annotations

import importlib.util
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


def check_requirements(path: Path) -> RequirementsCheck:
    """The file's package count and the problems the runner would refuse it for."""
    check = RequirementsCheck(path)
    try:
        if not path.exists():
            return check
        check.exists = True
        if not path.is_file():
            check.problems.append(f"{path} is not a file.")
            return check
        rules = _runner_rules()
        with open(path, "rb") as handle:
            data = handle.read(rules.MAX_REQUIREMENTS_BYTES + 1)
    except OSError as exc:
        check.problems.append(f"{path} cannot be read: {exc.strerror or exc}.")
        return check
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
