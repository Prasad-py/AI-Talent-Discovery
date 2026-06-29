"""
Job-scoped progress event bus.

The pipeline modules call progress.emit(...) at meaningful milestones. When a web job
is running, those events stream to the browser (SSE) so the user sees exactly what the
system is doing ("Searching GitHub in Bengaluru", "Fetching from Hugging Face",
"Building 360 profile for ...", "Scoring ..."). Outside a job, emit() is a no-op, so
CLI usage is unaffected.
"""

from __future__ import annotations

import queue
import threading
import time
from contextvars import ContextVar

from rich.console import Console

_current: ContextVar[str | None] = ContextVar("scout_job_id", default=None)
_queues: dict[str, "queue.Queue"] = {}
_results: dict[str, dict] = {}
_status: dict[str, str] = {}
_lock = threading.Lock()
_console = Console()

SENTINEL = {"stage": "__end__"}

# stdout styling so the terminal trace is as detailed as the browser console.
_STYLE = {
    "stage": "bold cyan", "discover": "green", "score": "yellow", "360": "magenta",
    "intake": "cyan", "plan": "cyan", "setup": "dim", "warn": "yellow",
    "error": "bold red", "success": "bold green",
}


def start_job(job_id: str) -> None:
    with _lock:
        _queues[job_id] = queue.Queue()
        _status[job_id] = "running"
        _results.pop(job_id, None)


def bind(job_id: str) -> None:
    """Call inside the worker thread so emit() knows which job it belongs to."""
    _current.set(job_id)


def current_job() -> str | None:
    """The job bound to the current thread (used to propagate into worker threads)."""
    return _current.get()


def emit(stage: str, message: str, data: dict | None = None, level: str = "info") -> None:
    # Mirror to stdout so the terminal shows the same detailed live trace as the UI.
    try:
        style = _STYLE.get(level if level in _STYLE else stage, "white")
        _console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] [{style}]{stage:>8}[/] {message}")
    except Exception:  # noqa: BLE001
        pass

    job_id = _current.get()
    if not job_id:
        return
    q = _queues.get(job_id)
    if q is not None:
        q.put({
            "ts": time.time(),
            "stage": stage,
            "message": message,
            "data": data or {},
            "level": level,
        })


def get_queue(job_id: str) -> "queue.Queue | None":
    return _queues.get(job_id)


def set_result(job_id: str, result: dict) -> None:
    with _lock:
        _results[job_id] = result
        _status[job_id] = "done"


def get_result(job_id: str) -> dict | None:
    return _results.get(job_id)


def get_status(job_id: str) -> str:
    return _status.get(job_id, "unknown")


def fail(job_id: str, error: str) -> None:
    with _lock:
        _status[job_id] = "error"
    emit("error", error, level="error")


def finish(job_id: str) -> None:
    q = _queues.get(job_id)
    if q is not None:
        q.put(SENTINEL)
