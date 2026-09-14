#!/usr/bin/env bash
# Start the PyQt6 builder for the JupyterLab-on-Tailscale stack.
#
# Creates (or reuses) a project-local .venv, installs the pinned PyQt6 wheels
# from requirements-builder.txt into it, and runs builder.py. Nothing is
# installed globally; delete .venv to remove every builder dependency.
#
#   ./run-builder.sh              start the GUI
#   PYTHON=/usr/bin/python3.13 ./run-builder.sh   use another interpreter
set -Eeuo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV="$REPO/.venv"
REQUIREMENTS="$REPO/requirements-builder.txt"
STAMP="$VENV/.builder-stamp"

# The distribution's interpreter, not whatever `python3` comes first on PATH:
# on this laptop that is conda's, which a desktop launcher does not see, so a
# terminal and a launcher would otherwise build (and break) different venvs.
PY="${PYTHON:-/usr/bin/python3}"

die() {
  printf 'run-builder.sh: %s\n' "$*" >&2
  exit 1
}
trap 'die "unexpected failure at line $LINENO"' ERR

# A bare name (PYTHON=python3.13) is looked up on PATH.
py_path="$PY"
[[ "$PY" == */* ]] || py_path="$(command -v -- "$PY" 2>/dev/null || true)"
[[ -n "$py_path" && -x "$py_path" ]] || die "Python interpreter '$PY' not found. Install it with:
    sudo apt install python3
or point PYTHON at another interpreter (PYTHON=/path/to/python3 $0)."
PY="$py_path"

[[ -f "$REQUIREMENTS" ]] || die "missing $REQUIREMENTS"

PY_VERSION="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
PY_MINOR="${PY_VERSION%.*}"

# Debian/Ubuntu split venv and ensurepip out of the base package; without them
# `python3 -m venv` creates a venv without pip and fails half-way.
if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  die "Python $PY_VERSION cannot create virtual environments (venv/ensurepip missing). Install:
    sudo apt install python3-venv      (or python${PY_MINOR}-venv)"
fi

# Qt >= 6.5 refuses to load its X11 (xcb) platform plugin without
# libxcb-cursor0. Headless (offscreen) and Wayland sessions do not need it.
case "${QT_QPA_PLATFORM:-}" in
  offscreen* | wayland* | minimal*) ;;
  *)
    # Captured rather than piped into `grep -q`: an early grep exit would
    # SIGPIPE ldconfig and trip pipefail.
    libs="$(ldconfig -p 2>/dev/null || /sbin/ldconfig -p 2>/dev/null || true)"
    if [[ "$libs" != *libxcb-cursor.so.0* ]] && ! compgen -G '/usr/lib/*/libxcb-cursor.so.0*' >/dev/null; then
      die "Qt needs the X11 cursor library libxcb-cursor0. Install:
    sudo apt install libxcb-cursor0"
    fi
    ;;
esac

create_venv() {
  rm -rf -- "$VENV"
  if ! "$PY" -m venv "$VENV"; then
    rm -rf -- "$VENV"
    die "could not create $VENV with $PY (see the error above)."
  fi
}

# PyQt6-sip is compiled per CPython version: a venv built by another
# interpreter version (or whose base interpreter is gone) is rebuilt.
venv_version=""
if [[ -x "$VENV/bin/python" ]]; then
  venv_version="$("$VENV/bin/python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)"
fi
if [[ "$venv_version" != "$PY_VERSION" ]]; then
  if [[ -e "$VENV" ]]; then
    printf 'Recreating %s for Python %s (was: %s)\n' "$VENV" "$PY_VERSION" "${venv_version:-unusable}" >&2
  else
    printf 'Creating %s with Python %s\n' "$VENV" "$PY_VERSION" >&2
  fi
  create_venv
fi

# Reinstall only when the pinned requirements or the interpreter changed.
want="requirements=$(sha256sum "$REQUIREMENTS" | cut -d' ' -f1) python=$PY_VERSION"
have="$(cat "$STAMP" 2>/dev/null || true)"
if [[ "$have" != "$want" ]]; then
  # Without a matching stamp the venv may be half-made (Ctrl+C while ensurepip
  # ran leaves bin/python but no pip), so check pip before relying on it.
  if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    printf 'Recreating %s: it has no working pip (interrupted setup?)\n' "$VENV" >&2
    create_venv
  fi
  printf 'Installing builder dependencies into %s (PyQt6 wheels: up to about 95 MB to download)\n' "$VENV" >&2
  if ! "$VENV/bin/python" -m pip install \
    --disable-pip-version-check --no-input --only-binary=:all: \
    -r "$REQUIREMENTS"; then
    die "installing the builder dependencies failed (see pip's message above).
Check the network connection and run $0 again. If it keeps failing, delete the venv and retry:
    rm -rf -- '$VENV'"
  fi
  # Written last, so an interrupted install is retried on the next start.
  printf '%s\n' "$want" >"$STAMP.tmp"
  mv -f -- "$STAMP.tmp" "$STAMP"
fi

trap - ERR
exec "$VENV/bin/python" "$REPO/builder.py" "$@"
