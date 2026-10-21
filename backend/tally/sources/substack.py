"""Substack newsletters."""
from __future__ import annotations

import re

from .base import SourceError
from .rss import RSSSource

_NAME = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}$")


class SubstackRSSSource(RSSSource):
    """A Substack publication. Like Reddit, a Substack IS an RSS feed with a known URL
    pattern (https://<name>.substack.com/feed), so it inherits all parsing from
    RSSSource and only adds the pattern and its own label, so posts are stored as
    source='substack' and each writer becomes a 'substack' contributor.

    Accepts a publication name ("stocksunderrocks") or a full feed URL (for Substacks on
    custom domains).
    """

    kind = "substack"
    source_name = "substack"

    def __init__(self, publication: str, limit: int = 25, timeout_s: float = 15.0):
        publication = publication.strip()
        if publication.startswith("https://"):
            url = publication if publication.rstrip("/").endswith("/feed") else publication.rstrip("/") + "/feed"
            name = re.sub(r"^https://|/feed/?$", "", url)
        else:
            name = publication.lower().removesuffix(".substack.com")
            if not _NAME.match(name):
                raise SourceError(f"invalid Substack name: {publication!r}")
            url = f"https://{name}.substack.com/feed"
        super().__init__(url, limit, timeout_s)
        self.publication = name
        self.target = name
