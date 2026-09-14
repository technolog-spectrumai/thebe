"""Host statistics read from the host's /proc and /sys, bind-mounted read-only.

psutil is deliberately not used: it hard-codes /sys paths, and its network counters
come from /proc/net/dev, a symlink into the *reading* process's network namespace (the
container's own eth0). The host's interfaces are seen through PID 1 instead:
HOST_PROC/1/net/dev.

Every reader tolerates missing or unreadable files: the section then carries null
values plus an "error" string instead of failing the whole request.
"""

import asyncio
import logging
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("stats.host")

HOST_PROC = Path(os.environ.get("HOST_PROC") or "/proc")
HOST_SYS = Path(os.environ.get("HOST_SYS") or "/sys")
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR") or "/workspace"
# Inside the container gethostname() is the container ID, so the installer passes the
# host's name in HOST_NAME; the fallback only matters when running outside Compose.
HOST_NAME = os.environ.get("HOST_NAME") or socket.gethostname()

# Loopback and container plumbing are noise on a dashboard.
EXCLUDED_INTERFACES = ("lo",)
EXCLUDED_INTERFACE_PREFIXES = ("veth", "docker", "br-", "virbr")
KIND_ORDER = {"physical": 0, "tailscale": 1, "virtual": 2}

# hwmon drivers that report CPU temperature, in order of preference.
CPU_SENSOR_DRIVERS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")
# Whole-package readings: Intel "Package id N"; AMD "Tdie" before "Tctl", because Tctl
# may include an offset used for fan control.
PACKAGE_LABELS = ("package id", "tdie", "tctl")


class Unavailable(Exception):
    """A statistic could not be read; str(exc) says why."""


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise Unavailable(f"{path} does not exist") from None
    except OSError as exc:
        raise Unavailable(f"{path} is not readable ({exc.strerror or exc})") from None


def _problem(exc: Exception, source: Path) -> str:
    return str(exc) if isinstance(exc, Unavailable) else f"unexpected format in {source}"


