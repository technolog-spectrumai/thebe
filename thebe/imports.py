"""Copy-in of host directories: up to seven directories copied into <workspace>/imported/<name>/.

Deliberately a plain copy on Deploy / install, before JupyterLab starts: no bind mount, no
symbolic link, nothing that keeps a live connection to the source. JupyterLab keeps seeing only
its normal /workspace. One implementation for the builder and run.py (./run.sh import, install,
update); both reach it through `run.py import`.

Safety rules:
- Symbolic links are never followed, created or copied: in the source they are skipped and
  reported; in the destination a link where a directory should be stops that branch.
- A source must be a real, readable directory that neither is, lies inside, nor contains the
  workspace (which would copy the workspace into itself).
- Each destination belongs to one source, recorded in <dest>/.thebe-import. A later Deploy updates
  that copy in place: changed files are replaced atomically, unchanged ones are left alone, and
  nothing is ever deleted - files added in JupyterLab stay. A destination that belongs to another
  source, or that Thebe did not create, is refused instead of merged.
"""

from __future__ import annotations

import errno
import json
import os
import pwd
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from thebe.settings import has_control

MAX_IMPORT_DIRS = 7
IMPORT_DIR = "imported"                 # <workspace>/imported/<name>/
MARKER = ".thebe-import"                # in each destination: which source it copies
MAX_NAME_BYTES = 255
MAX_REPORTED = 20                       # skipped entries listed per source, then a count

Log = Callable[[str], None]

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class ImportDir:
    """One configured source: the path as written (resolved against config.yaml's directory)."""
    path: str
    name: str = ""          # destination name; '' = the source directory's own name

    def dest_name(self) -> str:
        return self.name or Path(self.path.rstrip("/") or "/").expanduser().name


@dataclass(frozen=True)
class PlannedImport:
    source: Path            # absolute, parents resolved; the directory itself is not a link
    name: str               # <workspace>/imported/<name>


@dataclass
class ImportResult:
    source: Path
    dest: Path
    copied: int = 0
    unchanged: int = 0
    bytes: int = 0
    dirs: int = 0
    skipped: list[str] = field(default_factory=list)      # "<relative path>: <why>"
    current: str = ""       # the entry being copied, for an error message


class ImportFailed(Exception):
    """Copying one source failed; the message names the source and what went wrong."""


# --------------------------------------------------------------------------
# config.yaml: the import_dirs list
# --------------------------------------------------------------------------

def name_problem(name: str) -> str:
    """Why `name` cannot be a directory name under imported/ ('' when it can)."""
    if name in ("", ".", ".."):
        return "is not a usable directory name"
    if "/" in name or "\0" in name or has_control(name):
        return "must not contain '/' or control characters"
    if name == MARKER or len(name.encode("utf-8", "surrogateescape")) > MAX_NAME_BYTES:
        return "is reserved or too long"
    return ""


def parse_import_dirs(value: object) -> tuple[list[ImportDir], list[str]]:
    """The import_dirs entries of config.yaml: a path, or a mapping with path and name.

    Empty entries are unused slots. At most MAX_IMPORT_DIRS are kept; more is a problem.
    """
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["import_dirs must be a list of directories (lines starting with '- ')."]
    dirs, problems = [], []
    for number, entry in enumerate(value, 1):
        where = f"import_dirs entry {number}"
        if isinstance(entry, str):
            path, name = entry, ""
        elif isinstance(entry, dict):
            unknown = [str(key) for key in entry if key not in ("path", "name")]
            if unknown:
                problems.append(f"{where} has unknown keys: {', '.join(unknown)} (only path and name).")
            path, name = entry.get("path", ""), entry.get("name") or ""
            if not isinstance(path, str) or not isinstance(name, str):
                problems.append(f"{where}: path and name must be text.")
                continue
        elif entry is None:
            continue
        else:
            problems.append(f"{where} must be a directory path or a mapping with path and name.")
            continue
        if not path.strip():
            if name:
                problems.append(f"{where} has a name but no path.")
            continue
        if has_control(path):
            problems.append(f"{where}: the path must not contain control characters.")
            continue
        dirs.append(ImportDir(path, name))
    if len(dirs) > MAX_IMPORT_DIRS:
        problems.append(f"import_dirs lists {len(dirs)} directories; at most {MAX_IMPORT_DIRS} are copied.")
        dirs = dirs[:MAX_IMPORT_DIRS]
    return dirs, problems


