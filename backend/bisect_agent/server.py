"""HTTP API + static UI. Runs execute in background threads; the UI follows them over SSE."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent, JobInput, JobState, new_job_id
from .config import settings
from .gitops import GitError, validate_repo_url
from .nebius import TokenFactory
from .router import MODELS, ROUTES, TIER_PARAMS
from .sandbox import SUPPORTED_PYTHONS
from .store import Store
from .tavily import Tavily

ROOT = Path(__file__).resolve().parents[2]
WEB_OUT = ROOT / "web" / "out"
SEEDS_DIR = ROOT / "seeds" / "runs"

S = settings()
store = Store(S.db_path)
MAX_CONCURRENT = int(__import__("os").environ.get("BISECT_MAX_CONCURRENT", 2))
PER_IP_PER_HOUR = int(__import__("os").environ.get("BISECT_PER_IP_PER_HOUR", 4))
_slots = threading.Semaphore(MAX_CONCURRENT)
_recent: dict[str, deque] = defaultdict(deque)

app = FastAPI(title="Bisect", version="0.1.0")


class RunRequest(BaseModel):
    repo_url: str = Field(max_length=300)
    bug_report: str = Field(min_length=20, max_length=4000)
    good_ref: str | None = Field(default=None, max_length=200)
    bad_ref: str | None = Field(default=None, max_length=200)
    python: str = "3.11"
    propose_fix: bool = True


def load_seeds() -> None:
    if not SEEDS_DIR.exists():
        return
    for f in sorted(SEEDS_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        if not store.has(data["state"]["id"]):
            store.import_run(data, title=data.get("title", ""))


@app.on_event("startup")
def _startup() -> None:
    store.mark_interrupted()
    load_seeds()


def _start_job(req: RunRequest) -> str:
    state = JobState(id=new_job_id(), input=JobInput(**req.model_dump()))
    store.create_run(state.to_dict())

    def emit(kind: str, payload: dict) -> None:
        store.add_event(state.id, kind, payload)
        if kind in ("phase", "step", "first_bad", "explanation", "fix", "verification", "report", "research", "repro"):
            store.save_state(state.to_dict())

    def work() -> None:
        emit("log", {"message": "Queued; waiting for a free sandbox slot.", "level": "info"})
        with _slots:
            Agent(S, state, emit).run()
        store.save_state(state.to_dict())

    threading.Thread(target=work, name=f"job-{state.id}", daemon=True).start()
    return state.id


@app.post("/api/runs")
def create_run(req: RunRequest, request: Request):
    try:
        validate_repo_url(req.repo_url)
    except GitError as e:
        raise HTTPException(400, str(e))
    if req.python not in SUPPORTED_PYTHONS:
        raise HTTPException(400, f"python must be one of {', '.join(SUPPORTED_PYTHONS)}")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    window = _recent[ip]
    now = time.time()
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= PER_IP_PER_HOUR:
        raise HTTPException(429, "rate limit: a few runs per hour per visitor; the seeded runs are always available")
    window.append(now)
    return {"id": _start_job(req)}


@app.get("/api/runs")
def list_runs():
    return store.list()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    st = store.get(run_id)
    if not st:
        raise HTTPException(404, "run not found")
    return st


@app.get("/api/runs/{run_id}/export")
def export_run(run_id: str):
    if not store.has(run_id):
        raise HTTPException(404, "run not found")
    return store.export(run_id)


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = 0, replay_speed: float = 0):
    """SSE stream. Finished runs replay their recorded events; replay_speed>0 paces them for the demo."""
    if not store.has(run_id):
        raise HTTPException(404, "run not found")

    async def gen():
        last = after
        prev_ts = None
        while True:
            if await request.is_disconnected():
                return
            evs = store.events(run_id, last)
            for ev in evs:
                if replay_speed > 0 and prev_ts is not None:
                    await asyncio.sleep(min(2.0, max(0.0, (ev["ts"] - prev_ts) / replay_speed)))
                prev_ts = ev["ts"]
                last = ev["seq"]
                yield f"id: {ev['seq']}\nevent: {ev['kind']}\ndata: {json.dumps(ev)}\n\n"
            st = store.get(run_id)
            if st and st["phase"] == "done" and not store.events(run_id, last):
                yield "event: end\ndata: {}\n\n"
                return
            if not evs:
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/config")
def config():
    return {
        "provider": "Nebius Token Factory",
        "base_url": TokenFactory().base_url,
        "tiers": {t.value: {"model": m, **TIER_PARAMS[t]} for t, m in MODELS.items()},
        "routes": {task: tier.value for task, tier in ROUTES.items()},
        "limits": asdict(S.limits),
        "nebius_configured": TokenFactory().configured,
        "tavily_configured": Tavily().configured,
        "pythons": list(SUPPORTED_PYTHONS),
    }


@app.get("/api/health")
def health():
    return {"ok": True}


if WEB_OUT.exists():
    app.mount("/_next", StaticFiles(directory=WEB_OUT / "_next"), name="next")

    @app.get("/{path:path}")
    def spa(path: str):
        candidate = (WEB_OUT / path).resolve()
        if path and candidate.is_relative_to(WEB_OUT) and candidate.is_file():
            return FileResponse(candidate)
        html = (WEB_OUT / path / "index.html").resolve()
        if path and html.is_relative_to(WEB_OUT) and html.is_file():
            return FileResponse(html)
        page = (WEB_OUT / f"{path}.html").resolve()
        if path and page.is_relative_to(WEB_OUT) and page.is_file():
            return FileResponse(page)
        return FileResponse(WEB_OUT / "index.html")
else:
    @app.get("/")
    def no_ui():
        return JSONResponse({"message": "UI not built; run `make web`. API is under /api."})
