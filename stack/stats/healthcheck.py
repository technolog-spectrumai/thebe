"""Docker healthcheck for the stats container: GET /health with Basic auth.

Standard library only, and independent of app.py, so a broken app import still yields
a clean "unhealthy" instead of a traceback. Exit 0 on HTTP 200, 1 otherwise. With
JLT_TLS_CERT set (compose.tls.yaml) the server speaks HTTPS, and so does this check.
"""

import base64
import os
import ssl
import sys
import urllib.error
import urllib.request

PORT = 8889
TIMEOUT = 4.0


def target() -> tuple[str, ssl.SSLContext | None]:
    """The /health URL and, for HTTPS, the TLS context to use."""
    if not os.environ.get("JLT_TLS_CERT"):
        return f"http://127.0.0.1:{PORT}/health", None
    # Not verified: the certificate names the machine's public MagicDNS host, not 127.0.0.1,
    # and this check only asks whether the server in this container answers.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return f"https://127.0.0.1:{PORT}/health", context


def main() -> int:
    url, context = target()
    username = os.environ.get("STATS_USER") or "jupyter"
    password_file = os.environ.get("JUPYTER_PASSWORD_FILE") or "/run/secrets/jupyter_password"
    try:
        with open(password_file, "rb") as handle:
            password = handle.read()
    except OSError as exc:
        print(f"healthcheck: cannot read {password_file}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    # Same rule as the app: strip one trailing newline.
    if password.endswith(b"\r\n"):
        password = password[:-2]
    elif password.endswith(b"\n"):
        password = password[:-1]

    token = base64.b64encode(username.encode("utf-8") + b":" + password).decode("ascii")
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    # No proxy: HTTP(S)_PROXY in the environment must not intercept a loopback check.
    handlers = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (OSError, ValueError) as exc:  # connection refused, timeout, TLS handshake failure
        print(f"healthcheck: {url}: {getattr(exc, 'reason', exc)}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"healthcheck: {url} answered HTTP {status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
