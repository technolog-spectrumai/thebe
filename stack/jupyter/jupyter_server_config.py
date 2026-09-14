"""JupyterLab server configuration, baked into the jupyterlab image.

Loaded explicitly by the image CMD: jupyter lab --config=/srv/jupyter/jupyter_server_config.py
The server listens on every interface of its own container; Compose publishes the port
on the Tailscale IPv4 only.
"""

import os
import re
import ssl
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


def tls_files(cert, key):
    """Return (cert, key) for HTTPS, None for plain HTTP, or stop with a clear message.

    compose.tls.yaml sets both variables to the certificate for the MagicDNS name. A
    missing or unreadable file must stop the server: otherwise Tornado fails later with a
    traceback, or JupyterLab would silently come up without the HTTPS it was asked for.
    """
    if not cert and not key:
        return None
    if not cert or not key:
        raise SystemExit(
            "jupyter_server_config: JLT_TLS_CERT and JLT_TLS_KEY must be set together "
            f"(JLT_TLS_CERT={cert!r}, JLT_TLS_KEY={key!r})."
        )
    for label, path in (("certificate", cert), ("key", key)):
        try:
            with open(path, "rb"):
                pass
        except FileNotFoundError:
            raise SystemExit(
                f"jupyter_server_config: TLS {label} {path} does not exist; "
                "run setup-jupyterlab-tailscale.sh update."
            ) from None
        except OSError as exc:
            raise SystemExit(
                f"jupyter_server_config: TLS {label} {path} cannot be read: {exc.strerror or exc}"
            ) from None
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    except (ssl.SSLError, OSError) as exc:
        raise SystemExit(
            f"jupyter_server_config: TLS key {key} does not match certificate {cert} "
            f"(or a file is not PEM): {exc}"
        ) from None
    return cert, key


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

# --- HTTPS (compose.tls.yaml) -------------------------------------------------
# The certificate from 'tailscale cert' for the MagicDNS name, on the same port 8888.
TLS = tls_files(os.environ.get("JLT_TLS_CERT", ""), os.environ.get("JLT_TLS_KEY", ""))
if TLS is not None:
    c.ServerApp.certfile, c.ServerApp.keyfile = TLS

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
