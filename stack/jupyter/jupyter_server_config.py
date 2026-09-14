"""JupyterLab server configuration, baked into the jupyterlab image.

Loaded explicitly by the image CMD: jupyter lab --config=/srv/jupyter/jupyter_server_config.py
The server listens on every interface of its own container; Compose publishes the port
on the Tailscale IPv4 only.
"""

import re
from pathlib import Path

c = get_config()  # noqa: F821 - provided by traitlets when it runs this file

HASHED_PASSWORD_FILE = Path("/run/secrets/jupyter_hashed_password")

# Output of jupyter_server.auth.passwd(): argon2 (what the installer writes) or the
# legacy salted sha1 form. A malformed hash is not rejected at startup; passwd_check
# raises on every login attempt instead, which the browser sees as HTTP 500.
HASH_PATTERN = re.compile(
    r"argon2:\$argon2(?:id|i|d)\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+"
    r"|sha1:[0-9a-f]+:[0-9a-f]{40}"
)


def read_hashed_password(path):
    """Return the stripped hash from the secret file or stop the server with a clear message.

    SystemExit rather than an ordinary exception: traitlets only logs an exception raised
    in a config file and then starts the server without this configuration.
    """
    prefix = f"jupyter_server_config: {path}"
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise SystemExit(
            f"{prefix} does not exist; the jupyter_hashed_password secret is not mounted."
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"{prefix} cannot be read: {exc}") from None
    if not value:
        raise SystemExit(f"{prefix} is empty; expected a Jupyter password hash.")
    if not HASH_PATTERN.fullmatch(value):
        # Never echo the file content.
        raise SystemExit(
            f"{prefix} does not contain a Jupyter password hash "
            "(expected 'argon2:$argon2id$...' from jupyter_server.auth.passwd)."
        )
    return value


# --- network ---------------------------------------------------------------
c.ServerApp.ip = "0.0.0.0"
c.ServerApp.port = 8888
# The default (50) would silently move the server to another port if 8888 were busy,
# away from the published one.
c.ServerApp.port_retries = 0
c.ServerApp.open_browser = False
# Browsers arrive with Host <ts-ip>:<port> and the dashboard with jupyterlab:8888;
# neither counts as local, so remote access must be allowed explicitly.
c.ServerApp.allow_remote_access = True
c.ServerApp.allow_root = False
c.ServerApp.root_dir = "/workspace"

# --- authentication: hashed password only, no token ---------------------------
c.PasswordIdentityProvider.hashed_password = read_hashed_password(HASHED_PASSWORD_FILE)
# Second guard: with an empty hash the server exits instead of running without a password.
c.PasswordIdentityProvider.password_required = True
c.PasswordIdentityProvider.allow_password_change = False
# An explicit empty token disables token login, including a stray JUPYTER_TOKEN variable.
c.IdentityProvider.token = ""
# The cookie signing key lives on the state volume, so logins survive container
# recreation. The effective secret also mixes in the hash, so a new password (a new
# hash) logs every browser out.
c.ServerApp.cookie_secret_file = "/state/jupyter_cookie_secret"

# --- features ----------------------------------------------------------------
c.ServerApp.terminals_enabled = True
# Kernels are deliberately never culled: a long computation must survive a closed tablet.
c.LabApp.news_url = None  # no announcement feed requests
c.LabApp.check_for_updates_class = "jupyterlab.NeverCheckForUpdate"
# The image is rebuilt, not modified: no extension installs from the Lab UI.
c.LabApp.extension_manager = "readonly"
