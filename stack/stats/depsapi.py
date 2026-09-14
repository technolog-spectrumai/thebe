"""Talks to the deps runner (pip installs for kernels) over the Compose network.

Standard library only. The runner requires Basic auth with the dashboard's user name and
the runner token (DEPS_TOKEN_FILE), never the JupyterLab password. Its status codes and
JSON bodies are passed through to the browser, with two exceptions: when it cannot be
reached the answer is 503 {"error": "dependency runner unavailable"}, and a 401 from the
runner (the two containers disagree about the token) becomes a 502, so the browser never
mistakes it for its own sign-in failing.
"""

import base64
import http.client
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("stats.deps")

UNAVAILABLE = "dependency runner unavailable"
# /state carries the requirements text (<= 64 KB) and the installed list; far below this.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class DepsClient:
    """Blocking calls to the runner; use from a worker thread (asyncio.to_thread)."""

    def __init__(self, base_url: str, username: bytes, token: bytes, *, unavailable_reason: str | None = None):
        self._base = base_url.rstrip("/")
        self._unavailable_reason = unavailable_reason
        credentials = base64.b64encode(username + b":" + token).decode("ascii")
        self._authorization = f"Basic {credentials}"
        # An empty ProxyHandler: HTTP(S)_PROXY from the environment must never route the
        # internal URL (or the password) through a proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._reachable = True  # log only when reachability changes, not on every poll

    def call(self, method: str, path: str, body: dict | None = None, *, timeout: float) -> tuple[int, dict]:
        if self._unavailable_reason:
            return 503, {"error": UNAVAILABLE, "detail": self._unavailable_reason}
        if not self._base:
            return 503, {"error": UNAVAILABLE, "detail": "DEPS_INTERNAL_URL is not set"}
        headers = {"Accept": "application/json", "Authorization": self._authorization}
        data = None
        if body is not None:
            try:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            except (UnicodeEncodeError, RecursionError, ValueError, TypeError):
                # json.loads accepts lone surrogates such as "\udc80", which UTF-8 cannot carry.
                return 400, {"error": "The request body contains text that is not valid Unicode."}
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._base + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException):
                raw = b""
            finally:
                exc.close()
        except (OSError, http.client.HTTPException, ValueError) as exc:  # refused, DNS, timeout
            if self._reachable:
                log.warning("dependency runner at %s is not reachable: %s", self._base, getattr(exc, "reason", exc))
                self._reachable = False
            return 503, {"error": UNAVAILABLE}
        if not self._reachable:
            log.info("dependency runner at %s is reachable again", self._base)
            self._reachable = True

        if status == 401:
            log.warning("dependency runner rejected the dashboard's credentials")
            return 502, {
                "error": "The dependency runner rejected the dashboard's token. Run restart so both containers read the same one."
            }
        if len(raw) > MAX_RESPONSE_BYTES:
            return 502, {"error": "The dependency runner sent an oversized response."}
        try:
            payload = json.loads(raw)
        except ValueError:
            return 502, {"error": "The dependency runner sent an invalid response."}
        if not isinstance(payload, dict):
            return 502, {"error": "The dependency runner sent an invalid response."}
        return status, payload
