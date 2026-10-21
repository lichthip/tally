# =============================================================================
# api/main.py: THE WEB SERVER'S ENTRY POINT
# -----------------------------------------------------------------------------
# FastAPI = the Python web framework. It turns Python functions into HTTP endpoints
# (the functions are in routes.py). `uvicorn` is the program that actually listens on
# a port and passes HTTP requests to FastAPI.
#   Render runs:  uvicorn tally.api.main:app --host 0.0.0.0 --port $PORT
#   which means:  "in file tally/api/main.py, serve the object called `app`".
#
# This file:
#   1. lifespan(): code that runs ONCE at startup (create tables, build the Runtime,
#      start worker/scheduler threads) and once at shutdown.
#   2. create_app(): builds the FastAPI app, adds CORS, attaches the routes.
# =============================================================================
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

# Log format: "2026-10-21 16:00:00 INFO tally.worker: message".
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tally")


# Everything BEFORE `yield` runs at startup; everything AFTER it runs at shutdown.
# (`async` is required by FastAPI for this hook; nothing here actually awaits.)
@asynccontextmanager
async def lifespan(app: FastAPI):
    st = get_settings()
    if st.admin_token == "change-me":
        log.warning("ADMIN_TOKEN is the default; set a real one before deploying")
    init_db()                      # create tables + the append-only trigger (safe to repeat)
    rt = Runtime(st)               # build every component
    set_runtime(rt)                # make it available to the route functions (see deps.py)
    if st.embedded_worker:
        rt.start_workers(st.worker_concurrency)
    if st.embedded_scheduler:
        rt.start_scheduler()
    # The line you look for in Render's logs to know startup worked.
    log.info("tally ready: queue=%s extractor=%s market=%s", rt.queue.backend, rt.extractor.id,
             st.market_data_provider)
    yield                          # <- the server runs, handling requests, while paused here
    rt.shutdown()


def create_app() -> FastAPI:
    st = get_settings()
    app = FastAPI(title="Tally", version="1.0.0", lifespan=lifespan,
                  description="Who actually makes good calls? An honest, timestamped track record.")
    # CORS (Cross-Origin Resource Sharing): browsers block a page on one site (the Vercel
    # frontend) from reading responses from another site (the Render API) unless the API
    # says that site is allowed. This lists the allowed sites (the CORS_ORIGINS setting).
    app.add_middleware(CORSMiddleware, allow_origins=st.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["*"])
    app.include_router(router)     # attach every endpoint defined in routes.py
    return app


# Module-level object that uvicorn looks for ("tally.api.main:app").
app = create_app()
