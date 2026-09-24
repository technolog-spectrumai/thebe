"""Statistics dashboard for the jupyterlab-tailscale stack (FastAPI, served by uvicorn).

Runs in the `stats` container on port 8889 (serve.py starts uvicorn, over HTTPS when a
certificate is configured), published only on the host's Tailscale IPv4. Every path -
pages, /theme.css, /api/*, /health and /static/* - requires HTTP Basic auth with
STATS_USER and the JupyterLab password read from JUPYTER_PASSWORD_FILE.

Environment (set by compose.yaml, and compose.tls.yaml for HTTPS):
  STATS_USER, JUPYTER_PASSWORD_FILE      Basic auth credentials
  JUPYTER_INTERNAL_URL                   JupyterLab on the Compose network (kernels, health);
                                         https://jupyterlab:8888 when JupyterLab serves HTTPS
  JUPYTER_PUBLIC_URL                     link target in the navigation bar
  JLT_TLS_CERT                           the certificate serve.py loaded; the footer shows its
                                         name and expiry (unset: plain HTTP)
  DEPS_INTERNAL_URL, DEPS_TOKEN_FILE     deps runner behind the Dependencies page and its token
  AI_ENABLED, AI_INTERNAL_URL,           the AI gateway (token budget, answer times) and its
  AI_TOKEN_FILE                          token; AI_ENABLED=0: config.yaml has no ai: section
  HOST_NAME, HOST_PROC, HOST_SYS         host identity and the read-only /proc and /sys mounts
  WORKSPACE_DIR                          filesystem whose usage is reported
  THEME                                  theme file under theme/ without .json (default amazing)
"""

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import hoststats
import pages
import theme
from aiapi import AiClient
from cache import RefreshingValue
from depsapi import DepsClient
from gpustats import GpuReader
from jupyterapi import JupyterClient, jupyter_reachable
from security import (
    BasicAuthMiddleware,
    ConfigError,
    CsrfGuardMiddleware,
    SecurityHeadersMiddleware,
    read_password_file,
)

BASE_DIR = Path(__file__).resolve().parent

# uvicorn configures only its own loggers; give ours a handler in the same format.
log = logging.getLogger("stats")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

QUIET_POLL_PATHS = ("/api/stats", "/health", "/api/dependencies", "/api/dependencies/log")


