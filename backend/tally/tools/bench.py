# =============================================================================
# tools/bench.py: THE QUEUE BENCHMARK (Postgres vs Redis, same workload)
# -----------------------------------------------------------------------------
# Produces the numbers in docs/benchmarks.md. For each backend:
#   1. ENQUEUE throughput: add N jobs as fast as possible -> jobs/second
#   2. DRAIN throughput:   W worker threads empty the queue -> jobs/second
#      (optionally "crashing" some workers before they ack, to prove recovery)
#   3. LATENCY: add jobs at a steady 200/s while workers consume; measure time from
#      enqueue to ack for each job -> p50 / p95 / p99
#
# The crash test is the important one: with --crash-rate 0.05, 5% of first deliveries
# do the work and then "die" without acking. We check every job still completes (the
# lease expires, the job is redelivered) and count how many ran TWICE: at-least-once
# delivery, measured.
#
# Run: python -m tally bench --crash-rate 0.05 --out ../docs/benchmarks.md
# =============================================================================
"""Queue benchmark: Postgres (SKIP LOCKED) vs Redis Streams on identical workloads.

Three phases per backend:
  1. enqueue throughput  - single producer, N jobs as fast as possible
  2. drain throughput    - W worker threads consume the backlog (simulated work per job)
  3. steady-state latency- producer at a fixed rate while workers consume; end-to-end
                           latency (enqueue -> ack) percentiles. This exposes the cost of
                           Postgres polling vs Redis blocking reads.
Optional: --crash-rate p makes workers "crash" (drop without ack) on p of first deliveries.
We then verify every job still completes (lease expiry -> redelivery) and count duplicates,
which is at-least-once delivery made visible.
"""
from __future__ import annotations

import datetime as dt
import random
import statistics
import threading
import time
from collections import Counter     # a dict that counts things: c["x"] += 1

from ..queue import build_queue


# Percentile: sort the values and pick the one p% of the way along.
# _pct(latencies, 95) = the value that 95% of measurements are at or below.
def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


# Run `n_workers` threads that claim and ack jobs until `target` distinct jobs are done.
def _consume(q, n_workers: int, target: int, work_ms: float, crash_rate: float,
             timeout_s: float, seed: int = 1):
    """Workers run until `target` distinct jobs are ACKED.

    A "crash" happens after the work is done but before the ack: the most dangerous
    moment, because the side effect already happened. The lease expires, the job is
    redelivered and executed again. `executions` counts side effects, so
    executions - unique jobs = duplicate executions = why handlers must be idempotent.
    """
    executions: Counter = Counter()      # job number -> how many times it was executed
    acked: set = set()                   # job numbers successfully acked
    latencies: list[float] = []
    crashed = [0]                        # a list so inner functions can modify it
    lock = threading.Lock()              # threads share the counters above
    done = threading.Event()

    def worker(i: int) -> None:
        rng = random.Random(seed + i)
        wid = f"bench-{i}"
        while not done.is_set():
            msgs = q.claim(wid, max_messages=10, block_ms=200)
            for m in msgs:
                if work_ms:
                    time.sleep(work_ms / 1000)         # pretend to do some work
                k = m.payload["k"]
                with lock:
                    executions[k] += 1                 # side effect has happened
                # Simulate a crash: only on a FIRST delivery, with probability crash_rate.
                if crash_rate and m.attempts == 1 and rng.random() < crash_rate:
                    with lock:
                        crashed[0] += 1
                    continue                           # crash before ack: lease will expire
                q.ack(m)
                now = time.time()
                with lock:
                    if k not in acked:
                        acked.add(k)
                        latencies.append(now - m.payload["t"])    # t = when it was enqueued
                    if len(acked) >= target:
                        done.set()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n_workers)]
    t0 = time.perf_counter()             # high-resolution timer
    for t in threads:
        t.start()
    done.wait(timeout_s)
    elapsed = time.perf_counter() - t0
    done.set()
    for t in threads:
        t.join(5)
    return executions, acked, latencies, crashed[0], elapsed


