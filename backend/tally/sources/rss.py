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
            raise SourceError("feed URL must start with https://")
        super().__init__(limit, timeout_s)
        self.url = url
        self.target = url

    def post_process(self, body: str) -> str:
        return body

    def fetch(self) -> Iterable[PostIn]:
        import feedparser

        # httpx downloads (timeout, redirects, real User-Agent); feedparser only parses.
        resp = self._http.get(self.url)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        feed_title = clean_text(feed.feed.get("title", "")) or self.url
        for e in feed.entries[: self.limit]:
            # Prefer the full body (<content:encoded>) over the summary, which for
            # newsletters is often a one-line subtitle with no tickers in it.
            body = e["content"][0].get("value", "") if e.get("content") else e.get("summary", "")
            body = self.post_process(clean_text(body))
            text = clean_text(f"{e.get('title', '')}. {body}")
            if len(text) < 3:
                continue
            posted = None
            if e.get("published_parsed"):
                posted = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
            author = re.sub(r"^/?u/", "", (e.get("author") or feed_title).strip())
            eid = e.get("id") or e.get("link")
            yield PostIn(text=text[:5000], author=(author or feed_title)[:128],
                         source=self.source_name, external_id=eid[:256] if eid else None,
                         url=e.get("link"), posted_at=posted)
