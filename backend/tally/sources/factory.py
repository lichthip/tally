"""Creating sources from a (kind, target) choice, and polling them."""
from __future__ import annotations

import logging

from .base import Source, SourceError
from .reddit import RedditAPISource, RedditRSSSource
from .rss import RSSSource
from .stocktwits import StockTwitsAPISource
from .substack import SubstackRSSSource

log = logging.getLogger(__name__)

# The single registry of available sources. Adding a source = one class + one line here.
SOURCE_TYPES: dict[str, type[Source]] = {
    "reddit": RedditRSSSource,
    "substack": SubstackRSSSource,
    "stocktwits": StockTwitsAPISource,
    "rss": RSSSource,
    "reddit_api": RedditAPISource,   # blocked; kept to demonstrate the 403
}


def build_source(kind: str, target: str, limit: int = 25) -> Source:
    cls = SOURCE_TYPES.get(kind)
    if cls is None:
        raise SourceError(f"unknown source kind: {kind!r}")
    return cls(target, limit=limit)


def configured_sources(subreddits: list[str], feeds: list[str], stocktwits: list[str] = (),
                       substacks: list[str] = (), limit: int = 25) -> list[Source]:
    return ([RedditRSSSource(s, limit=limit) for s in subreddits]
            + [SubstackRSSSource(s, limit=limit) for s in substacks]
            + [StockTwitsAPISource(t, limit=limit) for t in stocktwits]
            + [RSSSource(f, limit=limit) for f in feeds])


def poll_sources(sources: list[Source], ingest) -> dict[str, dict]:
    """Fetch every source and ingest each post. One failing source never blocks the rest."""
    report: dict[str, dict] = {}
    for src in sources:
        stats: dict = {"new": 0, "duplicate": 0, "filtered": 0, "errors": 0}
        try:
            for post in src.fetch():
                try:
                    res = ingest(post)
                    key = "filtered" if res.status == "filtered" else "duplicate" if res.duplicate else "new"
                    stats[key] += 1
                except Exception:  # noqa: BLE001
                    stats["errors"] += 1
                    log.exception("ingest failed for %s", src.label)
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            stats["error"] = f"{type(e).__name__}: {e}"[:300]
            log.warning("source %s failed: %s", src.label, e)
        report[src.label] = stats
    return report
