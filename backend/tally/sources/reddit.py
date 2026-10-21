"""Reddit: two implementations of the same source, and why only one is used.

RedditRSSSource      a subreddit via Reddit's public RSS feed (the one we use)
RedditAPISource   a subreddit via Reddit's JSON API. Kept as a record: since Oct 2026
                  Reddit answers anonymous API calls with "403 Blocked" and new OAuth
                  apps need manual approval. Swapping one for the other touched nothing
                  outside this file, which is the point of the Source abstraction.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterable

from ..schemas import PostIn
from .base import Source, SourceError, clean_text
from .rss import RSSSource

SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
# Reddit's feed appends "submitted by /u/name [link] [comments]" to every post body.
_FOOTER = re.compile(r"\s*submitted by\s+/u/\S+\s*\[link\]\s*\[comments\]\s*$")


def _subreddit(name: str) -> str:
    name = re.sub(r"^/?r/", "", name.strip())
    if not SUBREDDIT_RE.match(name):
        raise SourceError(f"invalid subreddit name: {name!r}")
    return name


class RedditRSSSource(RSSSource):
    """A subreddit's posts via RSS. A Reddit listing IS an RSS feed with a known URL
    pattern, so this inherits all parsing from RSSSource. Entry ids are Reddit's own
    post ids (t3_...), so each post is deduplicated however often it's polled."""

    kind = "reddit"
    source_name = "reddit"

    def __init__(self, subreddit: str, limit: int = 25, sort: str = "new", timeout_s: float = 15.0):
        if sort not in ("new", "hot", "top"):
            raise SourceError("sort must be new, hot or top")
        self.subreddit = _subreddit(subreddit)
        super().__init__(f"https://www.reddit.com/r/{self.subreddit}/{sort}/.rss", limit, timeout_s)
        self.target = f"r/{self.subreddit}"

    def post_process(self, body: str) -> str:
        return _FOOTER.sub("", body)


class RedditAPISource(Source):
    """Reddit's JSON API (/r/<sub>/new.json). Blocked for anonymous clients since
    Oct 2026; run `python -m tally poll --reddit-api stocks --dry-run` to see the 403."""

    kind = "reddit_api"

    def __init__(self, subreddit: str, limit: int = 25, timeout_s: float = 15.0):
        super().__init__(limit, timeout_s)
        self.subreddit = _subreddit(subreddit)
        self.target = f"r/{self.subreddit}"

    def fetch(self) -> Iterable[PostIn]:
        resp = self._http.get(f"https://www.reddit.com/r/{self.subreddit}/new.json",
                              params={"limit": self.limit})
        resp.raise_for_status()
        for child in resp.json().get("data", {}).get("children", []):
            d = child.get("data", {})
            text = clean_text(f"{d.get('title', '')}\n\n{d.get('selftext', '')}")
            author = d.get("author") or "[deleted]"
            if len(text) < 3 or author in ("[deleted]", "AutoModerator"):
                continue
            yield PostIn(text=text[:5000], author=author, source="reddit",
                         external_id=d.get("name") or d.get("id"),
                         url=f"https://www.reddit.com{d.get('permalink', '')}",
                         posted_at=dt.datetime.fromtimestamp(d.get("created_utc", 0), dt.timezone.utc))
