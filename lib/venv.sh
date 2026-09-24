# shellcheck shell=bash
# The project-local .venv shared by run.sh and run-builder.sh: pinned wheels from a requirements
# file, installed into <repo>/.venv only. Nothing is installed globally; delete .venv to remove
# every dependency. Sourced by those scripts after they set REPO and SCRIPT_NAME:
#
#   venv_setup <requirements file> <stamp name> <what is downloaded> [<file it includes>...]
#   exec "$VENV/bin/python" ...
#
# PYTHON=/path/to/python3 (or a name on PATH) picks another interpreter.

VENV="$REPO/.venv"

die() {
  printf '%s: %s\n' "$SCRIPT_NAME" "$*" >&2
  exit 1
}

# The distribution's interpreter, not whatever `python3` comes first on PATH: on a laptop that
# may be conda's, which a desktop launcher does not see, so a terminal and a launcher would
# otherwise build (and break) different venvs.
venv_find_python() {
  local py_path
  PY="${PYTHON:-/usr/bin/python3}"
  # A bare name (PYTHON=python3.13) is looked up on PATH.
  py_path="$PY"
  [[ "$PY" == */* ]] || py_path="$(command -v -- "$PY" 2>/dev/null || true)"
  [[ -n "$py_path" && -x "$py_path" ]] || die "Python interpreter '$PY' not found. Install it with:
    sudo apt install python3
or point PYTHON at another interpreter (PYTHON=/path/to/python3 $0)."
  PY="$py_path"
  PY_VERSION="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  # Debian/Ubuntu split venv and ensurepip out of the base package; without them
  # `python3 -m venv` creates a venv without pip and fails half-way.
  if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    die "Python $PY_VERSION cannot create virtual environments (venv/ensurepip missing). Install:
    sudo apt install python3-venv      (or python${PY_VERSION%.*}-venv)"
  fi
}

venv_create() {
  rm -rf -- "$VENV"
  if ! "$PY" -m venv "$VENV"; then
    rm -rf -- "$VENV"
    die "could not create $VENV with $PY (see the error above)."
  fi
}

venv_setup() {
  local requirements="$1" stamp="$VENV/.$2-stamp" what="$3" venv_version='' want have file
  shift 3
  for file in "$requirements" "$@"; do
    [[ -f "$file" ]] || die "missing $file"
  done
  venv_find_python

  # Compiled wheels (PyQt6-sip, PyYAML) are per CPython version: a venv built by another
  # interpreter version (or whose base interpreter is gone) is rebuilt.
  if [[ -x "$VENV/bin/python" ]]; then
    venv_version="$("$VENV/bin/python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)"
  fi
  if [[ "$venv_version" != "$PY_VERSION" ]]; then
    if [[ -e "$VENV" ]]; then
      printf 'Recreating %s for Python %s (was: %s)\n' "$VENV" "$PY_VERSION" "${venv_version:-unusable}" >&2
    else
      printf 'Creating %s with Python %s\n' "$VENV" "$PY_VERSION" >&2
    fi
    venv_create
  fi

  # Reinstall only when the pinned requirements (and the files they include) or the interpreter
  # changed. Each caller has its own stamp, so run.sh and run-builder.sh share one venv.
  want="requirements=$(cat -- "$requirements" "$@" | sha256sum | cut -d' ' -f1) python=$PY_VERSION"
  have="$(cat "$stamp" 2>/dev/null || true)"
  [[ "$have" != "$want" ]] || return 0
  # Without a matching stamp the venv may be half-made (Ctrl+C while ensurepip ran leaves
  # bin/python but no pip), so check pip before relying on it.
  if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    printf 'Recreating %s: it has no working pip (interrupted setup?)\n' "$VENV" >&2
    venv_create
  fi
  printf 'Installing dependencies into %s (%s)\n' "$VENV" "$what" >&2
  if ! (cd -- "$REPO" && "$VENV/bin/python" -m pip install \
    --disable-pip-version-check --no-input --only-binary=:all: -r "$requirements"); then
    die "installing the dependencies failed (see pip's message above).
Check the network connection and run $0 again. If it keeps failing, delete the venv and retry:
    rm -rf -- '$VENV'"
  fi
  # Written last, so an interrupted install is retried on the next start.
  printf '%s\n' "$want" >"$stamp.tmp"
  mv -f -- "$stamp.tmp" "$stamp"
}
