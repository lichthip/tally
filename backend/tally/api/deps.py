"""FastAPI dependencies: the shared runtime, DB sessions, auth, and rate limiting."""
from __future__ import annotations

import hmac
import threading
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..extraction.llm_clients import RateLimiter
from ..runtime import Runtime

_runtime: Runtime | None = None


def set_runtime(rt: Runtime) -> None:
    global _runtime
    _runtime = rt


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime not initialised")
    return _runtime


def get_db(rt: Runtime = Depends(get_runtime)) -> Iterator[Session]:
    with rt.sf() as s:
        yield s


def require_admin(x_admin_token: str | None = Header(default=None),
                  rt: Runtime = Depends(get_runtime)) -> str:
    # Constant-time comparison: don't leak how many leading characters matched.
    if not x_admin_token or not hmac.compare_digest(x_admin_token, rt.settings.admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin token required")
    return "admin"


class PerClientRateLimit:
    """Token bucket per client IP for the public submit endpoint. It protects the free LLM
    quota from one noisy client. In-memory, so per instance: good enough for a demo; a
    multi-instance deployment would keep the buckets in Redis."""

    def __init__(self, per_minute: float, burst: int = 3, max_clients: int = 10_000):
        self.per_minute = per_minute
        self.burst = burst
        self.max_clients = max_clients
        self._buckets: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()

    def __call__(self, request: Request) -> None:
        # Behind Render/Vercel the real client is the first X-Forwarded-For hop.
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")
        with self._lock:
            if len(self._buckets) > self.max_clients:
                self._buckets.clear()
            bucket = self._buckets.setdefault(ip, RateLimiter(self.per_minute, burst=self.burst))
        if not bucket.acquire(timeout=0):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "slow down: submit rate limit exceeded")
