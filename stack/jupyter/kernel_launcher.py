"""Kernel entry point for the python3 kernelspec: ipykernel plus the custom packages.

Packages installed from the Dependencies page live in /opt/custom/venv (the
custom_packages volume, mounted read-only here). Their site-packages directory is added
with site.addsitedir(), which appends it after the image's own site-packages and runs
its .pth files: an image package always wins over a custom copy of the same name, and a
broken custom package cannot stop the kernel from starting. The Jupyter server itself
never imports from the venv.

It also loads the %%ai magic (kernel/thebe_ai.py) into every kernel, through
extra_extensions: the user's own IPython `extensions` setting stays untouched, and a failure
to load it only logs a warning. Only kernel/ is added to sys.path, never /srv/jupyter itself.

Everything else is what ipykernel_launcher does.
"""

import site
import sys
from pathlib import Path

VENV_SITE_PACKAGES = Path(
    f"/opt/custom/venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
)
KERNEL_EXTENSIONS = Path(__file__).resolve().parent / "kernel"

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

    sys.path.insert(0, str(KERNEL_EXTENSIONS))
    sys.argv.append("--IPKernelApp.extra_extensions=thebe_ai")

    from ipykernel import kernelapp as app

    app.launch_new_instance()
