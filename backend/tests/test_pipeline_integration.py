"""Ingestion -> queue -> worker -> ideas -> scoring, against real Postgres."""
import datetime as dt

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from tally import review
from tally.extraction.extractors import RuleBasedExtractor
from tally.extraction.llm_clients import LLMTransientError
from tally.extraction.pipeline import ExtractionPipeline
from tally.extraction.symbols import SymbolResolver
from tally.ingestion.service import IngestionService
from tally.market.providers import SyntheticProvider
from tally.market.store import PriceStore
from tally.models import ContributorStat, IdeaMark, Job, RawPost, SignalSnapshot, TradeIdea
from tally.queue import PostgresQueue, RedisStreamQueue
from tally.schemas import PostIn
from tally.scoring.job import SCORING_LOCK_KEY, run_scoring
from tally.worker import ExtractionWorker


@pytest.fixture
def parts(sf):
    q = PostgresQueue(sf, queue="extract", lease_seconds=30, max_attempts=2, poll_interval_s=0.05)
    q.backoff_seconds = lambda a: 0
    resolver = SymbolResolver(sf)
    pipeline = ExtractionPipeline(RuleBasedExtractor(resolver.symbol_list()), resolver)
    return {"q": q, "ing": IngestionService(sf, q), "pipeline": pipeline, "resolver": resolver,
            "worker": ExtractionWorker(q, pipeline, sf, worker_id="test-worker")}


def count(sf, model):
    with sf() as s:
        return s.execute(select(func.count()).select_from(model)).scalar()


def test_duplicate_posts_are_deduplicated(sf, parts):
    p = PostIn(text="Long $NVDA", author="alice")
    a, b = parts["ing"].ingest(p), parts["ing"].ingest(p)
    assert not a.duplicate and b.duplicate and a.post_id == b.post_id
    assert count(sf, RawPost) == 1 and count(sf, Job) == 1


def test_idempotency_key_dedupes_client_retries(sf, parts):
    a = parts["ing"].ingest(PostIn(text="Long $NVDA", author="alice", external_id="req-1"))
    b = parts["ing"].ingest(PostIn(text="Long $NVDA (edited)", author="alice", external_id="req-1"))
    assert b.duplicate and a.post_id == b.post_id


def test_end_to_end_post_becomes_idea(sf, parts):
    res = parts["ing"].ingest(PostIn(text="Buying $TSLA puts, deliveries will miss", author="bob"))
    assert parts["worker"].run_once(block_ms=500) == 1
    with sf() as s:
        post = s.get(RawPost, res.post_id)
        ideas = s.execute(select(TradeIdea)).scalars().all()
    assert post.status == "extracted"
    assert [(i.ticker, i.direction) for i in ideas] == [("TSLA", "SHORT")]
    assert ideas[0].effective_at == post.received_at       # point-in-time: our clock
    assert parts["q"].stats().done == 1


def test_duplicate_job_does_not_duplicate_ideas(sf, parts):
    res = parts["ing"].ingest(PostIn(text="Long $AAPL", author="carol"))
    parts["q"].enqueue({"post_id": res.post_id})            # at-least-once: same job twice
    assert parts["worker"].run_once(block_ms=500) == 1
    assert parts["worker"].run_once(block_ms=500) == 1
    assert count(sf, TradeIdea) == 1


def test_trade_ideas_are_append_only(sf, parts):
    parts["ing"].ingest(PostIn(text="Long $AAPL", author="dave"))
    parts["worker"].run_once(block_ms=500)
    with pytest.raises(DBAPIError, match="append-only"):
        with sf.begin() as s:
            s.execute(text("UPDATE trade_ideas SET direction='SHORT'"))
    with pytest.raises(DBAPIError, match="append-only"):
        with sf.begin() as s:
            s.execute(text("DELETE FROM trade_ideas"))


class ExplodingPipeline:
    def run(self, text, when):
        raise LLMTransientError("429 from provider")


