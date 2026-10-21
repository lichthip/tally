"""The Source abstraction that every source implements."""
from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from typing import Iterable

import httpx

from ..schemas import PostIn

# Sites block unknown bots; identify honestly but in a browser-compatible way.
USER_AGENT = "Mozilla/5.0 (compatible; tally-research/0.1; personal project)"
_TAG = re.compile(r"<[^>]+>")


def clean_text(text: str) -> str:
    """Strip HTML tags and entities, collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text or ""))).strip()


class SourceError(ValueError):
    """Invalid source configuration (bad subreddit name, non-https URL...)."""


class Source(ABC):
    """Anything that can return recent posts as PostIn objects.

    Subclasses set `kind` (what type of source) and `target` (which one, e.g. a
    subreddit or symbol) and implement fetch(). Shared plumbing (the HTTP client and
    the post limit) lives here once, instead of being repeated in every subclass.
    """

    kind: str
    target: str

    def __init__(self, limit: int = 25, timeout_s: float = 15.0):
        self.limit = max(1, min(limit, 100))
        self._http = httpx.Client(timeout=timeout_s, follow_redirects=True,
                                  headers={"User-Agent": USER_AGENT})

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.target}"

    @abstractmethod
    def fetch(self) -> Iterable[PostIn]:
        """Return the newest posts, newest first, at most `self.limit`."""
