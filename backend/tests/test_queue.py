"""The JobQueue contract, run against BOTH backends with the same tests."""
import threading
import time
from collections import Counter

import pytest
from sqlalchemy import text

from tally.queue import PostgresQueue, RedisStreamQueue


@pytest.fixture(params=["postgres", "redis"])
def make_queue(request, sf, redis_client):
    def factory(lease=60, max_attempts=3):
        if request.param == "postgres":
            q = PostgresQueue(sf, queue="test", lease_seconds=lease, max_attempts=max_attempts,
                              poll_interval_s=0.05)
        else:
            q = RedisStreamQueue(redis_client, queue="test", lease_seconds=lease, max_attempts=max_attempts)
        q.backoff_seconds = lambda attempts: 0   # retries immediately in tests
        q.purge()
        return q
    factory.backend = request.param
    return factory


def test_enqueue_claim_ack(make_queue):
    q = make_queue()
    q.enqueue({"post_id": 1})
    [m] = q.claim("w1", block_ms=500)
    assert m.payload == {"post_id": 1} and m.attempts == 1
    assert q.claim("w2", block_ms=100) == []        # leased to w1, invisible to others
    assert q.ack(m)
    assert q.stats().in_flight == 0


def test_concurrent_workers_never_share_a_message(make_queue):
    q = make_queue()
    for k in range(200):
        q.enqueue({"k": k})
    seen = Counter()
    lock = threading.Lock()

    def worker(i):
        while True:
            msgs = q.claim(f"w{i}", max_messages=5, block_ms=200)
            if not msgs:
                return
            for m in msgs:
                with lock:
                    seen[m.payload["k"]] += 1
                q.ack(m)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(seen) == 200 and max(seen.values()) == 1


def test_expired_lease_is_redelivered(make_queue):
    q = make_queue(lease=1)
    q.enqueue({"k": 1})
    [m] = q.claim("crashy", block_ms=500)        # ...and then the worker "crashes"
    assert q.claim("w2", block_ms=100) == []
    time.sleep(1.3)
    [m2] = q.claim("w2", block_ms=500)
    assert m2.payload == {"k": 1} and m2.attempts == 2
    assert q.ack(m2)


def test_failure_retries_then_dead_letters(make_queue):
    q = make_queue(lease=1, max_attempts=2)
    q.enqueue({"k": 1})
    [m] = q.claim("w", block_ms=500)
    assert q.fail(m, "boom 1") is False
    if make_queue.backend == "redis":
        time.sleep(1.2)                           # redis retries via idle-time reclaim
    [m2] = q.claim("w", block_ms=1000)
    assert m2.attempts == 2
    assert q.fail(m2, "boom 2") is True           # attempts exhausted
    dead = q.dead_letters()
    assert len(dead) == 1 and "boom 2" in dead[0]["error"]
    assert q.stats().dead == 1


def test_stale_ack_is_fenced_off(sf):
    q = PostgresQueue(sf, queue="fence", lease_seconds=1, poll_interval_s=0.05)
    q.purge()
    q.enqueue({"k": 1})
    [slow] = q.claim("slow", block_ms=500)
    time.sleep(1.2)
    [fast] = q.claim("fast", block_ms=500)        # lease expired, reclaimed
    assert q.ack(slow) is False                    # stale owner can't mark it done...
    assert q.fail(slow, "late failure") is False
    assert q.ack(fast) is True                     # ...the current owner can


def test_postgres_enqueue_is_transactional(sf):
    q = PostgresQueue(sf, queue="tx")
    q.purge()
    with sf() as s:
        q.enqueue({"k": 1}, session=s)
        s.rollback()                               # e.g. the post insert failed
    assert q.claim("w", block_ms=100) == []
    with sf() as s:
        q.enqueue({"k": 2}, session=s)
        s.commit()
    assert [m.payload["k"] for m in q.claim("w", block_ms=200)] == [2]


def test_postgres_poison_message_dead_letters_after_crash_loop(sf):
    q = PostgresQueue(sf, queue="poison", lease_seconds=1, max_attempts=1, poll_interval_s=0.05)
    q.purge()
    q.enqueue({"k": 1})
    q.claim("crash1", block_ms=200)
    time.sleep(1.2)
    assert q.claim("w2", block_ms=200) == []       # attempt 2 > max 1: dead, not redelivered
    with sf() as s:
        assert s.execute(text("SELECT status FROM jobs WHERE queue='poison'")).scalar() == "dead"
