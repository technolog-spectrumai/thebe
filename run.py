#!/usr/bin/env python3
"""Headless Thebe: deploy and run JupyterLab on Tailscale from config.yaml, without the GUI.

Start it with ./run.sh, which keeps PyYAML inside ./.venv. ./run.sh help lists the commands.
"""

import sys

try:
    from thebe.cli import main
except ModuleNotFoundError as exc:
    if exc.name != "yaml":
        raise
    sys.exit("run.py: PyYAML is missing. Start it with ./run.sh, which installs it into ./.venv.")

if __name__ == "__main__":
    sys.exit(main())
