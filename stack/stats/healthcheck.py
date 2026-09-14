"""Docker healthcheck for the stats container: GET /health with Basic auth.

Standard library only, and independent of app.py, so a broken app import still yields
a clean "unhealthy" instead of a traceback. Exit 0 on HTTP 200, 1 otherwise.
"""

import base64
import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8889/health"
TIMEOUT = 4.0


def main() -> int:
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
    request = urllib.request.Request(URL, headers={"Authorization": f"Basic {token}"})
    # No proxy: HTTP_PROXY in the environment must not intercept a loopback check.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (OSError, ValueError) as exc:  # connection refused, timeout
        print(f"healthcheck: {URL}: {getattr(exc, 'reason', exc)}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"healthcheck: {URL} answered HTTP {status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
