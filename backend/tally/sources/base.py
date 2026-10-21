# =============================================================================
# sources/base.py: THE "CONTRACT" EVERY SOURCE MUST FOLLOW
# -----------------------------------------------------------------------------
# OOP idea used here: an ABSTRACT BASE CLASS (ABC).
#   Source says "every source has a kind, a target, and a fetch() method that returns
#   PostIn objects". It doesn't say HOW to fetch; each subclass decides that.
#   You can't create a plain Source(); only the concrete subclasses.
#
# Family tree:
#   Source (abstract, this file)
#     |-- RSSSource              (rss.py)         any RSS feed
#     |     |-- RedditRSSSource  (reddit.py)      a subreddit's RSS feed
#     |     `-- SubstackRSSSource(substack.py)    a newsletter's RSS feed
#     |-- RedditAPISource        (reddit.py)      Reddit JSON API (blocked)
#     `-- StockTwitsAPISource    (stocktwits.py)  StockTwits JSON API (blocked)
#
# Why: the rest of Tally only ever calls `source.fetch()`. It never needs to know
# which site the posts came from (polymorphism). Adding a site = one new subclass.
# =============================================================================
"""The Source abstraction that every source implements."""
from __future__ import annotations

import html                          # html.unescape turns "&amp;" into "&"
import re                            # regular expressions (text pattern matching)
from abc import ABC, abstractmethod
from typing import Iterable

import httpx                         # the HTTP client library (downloads web pages)

from ..schemas import PostIn         # ".." = go up one package (tally/), then schemas.py

# Sites block unknown bots; identify honestly but in a browser-compatible way.
# (We say what we are, "tally-research ... personal project"; we don't pretend to be Chrome.)
USER_AGENT = "Mozilla/5.0 (compatible; tally-research/0.1; personal project)"
# Regex matching any HTML tag like <p> or <a href="...">. Compiled once, reused.
_TAG = re.compile(r"<[^>]+>")


# Turn messy HTML into plain text:  "<p>Long&nbsp;$NVDA</p>\n\n"  ->  "Long $NVDA"
# Steps, inside out: replace tags with spaces -> decode HTML entities -> squash runs
# of whitespace into one space -> trim the ends. `text or ""` guards against None.
def clean_text(text: str) -> str:
    """Strip HTML tags and entities, collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text or ""))).strip()


# A custom error type, so callers can catch "bad source config" specifically
# (the API turns it into a 400 Bad Request instead of a server crash).
class SourceError(ValueError):
    """Invalid source configuration (bad subreddit name, non-https URL...)."""


class Source(ABC):
    """Anything that can return recent posts as PostIn objects.

    Subclasses set `kind` (what type of source) and `target` (which one, e.g. a
    subreddit or symbol) and implement fetch(). Shared plumbing (the HTTP client and
    the post limit) lives here once, instead of being repeated in every subclass.
    """

    # Declared here (type only); each subclass fills them in.
    kind: str        # e.g. "reddit"
    target: str      # e.g. "r/stocks"

    # Constructor shared by every subclass (they call super().__init__(...)).
    def __init__(self, limit: int = 25, timeout_s: float = 15.0):
        self.limit = max(1, min(limit, 100))    # clamp to 1..100 posts per fetch
        # One reusable HTTP client: gives up after timeout_s, follows redirects, and
        # sends our User-Agent header on every request.
        self._http = httpx.Client(timeout=timeout_s, follow_redirects=True,
                                  headers={"User-Agent": USER_AGENT})

    # A readable name like "reddit:r/stocks", used in reports and on the System tab.
    @property
    def label(self) -> str:
        return f"{self.kind}:{self.target}"

    # @abstractmethod: every subclass MUST write its own fetch(), or Python refuses to
    # create it. "Iterable[PostIn]" = something you can loop over that yields PostIns
    # (our subclasses use `yield`, which makes fetch() a generator: it hands out one
    # post at a time instead of building a whole list first).
    @abstractmethod
    def fetch(self) -> Iterable[PostIn]:
        """Return the newest posts, newest first, at most `self.limit`."""
