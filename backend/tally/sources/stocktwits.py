"""StockTwits: a social network built for stock ideas."""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterable

from ..schemas import PostIn
from .base import Source, SourceError, clean_text

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
    BASE = "https://api.stocktwits.com/api/2/streams"

    def __init__(self, target: str = "trending", limit: int = 25, timeout_s: float = 15.0):
        target = target.strip().lstrip("$").upper()
        if target != "TRENDING" and not SYMBOL_RE.match(target):
            raise SourceError(f"invalid StockTwits symbol: {target!r}")
        super().__init__(limit, timeout_s)
        self.target = "trending" if target == "TRENDING" else target

    @property
    def url(self) -> str:
        if self.target == "trending":
            return f"{self.BASE}/trending.json"
        return f"{self.BASE}/symbol/{self.target}.json"

    def fetch(self) -> Iterable[PostIn]:
        resp = self._http.get(self.url)
        resp.raise_for_status()
        for m in resp.json().get("messages", [])[: self.limit]:
            body = clean_text(m.get("body", ""))
            user = (m.get("user") or {}).get("username")
            if len(body) < 3 or not user:
                continue
            posted = None
            if m.get("created_at"):
                posted = dt.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
            sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            yield PostIn(text=body[:5000], author=user[:128], source="stocktwits",
                         external_id=str(m["id"]), url=f"https://stocktwits.com/{user}/message/{m['id']}",
                         posted_at=posted, meta={"author_sentiment": sentiment} if sentiment else None)
