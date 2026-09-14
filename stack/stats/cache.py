"""A value produced by a blocking function and refreshed in a worker thread."""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("stats.cache")


class RefreshingValue:
    """Cache `fn()` for `ttl` seconds without ever blocking the event loop.

    At most one refresh runs at a time, in a worker thread. A caller waits up to `wait`
    seconds for a refresh it triggered; if the refresh is slower (a DNS lookup that
    hangs, say) the caller gets the previous value, or `placeholder` before the first
    refresh has finished, and the refresh completes in the background.

    `fn` is expected to report its own failures in the value it returns; an unexpected
    exception is logged and turned into a value by `on_error`.
    """

    def __init__(
        self,
        fn: Callable[[], Any],
        *,
        ttl: float,
        wait: float,
        placeholder: Any,
        on_error: Callable[[Exception], Any],
    ):
        self._fn = fn
        self._ttl = ttl
        self._wait = wait
        self._placeholder = placeholder
        self._on_error = on_error
        self._value = placeholder
        self._fresh_until = 0.0
        self._has_value = False
        self._task: asyncio.Task | None = None

    async def get(self) -> Any:
        if self._task is None and time.monotonic() >= self._fresh_until:
            self._task = asyncio.create_task(self._refresh())
        task = self._task
        if task is not None:
            try:
                # shield(): a caller giving up must not cancel the shared refresh.
                await asyncio.wait_for(asyncio.shield(task), self._wait)
            except TimeoutError:
                pass
        return self._value if self._has_value else self._placeholder

    async def _refresh(self) -> None:
        try:
            value = await asyncio.to_thread(self._fn)
        except Exception as exc:
            log.exception("refreshing %s failed", getattr(self._fn, "__qualname__", self._fn))
            value = self._on_error(exc)
        self._value = value
        self._has_value = True
        self._fresh_until = time.monotonic() + self._ttl
        self._task = None
