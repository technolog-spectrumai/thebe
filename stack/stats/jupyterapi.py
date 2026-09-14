"""Talks to the JupyterLab container over the Compose network, standard library only.

Kernel listing needs a logged-in session. JupyterLab verifies every login with argon2
(10 MiB, 10 iterations), so the session cookie is kept for the life of the process and
a new login happens only when an API call answers 403 (expired cookie, or a new
password changed JupyterLab's cookie secret) - never once per poll.
"""

import http.client
import http.cookiejar
import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("stats.jupyter")

REQUEST_TIMEOUT = 3.0
REACHABILITY_TIMEOUT = 2.0
# After a rejected login, wait this long before costing JupyterLab another argon2 check.
LOGIN_RETRY_SECONDS = 60.0


class JupyterError(Exception):
    """Kernel information is unavailable; the message is shown on the dashboard."""


class _Forbidden(Exception):
    """An API call answered 403: the session is missing or no longer valid."""


def _opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    # An empty ProxyHandler: HTTP(S)_PROXY from the environment must never route the
    # internal URL (or the password) through a proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)


def _describe(exc: Exception) -> str:
    """A short, non-technical reason for the dashboard ("connection refused", ...)."""
    reason = getattr(exc, "reason", None) or exc  # URLError wraps the socket error
    if isinstance(reason, socket.gaierror):
        return "host name not found"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, TimeoutError):
        return "timed out"
    return str(reason) or type(reason).__name__


def jupyter_reachable(base_url: str) -> str:
    """"reachable" when JupyterLab answers GET /api (which needs no login)."""
    request = urllib.request.Request(base_url.rstrip("/") + "/api", headers={"Accept": "application/json"})
    try:
        with _opener().open(request, timeout=REACHABILITY_TIMEOUT) as response:
            response.read()
            return "reachable" if response.status == 200 else "unreachable"
    except (OSError, http.client.HTTPException, ValueError):
        return "unreachable"


class JupyterClient:
    """Form login plus GET /api/kernels. Not thread-safe: use it from one thread at a time."""

    def __init__(self, base_url: str, password: bytes):
        self._base = base_url.rstrip("/")
        self._base_path = urllib.parse.urlsplit(self._base).path
        self._password = password
        self._jar = http.cookiejar.CookieJar()
        self._opener = _opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._login_retry_at = 0.0
        self._login_error = ""

    # -- public ---------------------------------------------------------------------

    def kernel_stats(self) -> dict:
        try:
            kernels = self._list_kernels()
        except JupyterError as exc:
            return {"available": False, "count": None, "busy": None, "items": [], "error": str(exc)}
        if not isinstance(kernels, list):
            return {
                "available": False,
                "count": None,
                "busy": None,
                "items": [],
                "error": "JupyterLab sent an unexpected kernel list",
            }
        items = [
            {
                "id": str(kernel.get("id", "")),
                "name": str(kernel.get("name", "")),
                "execution_state": str(kernel.get("execution_state", "unknown")),
                "connections": kernel.get("connections") if isinstance(kernel.get("connections"), int) else None,
                "last_activity": kernel.get("last_activity"),
            }
            for kernel in kernels
            if isinstance(kernel, dict)
        ]
        return {
            "available": True,
            "count": len(items),
            "busy": sum(1 for item in items if item["execution_state"] == "busy"),
            "items": items,
            "error": None,
        }

    # -- internals ------------------------------------------------------------------

    def _list_kernels(self):
        if not self._has_session():
            self._login()
            return self._get_json_after_login("/api/kernels")
        try:
            return self._get_json("/api/kernels")
        except _Forbidden:
            log.info("JupyterLab session expired; logging in again")
        self._login()
        return self._get_json_after_login("/api/kernels")

    def _get_json_after_login(self, path: str):
        try:
            return self._get_json(path)
        except _Forbidden:
            self._jar.clear()
            self._block_login(f"JupyterLab refused {path} right after logging in (HTTP 403)")
            raise JupyterError(self._login_error) from None

    def _has_session(self) -> bool:
        # The session cookie is "username-<host>-<port>"; _xsrf alone is not a session.
        return any(cookie.name.startswith("username-") for cookie in self._jar)

    def _request(self, request: urllib.request.Request | str) -> bytes:
        """Return the response body. HTTP error statuses propagate as HTTPError."""
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:  # URLError is an OSError
            raise JupyterError(f"JupyterLab is not reachable: {_describe(exc)}") from None

    def _get_json(self, path: str):
        request = urllib.request.Request(self._base + path, headers={"Accept": "application/json"})
        try:
            body = self._request(request)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 403:
                raise _Forbidden from None
            raise JupyterError(f"JupyterLab answered HTTP {exc.code} for {path}") from None
        try:
            return json.loads(body)
        except ValueError:
            raise JupyterError(f"JupyterLab sent invalid JSON for {path}") from None

    def _block_login(self, message: str) -> None:
        self._login_error = message
        self._login_retry_at = time.monotonic() + LOGIN_RETRY_SECONDS
        log.warning("%s; next login attempt in %.0f s", message, LOGIN_RETRY_SECONDS)

    def _login(self) -> None:
        if time.monotonic() < self._login_retry_at:
            raise JupyterError(self._login_error)
        self._jar.clear()

        # 1. GET /login sets the _xsrf cookie that the form post must echo back.
        try:
            self._request(self._base + "/login")
        except urllib.error.HTTPError as exc:
            exc.close()
            raise JupyterError(f"JupyterLab answered HTTP {exc.code} for /login") from None
        xsrf = next((cookie.value for cookie in self._jar if cookie.name == "_xsrf"), None)
        if not xsrf:
            self._block_login("JupyterLab did not issue an _xsrf cookie at /login")
            raise JupyterError(self._login_error)

        # 2. POST the password. On success JupyterLab sets the session cookie and
        #    redirects to `next`; /api is a tiny unauthenticated JSON document.
        form = urllib.parse.urlencode({"_xsrf": xsrf, "password": self._password, "next": self._base_path + "/api"})
        request = urllib.request.Request(
            self._base + "/login",
            data=form.encode("ascii"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            self._request(request)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 401:
                self._block_login("JupyterLab rejected the password")
            else:
                self._block_login(f"JupyterLab login failed (HTTP {exc.code})")
            raise JupyterError(self._login_error) from None
        if not self._has_session():
            self._block_login("JupyterLab accepted the login form but set no session cookie")
            raise JupyterError(self._login_error)
        self._login_retry_at = 0.0
        log.info("logged in to JupyterLab at %s", self._base)
