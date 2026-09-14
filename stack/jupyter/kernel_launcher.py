"""Kernel entry point for the python3 kernelspec: ipykernel plus the custom packages.

Packages installed from the Dependencies page live in /opt/custom/venv (the
custom_packages volume, mounted read-only here). Their site-packages directory is added
with site.addsitedir(), which appends it after the image's own site-packages and runs
its .pth files: an image package always wins over a custom copy of the same name, and a
broken custom package cannot stop the kernel from starting. The Jupyter server itself
never imports from the venv.

Everything else is what ipykernel_launcher does.
"""

import site
import sys
from pathlib import Path

VENV_SITE_PACKAGES = Path(
    f"/opt/custom/venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
)

if __name__ == "__main__":
    # Running a script puts its directory (/srv/jupyter) first on sys.path; notebooks
    # must not import the server config from there. ipykernel_launcher removes the cwd
    # for the same reason; InteractiveShellApp.init_path() adds the cwd back later.
    if sys.path and (sys.path[0] == "" or Path(sys.path[0]).resolve() == Path(__file__).resolve().parent):
        del sys.path[0]

    if VENV_SITE_PACKAGES.is_dir():
        try:
            site.addsitedir(str(VENV_SITE_PACKAGES))
        except Exception as exc:  # the kernel must start even with a damaged venv
            print(f"kernel_launcher: ignoring {VENV_SITE_PACKAGES}: {exc!r}", file=sys.stderr)

    from ipykernel import kernelapp as app

    app.launch_new_instance()
