"""HTTP Basic authentication and security headers, as pure ASGI middleware.

app.py wraps the whole FastAPI application in these two classes instead of using
FastAPI's add_middleware(): that way they also cover static files, 404s and the 500
page produced by Starlette's error middleware, and nothing is reachable without the
password (not even /health).
"""

import base64
import binascii
import secrets
from pathlib import Path

REALM = "jupyterlab-tailscale"

# Scripts and styles come only from /static (no inline code, no CDN). Meter widths are
# set through the CSSOM (element.style.width), which style-src does not restrict, so
# 'unsafe-inline' is not needed.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
)


class ConfigError(Exception):
    """The service cannot start safely; the message is meant for the operator."""


def read_password_file(path: str) -> bytes:
    """Return the password bytes, refusing a missing, unreadable or empty file."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"password file {path} does not exist") from None
    except OSError as exc:
        raise ConfigError(f"cannot read password file {path}: {exc.strerror or exc}") from None
    # The installer writes the secret without a newline; tolerate one added by an editor.
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    if not data:
        raise ConfigError(f"password file {path} is empty")
    return data


class BasicAuthMiddleware:
    """Require `Authorization: Basic` with the configured user and password on every request."""

    def __init__(self, app, *, username: bytes, password: bytes, realm: str = REALM):
        self.app = app
        self._username = username
        self._password = password
        self._challenge = f'Basic realm="{realm}", charset="UTF-8"'.encode("latin-1")

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
        elif kind == "http":
            if self._authorized(scope["headers"]):
                await self.app(scope, receive, send)
            else:
                await self._send_challenge(send)
        elif kind == "websocket":
            # No WebSocket routes exist; refuse the handshake without looking further.
            await send({"type": "websocket.close", "code": 1008})

    def _authorized(self, headers) -> bool:
        value = next((v for k, v in headers if k == b"authorization"), None)
        if value is None:
            return False
        scheme, _, credentials = value.partition(b" ")
        if scheme.lower() != b"basic":
            return False
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        username, separator, password = decoded.partition(b":")
        if not separator:
            return False
        # Compare both parts every time so the response time does not reveal which one
        # was wrong. The browser sends UTF-8 because the challenge asks for it.
        username_ok = secrets.compare_digest(username, self._username)
        password_ok = secrets.compare_digest(password, self._password)
        return username_ok and password_ok

    async def _send_challenge(self, send):
        body = b"401 Unauthorized: sign in with the JupyterLab password.\n"
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", self._challenge),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Add SECURITY_HEADERS to every HTTP response, replacing any value the app set."""

    _names = frozenset(name for name, _ in SECURITY_HEADERS)

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", ())
                    if name.lower() not in self._names
                ]
                headers.extend(SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