def test_transient_failures_retry_then_mark_post_failed(sf, parts):
    res = parts["ing"].ingest(PostIn(text="Long $AAPL", author="erin"))
    w = ExtractionWorker(parts["q"], ExplodingPipeline(), sf, worker_id="boom")
    w.run_once(block_ms=500)      # attempt 1 -> retry
    w.run_once(block_ms=500)      # attempt 2 = max -> dead letter
    with sf() as s:
        post = s.get(RawPost, res.post_id)
    assert post.status == "failed" and "429" in post.error
    assert parts["q"].stats().dead == 1


class FlakyRedisQueue(RedisStreamQueue):
    """Enqueue fails once: simulates a crash between the DB commit and XADD."""
    failed_once = False

    def enqueue(self, payload, *, session=None):
        if not self.failed_once:
            self.failed_once = True
            raise ConnectionError("redis down")
        return super().enqueue(payload, session=session)


def test_redis_dual_write_gap_is_repaired_by_sweeper(sf, redis_client):
    q = FlakyRedisQueue(redis_client, queue="sweep", lease_seconds=30)
    q.purge()
    ing = IngestionService(sf, q)
    res = ing.ingest(PostIn(text="Long $AAPL", author="frank"))
    assert res.job_id is None                                # post saved, job lost
    with sf.begin() as s:                                    # pretend 2 minutes passed
        s.execute(text("UPDATE raw_posts SET received_at = now() - interval '2 minutes'"))
    assert ing.requeue_stale_pending() == 1
    [m] = q.claim("w", block_ms=500)
    assert m.payload == {"post_id": res.post_id}


def test_review_approval_uses_approval_time(sf, parts):
    res = parts["ing"].ingest(PostIn(text="hmm $ZZZZ is going up, buying", author="gina"))
    with sf.begin() as s:   # force the post into review with an unresolved candidate
        s.execute(text("UPDATE raw_posts SET status='needs_review', received_at=now()-interval '5 days' "
                       "WHERE id=:i"), {"i": res.post_id})
    with pytest.raises(review.ReviewError):
        review.approve(sf, parts["resolver"], res.post_id)   # no candidates: must give overrides
    n = review.approve(sf, parts["resolver"], res.post_id,
                       [review.Override(ticker="AAPL", direction="LONG")])
    with sf() as s:
        idea = s.execute(select(TradeIdea)).scalar_one()
    assert n == 1 and idea.received_at < idea.effective_at   # entry priced at approval, not receipt
    assert (dt.datetime.now(dt.timezone.utc) - idea.effective_at).total_seconds() < 60


def test_scoring_job_marks_ideas_and_scores_contributors(sf, parts):
    for author, text_ in [("hank", "Long $AAPL"), ("hank", "Short $TSLA"), ("ivy", "Long $NVDA")]:
        r = parts["ing"].ingest(PostIn(text=text_, author=author))
        with sf.begin() as s:
            s.execute(text("UPDATE raw_posts SET received_at = now() - interval '200 days' WHERE id=:i"),
                      {"i": r.post_id})
    while parts["worker"].run_once(block_ms=200):
        pass
    store = PriceStore(sf, SyntheticProvider())
    summary = run_scoring(sf, store)
    assert summary["ideas_marked"] == 3
    with sf() as s:
        marks = s.execute(select(IdeaMark)).scalars().all()
        stats = s.execute(select(ContributorStat)).scalars().all()
        snap = s.execute(select(SignalSnapshot)).scalar_one()
    assert {m.status for m in marks} == {"closed"}           # 20-day horizon, 200 days ago
    assert all(m.excess_return is not None for m in marks)
    assert len(stats) == 2 and snap.payload["model"]["n_closed_ideas"] == 3
    # Re-running is idempotent: closed marks are final, nothing duplicated.
    assert run_scoring(sf, store)["ideas_marked"] == 0


def test_scoring_is_single_flight(sf, engine):
    store = PriceStore(sf, SyntheticProvider())
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": SCORING_LOCK_KEY})
        assert run_scoring(sf, store)["skipped"] is True
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SCORING_LOCK_KEY})
