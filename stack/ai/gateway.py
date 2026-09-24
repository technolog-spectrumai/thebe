"""AI gateway for the jupyterlab-tailscale stack: the one place that holds the API keys.

Runs in the `ai` container (Compose profile ai, deployed only when config.yaml has an ai:
section) on port 8891 of the `ai` network. Only jupyterlab and stats join that network; the
port is never published, and the deps container (where pip runs the build scripts of
third-party packages) cannot reach it. The %%ai magic in the kernels, and so the AI button in
the cell toolbar, send prompts here. The gateway asks OpenAI or Anthropic, streams the answer
back, counts the tokens against the budget and times every request; the Statistics page reads
the figures from /status.

Every route requires "Authorization: Bearer <token>" with the random token from AI_TOKEN_FILE,
which the installer shares with jupyterlab and stats only. The API keys never leave this
process: not in a response, a log line or an error message.

Routes:
  GET  /health    {"status": "ok"}
  GET  /status    providers (no keys), the default, the budget and the usage figures
  POST /generate  {"prompt": "...", "provider": "claude" | null, "model": "..." | null}
                  -> 200 application/x-ndjson: {"text": "..."} lines while the answer arrives,
                     then one {"done": true, ...} or {"error": "..."} line
                  -> JSON {"error": "..."} with 4xx/5xx before anything is sent: an unknown
                     provider, the budget used up, too many requests at once

Token budget: max_tokens counts the input and output tokens of all providers together per
period (month: from the 1st, UTC; total: never reset). A request is refused when the budget is
used up or too small for its prompt, and its answer is capped at what is left. The counts are
the providers' own usage figures, known once an answer is complete, so the last request of a
period can go over by the part of its prompt the estimate missed. An answer the notebook stops
waiting for (Kernel -> Interrupt) is still received to the end, so its tokens are counted.

Usage ledger: AI_DATA_DIR/usage.jsonl (the ai_usage volume), one line per request that reached
a provider: time, provider, model, input and output tokens, seconds, ok. Never the prompt or
the answer.

Environment:
  AI_CONFIG_FILE   the ai: section as JSON, with the keys (default /run/secrets/ai_config)
  AI_TOKEN_FILE    the bearer token (default /run/secrets/ai_token)
  AI_DATA_DIR      directory of usage.jsonl (default /var/lib/thebe-ai)
"""

from __future__ import annotations

import datetime
import http.server
import json
import logging
import math
import os
import re
import secrets
import signal
import statistics
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple

PORT = 8891
APIS = ("anthropic", "openai")
PERIODS = ("month", "total")
MAX_RETRIES = 2
MAX_ACTIVE = 4                       # answers generated at the same time
MAX_BODY_BYTES = 512 * 1024
MAX_PROMPT_CHARS = 100_000
MIN_OUTPUT_TOKENS = 64               # a prompt is refused when less than this would be left for the answer
MIN_TOKEN_LENGTH = 32
SOCKET_TIMEOUT = 30
SHUTDOWN_WAIT = 25.0                 # seconds to let answers in progress finish (stop_grace_period is 45)

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")

SYSTEM_PROMPT = (
    "You write Python code for one Jupyter notebook cell. Reply with exactly one ```python code block "
    "and nothing else. Explain only in brief # comments inside the code. Use only the variables you are "
    "told about, and the standard library, numpy, pandas and matplotlib unless the request names others. "
    "Never install packages, never read secrets or environment variables, never delete files."
)

log = logging.getLogger("ai")


class ConfigError(Exception):
    """The settings file cannot be used. The message never contains a key."""


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.body = status, {"error": message}


class ProviderError(Exception):
    """A request to a provider failed; the message is meant for the notebook user."""


# --- settings ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderSettings:
    name: str
    api: str
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None


@dataclass(frozen=True)
class Settings:
    providers: dict[str, ProviderSettings]
    default: str
    max_tokens: int = 0              # 0: no limit
    period: str = "month"
    timeout: float = 60.0
    max_output_tokens: int = 2048


