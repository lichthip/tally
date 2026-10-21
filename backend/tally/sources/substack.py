# =============================================================================
# sources/substack.py: A SUBSTACK NEWSLETTER
# -----------------------------------------------------------------------------
# Every Substack has a feed at https://<name>.substack.com/feed, so this class only
# works out that URL and inherits everything else from RSSSource.
# Posts are stored with source="substack", so each writer becomes a contributor.
# =============================================================================
"""Substack newsletters."""
from __future__ import annotations

import re

from .base import SourceError
from .rss import RSSSource

# Valid Substack subdomain: lowercase letters/digits/hyphens, 2-63 chars, not starting with "-".
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
            # Case 1: a full URL (some Substacks use their own domain, e.g. https://www.example.com).
            # Make sure it ends in /feed, then derive a short name for the label.
            url = publication if publication.rstrip("/").endswith("/feed") else publication.rstrip("/") + "/feed"
            name = re.sub(r"^https://|/feed/?$", "", url)
        else:
            # Case 2: just a name ("stocksunderrocks" or "stocksunderrocks.substack.com").
            name = publication.lower().removesuffix(".substack.com")
            if not _NAME.match(name):
                raise SourceError(f"invalid Substack name: {publication!r}")
            url = f"https://{name}.substack.com/feed"
        super().__init__(url, limit, timeout_s)     # RSSSource checks https and sets up HTTP
        self.publication = name
        self.target = name
