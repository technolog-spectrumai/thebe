"""Reading the stack's state: Tailscale, docker compose, installer output, child environments."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from thebe.settings import (
    GID_TEXT, MAX_GID, PAGES, PROJECT, TAILNET, Page, check_port, is_public_host, is_valid_hostname,
)

MASK = "********"

# Added to every child environment: plain, uncoloured, unbuffered output.
CHILD_ENV_EXTRA = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PYTHONUNBUFFERED": "1",
    "COMPOSE_ANSI": "never",
    "BUILDKIT_PROGRESS": "plain",
}



@dataclass(frozen=True)
class TailscaleStatus:
    ip: str = ""
    problem: str = ""
    name: str = ""       # full MagicDNS name ('' when MagicDNS is off or the name is unusable)


_TAILSCALE_STATES = {
    "NeedsLogin": "Tailscale is logged out (run: sudo tailscale up)",
    "NeedsMachineAuth": "Tailscale is waiting for this machine to be approved",
    "Stopped": "Tailscale is stopped (run: tailscale up)",
    "Starting": "Tailscale is still starting",
    "NoState": "Tailscale has no state yet",
}


def _load_json(text: str) -> object:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        # Merged stderr can put a warning in front of the document.
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
    return None


def parse_tailscale_status(text: str) -> TailscaleStatus:
    data = _load_json(text)
    if not isinstance(data, dict) or "BackendState" not in data:
        return TailscaleStatus(problem="Tailscale status could not be read")
    state = str(data.get("BackendState") or "")
    if state != "Running":
        return TailscaleStatus(problem=_TAILSCALE_STATES.get(state, f"Tailscale is not connected ({state or 'unknown'})"))
    node = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    for address in node.get("TailscaleIPs") or []:
        try:
            ip = ipaddress.ip_address(str(address))
        except ValueError:
            continue
        if ip.version == 4 and ip in TAILNET:
            return TailscaleStatus(ip=str(ip), name=magicdns_name(data))
    return TailscaleStatus(problem="Tailscale runs but has no IPv4 address in 100.64.0.0/10")


def magicdns_name(data: Mapping) -> str:
    """This machine's MagicDNS name from `tailscale status --json`, the way the installer reads it."""
    tailnet = data.get("CurrentTailnet") if isinstance(data.get("CurrentTailnet"), dict) else {}
    node = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    if tailnet.get("MagicDNSEnabled") is not True or not isinstance(node.get("DNSName"), str):
        return ""
    name = node["DNSName"].lower().removesuffix(".")
    return name if is_valid_hostname(name) else ""


def parse_compose_ps(text: str, project: str = PROJECT) -> dict[str, dict]:
    """`docker compose ps -a --format json` (JSON Lines or an array) -> per-service info."""
    loaded = _load_json(text) if text.lstrip().startswith("[") else None
    if isinstance(loaded, list):
        records = loaded
    else:
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith(("{", "[")):    # skip warnings sharing the channel
                continue
            try:
                record = json.loads(line)
            except ValueError:                     # e.g. "[+] Running 2/2"
                continue
            records.extend(record if isinstance(record, list) else [record])
    services: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("Service"):
            continue
        if record.get("Project") not in (None, "", project):
            continue
        ports = set()
        for publisher in record.get("Publishers") or []:
            try:
                if int(publisher.get("PublishedPort") or 0) > 0:
                    ports.add(int(publisher["PublishedPort"]))
            except (AttributeError, TypeError, ValueError):
                continue
        info = {"state": str(record.get("State", "")).lower(),
                "health": str(record.get("Health", "")).lower(), "ports": ports}
        previous = services.get(record["Service"])
        if previous is None or previous["state"] != "running":
            services[record["Service"]] = info
    return services


def service_state(entry: Mapping | None, *, disabled: bool = False) -> tuple[str, str]:
    """(label, kind) for a service; kind is success, caution, warn or muted."""
    if entry is None:
        return ("Disabled", "muted") if disabled else ("Not deployed", "muted")
    state, health = entry.get("state", ""), entry.get("health", "")
    if state == "running":
        if health == "healthy":
            return "Running", "success"
        if health == "unhealthy":
            return "Unhealthy", "warn"
        return "Starting", "caution"
    if state == "restarting":
        return "Restarting", "warn"
    if state == "paused":
        return "Paused", "caution"
    return "Stopped", "muted"