class _QuietPolling(logging.Filter):
    """Drop access-log lines for successful dashboard polls and health checks.

    An open page polls every few seconds; failures (401s included) are still logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access arguments: (client, method, path, http_version, status)
        if isinstance(record.args, tuple) and len(record.args) == 5:
            _, method, path, _, status = record.args
            if method == "GET" and isinstance(status, int) and status < 400:
                return str(path).split("?", 1)[0] not in QUIET_POLL_PATHS
        return True


logging.getLogger("uvicorn.access").addFilter(_QuietPolling())

# python:3.13-slim has no /etc/mime.types, and the standard library table lacks woff2.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")

# --- configuration -------------------------------------------------------------------

try:
    PASSWORD = read_password_file(os.environ.get("JUPYTER_PASSWORD_FILE") or "/run/secrets/jupyter_password")
except ConfigError as exc:
    # Never serve the dashboard without a password. SystemExit ends uvicorn with status 1
    # and prints this line, which `docker compose logs stats` then shows.
    raise SystemExit(f"stats: refusing to start: {exc}") from None

USERNAME = (os.environ.get("STATS_USER") or "jupyter").encode("utf-8")
JUPYTER_INTERNAL_URL = os.environ.get("JUPYTER_INTERNAL_URL") or "http://jupyterlab:8888"
JUPYTER_PUBLIC_URL = os.environ.get("JUPYTER_PUBLIC_URL") or ""
# Set but empty disables the proxy: the page then shows the "unavailable" state.
DEPS_INTERNAL_URL = os.environ.get("DEPS_INTERNAL_URL", "http://deps:8890")
# The runner accepts its own random token, not the password (see deps_runner.py). Without
# it only the Dependencies page is unavailable; the dashboard itself keeps working.
_deps_token_file = os.environ.get("DEPS_TOKEN_FILE") or "/run/secrets/deps_token"
try:
    DEPS_TOKEN = read_password_file(_deps_token_file)
    _deps_problem = None
except ConfigError as exc:
    DEPS_TOKEN, _deps_problem = b"", f"runner token: {exc}"
    log.warning("Dependencies page disabled: %s", _deps_problem)

# The AI gateway's token, shared with jupyterlab. Without it only the AI figures are missing.
AI_ENABLED = (os.environ.get("AI_ENABLED") or "0") == "1"
AI_INTERNAL_URL = os.environ.get("AI_INTERNAL_URL", "http://ai:8891")
try:
    AI_TOKEN = read_password_file(os.environ.get("AI_TOKEN_FILE") or "/run/secrets/ai_token")
    _ai_problem = None
except ConfigError as exc:
    AI_TOKEN, _ai_problem = b"", f"AI gateway token: {exc}"
    if AI_ENABLED:
        log.warning("AI figures disabled: %s", _ai_problem)

# Colours of every page, generated once from theme/<THEME>.json. An unknown or broken theme
# logs one warning and falls back to Amazing Moon: the dashboard always starts.
THEME = theme.load_theme(os.environ.get("THEME") or theme.DEFAULT_THEME, BASE_DIR / "theme")
THEME_CSS = theme.theme_css(THEME).encode("utf-8")

# The HTTPS certificate (serve.py has already checked that it loads). Read once: uvicorn keeps
# the certificate it started with, so a renewed file only counts after a restart.
TLS = pages.read_tls_state(os.environ.get("JLT_TLS_CERT") or "")
if TLS.problem:
    log.warning("HTTPS certificate: %s", TLS.problem)

# --- data sources --------------------------------------------------------------------

sampler = hoststats.Sampler(interval=2.0)
gpu_reader = GpuReader()
jupyter_client = JupyterClient(JUPYTER_INTERNAL_URL, PASSWORD)
deps_client = DepsClient(DEPS_INTERNAL_URL, USERNAME, DEPS_TOKEN, unavailable_reason=_deps_problem)
ai_client = AiClient(AI_INTERNAL_URL, AI_TOKEN, enabled=AI_ENABLED, unavailable_reason=_ai_problem)


def _kernels_error(message: str) -> dict:
    return {"available": False, "count": None, "busy": None, "items": [], "error": message}


def _gpu_error(message: str) -> dict:
    return {"available": False, "devices": [], "error": message}


kernels = RefreshingValue(
    jupyter_client.kernel_stats,
    ttl=5.0,
    wait=2.0,
    placeholder=_kernels_error("Checking JupyterLab…"),
    on_error=lambda exc: _kernels_error(f"kernel check failed ({type(exc).__name__})"),
)
gpu = RefreshingValue(
    gpu_reader.read,
    ttl=2.0,
    wait=2.0,
    placeholder=_gpu_error("Checking for NVIDIA GPUs…"),
    on_error=lambda exc: _gpu_error(f"GPU check failed ({type(exc).__name__})"),
)
ai_status = RefreshingValue(
    ai_client.status,
    ttl=5.0,
    wait=2.0,
    placeholder={"available": False, "enabled": AI_ENABLED, "error": "Checking the AI gateway…"},
    on_error=lambda exc: {"available": False, "enabled": AI_ENABLED, "error": f"AI check failed ({type(exc).__name__})"},
)
jupyter_health = RefreshingValue(
    lambda: jupyter_reachable(JUPYTER_INTERNAL_URL),
    ttl=10.0,
    wait=2.5,
    placeholder="unreachable",
    on_error=lambda exc: "unreachable",
)


def _local_stats() -> dict:
    """Sections read from /proc, /sys and statvfs (run in a worker thread)."""
    return {
        "host": hoststats.host_info(),
        "cpu": sampler.cpu_snapshot(),
        "memory": hoststats.memory_info(),
        "disk": hoststats.disk_info(),
        "network": sampler.network,
    }


async def _release_idle_gpu() -> None:
    while True:
        await asyncio.sleep(30)
        gpu_reader.release_if_idle()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = [
        asyncio.create_task(sampler.run(), name="sample-host"),
        asyncio.create_task(_release_idle_gpu(), name="release-idle-gpu"),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(gpu_reader.shutdown)


# --- application ---------------------------------------------------------------------

api = FastAPI(
    title="jupyterlab-tailscale statistics",
    docs_url=None,  # the interactive docs would need a CDN and are not part of the product
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


async def theme_stylesheet() -> Response:
    return Response(THEME_CSS, media_type="text/css")


# Registered before the /static mount, so no file under static/ can ever answer in its place.
api.add_api_route("/theme.css", theme_stylesheet, methods=["GET"], response_class=Response, include_in_schema=False)
api.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

renderer = pages.PageRenderer(
    host_name=hoststats.HOST_NAME,
    jupyter_public_url=JUPYTER_PUBLIC_URL,
    theme_color={mode: THEME.tokens(mode)["appbar-bg"] for mode in theme.MODES},
    tls=TLS,
)


def _page_endpoint(item: pages.NavItem):
    async def show_page() -> HTMLResponse:
        # Pre-rendered at start-up; only the certificate state in the footer depends on the date.
        return HTMLResponse(renderer.page(item.key))

    show_page.__name__ = f"page_{item.key}"
    return show_page


for _item in pages.NAV_ITEMS:
    api.add_api_route(
        _item.path,
        _page_endpoint(_item),
        methods=["GET"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )


@api.get("/api/stats")
async def stats() -> JSONResponse:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    local, kernel_data, gpu_data, ai_data = await asyncio.gather(
        asyncio.to_thread(_local_stats),
        kernels.get(),
        gpu.get(),
        ai_status.get(),
    )
    return JSONResponse({"generated_at": generated_at, **local, "kernels": kernel_data, "gpu": gpu_data,
                         "ai": ai_data})


@api.get("/health")
async def health() -> JSONResponse:
    # 200 whenever this app works; JupyterLab's state is informational only.
    return JSONResponse({"status": "ok", "jupyter": await jupyter_health.get()})


# --- Dependencies API: a thin proxy to the deps runner ----------------------------------
# Writes below /api/dependencies are additionally guarded by CsrfGuardMiddleware.

DEPS_MAX_BODY_BYTES = 64 * 1024
DEPS_TIMEOUT = 10.0
# Emptying a large pip cache can take a while; the runner does it before answering.
DEPS_CACHE_CLEAR_TIMEOUT = 120.0
_OFFSET = re.compile(r"[0-9]{1,15}")


async def _deps(method: str, path: str, body: dict | None = None, timeout: float = DEPS_TIMEOUT) -> JSONResponse:
    status, payload = await asyncio.to_thread(deps_client.call, method, path, body, timeout=timeout)
    return JSONResponse(payload, status_code=status)


async def _json_body(request: Request) -> dict | JSONResponse:
    """The request's JSON object (at most 64 KB), or the error response to send instead."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isascii() or not declared.isdigit()):
        return JSONResponse({"error": "Invalid Content-Length."}, status_code=400)
    if declared is not None and int(declared) > DEPS_MAX_BODY_BYTES:
        return JSONResponse({"error": "The request body is larger than 64 KB."}, status_code=413)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > DEPS_MAX_BODY_BYTES:
            return JSONResponse({"error": "The request body is larger than 64 KB."}, status_code=413)
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):  # UnicodeDecodeError; RecursionError: deep nesting
        return JSONResponse({"error": "The request body is not valid JSON."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "The request body must be a JSON object."}, status_code=400)
    return body