def _number(data: dict, key: str, default: int, low: int, high: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{key} must be a whole number from {low} to {high}")
    return value


def parse_settings(data: object) -> Settings:
    """The settings thebe.ai.gateway_settings() wrote; checked again, the keys never in a message."""
    if not isinstance(data, dict):
        raise ConfigError("the settings are not a JSON object")
    raw = data.get("providers")
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("no providers are configured")
    providers = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(entry, dict):
            raise ConfigError(f"provider {name!r} is not usable")
        api, model, key, base_url = (entry.get(k) for k in ("api", "model", "api_key", "base_url"))
        if api not in APIS:
            raise ConfigError(f"provider {name}: api must be one of {', '.join(APIS)}")
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise ConfigError(f"provider {name}: the model name is not usable")
        if not isinstance(key, str) or not key.strip() or any(ch.isspace() for ch in key):
            raise ConfigError(f"provider {name}: the api_key is missing or not usable")
        if base_url is not None and (not isinstance(base_url, str)
                                     or urllib.parse.urlsplit(base_url).scheme not in ("http", "https")):
            raise ConfigError(f"provider {name}: base_url must be an http(s) address")
        providers[name] = ProviderSettings(name, api, model, key, base_url or None)
    default = data.get("default") or next(iter(providers))
    if default not in providers:
        raise ConfigError(f"the default provider {default!r} is not configured")
    period = data.get("period", "month")
    if period not in PERIODS:
        raise ConfigError(f"period must be one of {', '.join(PERIODS)}")
    return Settings(providers, default, _number(data, "max_tokens", 0, 0, 10 ** 12), period,
                    float(_number(data, "timeout", 60, 5, 600)), _number(data, "max_output_tokens", 2048, 16, 200_000))


def load_settings(path: Path) -> Settings:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path} is not UTF-8") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError(f"{path} is not valid JSON") from None
    return parse_settings(data)


def read_token(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise SystemExit(f"ai gateway: cannot read the token file {path}: {exc.strerror or exc}") from None
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(f"ai gateway: the token in {path} is shorter than {MIN_TOKEN_LENGTH} characters")
    return token


# --- the ledger: tokens and times of every request -----------------------------------------

class Record(NamedTuple):
    t: float                  # when the request started (Unix time)
    provider: str
    model: str
    input: int
    output: int
    seconds: float
    ok: bool


class Reservation(NamedTuple):
    output_cap: int           # max output tokens for this answer
    tokens: int               # held back from the budget while it runs


class BudgetError(Exception):
    """The budget cannot cover the request; the message says how much is left and when it resets."""


def _utc(t: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc)


def _iso(t: float | None) -> str | None:
    return None if t is None else _utc(t).strftime("%Y-%m-%dT%H:%M:%SZ")


def _times(values: list[float]) -> dict:
    """Count, mean and sample standard deviation in seconds (None where undefined)."""
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 3) if values else None,
        "stdev": round(statistics.stdev(values), 3) if len(values) >= 2 else None,
    }


