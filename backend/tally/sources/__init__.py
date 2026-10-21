# =============================================================================
# sources/__init__.py: THE PACKAGE'S "FRONT DOOR"
# -----------------------------------------------------------------------------
# Any folder with an __init__.py is a Python *package*, so other code can write
# `from tally.sources import RedditRSSSource`. Python runs this file the first time
# anything imports from the package.
# Here it re-exports the important names, so callers don't need to know which file
# each class lives in (e.g. runtime.py writes `from .sources import build_source`).
# __all__ lists the public names (what `from tally.sources import *` would give you).
# =============================================================================
"""Sources: everything that brings posts into Tally.

Naming convention: <Platform><Protocol>Source. The protocol says HOW we fetch
(RSS feed vs JSON API); the generic RSSSource has no platform because it works for any feed.

    base.py        Source (abstract)               shared plumbing + the fetch() contract
    rss.py         RSSSource(Source)               any RSS/Atom feed
    reddit.py      RedditRSSSource(RSSSource)      subreddit via RSS (used)
                   RedditAPISource(Source)         subreddit via JSON API (blocked, kept)
    substack.py    SubstackRSSSource(RSSSource)    newsletter by publication name
    stocktwits.py  StockTwitsAPISource(Source)     StockTwits JSON API (blocked by bot protection)
    factory.py     build_source / poll_sources / the SOURCE_TYPES registry

The rest of the system only depends on `Source`, so sources can be added or swapped
without touching ingestion, extraction or scoring. Polling is safe to repeat:
ingestion dedupes on (source, external_id).
"""
from .base import Source, SourceError
from .factory import SOURCE_TYPES, build_source, configured_sources, poll_sources
from .reddit import RedditAPISource, RedditRSSSource
from .rss import RSSSource
from .stocktwits import StockTwitsAPISource
from .substack import SubstackRSSSource

__all__ = ["Source", "SourceError", "RSSSource", "RedditRSSSource", "RedditAPISource", "SubstackRSSSource",
           "StockTwitsAPISource", "SOURCE_TYPES", "build_source", "configured_sources", "poll_sources"]
