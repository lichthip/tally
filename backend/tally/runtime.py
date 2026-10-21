# =============================================================================
# runtime.py: BUILDS EVERYTHING AND WIRES IT TOGETHER (the "composition root")
# -----------------------------------------------------------------------------
# Every other class receives its helpers through its constructor (dependency injection):
# IngestionService(session_factory, queue), ExtractionWorker(queue, pipeline, ...).
# SOMEONE has to create the real objects and hand them over: that's this file, and
# only this file. Read Runtime.__init__ top to bottom to see the whole system assembled:
#
#   settings -> session factory (DB) -> queue -> price provider -> price store (cache)
#            -> symbol resolver -> extractor (LLM or rules) -> pipeline
#            -> ingestion service -> rate limiter
#
# It also has the "actions" other entry points need: score(), poll(), preview(),
# starting worker threads, and the scheduler loop.
#
# Created by: api/main.py at web-server startup, and by most __main__.py CLI commands.
# =============================================================================
"""Composition root: builds every component from settings, in one place.

Nothing else constructs concrete classes (PostgresQueue, GeminiClient, ...) directly; they
receive their collaborators through constructors (dependency injection). That is what lets
tests swap in a synthetic price feed or a rule-based extractor without monkeypatching.
"""
from __future__ import annotations

import logging
import threading
import time

