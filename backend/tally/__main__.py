# =============================================================================
# __main__.py: THE COMMAND LINE (python -m tally <command>)
# -----------------------------------------------------------------------------
# `python -m tally` runs this file (a package's __main__.py is its "run me" file).
# argparse turns the words you type into an `args` object:
#     python -m tally poll --substack stocksunderrocks --dry-run
#     -> args.cmd == "poll", args.substack == "stocksunderrocks", args.dry_run == True
# Then a chain of `if args.cmd == ...` blocks runs the matching code.
#
# Most commands build a Runtime (the same object the web server uses), so the CLI and
# the API share all the logic and only the "front door" differs.
# =============================================================================
"""Command line entry point:  python -m tally <command>

  serve        run the API (uvicorn)
  worker       run extraction workers (separate process)
  scheduler    run the scoring / polling / sweeper loops (separate process)
  init-db      create tables, triggers and the symbol master
  reset-db     DROP everything (dev only)
  submit       ingest one post from the command line
  score        run one scoring pass now
  poll         poll configured sources once
  simulate     create simulated contributors with known skill, score, and report
  eval         run the extraction eval set against the configured extractor
  bench        benchmark the Postgres vs Redis queues (throughput, latency, crashes)
  reprocess    re-run extraction for given post ids (e.g. after a prompt/pipeline change)
  check-llm    verify the LLM key: list available models and run one test extraction
"""
from __future__ import annotations

import argparse
import json
import logging
import signal        # lets us react to Ctrl+C (SIGINT) and to "please stop" from the OS (SIGTERM)
import sys


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # silence a line per HTTP request


