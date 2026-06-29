"""
Talent Scout web UI (FastAPI).

    ./.venv/bin/python -m scout.cli serve      # or: uvicorn webapp.server:app

Endpoints:
  GET  /                     -> the single-page UI
  POST /api/run              -> start a discovery+scoring job, returns {job_id}
  GET  /api/stream/{job_id}  -> Server-Sent Events of live progress
  GET  /api/results/{job_id} -> final ranked candidates JSON
  GET  /api/config           -> defaults (areas, model, metric legend)
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scout import progress  # noqa: E402
from scout.config import get_settings, load_icp  # noqa: E402
from scout.scoring.scorecard import METRICS  # noqa: E402
from scout.webjob import run_job  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Talent Scout")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/config")
def config() -> JSONResponse:
    icp = load_icp()
    return JSONResponse({
        "model": get_settings().model,
        "default_areas": icp.get("geo", {}).get("areas", []),
        "default_country": icp.get("geo", {}).get("country", "India"),
        "default_role": icp.get("role"),
        "default_description": icp.get("description"),
        "metrics": [
            {"metric": m.replace("_", " "), "pillar": p, "definition": d}
            for m, (p, _w, d) in METRICS.items()
        ],
    })


@app.post("/api/run")
async def run(payload: dict) -> JSONResponse:
    job_id = uuid.uuid4().hex[:12]
    progress.start_job(job_id)
    thread = threading.Thread(target=run_job, args=(job_id, payload), daemon=True)
    thread.start()
    return JSONResponse({"job_id": job_id})


@app.get("/api/stream/{job_id}")
def stream(job_id: str) -> StreamingResponse:
    q = progress.get_queue(job_id)

    def gen():
        if q is None:
            yield f"data: {json.dumps({'stage': 'error', 'message': 'unknown job'})}\n\n"
            return
        yield f"data: {json.dumps({'stage': 'connected', 'message': 'connected'})}\n\n"
        while True:
            try:
                evt = q.get(timeout=20)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if evt.get("stage") == "__end__":
                final = progress.get_result(job_id)
                yield f"data: {json.dumps({'stage': 'end', 'status': progress.get_status(job_id), 'has_results': bool(final)})}\n\n"
                break
            yield f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/results/{job_id}")
def results(job_id: str) -> JSONResponse:
    return JSONResponse({"status": progress.get_status(job_id), "results": progress.get_result(job_id)})


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
