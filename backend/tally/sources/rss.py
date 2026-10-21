# =============================================================================
# sources/rss.py: READ ANY RSS FEED
# -----------------------------------------------------------------------------
# RSS = a standard XML format websites publish so programs can read their latest
# posts (like a machine-readable "what's new" list). Reddit and Substack both offer one.
#
# RSSSource does the generic work: download the feed, parse it, and turn each entry
# into a PostIn. RedditRSSSource and SubstackRSSSource INHERIT from it and only change
# the URL (and, for Reddit, strip a footer). That's inheritance used for code reuse.
#
# Called by: factory.build_source / configured_sources -> runtime.poll()/preview().
# =============================================================================
"""Generic RSS/Atom feeds: blogs, newsletters, anything that publishes a feed."""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterable

from ..schemas import PostIn
from .base import Source, SourceError, clean_text


class RSSSource(Source):
    """Any RSS/Atom feed. Author = the entry's author, else the feed's title.

    Subclasses (RedditRSSSource, SubstackRSSSource) reuse all of this parsing and only add
    their URL pattern, validation and label. Override `post_process` to clean
    source-specific boilerplate.
    """

    kind = "rss"
    source_name = "rss"     # stored in raw_posts.source

    def __init__(self, url: str, limit: int = 25, timeout_s: float = 15.0):
        if not url.startswith("https://"):
            # The server fetches this URL, so arbitrary URLs would be an SSRF risk
            # (e.g. probing internal addresses). https-only + admin-only keeps it to public feeds.
            # (SSRF = Server-Side Request Forgery: tricking OUR server into requesting a
            # URL the attacker can't reach directly, like an internal admin page.)
            raise SourceError("feed URL must start with https://")
        super().__init__(limit, timeout_s)     # run Source.__init__ (HTTP client, limit)
        self.url = url
        self.target = url

    # A "hook": does nothing here, but subclasses can override it to clean the body.
    # RedditRSSSource overrides it to remove "submitted by /u/... [link] [comments]".
    # (This is the Template Method pattern: the parent runs the steps, children tweak one.)
    def post_process(self, body: str) -> str:
        return body

    def fetch(self) -> Iterable[PostIn]:
        import feedparser     # imported here, not at the top, so the app starts even if it's missing

        # httpx downloads (timeout, redirects, real User-Agent); feedparser only parses.
        resp = self._http.get(self.url)
        resp.raise_for_status()            # 403/404/429/500 -> raise an error instead of parsing junk
        feed = feedparser.parse(resp.content)
        feed_title = clean_text(feed.feed.get("title", "")) or self.url
        for e in feed.entries[: self.limit]:     # [: self.limit] = only the first N entries
            # Prefer the full body (<content:encoded>) over the summary, which for
            # newsletters is often a one-line subtitle with no tickers in it.
            body = e["content"][0].get("value", "") if e.get("content") else e.get("summary", "")
            body = self.post_process(clean_text(body))
            text = clean_text(f"{e.get('title', '')}. {body}")   # title + body as one text
            if len(text) < 3:
                continue                  # empty entry: skip it
            posted = None
            if e.get("published_parsed"):
                # published_parsed is a time tuple (year, month, day, h, m, s, ...).
                # *tuple[:6] "unpacks" the first 6 numbers as arguments to datetime(...).
                posted = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
            # Author: the entry's author, else the feed's name. Strip a leading "/u/".
            author = re.sub(r"^/?u/", "", (e.get("author") or feed_title).strip())
            eid = e.get("id") or e.get("link")     # the entry's unique id (for dedupe)
            # `yield` hands this one post back to the caller and pauses here until the
            # caller asks for the next one. Lengths are trimmed to the PostIn limits.
            yield PostIn(text=text[:5000], author=(author or feed_title)[:128],
                         source=self.source_name, external_id=eid[:256] if eid else None,
                         url=e.get("link"), posted_at=posted)
