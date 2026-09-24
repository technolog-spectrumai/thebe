#!/usr/bin/env bash
# Start the PyQt6 builder for the JupyterLab-on-Tailscale stack.
#
# Creates (or reuses) the project-local .venv (lib/venv.sh, shared with run.sh),
# installs the pinned PyQt6 and PyYAML wheels from requirements-builder.txt into
# it, and runs builder.py. Nothing is installed globally; delete .venv to remove
# every builder dependency.
#
#   ./run-builder.sh              start the GUI
#   PYTHON=/usr/bin/python3.13 ./run-builder.sh   use another interpreter
set -Eeuo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_NAME='run-builder.sh'
# shellcheck source=lib/venv.sh
source "$REPO/lib/venv.sh"
trap 'die "unexpected failure at line $LINENO"' ERR

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

venv_setup "$REPO/requirements-builder.txt" builder 'PyQt6 wheels: up to about 95 MB to download' \
  "$REPO/requirements-run.txt"

trap - ERR
exec "$VENV/bin/python" "$REPO/builder.py" "$@"
