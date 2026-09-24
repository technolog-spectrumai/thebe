"""Dependency runner for the jupyterlab-tailscale stack: pip installs for notebook kernels.

Runs in the `deps` container, which uses the JupyterLab image itself, so pip sees exactly
the packages notebooks already have. Standard library only. Listens on port 8890 on the
Compose network (never published); the stats dashboard proxies its /dependencies page here.

Every route requires HTTP Basic auth with STATS_USER and the runner token from
DEPS_TOKEN_FILE, a random secret the installer shares only with the dashboard. This
container never gets the JupyterLab password or its hash: pip runs the build scripts of
third-party packages here, and those can read every file the runner can. Bodies are JSON
(application/json, at most 64 KB).

Custom packages go into a virtualenv on the custom_packages volume:
  CUSTOM_DIR/venv              python -m venv --system-site-packages --without-pip
  CUSTOM_DIR/requirements.txt  saved from the page, validated first
  CUSTOM_DIR/baseline.txt      the project's requirements.txt, handed over on every deploy
  CUSTOM_DIR/baseline.json     what the last job installed from it (to skip unchanged deploys)
  CUSTOM_DIR/job.json          state of the last job (atomic writes)
  CUSTOM_DIR/job.log           output of the last job
  CUSTOM_DIR/tmp               TMPDIR for pip (the root filesystem is read-only)
Every install job installs the baseline and the page's list together, in one pip run, so
the two can never disagree. pip runs with --constraint /opt/constraints.txt (pip freeze of
the image), so a package the image already has keeps its pinned version and is never
installed a second time. Kernels add the venv's site-packages after the image's own
(kernel_launcher.py).

Besides the HTTP server (no arguments) the script has two commands for the installer:
  deps_runner.py check           validate a requirements file on stdin (exit 2 on problems)
  deps_runner.py baseline [--local]
                                 make the environment match the requirements.txt on stdin:
                                 through the running server, or with --local in a one-off
                                 container when statistics (and so the server) are off

Environment:
  STATS_USER, DEPS_TOKEN_FILE         Basic auth credentials
  CUSTOM_DIR                          volume root (default /opt/custom)
  PIP_CACHE_DIR                       pip download cache (default /var/cache/pip)
"""

import base64
import binascii
import contextlib
import datetime
import errno
import fcntl
import hashlib
import http.client
import http.server
import importlib.metadata
import json
import logging
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

PORT = 8890
REALM = "jupyterlab-tailscale dependency runner"

CUSTOM_DIR = Path(os.environ.get("CUSTOM_DIR") or "/opt/custom")
PIP_CACHE_DIR = Path(os.environ.get("PIP_CACHE_DIR") or "/var/cache/pip")
CONSTRAINTS_FILE = Path(os.environ.get("CONSTRAINTS_FILE") or "/opt/constraints.txt")
VENV_DIR = CUSTOM_DIR / "venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
PYTHON_TAG = f"python{sys.version_info.major}.{sys.version_info.minor}"
VENV_SITE_PACKAGES = VENV_DIR / "lib" / PYTHON_TAG / "site-packages"
REQUIREMENTS_FILE = CUSTOM_DIR / "requirements.txt"
JOB_FILE = CUSTOM_DIR / "job.json"
LOG_FILE = CUSTOM_DIR / "job.log"
JOB_TMP_DIR = CUSTOM_DIR / "tmp"
BASELINE_FILE = CUSTOM_DIR / "baseline.txt"
BASELINE_STATE_FILE = CUSTOM_DIR / "baseline.json"
LOCK_FILE = CUSTOM_DIR / ".job.lock"
PYTHON_XY = f"{sys.version_info.major}.{sys.version_info.minor}"

# A 64 KB requirements file as a JSON string (newlines escaped) needs more than 64 KB.
MAX_BODY_BYTES = 192 * 1024
MIN_TOKEN_LENGTH = 32
# Seconds a connection may stay silent (request line, headers or body) before it is dropped,
# so half-sent requests from the Compose network cannot pile up handler threads.
SOCKET_TIMEOUT = 30
MAX_REQUIREMENTS_BYTES = 64 * 1024
MAX_REQUIREMENTS_LINES = 500
MAX_LOG_CHUNK = 64 * 1024
MAX_LOG_BYTES = 20 * 1024 * 1024
CANCEL_KILL_AFTER = 10.0
SIZES_MAX_AGE = 60.0

JOB_ACTIONS = ("install", "reset")
OFFSET_RE = re.compile(r"[0-9]{1,15}")

log = logging.getLogger("deps")


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- requirements validation ---------------------------------------------------------
#
# pip parses a requirements file line by line (pip/_internal/req/req_file.py): it decodes
# the bytes (a BOM or a PEP 263 "coding:" comment in the first two lines picks the
# codec), splits with str.splitlines(), joins lines ending in a backslash, strips
# "<whitespace>#..." comments, expands ${VAR}, and hands everything from the first token
# starting with "-" to shlex + optparse, which also accepts unambiguous abbreviations of
# long options (--req means --requirement). The checks below follow the same steps, so
# an option cannot be hidden from them by a codec, a line separator, a continuation,
# quotes or an abbreviation.

COMMENT_RE = re.compile(r"(^|\s+)#.*$")  # pip's own comment pattern
PEP263_RE = re.compile(r"coding[:=]\s*([-\w.]+)")
ENV_VAR_RE = re.compile(r"\$\{[^}]*\}")

# Options that would install outside the managed venv or read other files. Short options
# take a value, so "-rfile" is "-r file".
FORBIDDEN_SHORT = {
    "r": "--requirement",
    "c": "--constraint",
    "e": "--editable",
    "t": "--target",
}
FORBIDDEN_LONG = ("requirement", "constraint", "editable", "target", "prefix", "root", "user", "src")
# Exact option names that are fine even though they are a prefix of a forbidden one
# (optparse prefers an exact match: --pre is --pre, not an abbreviation of --prefix).
ALLOWED_EXACT_LONG = frozenset(
    (
        "index-url",
        "extra-index-url",
        "no-index",
        "find-links",
        "pre",
        "trusted-host",
        "prefer-binary",
        "only-binary",
        "no-binary",
        "require-hashes",
        "no-require-hashes",
        "hash",
        "config-settings",
        "use-feature",
        "all-releases",
        "only-final",
        "pypi-url",
    )
)
FORBIDDEN_REASON = "it would install outside the managed environment or read another file"

