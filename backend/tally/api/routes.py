"""HTTP routes. Thin: validate input, call a service, shape the response."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from .. import review as review_service
from ..models import (
    Contributor,
    ContributorStat,
    IdeaMark,
    RawPost,
    ScoringRun,
    SignalSnapshot,
    TradeIdea,
    WorkerHeartbeat,
)
from ..runtime import Runtime
from ..schemas import IngestResult, PostIn
from ..tools.simulate import simulate, simulation_report
from .deps import PerClientRateLimit, get_db, get_runtime, require_admin

router = APIRouter(prefix="/api")
_submit_limit: PerClientRateLimit | None = None


def enforce_submit_limit(request: Request, rt: Runtime = Depends(get_runtime)) -> None:
    global _submit_limit
    if _submit_limit is None:
        _submit_limit = PerClientRateLimit(rt.settings.submit_rate_per_minute)
    _submit_limit(request)


# --------------------------------------------------------------------------- #
# serialisers
# --------------------------------------------------------------------------- #
def _iso(v):
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v


def mark_json(m: IdeaMark | None) -> dict | None:
    if m is None:
        return None
    return {k: _iso(getattr(m, k)) for k in (
        "status", "entry_date", "entry_price", "bench_ticker", "bench_entry_price", "mark_date",
        "mark_price", "bench_mark_price", "raw_return", "bench_return", "excess_return",
        "normalised_excess", "unscorable_reason")}


def idea_json(i: TradeIdea, m: IdeaMark | None = None, handle: str | None = None) -> dict:
    return {
        "id": i.id, "post_id": i.post_id, "contributor_id": i.contributor_id, "contributor": handle,
        "ticker": i.ticker, "direction": i.direction, "horizon_days": i.horizon_days,
        "horizon_inferred": i.horizon_inferred, "target_price": i.target_price,
        "conviction": i.conviction, "confidence": i.confidence, "rationale": i.rationale,
        "received_at": _iso(i.received_at), "effective_at": _iso(i.effective_at),
        "extractor": i.extractor, "prompt_version": i.prompt_version, "mark": mark_json(m),
    }


def post_json(p: RawPost, handle: str | None = None) -> dict:
    return {
        "id": p.id, "source": p.source, "contributor_id": p.contributor_id, "contributor": handle,
        "text": p.text, "url": p.url, "posted_at": _iso(p.posted_at), "received_at": _iso(p.received_at),
        "status": p.status, "error": p.error, "extractor": p.extractor,
        "processed_at": _iso(p.processed_at), "extraction": p.extraction,
    }


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
@router.get("/health")
def health(db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime)):
    db.execute(text("SELECT 1"))
    return {"ok": True, "queue": rt.queue.backend, "extractor": rt.extractor.id}


# --------------------------------------------------------------------------- #
# posts
# --------------------------------------------------------------------------- #
class SubmitPost(BaseModel):
    text: str = Field(min_length=3, max_length=5000)
    author: str = Field(min_length=1, max_length=64)
    url: str | None = Field(default=None, max_length=2000)
    posted_at: dt.datetime | None = None


@router.post("/posts", status_code=status.HTTP_202_ACCEPTED, response_model=IngestResult)
def submit_post(body: SubmitPost, rt: Runtime = Depends(get_runtime),
                _: None = Depends(enforce_submit_limit),
                idempotency_key: str | None = Header(default=None, max_length=128)):
    """Accept a post for processing. Returns immediately (202); extraction is async.
    Send an `Idempotency-Key` header to make client retries safe."""
    post = PostIn(text=body.text, author=body.author, source="manual", url=body.url,
                  posted_at=body.posted_at, external_id=idempotency_key)
    return rt.ingestion.ingest(post)


@router.get("/posts")
def list_posts(status_: str | None = Query(default=None, alias="status"),
               limit: int = Query(50, le=200), offset: int = 0, db: Session = Depends(get_db)):
    q = (select(RawPost, Contributor.handle).join(Contributor)
         .order_by(desc(RawPost.id)).limit(limit).offset(offset))
    if status_:
        q = q.where(RawPost.status == status_)
    return [post_json(p, h) for p, h in db.execute(q).all()]


@router.get("/posts/{post_id}")
def get_post(post_id: int, db: Session = Depends(get_db)):
    row = db.execute(select(RawPost, Contributor.handle).join(Contributor)
                     .where(RawPost.id == post_id)).first()
    if row is None:
        raise HTTPException(404, "post not found")
    p, handle = row
    ideas = db.execute(select(TradeIdea, IdeaMark).outerjoin(IdeaMark, IdeaMark.idea_id == TradeIdea.id)
                       .where(TradeIdea.post_id == post_id)).all()
    return {**post_json(p, handle), "ideas": [idea_json(i, m, handle) for i, m in ideas]}


# --------------------------------------------------------------------------- #
# ideas / leaderboard / contributors / signals
# --------------------------------------------------------------------------- #
@router.get("/ideas")
def list_ideas(mark_status: str | None = None, ticker: str | None = None,
               contributor_id: int | None = None, include_simulated: bool = True,
               limit: int = Query(100, le=500), offset: int = 0, db: Session = Depends(get_db)):
    q = (select(TradeIdea, IdeaMark, Contributor.handle)
         .join(Contributor, Contributor.id == TradeIdea.contributor_id)
         .outerjoin(IdeaMark, IdeaMark.idea_id == TradeIdea.id)
         .order_by(desc(TradeIdea.effective_at)).limit(limit).offset(offset))
    if mark_status:
        q = q.where(IdeaMark.status == mark_status)
    if ticker:
        q = q.where(TradeIdea.ticker == ticker.upper())
    if contributor_id:
        q = q.where(TradeIdea.contributor_id == contributor_id)
    if not include_simulated:
        q = q.where(Contributor.is_simulated.is_(False))
    return [idea_json(i, m, h) for i, m, h in db.execute(q).all()]


@router.get("/leaderboard")
def leaderboard(min_closed: int = 0, include_simulated: bool = True,
                sort: Literal["shrunk", "raw", "hit_rate", "n"] = "shrunk",
                db: Session = Depends(get_db)):
    q = (select(Contributor, ContributorStat)
         .join(ContributorStat, ContributorStat.contributor_id == Contributor.id)
         .where(ContributorStat.n_closed >= min_closed))
    if not include_simulated:
        q = q.where(Contributor.is_simulated.is_(False))
    rows = []
    for c, s in db.execute(q).all():
        ci = None
        if s.posterior_mean is not None and s.posterior_sd is not None:
            ci = [s.posterior_mean - 1.96 * s.posterior_sd, s.posterior_mean + 1.96 * s.posterior_sd]
        rows.append({
            "contributor_id": c.id, "handle": c.handle, "source": c.source,
            "is_simulated": c.is_simulated,
            "true_accuracy": (c.true_skill + 0.5) if c.true_skill is not None else None,
            "n_ideas": s.n_ideas, "n_closed": s.n_closed, "n_open": s.n_open,
            "n_unscorable": s.n_unscorable, "hit_rate": s.hit_rate, "mean_excess": s.mean_excess,
            "posterior_mean": s.posterior_mean, "posterior_sd": s.posterior_sd, "ci95": ci,
            "prob_positive": s.prob_positive, "updated_at": _iso(s.updated_at),
        })
    key = {"shrunk": "posterior_mean", "raw": "mean_excess", "hit_rate": "hit_rate", "n": "n_closed"}[sort]
    rows.sort(key=lambda r: (r[key] is not None, r[key] or 0), reverse=True)
    return rows


@router.get("/contributors/{cid}")
def contributor(cid: int, db: Session = Depends(get_db)):
    c = db.get(Contributor, cid)
    if c is None:
        raise HTTPException(404, "contributor not found")
    s = db.get(ContributorStat, cid)
    ideas = db.execute(select(TradeIdea, IdeaMark).outerjoin(IdeaMark, IdeaMark.idea_id == TradeIdea.id)
                       .where(TradeIdea.contributor_id == cid)
                       .order_by(desc(TradeIdea.effective_at)).limit(200)).all()
    return {"id": c.id, "handle": c.handle, "source": c.source, "is_simulated": c.is_simulated,
            "stats": None if s is None else {
                "n_ideas": s.n_ideas, "n_closed": s.n_closed, "hit_rate": s.hit_rate,
                "mean_excess": s.mean_excess, "posterior_mean": s.posterior_mean,
                "posterior_sd": s.posterior_sd, "prob_positive": s.prob_positive},
            "ideas": [idea_json(i, m, c.handle) for i, m in ideas]}


@router.get("/signals")
def signals(db: Session = Depends(get_db)):
    snap = db.execute(select(SignalSnapshot).order_by(desc(SignalSnapshot.id)).limit(1)).scalar()
    return snap.payload if snap else {"generated_at": None, "signals": [], "portfolio": {}, "risk": None}


@router.get("/simulation/report")
def sim_report(rt: Runtime = Depends(get_runtime)):
    return simulation_report(rt.sf)


# --------------------------------------------------------------------------- #
# review (human in the loop)
# --------------------------------------------------------------------------- #
class OverrideIn(BaseModel):
    ticker: str = Field(min_length=1, max_length=24)
    direction: Literal["LONG", "SHORT"]
    horizon_days: int | None = Field(default=None, ge=1, le=252)


class ApproveIn(BaseModel):
    overrides: list[OverrideIn] | None = None


@router.get("/review")
def review_queue(db: Session = Depends(get_db), _: str = Depends(require_admin)):
    rows = db.execute(select(RawPost, Contributor.handle).join(Contributor)
                      .where(RawPost.status == "needs_review").order_by(RawPost.id).limit(100)).all()
    return [post_json(p, h) for p, h in rows]


@router.post("/review/{post_id}/approve")
def approve(post_id: int, body: ApproveIn, rt: Runtime = Depends(get_runtime),
            who: str = Depends(require_admin)):
    overrides = [review_service.Override(**o.model_dump()) for o in body.overrides or []]
    try:
        n = review_service.approve(rt.sf, rt.resolver, post_id, overrides or None, reviewer=who)
    except review_service.ReviewError as e:
        raise HTTPException(409, str(e)) from e
    # effective_at = approval time: the entry price is the first market open after this.
    return {"post_id": post_id, "ideas_created": n,
            "effective_at": dt.datetime.now(dt.timezone.utc).isoformat()}


@router.post("/review/{post_id}/reject")
def reject(post_id: int, rt: Runtime = Depends(get_runtime), who: str = Depends(require_admin)):
    try:
        review_service.reject(rt.sf, post_id, reviewer=who)
    except review_service.ReviewError as e:
        raise HTTPException(409, str(e)) from e
    return {"post_id": post_id, "status": "rejected"}


# --------------------------------------------------------------------------- #
# system / ops
# --------------------------------------------------------------------------- #
@router.get("/admin/check")
def admin_check(_: str = Depends(require_admin)):
    """Lets the UI verify a token before showing admin-only tabs."""
    return {"ok": True}


@router.get("/system")
def system(db: Session = Depends(get_db), rt: Runtime = Depends(get_runtime),
           _: str = Depends(require_admin)):
    """Operator view. Every field answers a question an operator actually asks:
    is work flowing, are workers alive, is scoring fresh, what needs attention."""
    st = rt.settings
    try:
        q = rt.queue.stats()
        queue = {"backend": q.backend, "waiting": q.pending, "in_progress": q.in_flight,
                 "dead": q.dead, "oldest_waiting_s": q.oldest_pending_age_s}
        dead = rt.queue.dead_letters(10)
    except Exception as e:  # noqa: BLE001 - e.g. Redis down; report rather than 500
        queue, dead = {"backend": rt.queue.backend, "error": str(e)}, []
    now = dt.datetime.now(dt.timezone.utc)
    recent = now - dt.timedelta(hours=24)   # older heartbeats are long-gone processes: noise
    workers = [{"worker_id": w.worker_id, "processed": w.processed, "failed": w.failed,
                "last_seen": _iso(w.last_seen), "alive": (now - w.last_seen).total_seconds() < 90}
               for w in db.execute(select(WorkerHeartbeat).where(WorkerHeartbeat.last_seen >= recent)
                                   .order_by(desc(WorkerHeartbeat.last_seen)).limit(10)).scalars()]
    runs = [{"id": r.id, "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
             "ideas_marked": r.ideas_marked, "contributors_scored": r.contributors_scored, "error": r.error}
            for r in db.execute(select(ScoringRun).order_by(desc(ScoringRun.id)).limit(5)).scalars()]
    post_counts = dict(db.execute(select(RawPost.status, func.count()).group_by(RawPost.status)).all())
    simulated = db.execute(select(Contributor.id).where(Contributor.is_simulated.is_(True)).limit(1)).first()
    return {
        "queue": queue, "dead_letters": dead, "workers": workers, "scoring_runs": runs,
        "post_counts": post_counts,
        "simulated_seeded": simulated is not None,
        "config": {
            "extractor": rt.extractor.id,
            "llm_requests_per_minute": st.llm_requests_per_minute if rt.extractor.uses_llm else None,
            "review_confidence_threshold": st.review_confidence_threshold,
            "market_data": st.market_data_provider,
            "scoring_interval_minutes": st.scoring_interval_minutes if st.embedded_scheduler else None,
            "queue_backend": st.queue_backend,
            "lease_seconds": st.lease_seconds,
            "max_attempts": st.max_attempts,
            "sources": [s.label for s in rt.configured_sources()],
        },
    }


# --------------------------------------------------------------------------- #
# sources: preview and pull from a chosen source (System tab)
# --------------------------------------------------------------------------- #
SUGGESTED = {
    "reddit": ["stocks", "investing", "ValueInvesting", "SecurityAnalysis", "StockMarket", "wallstreetbets"],
    "substack": ["stocksunderrocks", "stockpicking"],
    "stocktwits": ["trending", "NVDA", "TSLA", "AAPL", "SPY"],
    "rss": ["https://stocksunderrocks.substack.com/feed"],
}


class SourceIn(BaseModel):
    kind: Literal["reddit", "substack", "stocktwits", "rss"]
    target: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=10, ge=1, le=25)   # each kept post can cost an LLM call


def _source_or_400(rt: Runtime, body: SourceIn):
    from ..sources import SourceError
    try:
        return rt.build_source(body.kind, body.target, body.limit)
    except SourceError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/sources")
def sources(rt: Runtime = Depends(get_runtime), _: str = Depends(require_admin)):
    """What the scheduler polls automatically (from .env) plus suggestions for the picker."""
    return {"scheduled": [s.label for s in rt.configured_sources()], "suggested": SUGGESTED,
            "poll_interval_minutes": rt.settings.source_poll_interval_minutes}


@router.post("/sources/preview")
def sources_preview(body: SourceIn, rt: Runtime = Depends(get_runtime), _: str = Depends(require_admin)):
    """Dry run: fetch and show what would be kept. Saves nothing, calls no LLM."""
    src = _source_or_400(rt, body)
    try:
        return rt.preview(src)
    except Exception as e:  # noqa: BLE001 - upstream blocked/down: tell the operator why
        raise HTTPException(502, f"{src.label} fetch failed: {type(e).__name__}: {e}") from e


@router.post("/sources/ingest")
def sources_ingest(body: SourceIn, rt: Runtime = Depends(get_runtime), _: str = Depends(require_admin)):
    """Fetch and ingest: kept posts are saved and queued for LLM extraction."""
    src = _source_or_400(rt, body)
    report = rt.poll([src])["sources"][src.label]
    if "error" in report:
        raise HTTPException(502, f"{src.label} fetch failed: {report['error']}")
    return {"source": src.label, **report}


@router.post("/admin/retry-failed")
def admin_retry_failed(rt: Runtime = Depends(get_runtime), _: str = Depends(require_admin)):
    """Re-queue every post whose extraction failed permanently (e.g. after fixing a retired
    model name or an expired API key)."""
    with rt.sf() as s:
        ids = list(s.execute(select(RawPost.id).where(RawPost.status == "failed")).scalars())
    results = rt.ingestion.requeue(ids)
    return {"requeued": sum(v == "re-queued" for v in results.values())}


@router.post("/admin/score", status_code=202)
def admin_score(bg: BackgroundTasks, rt: Runtime = Depends(get_runtime), _: str = Depends(require_admin)):
    bg.add_task(rt.score)
    return {"accepted": True}


@router.post("/admin/poll", status_code=202)
def admin_poll(bg: BackgroundTasks, rt: Runtime = Depends(get_runtime), _: str = Depends(require_admin)):
    bg.add_task(rt.poll)
    return {"accepted": True}


@router.post("/admin/simulate", status_code=202)
def admin_simulate(bg: BackgroundTasks, rt: Runtime = Depends(get_runtime), _: str = Depends(require_admin)):
    def job():
        simulate(rt.sf, rt.store)
        rt.score()
    bg.add_task(job)
    return {"accepted": True}
