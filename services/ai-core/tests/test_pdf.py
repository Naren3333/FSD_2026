"""PyMuPDF calls in worker threads never overlap (RAG_PLAN Iteration 14: overlapping calls changed results)."""

import asyncio
import threading
import time

import pdf


def test_pdf_work_in_threads_runs_one_call_at_a_time() -> None:
    running, most = 0, 0
    guard = threading.Lock()

    def work() -> None:
        nonlocal running, most
        with guard:
            running += 1
            most = max(most, running)
        time.sleep(0.05)
        with guard:
            running -= 1

    async def main() -> None:
        await asyncio.gather(*(pdf.in_thread(work) for _ in range(4)))

    asyncio.run(main())
    assert most == 1
