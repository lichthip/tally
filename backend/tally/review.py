"""Human-in-the-loop review of low-confidence / unresolved extractions.

Anti-cheating rule: an approved idea's entry price uses the APPROVAL time
(effective_at = now), not the original receipt time. Otherwise a reviewer could wait a
week, see which ideas went up, and approve only those, which would be hindsight bias.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db import session_scope
from .extraction.symbols import SymbolResolver
from .extraction.types import IdeaCandidate
from .models import RawPost
from .worker import insert_ideas


class ReviewError(ValueError):
    pass


@dataclass
class Override:
    ticker: str
    direction: str
    horizon_days: int | None = None


def approve(sf: sessionmaker[Session], resolver: SymbolResolver, post_id: int,
            overrides: list[Override] | None = None, reviewer: str = "admin") -> int:
    now = dt.datetime.now(dt.timezone.utc)
    with session_scope(sf) as s:
        post = s.execute(select(RawPost).where(RawPost.id == post_id).with_for_update()).scalar_one_or_none()
        if post is None:
            raise ReviewError("post not found")
        if post.status != "needs_review":
            raise ReviewError(f"post is {post.status}, not needs_review")
        extraction = post.extraction or {}
        if overrides:
            candidates = []
            for o in overrides:
                ticker, how = resolver.resolve(o.ticker, None)
                if ticker is None:
                    raise ReviewError(f"cannot approve: {how}")
                candidates.append(IdeaCandidate(
                    ticker=ticker, direction=o.direction.upper(),
                    horizon_days=o.horizon_days or 20, horizon_inferred=o.horizon_days is None,
                    target_price=None, conviction="medium", confidence=1.0,
                    rationale=f"set by reviewer {reviewer}"))
        else:
            candidates = [IdeaCandidate(**c) for c in extraction.get("candidates", [])]
            if not candidates or any(c.ticker is None for c in candidates):
                raise ReviewError("candidates incomplete; supply overrides with explicit tickers")
        n = insert_ideas(s, post, candidates, extractor=f"{post.extractor or 'unknown'}+review:{reviewer}",
                         prompt_version=extraction.get("prompt_version"), effective_at=now)
        post.status = "extracted"
        post.extraction = {**extraction, "reviewed_by": reviewer, "reviewed_at": now.isoformat()}
        return n


def reject(sf: sessionmaker[Session], post_id: int, reviewer: str = "admin") -> None:
    with session_scope(sf) as s:
        post = s.execute(select(RawPost).where(RawPost.id == post_id).with_for_update()).scalar_one_or_none()
        if post is None:
            raise ReviewError("post not found")
        if post.status != "needs_review":
            raise ReviewError(f"post is {post.status}, not needs_review")
        post.status = "rejected"
        post.extraction = {**(post.extraction or {}), "reviewed_by": reviewer,
                           "reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