# U+202A-U+202E and U+2066-U+2069 reorder text on screen and could make a line look
# different from what pip reads.
BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def _bare(token: str) -> str:
    # shlex removes quotes and backslashes before optparse sees the token: "-r", -"r".
    return token.replace('"', "").replace("'", "").replace("\\", "")


def _forbidden_option(token: str) -> str | None:
    """The option a token would select if it is a forbidden one, else None."""
    bare = _bare(token)
    if bare.startswith("--"):
        name = bare[2:].split("=", 1)[0].lower()
        if not name or name in ALLOWED_EXACT_LONG:
            return None
        for option in FORBIDDEN_LONG:
            if option.startswith(name):
                return f"--{option}"
        return None
    if bare.startswith("-") and len(bare) >= 2:
        # -C is pip's --config-settings; the other capitals are no pip option at all.
        if bare[1] == "C":
            return None
        return FORBIDDEN_SHORT.get(bare[1].lower())
    return None


# Next to a package pip honours only these; every other option on that line is silently
# ignored (pip's handle_line). "torch --index-url .../whl/cpu" would therefore download the
# ~3 GB CUDA build from PyPI instead of the CPU build, so such lines are refused.
PACKAGE_LINE_OPTIONS = ("hash", "config-settings")


def _ignored_on_package_line(token: str) -> str | None:
    """The option name when pip would ignore this token next to a package, else None."""
    bare = _bare(token)
    if bare.startswith("--"):
        name = bare[2:].split("=", 1)[0].lower()
        if not name or any(option.startswith(name) for option in PACKAGE_LINE_OPTIONS):
            return None
        return f"--{name}"
    if bare.startswith("-") and len(bare) >= 2 and bare[1] != "C":
        return f"-{bare[1]}"
    return None


def _takes_separate_value(token: str) -> bool:
    """True for "-C" or "--config-settings"/"--hash" without "=": optparse then reads the
    next token as their value, even when it starts with "-"."""
    bare = _bare(token)
    if bare == "-C":
        return True
    if bare.startswith("--") and "=" not in bare:
        name = bare[2:].lower()
        return bool(name) and any(option.startswith(name) for option in PACKAGE_LINE_OPTIONS)
    return False


def _package_and_options(line: str) -> tuple[str, list[str]]:
    """pip's break_args_options: the package part ends at the first token starting with -."""
    tokens = line.split(" ")
    for index, token in enumerate(tokens):
        if token.startswith("-"):
            return " ".join(tokens[:index]).strip(), " ".join(tokens[index:]).split()
    return line.strip(), []


