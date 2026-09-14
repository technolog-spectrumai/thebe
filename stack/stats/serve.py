"""Starts the dashboard: uvicorn on port 8889, over HTTPS when a certificate is configured.

The stats image runs `python /srv/stats/serve.py`. compose.tls.yaml sets JLT_TLS_CERT and
JLT_TLS_KEY to the Tailscale certificate for the machine's MagicDNS name (mounted read-only at
/run/tls); the same port then speaks HTTPS. Without them it speaks plain HTTP, as before.

A certificate that is configured but cannot be used stops the container with one clear line
(`docker compose logs stats`) instead of silently falling back to HTTP: the published https://
URLs would not reach an HTTP server anyway, and a crash loop is visible in `status`.
"""

import os
import ssl
import sys
from collections.abc import Mapping

import uvicorn

# All interfaces of the container; the published port is bound to the Tailscale IPv4 only.
HOST = "0.0.0.0"
PORT = 8889


class TlsConfigError(Exception):
    """HTTPS is configured but cannot work; the message is meant for the operator."""


def tls_files(environ: Mapping[str, str] = os.environ) -> tuple[str, str] | None:
    """(certificate, key) when HTTPS is configured, None for plain HTTP."""
    cert = environ.get("JLT_TLS_CERT") or ""
    key = environ.get("JLT_TLS_KEY") or ""
    if not cert and not key:
        return None
    if not cert or not key:
        raise TlsConfigError("JLT_TLS_CERT and JLT_TLS_KEY must be set together")
    for label, path in (("certificate", cert), ("private key", key)):
        try:
            with open(path, "rb") as handle:
                handle.read(1)
        except OSError as exc:
            raise TlsConfigError(f"cannot read the TLS {label} {path}: {exc.strerror or exc}") from None
    # uvicorn loads the pair again when it starts; this makes a key that does not belong to the
    # certificate (or a truncated file) a one-line message instead of a traceback.
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    except (ssl.SSLError, OSError) as exc:
        raise TlsConfigError(f"the TLS certificate {cert} and key {key} cannot be used together: {exc}") from None
    return cert, key


def main() -> int:
    try:
        tls = tls_files()
    except TlsConfigError as exc:
        print(f"stats: refusing to start: {exc}", file=sys.stderr)
        return 1
    options = {"ssl_certfile": tls[0], "ssl_keyfile": tls[1]} if tls else {}
    uvicorn.run(
        "app:app",
        host=HOST,
        port=PORT,
        server_header=False,
        # The scheme and client address come from the connection itself. The CSRF guard compares
        # Origin with that scheme, so no X-Forwarded-* header sent by a client may change it.
        proxy_headers=False,
        **options,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
