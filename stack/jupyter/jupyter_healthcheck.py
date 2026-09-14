"""Docker healthcheck for the jupyterlab container: GET /api on the server's own port.

Standard library only. /api is unauthenticated in jupyter_server. With compose.tls.yaml
(JLT_TLS_CERT set) the server speaks HTTPS; the certificate names the public MagicDNS host,
not 127.0.0.1, and this check is only about liveness, so it is not verified here.
Exit 0 on HTTP 200, 1 otherwise.
"""

import os
import ssl
import sys
import urllib.error
import urllib.request

TIMEOUT = 4.0


def main() -> int:
    handlers = [urllib.request.ProxyHandler({})]  # HTTP(S)_PROXY must not intercept loopback
    if os.environ.get("JLT_TLS_CERT"):
        url = "https://127.0.0.1:8888/api"
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    else:
        url = "http://127.0.0.1:8888/api"
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(url, timeout=TIMEOUT) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (OSError, ValueError) as exc:  # connection refused, timeout, TLS handshake
        print(f"healthcheck: {url}: {getattr(exc, 'reason', exc)}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"healthcheck: {url} answered HTTP {status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