def render_import_dirs(dirs: list[ImportDir], scalar: Callable[[str], str]) -> list[str]:
    """config.yaml lines for import_dirs, with `scalar` quoting the text values."""
    if not dirs:
        return ["import_dirs: []"]
    lines = ["import_dirs:"]
    for item in dirs:
        if item.name:
            lines += [f"  - path: {scalar(item.path)}", f"    name: {scalar(item.name)}"]
        else:
            lines.append(f"  - {scalar(item.path)}")
    return lines


# --------------------------------------------------------------------------
# Checking the sources before anything is copied
# --------------------------------------------------------------------------

def default_workspace() -> Path:
    """The installer's default: ~/jupyter-workspace of the account (not of a changed $HOME)."""
    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        home = ""
    return Path(home or os.environ.get("HOME") or Path.home()) / "jupyter-workspace"


def _source_path(item: ImportDir, base_dir: Path) -> Path:
    path = Path(item.path).expanduser()
    return path if path.is_absolute() else base_dir / path


def _is_within(path: Path, other: Path) -> bool:
    return path == other or other in path.parents


def plan_imports(dirs: list[ImportDir], base_dir: Path, workspace: Path) -> tuple[list[PlannedImport], list[str]]:
    """The checked plan and every problem; nothing is copied while there is a problem.

    base_dir: where relative paths start (config.yaml's directory). workspace: the effective one.
    """
    plans, problems = [], []
    workspace_real = Path(os.path.realpath(workspace))
    by_name: dict[str, Path] = {}
    if len(dirs) > MAX_IMPORT_DIRS:
        problems.append(f"At most {MAX_IMPORT_DIRS} directories can be imported ({len(dirs)} are listed).")
        dirs = dirs[:MAX_IMPORT_DIRS]
    for item in dirs:
        source = _source_path(item, base_dir)
        label = f"Import {item.path}"
        try:
            info = os.lstat(source)
        except FileNotFoundError:
            problems.append(f"{label}: {source} does not exist.")
            continue
        except OSError as exc:
            problems.append(f"{label}: {source} cannot be checked ({exc.strerror or exc}).")
            continue
        if stat.S_ISLNK(info.st_mode):
            problems.append(f"{label}: {source} is a symbolic link; select the directory it points to "
                            f"({os.path.realpath(source)}).")
            continue
        if not stat.S_ISDIR(info.st_mode):
            problems.append(f"{label}: {source} is not a directory.")
            continue
        if not os.access(source, os.R_OK | os.X_OK):
            problems.append(f"{label}: {source} is not readable.")
            continue
        # The directory itself is no link, so this only resolves its parents and any '..'.
        source = Path(os.path.realpath(source))
        name = item.name or source.name
        problem = name_problem(name)
        if problem:
            problems.append(f"{label}: the name {name!r} {problem}; give it another name.")
            continue
        if _is_within(source, workspace_real):
            problems.append(f"{label}: {source} is inside the workspace {workspace_real}; it is already there.")
            continue
        if _is_within(workspace_real, source):
            problems.append(f"{label}: {source} contains the workspace {workspace_real}; copying it would "
                            "copy the workspace into itself.")
            continue
        if name in by_name:
            problems.append(f"{label}: {source} and {by_name[name]} would both be copied to "
                            f"{IMPORT_DIR}/{name}; give one of them another name.")
            continue
        by_name[name] = source
        plans.append(PlannedImport(source, name))
    return plans, problems


def describe_plan(plans: list[PlannedImport], workspace: Path) -> list[str]:
    """What will be copied where, for the log."""
    if not plans:
        return []
    lines = [f"Copying {len(plans)} director{'y' if len(plans) == 1 else 'ies'} into {workspace / IMPORT_DIR} "
             "(a plain copy: symbolic links are skipped, nothing is deleted)"]
    lines += [f"  {plan.source} -> {IMPORT_DIR}/{plan.name}/" for plan in plans]
    return lines


# --------------------------------------------------------------------------
# Copying (file descriptors all the way: no path is followed through a link)
# --------------------------------------------------------------------------

def _open_dir(name: str, parent_fd: int | None) -> int:
    return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)


