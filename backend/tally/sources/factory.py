# =============================================================================
# sources/factory.py: CREATE SOURCES BY NAME, AND POLL THEM
# -----------------------------------------------------------------------------
# Factory pattern: callers say WHAT they want ("reddit", "stocks"), and this file
# decides WHICH class to build. The website's Sources card and the CLI never import
# RedditRSSSource directly; they call build_source("reddit", "stocks").
#
# Called by: runtime.py (configured_sources, poll), api/routes.py (build_source via
#            runtime), __main__.py `poll` command.
# =============================================================================
"""Creating sources from a (kind, target) choice, and polling them."""
from __future__ import annotations

import logging

from .base import Source, SourceError
from .reddit import RedditAPISource, RedditRSSSource
from .rss import RSSSource
from .stocktwits import StockTwitsAPISource
from .substack import SubstackRSSSource

# One logger per file, named after the module ("tally.sources.factory"), so log lines
# show where they came from.
log = logging.getLogger(__name__)

# The single registry of available sources. Adding a source = one class + one line here.
# A dict mapping a name -> a CLASS (not an object). `type[Source]` means "a Source subclass".
SOURCE_TYPES: dict[str, type[Source]] = {
    "reddit": RedditRSSSource,
    "substack": SubstackRSSSource,
    "stocktwits": StockTwitsAPISource,
    "rss": RSSSource,
    "reddit_api": RedditAPISource,   # blocked; kept to demonstrate the 403
}


# Look up the class by name and create an instance: build_source("reddit", "stocks")
# is the same as RedditRSSSource("stocks", limit=25). Unknown kind -> SourceError.
def build_source(kind: str, target: str, limit: int = 25) -> Source:
    cls = SOURCE_TYPES.get(kind)
    if cls is None:
        raise SourceError(f"unknown source kind: {kind!r}")
    return cls(target, limit=limit)


# The sources listed in .env (REDDIT_SUBREDDITS=..., etc.), as one list of objects.
# The scheduler polls these every hour. (`+` joins lists together.)
def configured_sources(subreddits: list[str], feeds: list[str], stocktwits: list[str] = (),
                       substacks: list[str] = (), limit: int = 25) -> list[Source]:
    return ([RedditRSSSource(s, limit=limit) for s in subreddits]
            + [SubstackRSSSource(s, limit=limit) for s in substacks]
            + [StockTwitsAPISource(t, limit=limit) for t in stocktwits]
            + [RSSSource(f, limit=limit) for f in feeds])


# Fetch every source, and pass every post to `ingest` (a function given to us by the
# caller; runtime.poll passes "keyword pre-filter, then IngestionService.ingest").
# Returns a report like {"reddit:r/stocks": {"new": 3, "duplicate": 7, "filtered": 15, "errors": 0}}.
def poll_sources(sources: list[Source], ingest) -> dict[str, dict]:
    """Fetch every source and ingest each post. One failing source never blocks the rest."""
    report: dict[str, dict] = {}
    for src in sources:
        stats: dict = {"new": 0, "duplicate": 0, "filtered": 0, "errors": 0}
        try:
            for post in src.fetch():
                # Inner try: one bad POST is counted and skipped; the rest still go in.
                try:
                    res = ingest(post)
                    # Decide which counter to bump (a chained "if/else" expression).
                    key = "filtered" if res.status == "filtered" else "duplicate" if res.duplicate else "new"
                    stats[key] += 1
                except Exception:  # noqa: BLE001
                    stats["errors"] += 1
                    log.exception("ingest failed for %s", src.label)
        # Outer try: a whole SOURCE failing (e.g. Reddit 429) is recorded and we move on
        # to the next source, instead of the entire poll crashing.
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            stats["error"] = f"{type(e).__name__}: {e}"[:300]    # this is the text you saw on the System tab
            log.warning("source %s failed: %s", src.label, e)
        report[src.label] = stats
    return report