from .config import Settings, get_settings
from .db import get_session_factory
from .extraction.extractors import RuleBasedExtractor, build_extractor
from .extraction.llm_clients import RateLimiter
from .extraction.pipeline import ExtractionPipeline
from .extraction.symbols import SymbolResolver
from .ingestion.service import IngestionService
from .market.providers import build_provider
from .market.store import PriceStore
from .queue import build_queue
from .schemas import IngestResult
from .scoring.job import run_scoring
from .sources import build_source, configured_sources, poll_sources
from .worker import ExtractionWorker

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings | None = None):
        # `a = b = x` sets both. `st` is just a short alias used below.
        self.settings = st = settings or get_settings()
        self.sf = get_session_factory()                         # database
        self.queue = build_queue(st.queue_backend)              # Postgres or Redis queue
        self.provider = build_provider(st.market_data_provider) # Yahoo or synthetic prices
        self.store = PriceStore(self.sf, self.provider)         # price cache wrapping the provider
        # The resolver checks unknown tickers by asking the price store "any prices?".
        self.resolver = SymbolResolver(self.sf, exists_fn=self.store.symbol_exists)
        self.extractor = build_extractor(st, self.sf, self.resolver.symbol_list())
        # When an LLM does the extraction, the keyword baseline acts as an independent
        # second opinion on posts the LLM rejects (catches false "no idea" verdicts).
        second = RuleBasedExtractor(self.resolver.symbol_list()) if self.extractor.uses_llm else None
        self.pipeline = ExtractionPipeline(self.extractor, self.resolver,
                                           confidence_threshold=st.review_confidence_threshold,
                                           default_horizon_days=st.default_horizon_days,
                                           second_opinion=second)
        self.ingestion = IngestionService(self.sf, self.queue)
        # One limiter shared by all worker threads in this process.
        self.limiter = (RateLimiter(st.llm_requests_per_minute)
                        if self.extractor.uses_llm else None)    # no LLM -> no limit needed
        # Injected so tests (and future sources) can swap the factory without patching.
        self.build_source = build_source
        self.stop = threading.Event()          # set at shutdown -> all background loops exit
        self.threads: list[threading.Thread] = []

    # ------------------------------------------------------------ actions
    # Run one scoring pass (see scoring/job.py).
    def score(self) -> dict:
        return run_scoring(self.sf, self.store,
                           normalisation_horizon=self.settings.normalisation_horizon_days)

    # ------------------------------------------------------------ sources
    # The sources listed in .env, as objects.
    def configured_sources(self) -> list:
        st = self.settings
        return configured_sources(st.subreddit_list, st.rss_feed_list, st.stocktwits_list,
                                  st.substack_list)

    # A fresh keyword detector built from the CURRENT symbol list (which can grow over time).
    def _detector(self) -> RuleBasedExtractor:
        return RuleBasedExtractor(self.resolver.symbol_list())

    # "Preview (free)" button and `poll --dry-run`: fetch and label keep/drop, save NOTHING.
    def preview(self, source) -> dict:
        """Fetch WITHOUT saving: shows which posts the keyword pre-filter would keep
        (and so would cost an LLM call). Used by `poll --dry-run` and the System tab."""
        detector = self._detector()
        items = []
        for p in source.fetch():
            items.append({"author": p.author, "text": p.text[:300], "url": p.url,
                          "posted_at": p.posted_at.isoformat() if p.posted_at else None,
                          "keep": detector.mentions_security(p.text),       # the pre-filter verdict
                          "author_sentiment": (p.meta or {}).get("author_sentiment")})
        return {"source": source.label, "fetched": len(items),
                "kept": sum(i["keep"] for i in items), "items": items}

    # "Ingest" button, `poll` command, and the hourly scheduler: fetch, pre-filter, save + enqueue.
    def poll(self, sources: list | None = None) -> dict:
        """Fetch and ingest. A cheap keyword pre-filter drops posts that mention no known
        security before they cost an LLM call (most forum posts are chatter)."""
        sources = sources if sources is not None else self.configured_sources()
        detector = self._detector()

        # A small function defined inside poll() and handed to poll_sources as `ingest`.
        # It can use `detector` and `self` from the surrounding function (a "closure").
        def ingest_if_relevant(post):
            if not detector.mentions_security(post.text):
                # Dropped: not saved, no job. post_id=0 because there's no row.
                return IngestResult(post_id=0, status="filtered", duplicate=False)
            return self.ingestion.ingest(post)

        return {"sources": poll_sources(sources, ingest_if_relevant)}

    # Build one worker, sharing this runtime's queue, pipeline, DB and limiter.
    def new_worker(self, idx: int = 0) -> ExtractionWorker:
        w = ExtractionWorker(self.queue, self.pipeline, self.sf, limiter=self.limiter,
                             stop_event=self.stop)
        w.worker_id = f"{w.worker_id}-t{idx}"      # suffix "-t0", "-t1" = thread number
        return w

    # ------------------------------------------------- background threads
    # Embedded mode (Render free plan): run n workers as threads inside the web server.
    # daemon=True: these threads don't stop the process from exiting.
    def start_workers(self, n: int) -> None:
        for i in range(n):
            t = threading.Thread(target=self.new_worker(i).run_forever, name=f"worker-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def start_scheduler(self) -> None:
        t = threading.Thread(target=self.scheduler_loop, name="scheduler", daemon=True)
        t.start()
        self.threads.append(t)

    # A tiny home-made "cron". Every 15 seconds, check each task: if its interval has
    # passed since it last ran, run it now.
    #   score   every 6h   (SCORING_INTERVAL_MINUTES)
    #   poll    every 1h   (only does anything if sources are set in .env)
    #   sweep   every 2min (Redis repair loop; does nothing with the Postgres queue)
    #   cleanup daily      (delete old finished jobs)
    def scheduler_loop(self) -> None:
        st = self.settings
        last = {"score": 0.0, "poll": 0.0, "sweep": 0.0, "cleanup": 0.0}   # 0 -> all run at startup
        intervals = {"score": st.scoring_interval_minutes * 60,
                     "poll": st.source_poll_interval_minutes * 60, "sweep": 120, "cleanup": 86400}
        # name -> the function to call. getattr(..., default): the Redis queue has no
        # cleanup(), so we use a do-nothing lambda instead.
        tasks = {"score": self.score, "poll": self.poll,
                 "sweep": self.ingestion.requeue_stale_pending,
                 "cleanup": getattr(self.queue, "cleanup", lambda: 0)}
        log.info("scheduler started: %s", intervals)
        while not self.stop.is_set():
            for name, fn in tasks.items():
                if time.monotonic() - last[name] >= intervals[name]:
                    last[name] = time.monotonic()
                    try:
                        log.info("scheduler: %s -> %s", name, fn())
                    except Exception:  # noqa: BLE001 - keep the scheduler alive
                        log.exception("scheduler task %s failed", name)
            self.stop.wait(15)        # sleep 15 s, but wake immediately on shutdown

    # Graceful shutdown: signal every loop to stop, then wait (up to 10 s each) for them.
    def shutdown(self, timeout: float = 10.0) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout)
