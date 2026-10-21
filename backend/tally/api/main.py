"""FastAPI application.

Run:  uvicorn tally.api.main:app --reload      (or: python -m tally serve)

On a single free-tier web service, set EMBEDDED_WORKER=true and EMBEDDED_SCHEDULER=true
and this process also runs the extraction workers and the scoring scheduler as
background threads. Locally or on a bigger deployment, run them as separate processes
(`python -m tally worker`, `python -m tally scheduler`) so they scale independently.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings
from ..db import init_db
from ..runtime import Runtime
from .deps import set_runtime
from .routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tally")


@asynccontextmanager
async def lifespan(app: FastAPI):
    st = get_settings()
    if st.admin_token == "change-me":
        log.warning("ADMIN_TOKEN is the default; set a real one before deploying")
    init_db()
    rt = Runtime(st)
    set_runtime(rt)
    if st.embedded_worker:
        rt.start_workers(st.worker_concurrency)
    if st.embedded_scheduler:
        rt.start_scheduler()
    log.info("tally ready: queue=%s extractor=%s market=%s", rt.queue.backend, rt.extractor.id,
             st.market_data_provider)
    yield
    rt.shutdown()


def create_app() -> FastAPI:
    st = get_settings()
    app = FastAPI(title="Tally", version="1.0.0", lifespan=lifespan,
                  description="Who actually makes good calls? An honest, timestamped track record.")
    app.add_middleware(CORSMiddleware, allow_origins=st.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["*"])
    app.include_router(router)
    return app


app = create_app()
