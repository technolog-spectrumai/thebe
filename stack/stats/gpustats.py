"""NVIDIA GPU statistics through NVML (the nvidia-ml-py package, imported as pynvml).

libnvidia-ml.so.1 is injected into the container by the NVIDIA container toolkit only
when the service runs with `gpus: all` (compose.gpu.yaml). Without it, every read
reports "not available" with the reason instead of failing.
"""

import logging
import threading
import time

try:
    import pynvml
except ImportError:  # the package is in the lock; keep the dashboard alive regardless
    pynvml = None

log = logging.getLogger("stats.gpu")

# A failed nvmlInit() is retried at most this often (e.g. the driver was being updated).
INIT_RETRY_SECONDS = 60.0
# NVML is shut down when no dashboard has asked for GPU data for this long: an open
# NVML session keeps /dev/nvidia* open, which can keep a laptop's discrete GPU from
# runtime-suspending. The next read initialises it again.
IDLE_SHUTDOWN_SECONDS = 120.0


def _query(function, *args):
    """Call one NVML query; None when the device does not support it (common on laptops)."""
    try:
        return function(*args)
    except pynvml.NVMLError:
        return None


class GpuReader:
    """Thread-safe: reads run in worker threads, the idle check on the event loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._initialized = False
        self._init_error = ""
        self._init_retry_at = 0.0
        self._last_read = 0.0

    def read(self) -> dict:
        with self._lock:
            self._last_read = time.monotonic()
            error = self._ensure_initialized()
            if error:
                return {"available": False, "devices": [], "error": error}
            try:
                devices = [self._device(index) for index in range(pynvml.nvmlDeviceGetCount())]
            except pynvml.NVMLError as exc:
                # e.g. the driver was reloaded underneath us: start over on the next read.
                self._shutdown_locked()
                return {"available": False, "devices": [], "error": f"NVML query failed: {exc}"}
        if not devices:
            return {"available": False, "devices": [], "error": "NVML reports no NVIDIA devices"}
        return {"available": True, "devices": devices, "error": None}

    def release_if_idle(self) -> None:
        # Never block the event loop behind a read in progress; try again next time.
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._initialized and time.monotonic() - self._last_read > IDLE_SHUTDOWN_SECONDS:
                self._shutdown_locked()
                log.info("NVML shut down after %.0f s without dashboard viewers", IDLE_SHUTDOWN_SECONDS)
        finally:
            self._lock.release()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_locked()

    # -- internals ------------------------------------------------------------------

    def _ensure_initialized(self) -> str:
        if self._initialized:
            return ""
        if pynvml is None:
            return "the nvidia-ml-py package is not installed"
        now = time.monotonic()
        if now < self._init_retry_at:
            return self._init_error
        try:
            pynvml.nvmlInit()
        except pynvml.NVMLError_LibraryNotFound:
            error = "libnvidia-ml is not available in this container (the service runs without GPU access)"
        except Exception as exc:  # NVMLError (no driver, no permission) or a loader error
            error = f"NVML could not be initialised: {exc}"
        else:
            self._initialized = True
            self._init_error = ""
            return ""
        if error != self._init_error:
            log.info("GPU statistics unavailable: %s", error)
        self._init_error = error
        self._init_retry_at = now + INIT_RETRY_SECONDS
        return error

    def _shutdown_locked(self) -> None:
        if self._initialized:
            self._initialized = False
            try:
                pynvml.nvmlShutdown()
            except pynvml.NVMLError:
                pass

    @staticmethod
    def _device(index: int) -> dict:
        handle = pynvml.nvmlDeviceGetHandleByIndex(index)
        name = _query(pynvml.nvmlDeviceGetName, handle)
        if isinstance(name, bytes):  # older bindings return bytes
            name = name.decode("utf-8", "replace")
        utilization = _query(pynvml.nvmlDeviceGetUtilizationRates, handle)
        memory = _query(pynvml.nvmlDeviceGetMemoryInfo, handle)
        temperature = _query(pynvml.nvmlDeviceGetTemperature, handle, pynvml.NVML_TEMPERATURE_GPU)
        power_mw = _query(pynvml.nvmlDeviceGetPowerUsage, handle)
        limit_mw = _query(pynvml.nvmlDeviceGetEnforcedPowerLimit, handle)

        memory_used = int(memory.used) if memory is not None else None
        memory_total = int(memory.total) if memory is not None else None
        return {
            "index": index,
            "name": name or f"GPU {index}",
            "utilization_percent": float(utilization.gpu) if utilization is not None else None,
            "memory_used": memory_used,
            "memory_total": memory_total,
            "memory_percent": (
                round(100.0 * memory_used / memory_total, 1) if memory_used is not None and memory_total else None
            ),
            "temperature_c": float(temperature) if temperature is not None else None,
            "power_w": round(power_mw / 1000, 1) if power_mw is not None else None,
            "power_limit_w": round(limit_mw / 1000, 1) if limit_mw else None,
        }
