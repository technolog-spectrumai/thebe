"""Statistics dashboard for the jupyterlab-tailscale stack (FastAPI, served by uvicorn).

Runs in the `stats` container on port 8889, published only on the host's Tailscale
IPv4. Every path - pages, /api/*, /health and /static/* - requires HTTP Basic auth with
STATS_USER and the JupyterLab password read from JUPYTER_PASSWORD_FILE.

Environment (set by compose.yaml):
  STATS_USER, JUPYTER_PASSWORD_FILE      Basic auth credentials
  JUPYTER_INTERNAL_URL                   JupyterLab on the Compose network (kernels, health)
  JUPYTER_PUBLIC_URL                     link target in the navigation bar
  HOST_NAME, HOST_PROC, HOST_SYS         host identity and the read-only /proc and /sys mounts
  WORKSPACE_DIR                          filesystem whose usage is reported
"""

import asyncio
import contextlib
import logging
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import hoststats
import pages
from cache import RefreshingValue
from gpustats import GpuReader
from jupyterapi import JupyterClient, jupyter_reachable
from security import BasicAuthMiddleware, ConfigError, SecurityHeadersMiddleware, read_password_file

BASE_DIR = Path(__file__).resolve().parent

# uvicorn configures only its own loggers; give ours a handler in the same format.
log = logging.getLogger("stats")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


class _QuietPolling(logging.Filter):
    """Drop access-log lines for successful dashboard polls and health checks.

    An open page polls every 3 s; failures (401s included) are still logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access arguments: (client, method, path, http_version, status)
        if isinstance(record.args, tuple) and len(record.args) == 5:
            _, method, path, _, status = record.args
            if method == "GET" and isinstance(status, int) and status < 400:
                return str(path).split("?", 1)[0] not in ("/api/stats", "/health")
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

# --- data sources --------------------------------------------------------------------

sampler = hoststats.Sampler(interval=2.0)
gpu_reader = GpuReader()
jupyter_client = JupyterClient(JUPYTER_INTERNAL_URL, PASSWORD)


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
api.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

renderer = pages.PageRenderer(host_name=hoststats.HOST_NAME, jupyter_public_url=JUPYTER_PUBLIC_URL)


def _page_endpoint(item: pages.NavItem):
    body = renderer.page(item.key)

    async def show_page() -> HTMLResponse:
        return HTMLResponse(body)

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
    local, kernel_data, gpu_data = await asyncio.gather(
        asyncio.to_thread(_local_stats),
        kernels.get(),
        gpu.get(),
    )
    return JSONResponse({"generated_at": generated_at, **local, "kernels": kernel_data, "gpu": gpu_data})


@api.get("/health")
async def health() -> JSONResponse:
    # 200 whenever this app works; JupyterLab's state is informational only.
    return JSONResponse({"status": "ok", "jupyter": await jupyter_health.get()})


# The ASGI application uvicorn serves: headers outermost so even 401s carry them.
app = SecurityHeadersMiddleware(BasicAuthMiddleware(api, username=USERNAME, password=PASSWORD))