# Pretty-print any result as indented JSON in the terminal.
def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    # ---- 1. Define the commands and their options ---------------------------
    p = argparse.ArgumentParser(prog="tally", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")      # store_true: a flag, True if present
    sub = p.add_subparsers(dest="cmd", required=True)           # the command word goes into args.cmd

    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--reload", action="store_true")            # auto-restart when code changes (dev)

    sp = sub.add_parser("worker")
    sp.add_argument("--concurrency", type=int, default=None)

    sub.add_parser("scheduler")
    sub.add_parser("init-db")
    sp = sub.add_parser("reset-db")
    sp.add_argument("--yes", action="store_true")               # safety: must confirm explicitly

    sp = sub.add_parser("submit")
    sp.add_argument("text")                                     # positional: the post text itself
    sp.add_argument("--author", default="cli_user")

    sub.add_parser("score")
    sp = sub.add_parser("poll")
    sp.add_argument("--subreddit", help="poll just this subreddit (overrides REDDIT_SUBREDDITS)")
    sp.add_argument("--rss", help="poll just this RSS/Atom feed URL, e.g. a Substack /feed")
    sp.add_argument("--substack", help="poll just this Substack publication, e.g. stocksunderrocks")
    sp.add_argument("--stocktwits", help="poll just this StockTwits stream: a symbol (NVDA) or 'trending'")
    sp.add_argument("--reddit-api", help="subreddit via Reddit's JSON API (blocked since Oct 2026; shows the 403)")
    sp.add_argument("--limit", type=int, default=25, help="max posts to fetch per source")
    sp.add_argument("--dry-run", action="store_true",
                    help="fetch and show what would be ingested; no database writes, no LLM calls")

    sp = sub.add_parser("simulate")
    sp.add_argument("--report-only", action="store_true")

    sp = sub.add_parser("eval")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--out", default=None, help="write markdown report here")

    sub.add_parser("check-llm")

    sp = sub.add_parser("reprocess")
    sp.add_argument("post_ids", type=int, nargs="+")            # nargs="+": one or more numbers

    sp = sub.add_parser("bench")
    sp.add_argument("--backend", choices=["postgres", "redis", "both"], default="both")
    sp.add_argument("--jobs", type=int, default=2000)
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--work-ms", type=float, default=2.0)
    sp.add_argument("--crash-rate", type=float, default=0.0)
    sp.add_argument("--lease", type=int, default=2)
    sp.add_argument("--out", default=None)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)

    # Imported here (not at the top) so `python -m tally --help` stays fast and works
    # even without a database.
    from .config import get_settings
    from .db import init_db

    st = get_settings()

    # ---- 2. Commands that DON'T need the Runtime ----------------------------
    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("tally.api.main:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.cmd == "init-db":
        init_db()
        print("database initialised")
        return 0

    if args.cmd == "reset-db":
        if not args.yes:
            print("refusing without --yes (this drops every Tally table)")
            return 1
        from sqlalchemy import text

        from .db import get_engine
        from .models import Base
        eng = get_engine()
        with eng.begin() as c:
            # The trigger must go first, or dropping trade_ideas would be blocked... by our own rule.
            c.execute(text("DROP TRIGGER IF EXISTS trade_ideas_append_only ON trade_ideas"))
        Base.metadata.drop_all(eng)       # drop every table
        init_db()                         # recreate them empty
        print("database reset")
        return 0

    if args.cmd == "check-llm":
        return _check_llm(st)

    if args.cmd == "bench":
        init_db()
        from .tools.bench import run_bench_cli
        return run_bench_cli(args)

    # ---- 3. Everything else needs the full Runtime --------------------------
    init_db()
    from .runtime import Runtime
    rt = Runtime(st)

    if args.cmd == "worker":
        n = args.concurrency or st.worker_concurrency
        rt.start_workers(n)
        # On Ctrl+C or a stop signal: set the stop flag (lambda *_ = ignore the arguments).
        signal.signal(signal.SIGINT, lambda *_: rt.stop.set())
        signal.signal(signal.SIGTERM, lambda *_: rt.stop.set())
        print(f"{n} worker thread(s) running on {rt.queue.backend} queue with {rt.extractor.id}. Ctrl+C to stop.")
        # Keep the main thread alive (checking once a second) until stop is set.
        while not rt.stop.wait(1):
            pass
        rt.shutdown()
        return 0

    if args.cmd == "scheduler":
        signal.signal(signal.SIGINT, lambda *_: rt.stop.set())
        signal.signal(signal.SIGTERM, lambda *_: rt.stop.set())
        rt.scheduler_loop()               # runs in this thread until stopped
        return 0

    if args.cmd == "submit":
        from .schemas import PostIn
        _print(rt.ingestion.ingest(PostIn(text=args.text, author=args.author)).model_dump())
        return 0

    if args.cmd == "score":
        _print(rt.score())
        return 0

    if args.cmd == "poll":
        from .sources import build_source
        # No flag -> sources stays None -> rt.poll uses the .env sources.
        sources = None
        if args.subreddit:
            sources = [build_source("reddit", args.subreddit, args.limit)]
        elif args.substack:
            sources = [build_source("substack", args.substack, args.limit)]
        elif args.stocktwits:
            sources = [build_source("stocktwits", args.stocktwits, args.limit)]
        elif args.reddit_api:
            sources = [build_source("reddit_api", args.reddit_api, args.limit)]
        elif args.rss:
            sources = [build_source("rss", args.rss, args.limit)]
        if args.dry_run:
            return _poll_dry_run(rt, sources)
        _print(rt.poll(sources))
        return 0

    if args.cmd == "reprocess":
        return _reprocess(rt, args.post_ids)

    if args.cmd == "simulate":
        from .tools.simulate import simulate, simulation_report
        if not args.report_only:
            _print(simulate(rt.sf, rt.store))
            _print(rt.score())
        _print(simulation_report(rt.sf))
        return 0

    if args.cmd == "eval":
        from .tools.evaluate import run_eval_cli
        return run_eval_cli(rt, args)

    return 1


def _reprocess(rt, post_ids: list[int]) -> int:
    """Put posts that produced no ideas back through extraction (see IngestionService.requeue)."""
    results = rt.ingestion.requeue(post_ids)
    for pid, outcome in results.items():
        print(f"post {pid}: {outcome}")
    if any(v == "re-queued" for v in results.values()):
        print("run `python -m tally worker` (or have `serve` running) to process them")
    return 0


def _poll_dry_run(rt, sources) -> int:
    """Show what polling WOULD do. Nothing is written, no LLM is called."""
    sources = sources or rt.configured_sources()
    if not sources:
        print("no sources: pass --subreddit / --stocktwits / --rss, or set them in .env")
        return 1
    for src in sources:
        print(f"\n=== {src.label}")
        try:
            r = rt.preview(src)
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed: {type(e).__name__}: {e}")
            continue
        for i in r["items"]:
            tag = f" ({i['author_sentiment']})" if i["author_sentiment"] else ""
            # {i['author']:<20} pads the name to 20 characters so the columns line up.
            print(f"  [{'KEEP' if i['keep'] else 'drop'}] {i['author']:<20}{tag} {i['text'][:80]!r}")
        print(f"  -> {r['fetched']} fetched, {r['kept']} would be ingested (= up to {r['kept']} LLM calls)")
    return 0


# Checks the LLM setup: is the key valid, is the model available, does a test call work?
def _check_llm(st) -> int:
    """No database needed: talks to the provider only."""
    import datetime as dt

    from .extraction.extractors import LLMIdeaExtractor, build_extractor
    from .extraction.llm_clients import LLMError

    if st.llm_provider == "rules":
        print("LLM_PROVIDER=rules: no LLM configured (set it to gemini, groq or openrouter in .env)")
        return 1
    try:
        ex = build_extractor(st, None)        # None = no DB, so no cache
    except LLMError as e:
        print(f"configuration error: {e}")
        return 1
    assert isinstance(ex, LLMIdeaExtractor)
    print(f"provider: {ex.client.provider}   configured model: {ex.client.model}")
    try:
        models = ex.client.list_models()
        print(f"models available to this key ({len(models)}):")
        for m in models:
            print(f"  {'*' if m == ex.client.model else ' '} {m}")    # * marks the configured one
        if models and ex.client.model not in models:
            print(f"\n!! '{ex.client.model}' is not in that list: set the model variable in .env to one of the above")
    except LLMError as e:
        print(f"could not list models: {e}")
    print("\ntest extraction: 'Grabbed some $TSLA puts, deliveries will disappoint'")
    try:
        out = ex.extract("Grabbed some $TSLA puts, deliveries will disappoint",
                         dt.datetime.now(dt.timezone.utc))
        _print(out)
        print("OK: the LLM is reachable and returning JSON")
        return 0
    except LLMError as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return 1


# True only when this file is run directly (python -m tally), not when it's imported.
# sys.exit(code): 0 = success, anything else = error (scripts and CI read this).
if __name__ == "__main__":
    sys.exit(main())
