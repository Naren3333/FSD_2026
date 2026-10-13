"""PyMuPDF work off the event loop, one call at a time per process (RAG_PLAN Iteration 14).

PyMuPDF (MuPDF underneath) is not safe to use from several threads at once. Two ingests run concurrently, each
parsing and rendering in worker threads, and when their PyMuPDF calls overlapped the results changed: a bulk
reindex dropped Software Processes' p.9 table and gave CLIP six extra "tables", while each document indexed
alone came out right. Every call that touches PyMuPDF in a worker thread goes through `in_thread`, which holds one
process-wide lock while it runs; the model calls between them still overlap.
"""

import threading
from collections.abc import Callable

import anyio

LOCK = threading.Lock()


async def in_thread[T](fn: Callable[..., T], *args: object) -> T:
    def call() -> T:
        with LOCK:
            return fn(*args)

    return await anyio.to_thread.run_sync(call)