def _percent(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


# --- host ------------------------------------------------------------------------

_cpu_model: str | None = None


def _read_cpu_model() -> str | None:
    """First model line of /proc/cpuinfo (x86 "model name", ARM "Model"/"Hardware")."""
    global _cpu_model
    if _cpu_model is None:
        for line in _read_text(HOST_PROC / "cpuinfo").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in ("model name", "Model", "cpu model", "Hardware"):
                if value.strip():
                    _cpu_model = " ".join(value.split())
                    break
    return _cpu_model


def host_info() -> dict:
    problems = []
    info = {
        "name": HOST_NAME,
        # Containers share the host kernel, so uname() inside the container is the host's.
        "kernel_release": os.uname().release,
        "cpu_model": None,
        "uptime_seconds": None,
        "boot_time": None,
        "load": None,
        "error": None,
    }
    try:
        info["cpu_model"] = _read_cpu_model()
    except Unavailable as exc:
        problems.append(str(exc))

    path = HOST_PROC / "uptime"
    try:
        info["uptime_seconds"] = round(float(_read_text(path).split()[0]), 1)
    except (Unavailable, ValueError, IndexError) as exc:
        problems.append(_problem(exc, path))

    path = HOST_PROC / "stat"
    try:
        btime = next(
            (int(line.split()[1]) for line in _read_text(path).splitlines() if line.startswith("btime ")),
            None,
        )
        if btime is None and info["uptime_seconds"] is not None:
            btime = time.time() - info["uptime_seconds"]
        if btime is not None:
            info["boot_time"] = utc_iso(btime)
    except (Unavailable, ValueError, IndexError) as exc:
        problems.append(_problem(exc, path))

    path = HOST_PROC / "loadavg"
    try:
        load = [float(value) for value in _read_text(path).split()[:3]]
        if len(load) != 3:
            raise ValueError(path)
        info["load"] = load
    except (Unavailable, ValueError) as exc:
        problems.append(_problem(exc, path))

    info["error"] = "; ".join(problems) or None
    return info


# --- CPU -------------------------------------------------------------------------


def read_cpu_times() -> dict[str, tuple[int, int]]:
    """{"cpu": (busy, total), "cpu0": ...} in clock ticks since boot, from /proc/stat."""
    path = HOST_PROC / "stat"
    times = {}
    for line in _read_text(path).splitlines():
        if not line.startswith("cpu"):
            continue
        name, *fields = line.split()
        try:
            # user nice system idle iowait irq softirq steal; guest and guest_nice are
            # already counted in user and nice, so they are left out of the total.
            values = [int(value) for value in fields[:8]]
        except ValueError:
            raise Unavailable(f"unexpected format in {path}") from None
        if len(values) < 4:
            continue
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        times[name] = (total - idle, total)
    if "cpu" not in times:
        raise Unavailable(f"no cpu line in {path}")
    return times


def _busy_percent(previous: tuple[int, int] | None, current: tuple[int, int] | None) -> float | None:
    if previous is None or current is None:
        return None
    elapsed = current[1] - previous[1]
    if elapsed <= 0:  # no tick elapsed, or the CPU went offline and came back
        return None
    busy = 100.0 * (current[0] - previous[0]) / elapsed
    return round(min(100.0, max(0.0, busy)), 1)


def _temperatures(chip: Path) -> list[tuple[str, float]]:
    readings = []
    for sensor in chip.glob("temp*_input"):
        try:
            celsius = int(sensor.read_text().strip()) / 1000
        except (OSError, ValueError):
            continue
        if not 0 < celsius < 150:  # unplugged or broken sensors report 0 or garbage
            continue
        try:
            label = sensor.with_name(sensor.name.replace("_input", "_label")).read_text().strip().lower()
        except OSError:
            label = ""
        readings.append((label, round(celsius, 1)))
    return readings


def cpu_temperature() -> float | None:
    """CPU package temperature from hwmon, or the hottest CPU sensor, or None."""
    try:
        chips = sorted((HOST_SYS / "class" / "hwmon").iterdir())
    except OSError:
        return None
    by_driver: dict[str, list[Path]] = {}
    for chip in chips:
        for name_file in (chip / "name", chip / "device" / "name"):
            try:
                by_driver.setdefault(name_file.read_text().strip(), []).append(chip)
                break
            except OSError:
                continue
    for driver in CPU_SENSOR_DRIVERS:
        readings = [reading for chip in by_driver.get(driver, ()) for reading in _temperatures(chip)]
        if not readings:
            continue
        for prefix in PACKAGE_LABELS:
            package = [celsius for label, celsius in readings if label.startswith(prefix)]
            if package:
                return max(package)
        return max(celsius for _, celsius in readings)
    return None


# --- memory and disk -------------------------------------------------------------


def memory_info() -> dict:
    keys = ("total", "available", "used", "percent", "swap_total", "swap_used", "swap_percent")
    result = dict.fromkeys(keys) | {"error": None}
    path = HOST_PROC / "meminfo"
    try:
        text = _read_text(path)
    except Unavailable as exc:
        return result | {"error": str(exc)}

    values = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        fields = rest.split()
        if separator and fields and fields[0].isdigit():
            # "kB" in meminfo means KiB; lines without a unit are counts.
            values[key] = int(fields[0]) * (1024 if fields[1:] == ["kB"] else 1)

    total = values.get("MemTotal")
    if not total:
        return result | {"error": f"MemTotal missing from {path}"}
    available = values.get("MemAvailable")
    if available is None:  # kernels before 3.14 have no MemAvailable
        available = sum(values.get(k, 0) for k in ("MemFree", "Buffers", "Cached", "SReclaimable"))
    # "used" as modern free(1) reports it: everything that is not available.
    used = max(0, total - available)
    result |= {"total": total, "available": available, "used": used, "percent": _percent(used, total)}

    swap_total, swap_free = values.get("SwapTotal"), values.get("SwapFree")
    if swap_total is not None and swap_free is not None:
        swap_used = max(0, swap_total - swap_free)
        result |= {
            "swap_total": swap_total,
            "swap_used": swap_used,
            "swap_percent": _percent(swap_used, swap_total) if swap_total else 0.0,
        }
    return result


def disk_info() -> dict:
    result = {"path": WORKSPACE_DIR, "total": None, "used": None, "free": None, "percent": None, "error": None}
    try:
        st = os.statvfs(WORKSPACE_DIR)
    except OSError as exc:
        return result | {"error": f"{WORKSPACE_DIR} is not accessible ({exc.strerror or exc})"}
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    # Free space for unprivileged users (without the root reserve), and percent relative
    # to used + that, exactly as df(1) reports them.
    free = st.f_bavail * st.f_frsize
    return result | {"total": total, "used": used, "free": free, "percent": _percent(used, used + free)}


# --- network ---------------------------------------------------------------------


def read_net_dev() -> dict[str, tuple[int, int]]:
    """{interface: (rx_bytes, tx_bytes)} for the host's network namespace."""
    path = HOST_PROC / "1" / "net" / "dev"
    counters = {}
    for line in _read_text(path).splitlines()[2:]:
        name, separator, rest = line.partition(":")
        fields = rest.split()
        if not separator or len(fields) < 9:
            continue
        try:
            counters[name.strip()] = (int(fields[0]), int(fields[8]))
        except ValueError:
            raise Unavailable(f"unexpected format in {path}") from None
    return counters


def interface_kind(name: str) -> str:
    if name.startswith("tailscale"):
        return "tailscale"
    # Hardware NICs (including virtio NICs of a VM) have a backing device in sysfs.
    if (HOST_SYS / "class" / "net" / name / "device").exists():
        return "physical"
    return "virtual"


def _rate(previous: int, current: int, elapsed: float) -> float | None:
    if current < previous:  # counter reset (interface re-created)
        return None
    return round((current - previous) / elapsed, 1)


def _sum_rates(rates: list[float | None]) -> float | None:
    return None if any(rate is None for rate in rates) else round(sum(rates), 1)


class Sampler:
    """Reads /proc/stat and the host's net/dev periodically.

    CPU usage and network rates are differences between two readings, so they are
    computed here in the background and served from memory.
    """

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self._cpu_previous: dict[str, tuple[int, int]] | None = None
        self._net_previous: tuple[float, dict[str, tuple[int, int]]] | None = None
        waiting = "waiting for the first sample"
        self.cpu = {"percent": None, "per_core": [], "cores": None, "error": waiting}
        self.network = {
            "interfaces": [],
            "totals": {"rx_bytes": None, "tx_bytes": None, "rx_rate": None, "tx_rate": None},
            "error": waiting,
        }

    async def run(self) -> None:
        delay = 0.5  # a short first interval, so rates exist right after start-up
        while True:
            try:
                self.sample()
            except Exception:
                log.exception("sampling host statistics failed")
            await asyncio.sleep(delay)
            delay = self.interval

    def sample(self) -> None:
        self._sample_cpu()
        self._sample_network(time.monotonic())

    def _sample_cpu(self) -> None:
        try:
            current = read_cpu_times()
        except Unavailable as exc:
            self._cpu_previous = None
            self.cpu = {"percent": None, "per_core": [], "cores": None, "error": str(exc)}
            return
        previous, self._cpu_previous = self._cpu_previous, current
        cores = sorted((name for name in current if name[3:].isdigit()), key=lambda name: int(name[3:]))
        if previous is None:
            self.cpu = {
                "percent": None,
                "per_core": [None] * len(cores),
                "cores": len(cores),
                "error": "waiting for a second sample",
            }
            return
        self.cpu = {
            "percent": _busy_percent(previous.get("cpu"), current["cpu"]),
            "per_core": [_busy_percent(previous.get(name), current[name]) for name in cores],
            "cores": len(cores),
            "error": None,
        }

    def _sample_network(self, now: float) -> None:
        try:
            counters = read_net_dev()
        except Unavailable as exc:
            self._net_previous = None
            self.network = {
                "interfaces": [],
                "totals": {"rx_bytes": None, "tx_bytes": None, "rx_rate": None, "tx_rate": None},
                "error": str(exc),
            }
            return
        previous = self._net_previous
        self._net_previous = (now, counters)

        interfaces = []
        for name, (rx_bytes, tx_bytes) in counters.items():
            if name in EXCLUDED_INTERFACES or name.startswith(EXCLUDED_INTERFACE_PREFIXES):
                continue
            rx_rate = tx_rate = None
            if previous is not None and name in previous[1] and now > previous[0]:
                elapsed = now - previous[0]
                previous_rx, previous_tx = previous[1][name]
                rx_rate = _rate(previous_rx, rx_bytes, elapsed)
                tx_rate = _rate(previous_tx, tx_bytes, elapsed)
            interfaces.append(
                {
                    "name": name,
                    "rx_bytes": rx_bytes,
                    "tx_bytes": tx_bytes,
                    "rx_rate": rx_rate,
                    "tx_rate": tx_rate,
                    "kind": interface_kind(name),
                }
            )
        interfaces.sort(key=lambda item: (KIND_ORDER[item["kind"]], item["name"]))

        # Totals count physical NICs only: tailscale0 traffic is already carried by them.
        physical = [item for item in interfaces if item["kind"] == "physical"]
        self.network = {
            "interfaces": interfaces,
            "totals": {
                "rx_bytes": sum(item["rx_bytes"] for item in physical),
                "tx_bytes": sum(item["tx_bytes"] for item in physical),
                "rx_rate": _sum_rates([item["rx_rate"] for item in physical]),
                "tx_rate": _sum_rates([item["tx_rate"] for item in physical]),
            },
            "error": None,
        }

    def cpu_snapshot(self) -> dict:
        """CPU section of /api/stats: sampled usage plus the current temperature."""
        snapshot = dict(self.cpu)
        snapshot["temperature_c"] = cpu_temperature()
        return snapshot
