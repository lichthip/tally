# =============================================================================
# sources/stocktwits.py: STOCKTWITS (JSON API; currently blocked with 403)
# -----------------------------------------------------------------------------
# StockTwits is a Twitter-like site just for stocks. Its API returns JSON, not RSS,
# so this class implements Source directly. Its bot protection answers our plain
# script with 403; we deliberately don't disguise the client to get past it.
# Interesting extra: authors can tag a message "Bullish"/"Bearish" themselves. We keep
# that tag in `meta` but never show it to the LLM, so it could later be used to check
# the LLM's direction independently.
# =============================================================================
"""StockTwits: a social network built for stock ideas."""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterable

from ..schemas import PostIn
from .base import Source, SourceError, clean_text

# Valid symbol: 1-12 uppercase letters, digits, dots or hyphens (e.g. "NVDA", "BRK.B").
SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")


class StockTwitsAPISource(Source):
    """Public JSON streams: one symbol ($NVDA) or 'trending'. A different protocol
    from RSS, so it implements Source directly rather than extending RSSSource.

    Authors can tag their own message Bullish/Bearish. That self-label is kept in
    PostIn.meta (shown in previews) and NOT passed to the LLM, so it can later serve as
    an independent check on the LLM's direction.

    Status (Oct 2026): the endpoint serves JSON to some clients but answers 403 to plain
    scripts from a home connection; its bot protection decides who gets through. We don't
    disguise the client to get around that; access would come from StockTwits' official
    API. The adapter stays because the code path is complete and tested.
    """

    kind = "stocktwits"
    BASE = "https://api.stocktwits.com/api/2/streams"    # class constant shared by all instances

    def __init__(self, target: str = "trending", limit: int = 25, timeout_s: float = 15.0):
        target = target.strip().lstrip("$").upper()      # "$nvda " -> "NVDA"
        if target != "TRENDING" and not SYMBOL_RE.match(target):
            raise SourceError(f"invalid StockTwits symbol: {target!r}")
        super().__init__(limit, timeout_s)
        self.target = "trending" if target == "TRENDING" else target

    # Which URL to call depends on the target: the trending stream or one symbol's stream.
    @property
    def url(self) -> str:
        if self.target == "trending":
            return f"{self.BASE}/trending.json"
        return f"{self.BASE}/symbol/{self.target}.json"

    def fetch(self) -> Iterable[PostIn]:
        resp = self._http.get(self.url)
        resp.raise_for_status()                          # this is where the 403 surfaces
        for m in resp.json().get("messages", [])[: self.limit]:
            body = clean_text(m.get("body", ""))
            # `(m.get("user") or {})`: if "user" is missing or None, use an empty dict
            # so .get("username") doesn't crash.
            user = (m.get("user") or {}).get("username")
            if len(body) < 3 or not user:
                continue
            posted = None
            if m.get("created_at"):
                # "2026-10-01T12:00:00Z": Python's parser wants "+00:00" instead of "Z".
                posted = dt.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
            # Dig out entities.sentiment.basic ("Bullish"/"Bearish") safely, step by step.
            sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            yield PostIn(text=body[:5000], author=user[:128], source="stocktwits",
                         external_id=str(m["id"]), url=f"https://stocktwits.com/{user}/message/{m['id']}",
                         posted_at=posted, meta={"author_sentiment": sentiment} if sentiment else None)
