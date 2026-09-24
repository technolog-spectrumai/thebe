"""Docker healthcheck for the ai container: GET /health with the bearer token.

Standard library only and independent of gateway.py, so a broken gateway still yields a clean
"unhealthy". Exit 0 on HTTP 200, 1 otherwise.
"""

import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8891/health"
TIMEOUT = 4.0


def main() -> int:
    token_file = os.environ.get("AI_TOKEN_FILE") or "/run/secrets/ai_token"
    try:
        with open(token_file, "rb") as handle:
            token = handle.read().strip().decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"healthcheck: cannot read {token_file}: {getattr(exc, 'strerror', None) or exc}", file=sys.stderr)
        return 1
    request = urllib.request.Request(URL, headers={"Authorization": f"Bearer {token}"})
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