def _ensure_real_dir(path: Path, mode: int = 0o700) -> int:
    """Create `path` (and its parents) if needed; an fd of it, refused when it is a link."""
    if not path.exists() and not path.is_symlink():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(path, mode)
        except FileExistsError:
            pass
    try:
        return os.open(path, _DIR_FLAGS)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ImportFailed(f"{path} is not a real directory (a symbolic link or a file); "
                               "move it away so Thebe can create it.") from None
        raise ImportFailed(f"{path} cannot be opened: {exc.strerror or exc}.") from None


def _read_marker(dest_fd: int) -> str | None:
    """The source recorded in a destination's marker; None when there is none."""
    try:
        fd = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dest_fd)
    except FileNotFoundError:
        return None
    except OSError:
        return ""
    with os.fdopen(fd, "rb") as handle:
        try:
            data = json.loads(handle.read(64 * 1024).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return ""
    return str(data.get("source", "")) if isinstance(data, dict) else ""


def _write_file_at(dir_fd: int, name: str, write: Callable[[int], None], mode: int) -> None:
    """Write `name` in dir_fd atomically: a temporary file, then rename over the old one."""
    tmp = f".{name}.thebe-tmp"[:MAX_NAME_BYTES]
    try:
        os.unlink(tmp, dir_fd=dir_fd)            # left over from an interrupted run
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        try:
            write(fd)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def _claim(dest_parent_fd: int, plan: PlannedImport, dest: Path) -> int:
    """An fd of the destination, created and marked for this source, or ImportFailed."""
    created = False
    try:
        os.mkdir(plan.name, 0o700, dir_fd=dest_parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        dest_fd = _open_dir(plan.name, dest_parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ImportFailed(f"{dest} exists but is not a directory Thebe made (a file or a symbolic link); "
                               "move it away or give the import another name.") from None
        raise
    if not created:
        owner = _read_marker(dest_fd)
        if owner != str(plan.source):
            os.close(dest_fd)
            if owner:
                raise ImportFailed(f"{dest} holds the copy of {owner}, not of {plan.source}; give one of "
                                   "them another name, or delete that copy in JupyterLab.")
            raise ImportFailed(f"{dest} already exists and was not made by Thebe's import; move or rename it, "
                               "or give the import another name.")
        return dest_fd
    # Marked first, so an interrupted first copy is simply continued by the next Deploy.
    marker = json.dumps({"source": str(plan.source)}, ensure_ascii=False).encode("utf-8") + b"\n"
    _write_file_at(dest_fd, MARKER, lambda fd: os.write(fd, marker), 0o600)
    return dest_fd


def _copy_file(src_dir_fd: int, dst_dir_fd: int, name: str, result: ImportResult, relative: str) -> None:
    try:
        src_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=src_dir_fd)
    except PermissionError:
        result.skipped.append(f"{relative}: not readable")
        return
    except OSError as exc:
        if exc.errno == errno.ELOOP:             # became a link while we looked
            result.skipped.append(f"{relative}: symbolic link")
            return
        raise
    try:
        info = os.fstat(src_fd)
        if not stat.S_ISREG(info.st_mode):
            result.skipped.append(f"{relative}: not a regular file")
            return
        try:
            old = os.lstat(name, dir_fd=dst_dir_fd)
        except FileNotFoundError:
            old = None
        if old is not None and stat.S_ISDIR(old.st_mode):
            result.skipped.append(f"{relative}: a directory in the copy has this name; left as it is")
            return
        if (old is not None and stat.S_ISREG(old.st_mode) and old.st_size == info.st_size
                and old.st_mtime_ns == info.st_mtime_ns):
            result.unchanged += 1
            return

        def write(fd: int) -> None:
            os.lseek(src_fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(src_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
            os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))

        # Permission bits of the source (executable scripts stay executable), never setuid/setgid.
        _write_file_at(dst_dir_fd, name, write, stat.S_IMODE(info.st_mode) & 0o777 | 0o600)
        result.copied += 1
        result.bytes += info.st_size
    finally:
        os.close(src_fd)


def _copy_dir(src_fd: int, dst_fd: int, relative: str, result: ImportResult, avoid: set[tuple[int, int]],
              top: bool = False) -> None:
    with os.scandir(src_fd) as entries:
        items = sorted(entries, key=lambda entry: entry.name)
    for entry in items:
        path = f"{relative}{entry.name}"
        if top and entry.name == MARKER:
            result.skipped.append(f"{path}: reserved name at the top of the copy")
            continue
        if entry.name.endswith(".thebe-tmp") and entry.name.startswith("."):
            continue
        result.current = path
        if entry.is_symlink():
            target = os.readlink(entry.name, dir_fd=src_fd)
            result.skipped.append(f"{path}: symbolic link to {target}")
        elif entry.is_dir(follow_symlinks=False):
            _copy_subdir(src_fd, dst_fd, entry.name, path, result, avoid)
        elif entry.is_file(follow_symlinks=False):
            _copy_file(src_fd, dst_fd, entry.name, result, path)
        else:
            result.skipped.append(f"{path}: not a regular file (device, socket or pipe)")


def _copy_subdir(src_parent: int, dst_parent: int, name: str, relative: str, result: ImportResult,
                 avoid: set[tuple[int, int]]) -> None:
    try:
        src_fd = _open_dir(name, src_parent)
    except PermissionError:
        result.skipped.append(f"{relative}/: not readable")
        return
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            result.skipped.append(f"{relative}: symbolic link")
            return
        raise
    try:
        info = os.fstat(src_fd)
        if (info.st_dev, info.st_ino) in avoid:
            # A bind mount of the workspace (or a loop) inside the source: never copy it into itself.
            result.skipped.append(f"{relative}/: the workspace or a directory already being copied")
            return
        mode = stat.S_IMODE(info.st_mode) & 0o777 | 0o700
        try:
            os.mkdir(name, mode, dir_fd=dst_parent)
        except FileExistsError:
            pass
        try:
            dst_fd = _open_dir(name, dst_parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                result.skipped.append(f"{relative}/: a file or symbolic link in the copy has this name; left as it is")
                return
            raise
        try:
            result.dirs += 1
            _copy_dir(src_fd, dst_fd, relative + "/", result, avoid | {(info.st_dev, info.st_ino)})
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def copy_import(plan: PlannedImport, workspace: Path) -> ImportResult:
    """Copy one planned source into <workspace>/imported/<name>/; ImportFailed names the source."""
    workspace = Path(os.path.realpath(workspace))       # a workspace behind a symlink is fine
    root = workspace / IMPORT_DIR
    dest = root / plan.name
    result = ImportResult(plan.source, dest)
    try:
        workspace_fd = _ensure_real_dir(workspace)
        try:
            workspace_info = os.fstat(workspace_fd)
            parent_fd = _ensure_real_dir(root)
        finally:
            os.close(workspace_fd)
        try:
            dest_fd = _claim(parent_fd, plan, dest)
        finally:
            os.close(parent_fd)
        try:
            try:
                src_fd = os.open(plan.source, _DIR_FLAGS)
            except OSError as exc:
                raise ImportFailed(f"Copying {plan.source} failed: it cannot be opened as a directory "
                                   f"({exc.strerror or exc}).") from None
            try:
                source_info = os.fstat(src_fd)
                avoid = {(workspace_info.st_dev, workspace_info.st_ino), (source_info.st_dev, source_info.st_ino)}
                _copy_dir(src_fd, dest_fd, "", result, avoid, top=True)
            finally:
                os.close(src_fd)
        finally:
            os.close(dest_fd)
    except ImportFailed:
        raise
    except OSError as exc:
        where = f" at {result.current}" if result.current else ""
        raise ImportFailed(f"Copying {plan.source} to {dest} failed{where}: {exc.strerror or exc}. "
                           "Files copied so far are complete; the rest of the workspace is untouched.") from None
    return result


def _size(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count} B"


def summarize(result: ImportResult) -> list[str]:
    lines = [f"{result.source} -> {IMPORT_DIR}/{result.dest.name}/: {result.copied} file(s) copied "
             f"({_size(result.bytes)}), {result.unchanged} unchanged, {result.dirs} folder(s)"]
    if result.skipped:
        lines.append(f"  skipped {len(result.skipped)}:")
        lines += [f"    {item}" for item in result.skipped[:MAX_REPORTED]]
        if len(result.skipped) > MAX_REPORTED:
            lines.append(f"    ... and {len(result.skipped) - MAX_REPORTED} more")
    return lines


def run_imports(plans: list[PlannedImport], workspace: Path, log: Log) -> list[ImportResult]:
    """Copy every plan in order, logging as it goes; the first failure stops with ImportFailed."""
    for line in describe_plan(plans, workspace):
        log(line)
    results = []
    for plan in plans:
        result = copy_import(plan, workspace)
        for line in summarize(result):
            log(line)
        results.append(result)
    return results