@api.get("/api/dependencies")
async def dependencies_state() -> JSONResponse:
    return await _deps("GET", "/state")


@api.get("/api/dependencies/log")
async def dependencies_log(request: Request) -> JSONResponse:
    offset = request.query_params.get("offset", "0")
    if not _OFFSET.fullmatch(offset):
        return JSONResponse({"error": "offset must be a non-negative integer"}, status_code=400)
    return await _deps("GET", f"/log?offset={int(offset)}", timeout=5.0)


@api.put("/api/dependencies/requirements")
async def dependencies_requirements(request: Request) -> JSONResponse:
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    return await _deps("PUT", "/requirements", body)


@api.post("/api/dependencies/jobs")
async def dependencies_jobs(request: Request) -> JSONResponse:
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    return await _deps("POST", "/jobs", body)


@api.post("/api/dependencies/cancel")
async def dependencies_cancel(request: Request) -> JSONResponse:
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    return await _deps("POST", "/cancel", body)


@api.post("/api/dependencies/cache/clear")
async def dependencies_cache_clear(request: Request) -> JSONResponse:
    body = await _json_body(request)
    if isinstance(body, JSONResponse):
        return body
    return await _deps("POST", "/cache/clear", body, timeout=DEPS_CACHE_CLEAR_TIMEOUT)


# The ASGI application uvicorn serves: headers outermost so even 401s carry them, then
# the password, then the cross-site guard for the Dependencies API.
app = SecurityHeadersMiddleware(
    BasicAuthMiddleware(
        CsrfGuardMiddleware(api, prefix="/api/dependencies"),
        username=USERNAME,
        password=PASSWORD,
    )
)
