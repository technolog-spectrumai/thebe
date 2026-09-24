#!/usr/bin/env bash
# Deploy and run JupyterLab on Tailscale from config.yaml, without the GUI.
#
# Creates (or reuses) the project-local .venv, installs the pinned PyYAML wheel from
# requirements-run.txt into it, and runs run.py. Works on a server without a display.
#
#   ./run.sh                  install: check config.yaml, write the settings, build and start
#   ./run.sh help             all commands (start, stop, status, logs, check, ...)
#   PYTHON=/usr/bin/python3.13 ./run.sh   use another interpreter
set -Eeuo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_NAME='run.sh'
# shellcheck source=lib/venv.sh
source "$REPO/lib/venv.sh"
trap 'die "unexpected failure at line $LINENO"' ERR

venv_setup "$REPO/requirements-run.txt" run 'PyYAML: well under 1 MB to download'

trap - ERR
exec "$VENV/bin/python" "$REPO/run.py" "$@"