def normalize_requirements(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if text.startswith("﻿"):
        text = text[1:]
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def validate_requirements(text: str) -> list[dict]:
    """Problems as [{line, message}]; an empty list means the text may be saved."""
    errors: list[dict] = []
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return [{"line": None, "message": "The text is not valid UTF-8."}]
    if size > MAX_REQUIREMENTS_BYTES:
        return [{"line": None, "message": f"The file is {size} bytes; the limit is 64 KB."}]
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_REQUIREMENTS_LINES:
        return [{"line": None, "message": f"The file has {len(lines)} lines; the limit is {MAX_REQUIREMENTS_LINES}."}]

    clean_lines: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        bad = next(
            (
                ch
                for ch in line
                if (unicodedata.category(ch) in ("Cc", "Zl", "Zp") and ch != "\t") or ch in BIDI_CONTROLS
            ),
            None,
        )
        if bad is not None:
            errors.append({"line": number, "message": f"Contains the control character U+{ord(bad):04X}."})
            continue
        if number <= 2 and line.startswith("#") and PEP263_RE.search(line):
            errors.append({"line": number, "message": "Encoding declarations (coding: ...) are not allowed; the file is always UTF-8."})
            continue
        if ENV_VAR_RE.search(line):
            errors.append({"line": number, "message": "Environment variables (${...}) are not supported."})
            continue
        clean_lines.append((number, line))

    for number, logical in _logical_lines(clean_lines):
        reported = set()
        for token in logical.split():
            option = _forbidden_option(token)
            if option and option not in reported:
                reported.add(option)
                errors.append({"line": number, "message": f"{option} is not allowed: {FORBIDDEN_REASON}."})
        if reported:
            continue
        package, options = _package_and_options(logical)
        if not package:
            continue
        value_follows = False
        for token in options:
            if value_follows:  # "-C --global-option=x" is one config setting, not an option
                value_follows = False
                continue
            if _takes_separate_value(token):
                value_follows = True
                continue
            option = _ignored_on_package_line(token)
            if option and option not in reported:
                reported.add(option)
                errors.append(
                    {
                        "line": number,
                        "message": f"pip ignores {option} next to a package. Put it on a line of its own, "
                        "where it applies to the whole file.",
                    }
                )
    return errors


def _logical_lines(lines: list[tuple[int, str]]):
    """pip's join_lines + ignore_comments: (first physical line number, text)."""
    pending: list[str] = []
    first = None
    for number, line in lines:
        if not line.endswith("\\") or COMMENT_RE.match(line):
            if COMMENT_RE.match(line):
                line = " " + line
            if pending:
                pending.append(line)
                joined, number = "".join(pending), first
                pending, first = [], None
            else:
                joined = line
            joined = COMMENT_RE.sub("", joined).strip()
            if joined:
                yield number, joined
        else:
            if not pending:
                first = number
            pending.append(line.strip("\\"))
    if pending:
        joined = COMMENT_RE.sub("", "".join(pending)).strip()
        if joined:
            yield first, joined


def has_requirements(text: str) -> bool:
    """True when some line names a package (not only options or comments)."""
    numbered = list(enumerate(text.split("\n"), start=1))
    return any(not logical.split()[0].startswith("-") for _, logical in _logical_lines(numbered))


def requirement_count(text: str) -> int:
    """Lines naming a package (options such as --index-url do not count)."""
    numbered = list(enumerate(text.split("\n"), start=1))
    return sum(1 for _, logical in _logical_lines(numbered) if not logical.split()[0].startswith("-"))


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def constraints_digest() -> str:
    """Changes when the image's pinned packages change (a rebuilt image with a new lock)."""
    try:
        return hashlib.sha256(CONSTRAINTS_FILE.read_bytes()).hexdigest()
    except OSError:
        return ""


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def read_baseline_state() -> dict:
    """What the last finished job installed from baseline.txt ({} when nothing is known)."""
    try:
        data = json.loads(BASELINE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --- small file helpers ----------------------------------------------------------------


def write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Kernels (same uid) and a human with `docker exec` may read it.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def empty_directory(path: Path) -> int:
    """Delete everything inside path (a mount point stays). Returns the entries removed."""
    removed = 0
    path.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(path):
        entry_path = Path(entry.path)
        try:
            remove_tree(entry_path)
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def disk_usage(root: Path) -> int:
    """Allocated bytes below root, like du: symlinks not followed, hard links counted once."""
    total = 0
    seen: set[tuple[int, int]] = set()
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if info.st_nlink > 1:
                        key = (info.st_dev, info.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                    total += info.st_blocks * 512
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
    return total


def utf8_safe_end(data: bytes) -> int:
    """Length of data without a multi-byte UTF-8 sequence cut off at the end."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 == 0x80:  # continuation byte: keep looking for the lead byte
            continue
        if byte >= 0xC0:
            needed = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return len(data) - back if needed > back else len(data)
        return len(data)
    return len(data)


# --- sizes (walked in a background thread) ---------------------------------------------


class Sizes:
    def __init__(self):
        self._lock = threading.Lock()
        self._values = {"venv_bytes": None, "cache_bytes": None, "computed_at": None}
        self._computed_monotonic = 0.0
        self._running = False
        self._again = False

    def snapshot(self) -> dict:
        with self._lock:
            values = dict(self._values)
            stale = time.monotonic() - self._computed_monotonic > SIZES_MAX_AGE
        if stale:
            self.refresh()
        # Free space is one statvfs call; always current.
        try:
            stats = os.statvfs(CUSTOM_DIR)
            values["free_bytes"] = stats.f_bavail * stats.f_frsize
            values["total_bytes"] = stats.f_blocks * stats.f_frsize
        except OSError:
            values["free_bytes"] = values["total_bytes"] = None
        return values

    @staticmethod
    def measure() -> dict:
        """The same numbers as snapshot(), measured now (for the command line)."""
        values = {"venv_bytes": disk_usage(VENV_DIR), "cache_bytes": disk_usage(PIP_CACHE_DIR)}
        try:
            stats = os.statvfs(CUSTOM_DIR)
            values.update(free_bytes=stats.f_bavail * stats.f_frsize, total_bytes=stats.f_blocks * stats.f_frsize)
        except OSError:
            values.update(free_bytes=None, total_bytes=None)
        return values

    def refresh(self) -> None:
        with self._lock:
            if self._running:
                self._again = True  # a job just changed something: walk once more
                return
            self._running = True
        threading.Thread(target=self._walk, name="sizes", daemon=True).start()

    def _walk(self) -> None:
        while True:
            try:
                venv = disk_usage(VENV_DIR)
                cache = disk_usage(PIP_CACHE_DIR)
            except Exception:  # never let a walk kill the thread silently
                log.exception("measuring sizes failed")
                venv = cache = None
            with self._lock:
                self._values = {"venv_bytes": venv, "cache_bytes": cache, "computed_at": utc_now()}
                self._computed_monotonic = time.monotonic()
                if not self._again:
                    self._running = False
                    return
                self._again = False


# --- jobs ------------------------------------------------------------------------------


class Busy(Exception):
    """A job (or a cache clean-up) is running."""


class JobRunner:
    """One job at a time in a worker thread; state persisted in job.json."""

    def __init__(self, sizes: Sizes):
        self._sizes = sizes
        self._lock = threading.Lock()
        self._maintenance = False
        self._process: subprocess.Popen | None = None
        self._cancel_requested = False
        self._shutting_down = False
        self._thread: threading.Thread | None = None
        self._log_handle = None
        self._log_written = 0
        self._log_capped = False
        self._installed_cache: tuple[int, list] | None = None
        self._lock_fd: int | None = None
        self._job_baseline = ""
        self.job = self._load_job()

    # -- state --------------------------------------------------------------------------

    @staticmethod
    def _empty_job() -> dict:
        return {
            "id": None,
            "action": None,
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "message": None,
        }

    def _load_job(self) -> dict:
        try:
            data = json.loads(JOB_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty_job()
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", JOB_FILE, exc)
            return self._empty_job()
        job = self._empty_job()
        if isinstance(data, dict):
            job.update({key: data.get(key) for key in job})
        if job["status"] == "running":
            # The container stopped or crashed while pip ran; the venv may be half done.
            job.update(
                status="interrupted",
                finished_at=job["finished_at"] or utc_now(),
                message="The dependency runner stopped while this job was running. Run Install / update again.",
            )
            self._persist(job)
        return job

    @staticmethod
    def _persist(job: dict) -> None:
        try:
            write_atomic(JOB_FILE, (json.dumps(job, indent=1) + "\n").encode("utf-8"))
        except OSError as exc:
            log.error("cannot write %s: %s", JOB_FILE, exc)

    def running(self) -> bool:
        with self._lock:
            return self.job["status"] == "running"

    def job_copy(self) -> dict:
        with self._lock:
            return dict(self.job)

    def installed(self) -> list[dict]:
        try:
            stamp = VENV_SITE_PACKAGES.stat().st_mtime_ns
        except OSError:
            return []
        cached = self._installed_cache
        if cached and cached[0] == stamp:
            return cached[1]
        packages = {}
        for dist in importlib.metadata.distributions(path=[str(VENV_SITE_PACKAGES)]):
            try:
                name = dist.metadata["Name"]
                version = dist.version
            except Exception:  # a half-written dist-info (e.g. after a cancelled job)
                continue
            if name:
                packages.setdefault(name.lower(), {"name": name, "version": version})
        result = sorted(packages.values(), key=lambda item: item["name"].lower())
        self._installed_cache = (stamp, result)
        return result

    # -- mutations guarded against a running job ----------------------------------------------

    def save_requirements(self, text: str) -> None:
        with self._lock:
            if self.job["status"] == "running" or self._maintenance:
                raise Busy
            write_atomic(REQUIREMENTS_FILE, text.encode("utf-8"))

    def clear_cache(self) -> int:
        with self._lock:
            if self.job["status"] == "running" or self._maintenance:
                raise Busy
            self._maintenance = True
        try:
            return empty_directory(PIP_CACHE_DIR)
        finally:
            with self._lock:
                self._maintenance = False
            self._sizes.refresh()

    def save_baseline(self, text: str) -> bool:
        """Store the project's requirements.txt; True when it differs from the stored one."""
        with self._lock:
            if self.job["status"] == "running" or self._maintenance:
                raise Busy
            if read_text_file(BASELINE_FILE) == text and BASELINE_FILE.exists():
                return False
            write_atomic(BASELINE_FILE, text.encode("utf-8"))
            return True

    def _record_baseline(self, ok: bool) -> None:
        state = {
            "sha256": self._job_baseline,
            "constraints": constraints_digest(),
            "python": PYTHON_XY,
            "ok": ok,
            "finished_at": utc_now(),
        }
        try:
            write_atomic(BASELINE_STATE_FILE, (json.dumps(state, indent=1) + "\n").encode("utf-8"))
        except OSError as exc:
            log.error("cannot write %s: %s", BASELINE_STATE_FILE, exc)

    def baseline_check(self, text: str) -> tuple[bool, str]:
        """(up to date, why): may a deploy skip the install for this requirements.txt?

        Skipped only when the last job installed exactly this text with the same image and
        finished, the venv works, and pip (offline, dry run) has nothing left to install.
        """
        if not has_requirements(text):
            return True, "lists no packages; nothing extra to install"
        state = read_baseline_state()
        if not state.get("sha256") or state.get("sha256") == text_digest(""):
            return False, "first install"
        if state.get("sha256") != text_digest(text):
            return False, "changed since the last install"
        if state.get("constraints") != constraints_digest() or state.get("python") != PYTHON_XY:
            return False, "the image's own packages changed since the last install"
        if not state.get("ok"):
            return False, "the last install did not finish"
        env = self.job_env()
        problem = self._venv_problem(env)
        if problem:
            return False, problem
        if not self._baseline_satisfied(env):
            return False, "some of its packages are missing from the environment"
        return True, "unchanged and already installed"

    @staticmethod
    def _baseline_satisfied(env: dict) -> bool:
        """pip's own answer, offline: would installing baseline.txt change anything?"""
        argv = [str(VENV_PYTHON), "-m", "pip", "install", "--dry-run", "--no-index", "--quiet", "--report", "-",
                "--disable-pip-version-check", "--no-input", "--constraint", str(CONSTRAINTS_FILE),
                "-r", str(BASELINE_FILE)]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=JOB_TMP_DIR, timeout=300,
                                    check=False, stdin=subprocess.DEVNULL)
            report = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False
        return isinstance(report, dict) and not report.get("install")

    def start(self, action: str) -> dict:
        with self._lock:
            if self.job["status"] == "running" or self._maintenance or self._shutting_down:
                raise Busy
            # Across processes too: a one-off `deps_runner.py baseline --local` container and
            # the server share the volume, and only one pip may change the venv at a time.
            lock_fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(lock_fd)
                raise Busy from None
            try:
                # What this job installs from the project's requirements.txt. Marked unfinished
                # now, so a job killed with its container never counts as installed.
                self._job_baseline = text_digest(read_text_file(BASELINE_FILE))
                self._record_baseline(False)
                # Truncate the log before the new job id is visible, so a reader never gets
                # the previous job's output under the new id.
                self._log_handle = open(LOG_FILE, "wb", buffering=0)
            except BaseException:
                os.close(lock_fd)
                raise
            self._lock_fd = lock_fd
            self._log_written = 0
            self._log_capped = False
            self.job = {
                "id": secrets.token_hex(8),
                "action": action,
                "status": "running",
                "started_at": utc_now(),
                "finished_at": None,
                "exit_code": None,
                "message": "Removing the environment" if action == "reset" else "Preparing the environment",
            }
            self._cancel_requested = False
            job = dict(self.job)
            self._persist(job)
            self._thread = threading.Thread(target=self._run, args=(action,), name="job", daemon=True)
            self._thread.start()
        return job

    def cancel(self) -> dict:
        with self._lock:
            if self.job["status"] != "running":
                raise Busy
            self._cancel_requested = True
            self.job["message"] = "Cancelling…"
            process = self._process
            job = dict(self.job)
        if process is not None:
            self._terminate(process)
        return job

    def shutdown(self) -> None:
        """SIGTERM: stop a running job, record it as interrupted, wait for the worker."""
        with self._lock:
            self._shutting_down = True
            self._cancel_requested = True
            process = self._process
            thread = self._thread
        if process is not None:
            self._terminate(process)
        if thread is not None:
            thread.join(CANCEL_KILL_AFTER + 5)

    def _terminate(self, process: subprocess.Popen) -> None:
        _signal_group(process, signal.SIGTERM)

        def kill_later():
            try:
                process.wait(CANCEL_KILL_AFTER)
            except subprocess.TimeoutExpired:
                self._write_log(f"\n==> Still running after {CANCEL_KILL_AFTER:.0f} s; killing it.\n")
                _signal_group(process, signal.SIGKILL)

        threading.Thread(target=kill_later, name="kill-later", daemon=True).start()

    # -- worker ---------------------------------------------------------------------------

    def _set_message(self, message: str) -> None:
        with self._lock:
            self.job["message"] = message

    def _write_log(self, text: str) -> None:
        self._write_log_bytes(text.encode("utf-8", errors="replace"))

    def _write_log_bytes(self, data: bytes) -> None:
        handle = self._log_handle
        if handle is None or self._log_capped:
            return
        room = MAX_LOG_BYTES - self._log_written
        if len(data) > room:
            data = data[:room]
            self._log_capped = True
        try:
            handle.write(data)
            self._log_written += len(data)
            if self._log_capped:
                handle.write(b"\n==> The log reached 20 MB; further output is not recorded.\n")
        except ValueError:
            pass  # closed by the finishing job while a late kill notice was written
        except OSError as exc:
            log.error("cannot write %s: %s", LOG_FILE, exc)

    def _command(self, argv: list[str], env: dict, cwd: Path) -> int:
        """Run argv in its own process group, streaming output into the log."""
        with self._lock:
            if self._cancel_requested:
                return -signal.SIGTERM
            # Logged only when the command really starts (a cancel may have come first).
            self._write_log("$ " + shlex.join(argv) + "\n")
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
            self._process = process
        reader = threading.Thread(target=self._pump, args=(process.stdout,), name="log-pump", daemon=True)
        reader.start()
        try:
            code = process.wait()
        finally:
            # A build backend that daemonised and kept the pipe open must not hang the job.
            reader.join(5)
            if reader.is_alive():
                _signal_group(process, signal.SIGKILL)
                reader.join(5)
            with self._lock:
                self._process = None
        return code

    def _pump(self, stream) -> None:
        with stream:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not chunk:
                    return
                self._write_log_bytes(chunk)

    def _cancelled(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def _run(self, action: str) -> None:
        status, exit_code, message = "failed", None, "The job failed"
        try:
            status, exit_code, message = self._run_steps(action)
        except Exception as exc:  # report instead of leaving the job "running" forever
            log.exception("job failed unexpectedly")
            self._write_log(f"\n==> Internal error: {type(exc).__name__}: {exc}\n")
            status, message = "failed", f"Internal error: {type(exc).__name__}"
        finally:
            with self._lock:
                if self._shutting_down and status != "succeeded":
                    status = "interrupted"
                    message = "The dependency runner was stopped while this job was running. Run Install / update again."
                self.job.update(status=status, exit_code=exit_code, message=message, finished_at=utc_now())
                job = dict(self.job)
                self._persist(job)
            self._write_log(f"\n==> {job['status'].capitalize()}: {message}\n")
            # A killed pip leaves its build directories and a partial download (possibly
            # hundreds of MB) in TMPDIR; free that space now rather than at the next job.
            try:
                empty_directory(JOB_TMP_DIR)
            except OSError as exc:
                log.warning("cannot empty %s: %s", JOB_TMP_DIR, exc)
            if self._log_handle is not None:
                self._log_handle.close()
                self._log_handle = None
            self._record_baseline(job["status"] == "succeeded")
            self._installed_cache = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)          # releases the flock
                self._lock_fd = None
            self._sizes.refresh()
            log.info("job %s (%s) finished: %s", job["id"], action, status)

    @staticmethod
    def job_env() -> dict:
        """The environment of every pip run: the shared download cache, TMPDIR on the volume."""
        return {
            "PATH": f"{VENV_DIR}/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "PIP_CACHE_DIR": str(PIP_CACHE_DIR),
            "TMPDIR": str(JOB_TMP_DIR),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }

    def _run_steps(self, action: str) -> tuple[str, int | None, str]:
        env = self.job_env()
        self._write_log(f"==> {'Reset & reinstall' if action == 'reset' else 'Install / update'} started {utc_now()}\n")

        if action == "reset" and VENV_DIR.exists():
            self._write_log(f"==> Removing {VENV_DIR}\n")
            remove_tree(VENV_DIR)
        if self._cancelled():
            return "cancelled", None, "Cancelled before pip started"

        # pip leaves partial downloads and build directories behind when it is killed.
        empty_directory(JOB_TMP_DIR)

        self._set_message("Checking the environment")
        problem = self._venv_problem(env)
        if problem:
            if VENV_DIR.exists():
                self._write_log(f"==> {problem}; recreating {VENV_DIR}\n")
                remove_tree(VENV_DIR)
            self._set_message("Creating the environment")
            code = self._command(
                [sys.executable, "-m", "venv", "--system-site-packages", "--without-pip", str(VENV_DIR)],
                env,
                JOB_TMP_DIR,
            )
            if self._cancelled():
                return "cancelled", code, "Cancelled while creating the environment"
            if code != 0:
                return "failed", code, f"Creating the environment failed (exit status {code})"

        # The project's requirements.txt (baseline) and the page's list, in one pip run.
        files = [path for path in (BASELINE_FILE, REQUIREMENTS_FILE) if has_requirements(read_text_file(path))]
        if not files:
            self._write_log("==> The requirements lists are empty; the environment is ready.\n")
            return "succeeded", 0, "No packages requested; the environment is ready"
        names = " and ".join("requirements.txt of the project" if path == BASELINE_FILE else "the page's list"
                             for path in files)
        self._write_log(f"==> Installing {names}\n")

        self._set_message("Running pip")
        argv = [
            str(VENV_PYTHON),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--progress-bar",
            "off",
            "--constraint",
            str(CONSTRAINTS_FILE),
        ]
        for path in files:
            argv += ["-r", str(path)]
        code = self._command(argv, env, JOB_TMP_DIR)
        if self._cancelled():
            return "cancelled", code, "Cancelled; packages installed before the cancel are kept"
        if code != 0:
            return "failed", code, f"pip exited with status {code}; see the log"
        return "succeeded", 0, "Packages installed. Restart the kernel to use them."

    def _venv_problem(self, env: dict) -> str | None:
        """Why the venv cannot be used as it is, or None when it is fine."""
        if not VENV_PYTHON.exists():
            return "The environment does not exist yet" if not VENV_DIR.exists() else "The environment has no Python"
        want = f"{sys.version_info.major}.{sys.version_info.minor}"
        try:
            result = subprocess.run(
                [str(VENV_PYTHON), "-c", "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.prefix)"],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"The environment's Python does not start ({type(exc).__name__})"
        lines = result.stdout.split()
        if result.returncode != 0 or len(lines) != 2:
            return f"The environment's Python does not start (exit status {result.returncode})"
        if lines[0] != want:
            return f"The environment was made for Python {lines[0]}, the image has {want}"
        if Path(lines[1]) != VENV_DIR:
            return "The environment is damaged (wrong prefix)"
        return None


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            log.warning("cannot signal process group %s: %s", process.pid, exc)


# --- HTTP ------------------------------------------------------------------------------


def read_token(path: str) -> bytes:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SystemExit(f"deps: refusing to start: cannot read token file {path}: {exc.strerror or exc}") from None
    # Same rule as the dashboard: tolerate one trailing newline added by an editor.
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    if not data:
        raise SystemExit(f"deps: refusing to start: token file {path} is empty")
    if len(data) < MIN_TOKEN_LENGTH:
        raise SystemExit(f"deps: refusing to start: token file {path} holds fewer than {MIN_TOKEN_LENGTH} characters")
    return data


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.body = {"error": message, **extra}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "deps-runner"
    sys_version = ""
    timeout = SOCKET_TIMEOUT
    # Set in main().
    username = b""
    password = b""  # the runner token
    runner: JobRunner
    sizes: Sizes

    # -- plumbing ----------------------------------------------------------------------------

    def log_message(self, format, *args):  # noqa: A002 - signature of the base class
        log.info("%s %s", self.address_string(), format % args)

    def log_request(self, code="-", size="-"):
        # Polling (state, log, health) succeeds every few seconds; keep failures and changes.
        # A malformed request line is answered before self.path exists.
        path = getattr(self, "path", "").split("?", 1)[0]
        if self.command == "GET" and isinstance(code, int) and code < 400 and path in ("/health", "/state", "/log"):
            return
        super().log_request(code, size)

    def _send_json(self, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        try:
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # The client gave up (a timeout of its own, a closed tab); nothing to report.
            self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        # Malformed requests and unknown methods get JSON too, not the base class's HTML page.
        self.close_connection = True
        reason = message or self.responses.get(code, ("error",))[0]
        self._send_json(code, {"error": str(reason).lower()})

    def _authorized(self) -> bool:
        value = self.headers.get("Authorization") or ""
        scheme, _, credentials = value.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        username, separator, password = decoded.partition(b":")
        if not separator:
            return False
        # Both parts every time, so timing does not reveal which one was wrong.
        username_ok = secrets.compare_digest(username, self.username)
        password_ok = secrets.compare_digest(password, self.password)
        return username_ok and password_ok

    def _read_json(self, required: bool) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(411, "chunked request bodies are not supported")
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header is not None else 0
        except ValueError:
            raise HttpError(400, "invalid Content-Length") from None
        if length < 0:
            raise HttpError(400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise HttpError(413, "request body larger than 192 KB")
        if length == 0:
            if required:
                raise HttpError(400, "a JSON body is required")
            return {}
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HttpError(415, "the body must be application/json")
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, OSError):
            self.close_connection = True
            raise HttpError(408, "the request body did not arrive in time") from None
        if len(raw) != length:
            self.close_connection = True
            raise HttpError(400, "the request body is shorter than its Content-Length")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            # RecursionError: deeply nested arrays or objects.
            raise HttpError(400, "the body is not valid JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "the body must be a JSON object")
        return body

    def _dispatch(self, method: str) -> None:
        if not self._authorized():
            # The body (if any) is never read; HTTP/1.0 closes the connection anyway.
            self._send_json(401, {"error": "unauthorized"}, {"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'})
            return
        parts = urllib.parse.urlsplit(self.path)
        route = ROUTES.get((method, parts.path))
        if route is None:
            known = any(path == parts.path for _, path in ROUTES)
            self._send_json(405 if known else 404, {"error": "method not allowed" if known else "not found"})
            return
        try:
            status, body = route(self, urllib.parse.parse_qs(parts.query))
        except HttpError as exc:
            status, body = exc.status, exc.body
        except Exception:
            log.exception("%s %s failed", method, parts.path)
            status, body = 500, {"error": "internal error in the dependency runner"}
        self._send_json(status, body)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    # Answered like any other method: 401 without credentials, then 405 for a known path.
    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def do_TRACE(self):
        self._dispatch("TRACE")

    # -- routes ------------------------------------------------------------------------------

    def health(self, _query):
        return 200, {"status": "ok"}

    def state(self, _query):
        try:
            requirements = REQUIREMENTS_FILE.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            requirements = ""
        baseline = read_text_file(BASELINE_FILE)
        applied = read_baseline_state()
        return 200, {
            "python": platform.python_version(),
            "job": self.runner.job_copy(),
            "requirements": requirements,
            # The project's requirements.txt as last deployed; read-only here.
            "baseline": {
                "text": baseline,
                "packages": requirement_count(baseline),
                "installed": bool(applied.get("ok")) and applied.get("sha256") == text_digest(baseline),
                "finished_at": applied.get("finished_at"),
            },
            "installed": self.runner.installed(),
            "sizes": self.sizes.snapshot(),
            "constraints_count": _constraints_count(),
        }

    def log_chunk(self, query):
        values = query.get("offset") or ["0"]
        # Plain ASCII digits only: int() alone would also take "+5", "1_0", " 5" or Arabic digits.
        if not OFFSET_RE.fullmatch(values[0]):
            raise HttpError(400, "offset must be a non-negative integer")
        offset = int(values[0])
        job = self.runner.job_copy()
        running = job["status"] == "running"
        reset = False
        try:
            with open(LOG_FILE, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if offset > size:
                    # A new job truncated the log since the client's last call.
                    offset, reset = 0, True
                handle.seek(offset)
                data = handle.read(MAX_LOG_CHUNK)
        except FileNotFoundError:
            data, reset = b"", offset > 0
            offset = 0
        # Do not split a UTF-8 character between two calls; the rest comes next time.
        end = utf8_safe_end(data) if (running or len(data) == MAX_LOG_CHUNK) else len(data)
        data = data[:end]
        return 200, {
            "offset": offset,
            "next_offset": offset + len(data),
            "text": data.decode("utf-8", errors="replace"),
            "running": running,
            "job_id": job["id"],
            "reset": reset,
        }

    def put_requirements(self, _query):
        body = self._read_json(required=True)
        text = body.get("text")
        if not isinstance(text, str):
            raise HttpError(400, "text must be a string")
        text = normalize_requirements(text)
        errors = validate_requirements(text)
        if errors:
            raise HttpError(400, "invalid requirements", errors=errors)
        try:
            self.runner.save_requirements(text)
        except Busy:
            raise HttpError(409, "a job is running; save after it has finished", job=self.runner.job_copy()) from None
        return 200, {"requirements": text, "saved_at": utc_now()}

    def put_baseline(self, _query):
        """The project's requirements.txt from a deploy (deps_runner.py baseline); not proxied
        by the dashboard, so the page cannot change it."""
        body = self._read_json(required=True)
        text = body.get("text")
        if not isinstance(text, str):
            raise HttpError(400, "text must be a string")
        text = normalize_requirements(text)
        errors = validate_requirements(text)
        if errors:
            raise HttpError(400, "invalid requirements", errors=errors)
        try:
            changed = self.runner.save_baseline(text)
        except Busy:
            raise HttpError(409, "a job is running; try again after it has finished", job=self.runner.job_copy()) from None
        up_to_date, reason = self.runner.baseline_check(text)
        return 200, {"changed": changed, "up_to_date": up_to_date, "reason": reason}

    def post_job(self, _query):
        body = self._read_json(required=True)
        action = body.get("action")
        if action not in JOB_ACTIONS:
            raise HttpError(400, "action must be install or reset")
        try:
            job = self.runner.start(action)
        except Busy:
            raise HttpError(409, "a job is already running", job=self.runner.job_copy()) from None
        return 202, {"job": job}

    def post_cancel(self, _query):
        self._read_json(required=False)
        try:
            job = self.runner.cancel()
        except Busy:
            raise HttpError(409, "no job is running", job=self.runner.job_copy()) from None
        return 200, {"job": job}

    def post_cache_clear(self, _query):
        self._read_json(required=False)
        try:
            removed = self.runner.clear_cache()
        except Busy:
            raise HttpError(409, "a job is running; clear the cache after it has finished", job=self.runner.job_copy()) from None
        return 200, {"cleared": True, "removed_entries": removed}


ROUTES = {
    ("GET", "/health"): Handler.health,
    ("GET", "/state"): Handler.state,
    ("GET", "/log"): Handler.log_chunk,
    ("PUT", "/requirements"): Handler.put_requirements,
    ("PUT", "/baseline"): Handler.put_baseline,
    ("POST", "/jobs"): Handler.post_job,
    ("POST", "/cancel"): Handler.post_cancel,
    ("POST", "/cache/clear"): Handler.post_cache_clear,
}

_constraints_cache: tuple[float, int] | None = None


def _constraints_count() -> int | None:
    global _constraints_cache
    try:
        mtime = CONSTRAINTS_FILE.stat().st_mtime
        if _constraints_cache and _constraints_cache[0] == mtime:
            return _constraints_cache[1]
        lines = CONSTRAINTS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    count = sum(1 for line in lines if line.strip() and not line.lstrip().startswith("#"))
    _constraints_cache = (mtime, count)
    return count


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


# --- command line: the installer's side of a deploy ------------------------------------

BUSY_WAIT_LIMIT = 2 * 3600       # seconds a deploy waits for a job started on the page


def _say(text: str) -> None:
    print(f"==> {text}", flush=True)


def _human(count) -> str:
    if count is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} TB"


def _say_sizes(sizes: dict | None) -> None:
    if sizes:
        _say(f"Custom packages {_human(sizes.get('venv_bytes'))}, download cache {_human(sizes.get('cache_bytes'))}, "
             f"free disk {_human(sizes.get('free_bytes'))}")


def _read_stdin_requirements() -> tuple[str, list[str]]:
    """The requirements.txt on stdin, normalized, and the problems the page would report."""
    data = sys.stdin.buffer.read(MAX_REQUIREMENTS_BYTES + 1)
    if len(data) > MAX_REQUIREMENTS_BYTES:
        return "", [f"The file is larger than {MAX_REQUIREMENTS_BYTES // 1024} KB."]
    try:
        text = normalize_requirements(data.decode("utf-8"))
    except UnicodeDecodeError:
        return "", ["The file is not UTF-8 text."]
    problems = [f"line {e['line']}: {e['message']}" if e["line"] else e["message"] for e in validate_requirements(text)]
    return text, problems


def _refuse(problems: list[str]) -> int:
    print("requirements.txt is not accepted (the Dependencies page's rules):", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 2


def cli_check() -> int:
    """Validate a requirements.txt on stdin before a deploy recreates anything."""
    text, problems = _read_stdin_requirements()
    if problems:
        return _refuse(problems)
    count = requirement_count(text)
    _say(f"requirements.txt: {count} package line(s), accepted" if count else "requirements.txt lists no packages")
    return 0


class _ErrorLines:
    """pip's error lines from a log, to name the failing requirement at the end."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._partial = ""
        self._in_conflict = False

    def feed(self, text: str) -> None:
        text = self._partial + text
        *complete, self._partial = text.split("\n")
        for line in complete:
            if line.startswith("ERROR:"):
                self.lines.append(line)
                self._in_conflict = False
            elif line.strip() == "The conflict is caused by:":
                self.lines.append(line.strip())
                self._in_conflict = True
            elif self._in_conflict:
                if line.strip():
                    self.lines.append("  " + line.strip())
                else:
                    self._in_conflict = False


def _finish(job: dict, errors: _ErrorLines, sizes: dict | None) -> int:
    if job.get("status") == "succeeded":
        _say("Packages from requirements.txt are installed. Restart running kernels to use new ones.")
        _say_sizes(sizes)
        return 0
    _say(f"Installing requirements.txt ended: {job.get('status')}: {job.get('message')}")
    for line in errors.lines[-15:]:
        print(f"    {line}", flush=True)
    _say("pip resolves and builds everything before it changes the environment, so a failed install keeps "
         "the packages installed before. The full log is on the Dependencies page (and in job.log).")
    _say_sizes(sizes)
    return 1


class _Api:
    """HTTP to the runner in this container, as the dashboard talks to it."""

    def __init__(self) -> None:
        username = (os.environ.get("STATS_USER") or "jupyter").encode("utf-8")
        token = read_token(os.environ.get("DEPS_TOKEN_FILE") or "/run/secrets/deps_token")
        self._auth = "Basic " + base64.b64encode(username + b":" + token).decode("ascii")

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
        try:
            headers = {"Authorization": self._auth, "Accept": "application/json"}
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read() or b"{}")
            return response.status, payload if isinstance(payload, dict) else {}
        finally:
            connection.close()

    def call_when_free(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
        """Like call(), but waits while a job (e.g. one started on the page) runs."""
        waited = 0.0
        while True:
            status, payload = self.call(method, path, body, timeout)
            if status != 409 or waited >= BUSY_WAIT_LIMIT:
                return status, payload
            if waited == 0:
                _say("A package job started on the Dependencies page is running; waiting for it to finish…")
            time.sleep(3)
            waited += 3


def _baseline_remote(api: _Api, text: str) -> int:
    status, body = api.call_when_free("PUT", "/baseline", {"text": text}, timeout=600)
    if status == 400:
        return _refuse([f"line {e.get('line')}: {e.get('message')}" for e in body.get("errors", [])] or [body.get("error", "")])
    if status != 200:
        _say(f"The package runner did not take requirements.txt: {body.get('error', status)}")
        return 1
    _say(f"requirements.txt: {body.get('reason')}")
    if body.get("up_to_date"):
        _say_sizes(api.call("GET", "/state")[1].get("sizes"))
        return 0
    status, body = api.call_when_free("POST", "/jobs", {"action": "install"})
    if status != 202:
        _say(f"The package runner did not start the install: {body.get('error', status)}")
        return 1
    job_id = body["job"]["id"]
    errors, offset = _ErrorLines(), 0
    try:
        while True:
            status, chunk = api.call("GET", f"/log?offset={offset}")
            if status != 200 or chunk.get("job_id") != job_id:
                break
            text = chunk.get("text", "")
            sys.stdout.write(text)
            sys.stdout.flush()
            errors.feed(text)
            offset = chunk.get("next_offset", offset)
            if not chunk.get("running") and not text:
                break
            if not text:
                time.sleep(1)
    except KeyboardInterrupt:
        api.call("POST", "/cancel")
        _say("Interrupted; the install job was cancelled.")
        return 130
    state = api.call("GET", "/state")[1]
    job = state.get("job") or {}
    if job.get("id") != job_id:
        job = {"status": "unknown", "message": "another job started meanwhile"}
    return _finish(job, errors, state.get("sizes"))


def _baseline_local(text: str) -> int:
    """With statistics off there is no server: run the same job in this one-off container."""
    prepare_dirs()
    runner = JobRunner(Sizes())
    try:
        runner.save_baseline(text)
    except Busy:
        _say("Another package job is running; try again when it has finished.")
        return 1
    up_to_date, reason = runner.baseline_check(text)
    _say(f"requirements.txt: {reason}")
    if up_to_date:
        _say_sizes(Sizes.measure())
        return 0
    try:
        runner.start("install")
    except Busy:
        _say("Another package job holds the environment; try again when it has finished.")
        return 1

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    errors, offset = _ErrorLines(), 0

    def pump() -> None:
        nonlocal offset
        with contextlib.suppress(FileNotFoundError), open(LOG_FILE, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
            offset += len(data)
            text = data.decode("utf-8", errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            errors.feed(text)

    try:
        while runner._thread is not None and runner._thread.is_alive():
            pump()
            time.sleep(0.5)
    except KeyboardInterrupt:
        runner.shutdown()
    pump()
    return _finish(runner.job_copy(), errors, Sizes.measure())


def cli_baseline(local: bool) -> int:
    """Make the custom environment match the project's requirements.txt (stdin)."""
    text, problems = _read_stdin_requirements()
    if problems:
        return _refuse(problems)
    if local:
        return _baseline_local(text)
    try:
        return _baseline_remote(_Api(), text)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        _say(f"The package runner in this container does not answer ({type(exc).__name__}: {exc}).")
        return 1


def prepare_dirs() -> None:
    try:
        CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
        JOB_TMP_DIR.mkdir(exist_ok=True)
        probe = CUSTOM_DIR / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise SystemExit(f"deps: refusing to start: {CUSTOM_DIR} is not writable: {exc.strerror or exc}") from None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s", stream=sys.stderr)
        if args == ["check"]:
            return cli_check()
        if args[0] == "baseline" and args[1:] in ([], ["--local"]):
            return cli_baseline(local=args[1:] == ["--local"])
        print("usage: deps_runner.py [check | baseline [--local]]", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s", stream=sys.stderr)
    Handler.username = (os.environ.get("STATS_USER") or "jupyter").encode("utf-8")
    Handler.password = read_token(os.environ.get("DEPS_TOKEN_FILE") or "/run/secrets/deps_token")
    prepare_dirs()
    # Stage 1 images created an empty site-packages mount point here; it is unused now.
    with contextlib.suppress(OSError):
        (CUSTOM_DIR / "site-packages").rmdir()

    sizes = Sizes()
    runner = JobRunner(sizes)
    Handler.sizes = sizes
    Handler.runner = runner
    sizes.refresh()

    server = Server(("0.0.0.0", PORT), Handler)
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("received %s; shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    serving = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    serving.start()
    log.info("listening on port %d (python %s, custom dir %s)", PORT, platform.python_version(), CUSTOM_DIR)
    stop.wait()

    server.shutdown()
    runner.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
