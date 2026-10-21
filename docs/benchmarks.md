# Queue benchmark: Postgres (SKIP LOCKED) vs Redis Streams

Reproduce with `python -m tally bench` (see flags with `--help`). Both runs below were on the same machine with local Postgres 16 and Redis 7, so read them as relative numbers, not absolute ones.

## Run 1: raw throughput (no simulated work, no crashes)

| Metric | postgres | redis |
|---|---|---|
| Enqueue throughput (jobs/s) | 1,222 | 10,424 |
| Drain throughput (jobs/s) | 1,547 | 4,324 |
| All jobs completed | True | True |
| Crashed deliveries (no ack) | 0 | 0 |
| Duplicate executions (redelivered after crash) | 0 | 0 |
| Drain time incl. crash recovery (s) | 1.9 | 0.7 |
| Steady-state latency p50 (ms) | 40.4 | 0.6 |
| Steady-state latency p95 (ms) | 135.5 | 0.9 |
| Steady-state latency p99 (ms) | 202.5 | 1.6 |

_3000 jobs, 4 worker threads, 0.0 ms simulated work per job, crash rate 0%, lease 2 s, steady rate 200/s. Run 2026-10-18 15:20._


## Run 2: 5% of workers crash after doing the work but before acking

| Metric | postgres | redis |
|---|---|---|
| Enqueue throughput (jobs/s) | 1,099 | 10,622 |
| Drain throughput (jobs/s) | 474 | 595 |
| All jobs completed | True | True |
| Crashed deliveries (no ack) | 108 | 107 |
| Duplicate executions (redelivered after crash) | 108 | 107 |
| Drain time incl. crash recovery (s) | 4.2 | 3.4 |
| Steady-state latency p50 (ms) | 7.2 | 2.8 |
| Steady-state latency p95 (ms) | 56.4 | 3.4 |
| Steady-state latency p99 (ms) | 116.5 | 4.7 |

_2000 jobs, 4 worker threads, 2.0 ms simulated work per job, crash rate 5%, lease 2 s, steady rate 200/s. Run 2026-10-18 15:20._


## What the numbers say

**Redis is faster at everything.** Enqueue is about 9x faster: an `XADD` is an in-memory append, while a Postgres `INSERT` writes to the WAL and fsyncs on commit. Latency is much lower too, because `XREADGROUP BLOCK` wakes a worker the moment a message arrives. The Postgres queue polls, so a job can wait up to one poll interval (200 ms with jitter) before anyone looks at it. That is where the 100+ ms p95/p99 comes from.

**Both recover from crashes, and both deliver duplicates.** In run 2, every crashed delivery was redelivered after its 2 s lease expired, so every job completed. It also means every crashed job **was executed twice** (duplicate executions = crashed deliveries). This is at-least-once delivery shown directly, and it is why Tally's worker is idempotent: it re-checks the post status under a row lock, and ideas have `UNIQUE(post_id, ticker)`.

**Why Postgres is still the default.** Tally's real bottleneck is the free LLM tier, at about 8 requests per minute. Even the slower Postgres queue has roughly 10,000x more throughput than the LLM can use. So the deciding factor isn't speed, it's correctness and operations:

- Postgres enqueue happens **in the same transaction** as the post insert. Redis can't join that transaction, so there is a gap between saving the post and enqueuing it, and it needs a sweeper to repair it (`IngestionService.requeue_stale_pending`).
- Postgres gives per-job exponential backoff for free (`available_at`). Redis Streams has no delayed delivery, so retries happen after the idle timeout.
- With Postgres there is one fewer piece of infrastructure to run, back up, and pay for on a free tier.

**When I'd switch to Redis (or Kafka).** If extraction moved to a self-hosted model with hundreds of requests per second, or if enqueue latency started to matter for users. Before switching, the first improvement to the Postgres queue would be `LISTEN/NOTIFY`, which wakes workers immediately and would remove most of the polling latency.