# host-setup <ts-ip> <port> [<port>] [--cert <fqdn> <gid>] | host-teardown. The pattern only
# admits the characters; root_step_from_line applies the installer's exact rules.
_ROOT_STEP = re.compile(
    r"ROOT_STEP_REQUIRED: (host-setup 100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?: [1-9][0-9]{3,4}){1,2}"
    r"(?: --cert [a-z0-9.-]{1,253} [1-9][0-9]{0,9})?|host-teardown)")


def root_step_from_line(line: str) -> list[str] | None:
    """Installer arguments for pkexec when `line` is a valid ROOT_STEP_REQUIRED line."""
    match = _ROOT_STEP.fullmatch(line.strip())
    if match is None:
        return None
    args = match.group(1).split(" ")
    if args[0] == "host-setup":
        try:
            if ipaddress.ip_address(args[1]) not in TAILNET:
                return None
        except ValueError:
            return None
        ports = args[2:]
        if "--cert" in ports:
            # The certificate for the full MagicDNS name, readable by the group of the desktop user.
            ports, (name, gid) = ports[:ports.index("--cert")], ports[ports.index("--cert") + 1:]
            if not is_valid_hostname(name) or not GID_TEXT.fullmatch(gid) or not 1 <= int(gid) <= MAX_GID:
                return None
        if not all(1024 <= int(port) <= 65535 for port in ports) or len(set(ports)) != len(ports):
            return None
    return args


def page_url(runtime: Mapping[str, str], page: Page) -> str:
    """URL of a page from the deployed runtime .env, or '' when it is not usable.

    PUBLIC_SCHEME and PUBLIC_HOST say how the stack is reached (https with the MagicDNS name
    when a certificate is in use). Older runtime files have neither and mean http://<TS_IP>;
    an empty value counts as unset, like ${PUBLIC_HOST:-...} in Compose.
    """
    scheme = runtime.get("PUBLIC_SCHEME") or "http"
    host = runtime.get("PUBLIC_HOST") or runtime.get("TS_IP", "")
    port = runtime.get(page.port_key, "")
    if scheme not in ("http", "https") or not is_public_host(host):
        return ""
    if check_port(port, page.label)[1]:
        return ""
    return f"{scheme}://{host}:{int(port)}{page.path}"


def service_url(runtime: Mapping[str, str], service: str) -> str:
    return next((page_url(runtime, p) for p in PAGES if p.service == service), "")


def mask_secrets(text: str, secrets: Iterable[str]) -> str:
    # Very short strings are skipped: masking every "a" would wreck the log,
    # and a valid password is at least 8 characters anyway.
    for secret in sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def clean_line(text: str) -> str:
    """Strip ANSI sequences and keep only the last state of a '\\r' progress line."""
    text = text.rstrip("\r\n")
    if "\r" in text:
        text = text.rsplit("\r", 1)[-1]
    return _ANSI.sub("", text)


def child_environment(base: Mapping[str, str], secrets: Iterable[str] = (),
                      overrides: Mapping[str, str] | None = None, *, plain: bool = True) -> dict[str, str]:
    """The environment for child processes. It never carries the password.

    plain: uncoloured, unbuffered output for a log panel (CHILD_ENV_EXTRA); off for a terminal.
    """
    env = dict(base)
    if plain:
        env.update(CHILD_ENV_EXTRA)
    env.update(overrides or {})
    env.pop("JUPYTER_PASSWORD", None)
    # The installer checks the password with bash, which counts bytes and
    # classifies only ASCII in the C locale. A UTF-8 locale makes its rules
    # the ones password_problems() mirrors.
    ctype = (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or "").lower()
    if "utf-8" not in ctype and "utf8" not in ctype:
        env["LC_ALL"] = "C.UTF-8"
    secrets = [s for s in secrets if len(s) >= 4]
    return {k: v for k, v in env.items() if not any(s in v or s in k for s in secrets)}