class Ledger:
    """Usage records in memory and appended to usage.jsonl; the budget arithmetic."""

    def __init__(self, path: Path | None, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.path, self.settings, self._clock = path, settings, clock
        self._lock = threading.Lock()
        self._records: list[Record] = []
        self._reserved = 0

    def load(self) -> None:
        """Read the ledger file; unreadable lines are skipped (a crash can cut the last one)."""
        if self.path is None:
            return
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("cannot read %s (%s); counting starts from zero", self.path, exc.strerror or exc)
            return
        skipped = 0
        for line in lines:
            try:
                item = json.loads(line)
                record = Record(float(item["t"]), str(item["provider"]), str(item["model"]), int(item["in"]),
                                int(item["out"]), float(item["s"]), bool(item["ok"]))
            except (ValueError, KeyError, TypeError):
                skipped += 1
                continue
            self._records.append(record)
        if skipped:
            log.warning("skipped %d unreadable line(s) in %s", skipped, self.path)

    # The period ------------------------------------------------------------------------

    def period_bounds(self, now: float) -> tuple[float | None, float | None]:
        if self.settings.period == "total":
            return None, None
        today = _utc(now)
        start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        return start.timestamp(), end.timestamp()

    def _period_records(self, now: float) -> list[Record]:
        start, _end = self.period_bounds(now)
        return [r for r in self._records if start is None or r.t >= start]

    def _period_text(self, now: float) -> str:
        return "this month" if self.settings.period == "month" else "in total"

    def _next_step(self, now: float) -> str:
        _start, end = self.period_bounds(now)
        when = f"It starts again on {_utc(end):%Y-%m-%d} (UTC). " if end else ""
        return when + "To continue now, raise ai.budget.max_tokens in config.yaml and run update."

    # Requests --------------------------------------------------------------------------

    def reserve(self, estimated_input: int) -> Reservation:
        """Hold back tokens for one request, or raise BudgetError."""
        cap = self.settings.max_output_tokens
        limit = self.settings.max_tokens
        if not limit:
            return Reservation(cap, 0)
        with self._lock:
            now = self._clock()
            used = sum(r.input + r.output for r in self._period_records(now))
            left = limit - used - self._reserved
            if left <= 0:
                raise BudgetError(f"The AI token budget is used up: {used:,} of {limit:,} tokens "
                                  f"{self._period_text(now)}. {self._next_step(now)}")
            if left < estimated_input + MIN_OUTPUT_TOKENS:
                raise BudgetError(f"Too little of the AI token budget is left for this prompt: it needs about "
                                  f"{estimated_input + MIN_OUTPUT_TOKENS:,} tokens, {left:,} are left "
                                  f"{self._period_text(now)}. {self._next_step(now)}")
            cap = min(cap, left - estimated_input)
            reservation = Reservation(cap, estimated_input + cap)
            self._reserved += reservation.tokens
            return reservation

    def finish(self, reservation: Reservation, record: Record) -> None:
        line = json.dumps({"t": round(record.t, 3), "provider": record.provider, "model": record.model,
                           "in": record.input, "out": record.output, "s": round(record.seconds, 3), "ok": record.ok})
        with self._lock:
            self._reserved -= reservation.tokens
            self._records.append(record)
            if self.path is None:
                return
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                # The count in memory still holds until the container restarts.
                log.warning("cannot write %s: %s", self.path, exc.strerror or exc)

    def summary(self) -> dict:
        """Budget and usage of the current period, overall and per provider."""
        with self._lock:
            now = self._clock()
            records = self._period_records(now)
            reserved = self._reserved
        start, end = self.period_bounds(now)
        used = sum(r.input + r.output for r in records)
        limit = self.settings.max_tokens
        providers = []
        for name, provider in self.settings.providers.items():
            mine = [r for r in records if r.provider == name]
            providers.append({
                "name": name, "api": provider.api, "model": provider.model,
                "requests": len(mine), "errors": sum(1 for r in mine if not r.ok),
                "input_tokens": sum(r.input for r in mine), "output_tokens": sum(r.output for r in mine),
                "seconds": _times([r.seconds for r in mine if r.ok]),
            })
        return {
            "period": self.settings.period,
            "period_start": _iso(start),
            "period_end": _iso(end),
            "max_tokens": limit,
            "used_tokens": used,
            "input_tokens": sum(r.input for r in records),
            "output_tokens": sum(r.output for r in records),
            "remaining_tokens": max(limit - used, 0) if limit else None,
            "in_progress_tokens": reserved,
            "requests": len(records),
            "errors": sum(1 for r in records if not r.ok),
            "seconds": _times([r.seconds for r in records if r.ok]),
            "providers": providers,
        }


# --- providers -----------------------------------------------------------------------------

class Answer(NamedTuple):
    input_tokens: int
    output_tokens: int
    truncated: bool           # stopped at the output cap


def estimate_tokens(text: str) -> int:
    """A generous guess (about 3 characters per token); the providers report the real count."""
    return math.ceil(len(text) / 3) + 8


def explain(exc: Exception, provider: ProviderSettings, model: str, settings: Settings) -> str:
    """One line per failure. The SDKs' exception classes share names; map them by name."""
    kind = type(exc).__name__
    who = f"{provider.name} ({model})"
    messages = {
        "AuthenticationError": f"{who}: the API key was rejected. Check ai.providers.{provider.name}.api_key "
                               "in config.yaml and run update.",
        "PermissionDeniedError": f"{who}: the key may not use this model.",
        "NotFoundError": f"{who}: the model is not available to this key. Check the model name.",
        "RateLimitError": f"{who}: rate limited (still after {MAX_RETRIES} retries). Wait a minute and try again.",
        "OverloadedError": f"{who}: the service is overloaded. Try again shortly.",
        "InternalServerError": f"{who}: the service had an internal error. Try again shortly.",
        "APITimeoutError": f"{who}: no answer within {settings.timeout:.0f} s.",
        "APIConnectionError": f"{who}: cannot reach the API (network, proxy or DNS).",
        "BadRequestError": f"{who}: the request was refused: {getattr(exc, 'message', '') or kind}",
    }
    status = getattr(exc, "status_code", None)
    return messages.get(kind) or f"{who}: {kind}{f' (HTTP {status})' if status else ''}."


class Providers:
    """One SDK client per provider, created at start-up (the clients are thread-safe)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._clients: dict[str, tuple[object, type[Exception]]] = {}
        for provider in settings.providers.values():
            if provider.api == "anthropic":
                import anthropic
                client = anthropic.Anthropic(api_key=provider.api_key, base_url=provider.base_url,
                                             timeout=settings.timeout, max_retries=MAX_RETRIES)
                self._clients[provider.name] = (client, anthropic.APIError)
            else:
                import openai
                client = openai.OpenAI(api_key=provider.api_key, base_url=provider.base_url,
                                       timeout=settings.timeout, max_retries=MAX_RETRIES)
                self._clients[provider.name] = (client, openai.APIError)

    def generate(self, provider: ProviderSettings, model: str, prompt: str, max_output_tokens: int,
                 on_text: Callable[[str], None]) -> Answer:
        client, api_error = self._clients[provider.name]
        try:
            if provider.api == "anthropic":
                return self._anthropic(client, model, prompt, max_output_tokens, on_text)
            return self._openai(client, model, prompt, max_output_tokens, on_text)
        except api_error as exc:
            raise ProviderError(explain(exc, provider, model, self.settings)) from None

    @staticmethod
    def _anthropic(client, model, prompt, max_output_tokens, on_text) -> Answer:
        with client.messages.stream(model=model, max_tokens=max_output_tokens, system=SYSTEM_PROMPT,
                                    messages=[{"role": "user", "content": prompt}]) as stream:
            for text in stream.text_stream:
                on_text(text)
            message = stream.get_final_message()
        usage = message.usage
        cached = (usage.cache_creation_input_tokens or 0) + (usage.cache_read_input_tokens or 0)
        return Answer(usage.input_tokens + cached, usage.output_tokens, message.stop_reason == "max_tokens")

    @staticmethod
    def _openai(client, model, prompt, max_output_tokens, on_text) -> Answer:
        with client.responses.stream(model=model, instructions=SYSTEM_PROMPT, input=prompt,
                                     max_output_tokens=max_output_tokens) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    on_text(event.delta)
            response = stream.get_final_response()
        usage = response.usage
        truncated = response.status == "incomplete"
        if usage is None:
            # A proxy that reports no usage: count a guess rather than nothing.
            text = "".join(getattr(part, "text", "") for item in response.output
                           for part in getattr(item, "content", None) or [])
            return Answer(estimate_tokens(SYSTEM_PROMPT + prompt), estimate_tokens(text), truncated)
        return Answer(usage.input_tokens, usage.output_tokens, truncated)


# --- HTTP ------------------------------------------------------------------------------------

class Gateway:
    """What the handler threads share: settings, ledger, SDK clients, the concurrency limit."""

    def __init__(self, settings: Settings, ledger: Ledger, providers: Providers, token: bytes) -> None:
        self.settings, self.ledger, self.providers, self.token = settings, ledger, providers, token
        self._slots = threading.BoundedSemaphore(MAX_ACTIVE)
        self._active = 0
        self._active_lock = threading.Lock()

    def status(self) -> dict:
        return {
            "default": self.settings.default,
            "providers": [{"name": p.name, "api": p.api, "model": p.model}
                          for p in self.settings.providers.values()],
            "timeout": self.settings.timeout,
            "max_output_tokens": self.settings.max_output_tokens,
            "usage": self.ledger.summary(),
        }

    @property
    def active(self) -> int:
        with self._active_lock:
            return self._active

    def begin(self) -> bool:
        if not self._slots.acquire(blocking=False):
            return False
        with self._active_lock:
            self._active += 1
        return True

    def end(self) -> None:
        with self._active_lock:
            self._active -= 1
        self._slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ai-gateway"
    sys_version = ""
    timeout = SOCKET_TIMEOUT
    gateway: Gateway                 # set in main() (or by a test)

    def log_message(self, format, *args):  # noqa: A002 - signature of the base class
        log.info("%s %s", self.address_string(), format % args)

    def log_request(self, code="-", size="-"):
        path = getattr(self, "path", "").split("?", 1)[0]
        if self.command == "GET" and isinstance(code, int) and code < 400 and path in ("/health", "/status"):
            return
        super().log_request(code, size)

    def _send_json(self, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        reason = message or self.responses.get(code, ("error",))[0]
        self._send_json(code, {"error": str(reason).lower()})

    def _authorized(self) -> bool:
        scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
        return scheme.lower() == "bearer" and secrets.compare_digest(token.strip().encode("utf-8", "replace"),
                                                                    self.gateway.token)

    def _read_json(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(411, "chunked request bodies are not supported")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise HttpError(400, "invalid Content-Length") from None
        if length <= 0:
            raise HttpError(400, "a JSON body is required")
        if length > MAX_BODY_BYTES:
            raise HttpError(413, "the request body is larger than 512 KB")
        if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
            raise HttpError(415, "the body must be application/json")
        try:
            raw = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            raise HttpError(408, "the request body did not arrive in time") from None
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise HttpError(400, "the body is not valid JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "the body must be a JSON object")
        return body

    def do_GET(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Bearer realm="ai-gateway"'})
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
        elif path == "/status":
            self._send_json(200, self.gateway.status())
        elif path == "/generate":
            self._send_json(405, {"error": "method not allowed"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Bearer realm="ai-gateway"'})
            return
        path = urllib.parse.urlsplit(self.path).path
        if path != "/generate":
            self._send_json(405 if path in ("/health", "/status") else 404,
                            {"error": "method not allowed" if path in ("/health", "/status") else "not found"})
            return
        try:
            self._generate()
        except HttpError as exc:
            self._send_json(exc.status, exc.body)

    # /generate -------------------------------------------------------------------------------

    def _request(self) -> tuple[ProviderSettings, str, str]:
        body = self._read_json()
        settings = self.gateway.settings
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HttpError(400, "the prompt is empty")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise HttpError(413, f"the prompt is longer than {MAX_PROMPT_CHARS:,} characters")
        name = body.get("provider") or settings.default
        if not isinstance(name, str) or name not in settings.providers:
            known = ", ".join(settings.providers)
            raise HttpError(404, f"No AI provider named {str(name)!r} is configured (configured: {known}).")
        provider = settings.providers[name]
        model = body.get("model") or provider.model
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise HttpError(400, "the model name is not usable (letters, digits and . _ : / @ + -)")
        return provider, model, prompt

    def _generate(self) -> None:
        provider, model, prompt = self._request()
        gateway = self.gateway
        if not gateway.begin():
            raise HttpError(429, f"{MAX_ACTIVE} AI answers are already being written; wait for one to finish.")
        try:
            reservation = gateway.ledger.reserve(estimate_tokens(SYSTEM_PROMPT + prompt))
        except BudgetError as exc:
            gateway.end()
            raise HttpError(429, str(exc)) from None
        started, wall = time.monotonic(), time.time()
        answer, error = Answer(0, 0, False), ""
        stream = _Stream(self)
        stream.start()
        try:
            answer = gateway.providers.generate(provider, model, prompt, reservation.output_cap, stream.text)
        except ProviderError as exc:
            error = str(exc)
        except Exception as exc:        # an SDK bug or an unexpected reply: say so, keep serving
            log.exception("%s %s failed", provider.name, model)
            error = f"{provider.name} ({model}): unexpected error ({type(exc).__name__})."
        finally:
            seconds = time.monotonic() - started
            gateway.ledger.finish(reservation, Record(wall, provider.name, model, answer.input_tokens,
                                                      answer.output_tokens, seconds, not error))
            gateway.end()
        log.info("%s %s: %d in + %d out tokens, %.1f s, %s%s", provider.name, model, answer.input_tokens,
                 answer.output_tokens, seconds, "failed" if error else "ok",
                 "" if stream.connected else " (the notebook stopped waiting)")
        if error:
            stream.line({"error": error, "seconds": round(seconds, 3)})
            return
        usage = gateway.ledger.summary()
        stream.line({
            "done": True, "provider": provider.name, "model": model,
            "input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens,
            "truncated": answer.truncated, "seconds": round(seconds, 3),
            "budget": {k: usage[k] for k in ("period", "max_tokens", "used_tokens", "remaining_tokens", "period_end")},
        })


class _Stream:
    """NDJSON lines to the client. When it goes away the answer is still read to the end."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.connected = True

    def start(self) -> None:
        handler = self.handler
        handler.send_response(200)
        handler.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.close_connection = True          # HTTP/1.0: the body ends when the connection closes
        try:
            handler.end_headers()
        except OSError:
            self.connected = False

    def line(self, item: dict) -> None:
        if not self.connected:
            return
        try:
            self.handler.wfile.write(json.dumps(item, ensure_ascii=False).encode("utf-8") + b"\n")
            self.handler.wfile.flush()
        except OSError:
            self.connected = False

    def text(self, text: str) -> None:
        if text:
            self.line({"text": text})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s", stream=sys.stderr)
    # One line per answer comes from the gateway itself; the SDKs' HTTP client would add one per request.
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    config_file = Path(os.environ.get("AI_CONFIG_FILE") or "/run/secrets/ai_config")
    try:
        settings = load_settings(config_file)
    except ConfigError as exc:
        # Exit so the container shows as failing: the deploy reports it instead of a silent gateway.
        raise SystemExit(f"ai gateway: refusing to start: {exc}. Check the ai: section of config.yaml "
                         "and run update.") from None
    token = read_token(os.environ.get("AI_TOKEN_FILE") or "/run/secrets/ai_token")
    data_dir = Path(os.environ.get("AI_DATA_DIR") or "/var/lib/thebe-ai")
    ledger = Ledger(data_dir / "usage.jsonl", settings)
    ledger.load()
    Handler.gateway = Gateway(settings, ledger, Providers(settings), token)

    server = Server(("0.0.0.0", PORT), Handler)
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("received %s; shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    budget = f"{settings.max_tokens:,} tokens per {settings.period}" if settings.max_tokens else "no token limit"
    log.info("listening on port %d: providers %s (default %s), %s", PORT,
             ", ".join(f"{p.name}={p.api}/{p.model}" for p in settings.providers.values()), settings.default, budget)
    stop.wait()
    server.shutdown()
    # Answers still being written are counted when they finish; give them a moment.
    deadline = time.monotonic() + SHUTDOWN_WAIT
    while Handler.gateway.active and time.monotonic() < deadline:
        time.sleep(0.2)
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