# All three phases for one backend. Returns a dict of results (one column of the table).
def run_bench(backend: str, jobs: int, workers: int, work_ms: float, crash_rate: float,
              lease: int, steady_rate: float = 200.0, steady_jobs: int = 600) -> dict:
    q = build_queue(backend, queue="bench", lease_seconds=lease)   # a separate "bench" queue
    q.purge()
    res: dict = {"backend": backend, "jobs": jobs, "workers": workers, "work_ms": work_ms,
                 "crash_rate": crash_rate, "lease_s": lease}

    # 1) enqueue throughput
    t0 = time.perf_counter()
    for k in range(jobs):
        q.enqueue({"k": k, "t": time.time()})
    res["enqueue_per_s"] = jobs / (time.perf_counter() - t0)

    # 2) drain
    execs, acked, _, crashed, elapsed = _consume(q, workers, jobs, work_ms, crash_rate,
                                                 timeout_s=max(60, lease * 10 + jobs / 50))
    res["drain_per_s"] = len(acked) / elapsed
    res["completed_unique"] = len(acked)
    res["all_completed"] = len(acked) == jobs
    res["crashed_deliveries"] = crashed
    res["duplicate_completions"] = sum(execs.values()) - len(execs)    # total runs - distinct jobs
    res["drain_seconds"] = elapsed
    q.purge()

    # 3) steady-state latency (no crashes)
    stop = threading.Event()

    # Adds jobs at a fixed rate (e.g. one every 5 ms for 200/s).
    def producer() -> None:
        interval = 1.0 / steady_rate
        nxt = time.perf_counter()
        for k in range(steady_jobs):
            q.enqueue({"k": k, "t": time.time()})
            nxt += interval
            time.sleep(max(0.0, nxt - time.perf_counter()))
        stop.set()

    pt = threading.Thread(target=producer, daemon=True)
    pt.start()
    _, _, lat2, _, _ = _consume(q, workers, steady_jobs, work_ms, 0.0, timeout_s=120)
    pt.join(5)
    lat_ms = [x * 1000 for x in lat2]          # seconds -> milliseconds
    res.update({"steady_rate_per_s": steady_rate, "latency_p50_ms": _pct(lat_ms, 50),
                "latency_p95_ms": _pct(lat_ms, 95), "latency_p99_ms": _pct(lat_ms, 99),
                "latency_mean_ms": statistics.fmean(lat_ms) if lat_ms else float("nan")})
    q.purge()
    return res


# Results -> the markdown table in docs/benchmarks.md.
def to_markdown(results: list[dict]) -> str:
    rows = [
        ("Enqueue throughput (jobs/s)", "enqueue_per_s", "{:,.0f}"),
        ("Drain throughput (jobs/s)", "drain_per_s", "{:,.0f}"),
        ("All jobs completed", "all_completed", "{}"),
        ("Crashed deliveries (no ack)", "crashed_deliveries", "{}"),
        ("Duplicate executions (redelivered after crash)", "duplicate_completions", "{}"),
        ("Drain time incl. crash recovery (s)", "drain_seconds", "{:.1f}"),
        ("Steady-state latency p50 (ms)", "latency_p50_ms", "{:.1f}"),
        ("Steady-state latency p95 (ms)", "latency_p95_ms", "{:.1f}"),
        ("Steady-state latency p99 (ms)", "latency_p99_ms", "{:.1f}"),
    ]
    head = "| Metric | " + " | ".join(r["backend"] for r in results) + " |"
    sep = "|---|" + "---|" * len(results)
    lines = [head, sep]
    for label, key, fmt in rows:
        lines.append(f"| {label} | " + " | ".join(fmt.format(r[key]) for r in results) + " |")
    r0 = results[0]
    meta = (f"\n_{r0['jobs']} jobs, {r0['workers']} worker threads, {r0['work_ms']} ms simulated work per job, "
            f"crash rate {r0['crash_rate']:.0%}, lease {r0['lease_s']} s, steady rate {r0['steady_rate_per_s']:.0f}/s. "
            f"Run {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}._\n")
    return "\n".join(lines) + "\n" + meta


def run_bench_cli(args) -> int:
    backends = ["postgres", "redis"] if args.backend == "both" else [args.backend]
    results = []
    for b in backends:
        print(f"benchmarking {b} ...", flush=True)
        results.append(run_bench(b, args.jobs, args.workers, args.work_ms, args.crash_rate, args.lease))
    md = to_markdown(results)
    print(md)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("# Queue benchmark\n\n" + md)
    return 0
