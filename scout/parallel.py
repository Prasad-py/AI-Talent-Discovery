"""
Small bounded-concurrency helper.

The pipeline is dominated by I/O- and LLM-wait (GitHub calls, web research, scoring),
so running candidates concurrently is a large speedup. This runs `fn` over `items` in a
thread pool, propagates the live-progress job into each worker thread, and (optionally)
emits a completion counter as tasks finish.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

from . import progress


def run_parallel(
    fn: Callable,
    items: Iterable,
    workers: int = 5,
    on_done: Callable[[int, int], None] | None = None,
) -> list:
    """Run fn(item) across a bounded thread pool. Exceptions per item are swallowed
    (returned as None) so one failure never sinks the batch."""
    items = list(items)
    total = len(items)
    if total == 0:
        return []
    job_id = progress.current_job()
    workers = max(1, min(workers, total))

    def _wrapped(x):
        if job_id:
            progress.bind(job_id)  # progress.emit() is thread-local; rebind per worker
        return fn(x)

    results, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_wrapped, it) for it in items]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:  # noqa: BLE001
                results.append(None)
            done += 1
            if on_done:
                try:
                    on_done(done, total)
                except Exception:  # noqa: BLE001
                    pass
    return results
