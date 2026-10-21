# Design decisions and trade-offs

Each entry gives the decision, the alternatives considered, and why. These are the "why did you…?" questions.

---

### 1. Asynchronous extraction behind a queue (not inside the HTTP request)
**Decision.** `POST /api/posts` stores the post and returns `202` with an id; workers extract asynchronously.
**Alternatives.** Call the LLM synchronously in the request.
**Why.** An LLM call takes 1–5 s, fails sometimes, and free tiers are rate-limited to a few requests per minute. Doing it synchronously would tie up API workers, turn LLM errors into HTTP errors, and let a burst of posts exceed the quota. The queue absorbs bursts, retries failures with backoff, and lets extraction scale separately from the API.
**Cost.** The client has to poll (`GET /api/posts/{id}`), and the system now has to handle delivery semantics.

### 2. Postgres as the default queue; Redis Streams as a benchmarked alternative
**Decision.** One `JobQueue` interface with two implementations. Postgres is the default.
**Alternatives.** Redis Streams, Kafka, RabbitMQ, SQS, Celery.
**Why.** See [benchmarks.md](benchmarks.md). Redis is about 9x faster at enqueue and has roughly 50x lower latency, but Tally's throughput is capped by the LLM at about 8 jobs per minute. Postgres wins on correctness: **the enqueue is in the same transaction as the post insert**, so a post can never exist without its job, or the reverse. It also has per-job backoff (`available_at`) and means one fewer system to operate.
Kafka would make sense for replayable event streams at high volume; it's operationally heavy for this.
**When I'd switch.** For self-hosted models running hundreds of requests per second, or when sub-100 ms enqueue-to-start latency matters. Before switching, I'd add `LISTEN/NOTIFY` to remove the polling delay.

### 3. Leases, not locks, for job ownership
**Decision.** A claimed job gets `lease_until = now() + 120s`. An expired lease makes it claimable again.
**Alternatives.** Hold a row lock or mutex for the whole job; mark jobs "running" with no timeout.
**Why.** A crashed worker can't release a lock. With a timeout-free "running" flag, a crash strands the job forever. Holding a DB row lock for the duration of an LLM call means a long-lived transaction (bloat, connection exhaustion). Leases recover automatically. The trade-off is that a *slow* worker can lose its lease while still working. That is handled by:
- **Fencing tokens.** `attempts` increments on each claim, and ack/fail only apply `WHERE attempts = :claimed_attempts`, so a stale worker can't overwrite the new owner's result.
- `extend_lease` for long jobs (heartbeat).

### 4. At-least-once delivery + idempotent handlers (not "exactly once")
**Decision.** Accept that a job may run twice; make running it twice harmless.
**Why.** Exactly-once delivery isn't achievable across a network: a worker can finish the work and crash before the ack. (The benchmark's crash test shows this: every crashed job was executed twice.) So the handler is idempotent:
- the post row is locked with `SELECT … FOR UPDATE` and its status re-checked before writing;
- ideas have `UNIQUE(post_id, ticker)` with `ON CONFLICT DO NOTHING`;
- the LLM call is cached, so a re-run usually doesn't even cost a request.

### 5. Idempotent ingestion through a database constraint
**Decision.** `raw_posts.dedupe_key` is UNIQUE; inserts use `ON CONFLICT DO NOTHING`. The key is the source's native id (Reddit `t3_…`, RSS guid, the client's `Idempotency-Key`) or, failing that, a hash of source + author + normalised text + claimed timestamp.
**Alternatives.** "SELECT, and INSERT if missing" in application code.
**Why.** Check-then-insert races under concurrency (two requests both see "missing"). The constraint is atomic. It also makes source polling stateless: re-fetching overlapping pages just produces no-op duplicates, so no fragile cursor is needed.
**Trade-off.** With content-hash keys, the same author posting identical text twice (without a timestamp) is treated as one post. That's acceptable, and clients that care send an `Idempotency-Key`.

### 6. The dual-write problem with Redis, and the sweeper
With the Redis backend, "save post" (Postgres) and "enqueue" (Redis) can't be atomic. If the process dies between them, the post is saved but never processed. `requeue_stale_pending` periodically re-enqueues posts that are still `pending` and were never (or long ago) enqueued; duplicates are harmless because of decision 4. The textbook alternative is the **transactional outbox** (write an outbox row in the same transaction, and have a relay publish it). The sweeper is a lightweight version of that idea.

### 7. LLM output is untrusted input
**Decision.** Never write LLM output straight into the database. The pipeline:
1. requests JSON (Gemini: schema-constrained decoding; others: JSON mode + schema in the prompt);
2. validates with Pydantic (types, enums, ranges; lenient normalisation like "bearish" → SHORT);
3. resolves tickers against a **symbol master**, then aliases, then a price-history check; otherwise the post goes to review;
4. applies a **confidence gate** (below 0.6 goes to review);
5. routes contradictions ("not an idea" but ideas returned) to review.

