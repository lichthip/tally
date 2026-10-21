"""The scoring job: mark every live idea, re-estimate skill, rebuild signals.

Runs on a schedule (embedded thread, CLI, or POST /admin/score). It is:
  * Idempotent: it only writes derived tables, fully recomputed each run. Running it
    twice is harmless; deleting all derived data and re-running reproduces it.
  * Single-flight: guarded by a Postgres advisory lock, so two instances (or a cron and
    a manual trigger) never score concurrently and interleave writes.
  * Batch, not streaming: ideas live for days to months, and prices move daily, so
    re-marking once or a few times a day is plenty. Streaming would add a lot of
    machinery for no accuracy gain at this horizon.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections import defaultdict

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from ..db import get_engine
from ..market.exchanges import exchange_for
from ..market.store import PriceStore
from ..models import ContributorStat, IdeaMark, ScoringRun, SignalSnapshot, TradeIdea
from .marking import IdeaSpec, MarkResult, mark_idea
from .signals import build_signals, daily_returns, historical_risk, portfolio_weights, signals_to_json
from .skill import contributor_weight, estimate_skill

log = logging.getLogger(__name__)
SCORING_LOCK_KEY = 7231002


def run_scoring(sf: sessionmaker[Session], store: PriceStore, *, now: dt.datetime | None = None,
                normalisation_horizon: int = 20) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    engine = sf.kw.get("bind") or get_engine()
    with engine.connect() as lock_conn:
        got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SCORING_LOCK_KEY}).scalar()
        if not got:
            return {"skipped": True, "reason": "another scoring run holds the lock"}
        try:
            return _run_locked(sf, store, now, normalisation_horizon)
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SCORING_LOCK_KEY})
            lock_conn.commit()


def _run_locked(sf: sessionmaker[Session], store: PriceStore, now: dt.datetime, norm_h: int) -> dict:
    t0 = time.monotonic()
    with sf.begin() as s:
        run = ScoringRun()
        s.add(run)
        s.flush()
        run_id = run.id
    try:
        marked = _mark_live_ideas(sf, store, now, norm_h)
        n_contrib, model = _recompute_stats(sf)
        snapshot = _rebuild_signals(sf, store, now, model)
        with sf.begin() as s:
            r = s.get(ScoringRun, run_id)
            r.finished_at = dt.datetime.now(dt.timezone.utc)
            r.ideas_marked = marked
            r.contributors_scored = n_contrib
        summary = {"run_id": run_id, "ideas_marked": marked, "contributors_scored": n_contrib,
                   "signals": len(snapshot.get("signals", [])), "seconds": round(time.monotonic() - t0, 2),
                   "model": model}
        log.info("scoring run %s: %s", run_id, summary)
        return summary
    except Exception as e:
        with sf.begin() as s:
            r = s.get(ScoringRun, run_id)
            r.finished_at = dt.datetime.now(dt.timezone.utc)
            r.error = f"{type(e).__name__}: {e}"[:2000]
        raise


def _mark_live_ideas(sf: sessionmaker[Session], store: PriceStore, now: dt.datetime, norm_h: int) -> int:
    with sf() as s:
        # Closed marks are final; everything else (new, pending, open, unscorable) is re-marked.
        rows = s.execute(
            select(TradeIdea.id, TradeIdea.ticker, TradeIdea.direction, TradeIdea.horizon_days,
                   TradeIdea.effective_at)
            .outerjoin(IdeaMark, IdeaMark.idea_id == TradeIdea.id)
            .where((IdeaMark.idea_id.is_(None)) | (IdeaMark.status != "closed"))
        ).all()
    if not rows:
        return 0

    by_ticker: dict[str, list] = defaultdict(list)
    for r in rows:
        by_ticker[r.ticker].append(r)

    bench_cache: dict[str, list] = {}
    results: list[tuple[int, MarkResult]] = []
    for ticker, ideas in by_ticker.items():
        ex = exchange_for(ticker)
        start = min(i.effective_at for i in ideas).date() - dt.timedelta(days=7)
        bars = store.get_bars(ticker, start)
        bench = ex.benchmark
        if bench not in bench_cache or (bench_cache[bench] and bench_cache[bench][0].date > start):
            bench_cache[bench] = store.get_bars(bench, start)
        for i in ideas:
            spec = IdeaSpec(ticker, i.direction, i.horizon_days, i.effective_at)
            results.append((i.id, mark_idea(spec, bars, bench_cache[bench], bench, ex, now, norm_h)))

    with sf.begin() as s:
        for idea_id, m in results:
            values = {k: getattr(m, k) for k in MarkResult.__dataclass_fields__}
            stmt = pg_insert(IdeaMark).values(idea_id=idea_id, **values)
            s.execute(stmt.on_conflict_do_update(index_elements=["idea_id"],
                                                 set_={**values, "updated_at": text("now()")}))
    return len(results)


def _recompute_stats(sf: sessionmaker[Session]):
    with sf() as s:
        rows = s.execute(
            select(TradeIdea.contributor_id, IdeaMark.status, IdeaMark.normalised_excess)
            .outerjoin(IdeaMark, IdeaMark.idea_id == TradeIdea.id)).all()
    closed: dict[int, list[float]] = defaultdict(list)
    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for cid, status, ne in rows:
        counts[cid]["total"] += 1
        counts[cid][status or "unmarked"] += 1
        if status == "closed" and ne is not None:
            closed[cid].append(ne)
        else:
            closed.setdefault(cid, [])
    estimates, model = estimate_skill(closed)
    with sf.begin() as s:
        for cid, est in estimates.items():
            c = counts[cid]
            values = dict(n_ideas=c["total"], n_closed=c["closed"],
                          n_open=c["open"] + c["pending_entry"] + c["unmarked"],
                          n_unscorable=c["unscorable"], hit_rate=est.hit_rate, mean_excess=est.mean,
                          posterior_mean=est.posterior_mean, posterior_sd=est.posterior_sd,
                          prob_positive=est.prob_positive)
            stmt = pg_insert(ContributorStat).values(contributor_id=cid, **values)
            s.execute(stmt.on_conflict_do_update(index_elements=["contributor_id"],
                                                 set_={**values, "updated_at": text("now()")}))
    return len(estimates), {"sigma2": model.sigma2, "tau2": model.tau2,
                            "n_contributors": model.n_contributors, "n_closed_ideas": model.n_ideas}


def _rebuild_signals(sf: sessionmaker[Session], store: PriceStore, now: dt.datetime, model: dict) -> dict:
    with sf() as s:
        live = s.execute(
            select(TradeIdea.ticker, TradeIdea.direction, TradeIdea.contributor_id)
            .join(IdeaMark, IdeaMark.idea_id == TradeIdea.id)
            .where(IdeaMark.status.in_(["open", "pending_entry"]))
            .order_by(TradeIdea.effective_at)).all()
        probs = dict(s.execute(select(ContributorStat.contributor_id, ContributorStat.prob_positive)).all())
    weights = {cid: contributor_weight(p) for cid, p in probs.items()}
    signals = build_signals([(r.ticker, r.direction, r.contributor_id) for r in live], weights)
    port = portfolio_weights(signals)

    rets = {}
    start = now.date() - dt.timedelta(days=400)
    for t in port:
        bars = store.get_bars(t, start)
        rets[t] = daily_returns([(b.date, b.close) for b in bars])
    risk = historical_risk(port, rets)

    payload = {"generated_at": now.isoformat(), "signals": signals_to_json(signals),
               "portfolio": port, "risk": risk, "model": model}
    with sf.begin() as s:
        s.add(SignalSnapshot(payload=payload))
    return payload
