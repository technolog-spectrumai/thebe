"""Docker healthcheck for the deps container: GET /health with Basic auth.

Standard library only and independent of deps_runner.py, so a broken runner still yields
a clean "unhealthy". Exit 0 on HTTP 200, 1 otherwise. The credential is the runner token
(DEPS_TOKEN_FILE), the same one the dashboard uses.
"""

import base64
import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8890/health"
TIMEOUT = 4.0


def main() -> int:
    username = os.environ.get("STATS_USER") or "jupyter"
    token_file = os.environ.get("DEPS_TOKEN_FILE") or "/run/secrets/deps_token"
    try:
        with open(token_file, "rb") as handle:
            secret = handle.read()
    except OSError as exc:
        print(f"healthcheck: cannot read {token_file}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    # Same rule as the runner: strip one trailing newline.
    if secret.endswith(b"\r\n"):
        secret = secret[:-2]
    elif secret.endswith(b"\n"):
        secret = secret[:-1]

    credentials = base64.b64encode(username.encode("utf-8") + b":" + secret).decode("ascii")
    request = urllib.request.Request(URL, headers={"Authorization": f"Basic {credentials}"})
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