**Prompt injection.** The post is wrapped in delimiters and the system prompt says to ignore instructions inside it. More importantly, the blast radius is small: the worst an injected post can do is produce a *valid-looking* idea, attributed to its own author, which still has to pass validation. The eval set includes an injection case.

**Error taxonomy.** Transient errors (429, 5xx, timeouts) are retried via the queue. Deterministic failures (non-JSON output, a safety block) go to human review, since retrying won't help. Configuration errors (a bad key) dead-letter.

### 8. Rate limiting before claiming
**Decision.** Workers take a token from a token bucket *before* claiming a job, and refund it if nothing was claimed.
**Why.** If a worker claimed first and then waited for quota, it would sit on a lease doing nothing, and the lease could expire, causing a duplicate run. The limiter is per process; a fleet would need a shared limiter (e.g. a Redis-based token bucket).

### 9. Append-only idea log, enforced by the database
**Decision.** A trigger rejects UPDATE/DELETE on `trade_ideas`. All computed values live in separate derived tables.
**Why.** The whole product is an honest track record. Enforcing immutability in the database (not just by convention in code) means no bug, script or person can quietly edit history. Derived data can always be rebuilt from facts, which also makes the scoring job trivially idempotent.

### 10. Point-in-time correctness
- **Our clock, not theirs.** `received_at` is when *we* got the post; an author's claimed time is stored but not trusted.
- **Next tradable open.** Entry is the first open after receipt, in the stock's exchange timezone. The same instant (21:14 Singapore time) is *after* the SGX open but *before* the NYSE open, so a US idea enters that night and an SGX idea enters the next morning.
- **Completed sessions only.** A bar dated today may be an in-progress session, so it can't close an idea.
- **Reviewer hindsight.** Ideas approved by a human are priced from the approval time. Otherwise a reviewer could wait, see which ideas went up, and approve only those.

### 11. Excess return, normalised by horizon
Raw returns reward bull markets; excess over the benchmark (SPY, ES3.SI, 2800.HK) measures skill. A short is scored as short-stock / long-benchmark. Because volatility scales with √time, excess is multiplied by √(20/h) so a 3-day call and a 6-month call land on one scale.
*Alternative considered:* risk-adjusting by each stock's own volatility (an information ratio per idea). That's more accurate but needs a volatility estimate per idea; a possible next step.

### 12. Empirical-Bayes shrinkage for skill
Normal–normal model with the prior centred on **zero** (skill must be demonstrated, not assumed). σ² is the pooled within-contributor variance; τ² comes from an n-weighted method-of-moments estimate. The first version used an unweighted average, and the simulation caught that a few 2-idea accounts dragged τ² to zero and flattened every estimate. The fix is the DerSimonian–Laird-style weighting.
**Validation.** `python -m tally simulate` plants contributors with known accuracy (including `sim_lucky_streak`, which is 3-for-3), runs the normal scoring job blind, and reports rank correlation with the truth for raw vs shrunk estimates.

### 13. Batch scoring instead of streaming
Ideas last days to months and prices are daily, so re-marking a few times a day is as accurate as streaming would be, with far fewer moving parts. The run is single-flight (advisory lock) and idempotent, so it's safe to trigger from the embedded scheduler, a cron, and a button at the same time.

### 14. Read-through price cache
Yahoo is free but unofficial and rate-limited. `price_coverage` records the date range held per ticker; the provider is only called when a request falls outside it or the cache is stale. On refresh, the **whole** range is re-downloaded, because adjusted prices are rewritten retroactively after each dividend or split, and mixing old and new adjusted closes would corrupt returns.

### 15. Missing data is surfaced, not dropped
Delisted or halted tickers are marked `unscorable` with a reason and counted per contributor. Silently dropping them would remove failures and inflate scores (survivorship bias).

### 16. Security basics
Admin endpoints need an `X-Admin-Token`, compared in constant time (`hmac.compare_digest`). The public submit endpoint has a per-IP token bucket (protecting the LLM quota) and strict size limits. All SQL is parameterised. CORS is restricted to configured origins. Known limits: `X-Forwarded-For` can be spoofed if the app isn't behind a trusted proxy, and the per-IP limiter is per instance.

---

## What I'd do next / at 100x scale

- `LISTEN/NOTIFY` (or move to Redis/Kafka) to drop queue polling latency; partition `jobs` by time and archive done jobs.
- A shared, distributed rate limiter, plus multiple LLM providers with fallback when one is rate-limited.
- Alembic migrations instead of `create_all`.
- A held-out eval set (the current one was written alongside the prompt and the regex baseline, so its numbers are optimistic), plus prompt A/B testing using `prompt_version`.
- Risk-adjusted per-idea scoring (volatility-scaled), time-decay so recent skill counts more, and sector-neutral benchmarks.
- OAuth Reddit access and more sources (X, Substack, StockTwits); per-source contributor identity linking.
- Observability: Prometheus metrics (queue depth, lease expiries, LLM latency/errors) and tracing across API → queue → worker.
