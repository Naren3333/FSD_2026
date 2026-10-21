"""pymupdf work off the event loop, one call at a time per process."""

import threading
from collections.abc import Callable

import anyio

LOCK = threading.Lock()


async def in_thread[T](fn: Callable[..., T], *args: object) -> T:
    def call() -> T:
        with LOCK:
            return fn(*args)

    return await anyio.to_thread.run_sync(call)
