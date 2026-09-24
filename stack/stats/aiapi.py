"""Reads the AI gateway's /status (token budget, answer times) over the Compose network.

Standard library only. The gateway requires the bearer token from AI_TOKEN_FILE, which the
installer shares with jupyterlab and this container, never the JupyterLab password. The API keys
are not part of the answer. AI_ENABLED says whether config.yaml has an ai: section: with AI off
the gateway is simply not deployed, which is not an error.
"""

import http.client
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("stats.ai")

MAX_RESPONSE_BYTES = 256 * 1024
OFF = {"available": False, "enabled": False,
       "error": "AI is off: config.yaml has no ai: section. Add one with an API key and run update."}


class AiClient:
    """Blocking calls to the gateway; use from a worker thread (RefreshingValue)."""

    def __init__(self, base_url: str, token: bytes, *, enabled: bool, unavailable_reason: str | None = None,
                 timeout: float = 4.0):
        self._base = base_url.rstrip("/")
        self._enabled = enabled
        self._unavailable_reason = unavailable_reason
        self._authorization = f"Bearer {token.decode('ascii', 'replace')}"
        self._timeout = timeout
        # An empty ProxyHandler: HTTP(S)_PROXY must never route the internal URL (or the token).
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._reachable = True      # log only when reachability changes, not on every poll

    def _unavailable(self, message: str) -> dict:
        return {"available": False, "enabled": True, "error": message}

    def status(self) -> dict:
        if not self._enabled:
            return dict(OFF)
        if self._unavailable_reason:
            return self._unavailable(self._unavailable_reason)
        if not self._base:
            return self._unavailable("AI_INTERNAL_URL is not set.")
        request = urllib.request.Request(self._base + "/status", headers={
            "Accept": "application/json", "Authorization": self._authorization})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 401:
                log.warning("the AI gateway rejected the dashboard's token")
                return self._unavailable("The AI gateway rejected the dashboard's token. Run restart.")
            return self._unavailable(f"The AI gateway answered HTTP {exc.code}.")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if self._reachable:
                log.warning("AI gateway at %s is not reachable: %s", self._base, getattr(exc, "reason", exc))
                self._reachable = False
            return self._unavailable("The AI gateway is not reachable. Run status; "
                                     "docker compose logs ai shows why it stopped.")
        if not self._reachable:
            log.info("AI gateway at %s is reachable again", self._base)
            self._reachable = True
        try:
            payload = json.loads(raw) if len(raw) <= MAX_RESPONSE_BYTES else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
            return self._unavailable("The AI gateway sent an invalid answer.")
        return {"available": True, "enabled": True, "error": None, **payload}
