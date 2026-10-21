# =============================================================================
# api/deps.py: SHARED HELPERS ("dependencies") FOR THE ROUTE FUNCTIONS
# -----------------------------------------------------------------------------
# FastAPI "dependency injection": a route can declare  rt: Runtime = Depends(get_runtime)
# and FastAPI calls get_runtime() for it on every request and passes in the result.
# The same idea gives routes a database session (get_db), admin checking (require_admin)
# and rate limiting (PerClientRateLimit), without repeating that code in every route.
# =============================================================================
"""FastAPI dependencies: the shared runtime, DB sessions, auth, and rate limiting."""
from __future__ import annotations

import hmac               # hmac.compare_digest = constant-time string comparison
import threading
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..extraction.llm_clients import RateLimiter
from ..runtime import Runtime

# The single Runtime for this process, stored at startup by api/main.py.
_runtime: Runtime | None = None


def set_runtime(rt: Runtime) -> None:
    global _runtime          # `global`: assign the module-level variable, not a new local one
    _runtime = rt


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime not initialised")
    return _runtime


# A database session for one request: opened before the route runs, closed after
# (the `yield` hands it to the route; the `with` closes it afterwards). Read-only use.
def get_db(rt: Runtime = Depends(get_runtime)) -> Iterator[Session]:
    with rt.sf() as s:
        yield s


# Guard for admin-only endpoints. FastAPI reads the "X-Admin-Token" HTTP header into
# x_admin_token (underscores become dashes). Wrong or missing -> 401 Unauthorized.
def require_admin(x_admin_token: str | None = Header(default=None),
                  rt: Runtime = Depends(get_runtime)) -> str:
    # Constant-time comparison: don't leak how many leading characters matched.
    # (A normal == stops at the first different character, so an attacker could time
    # responses to guess the token one character at a time. compare_digest always takes
    # the same time.)
    if not x_admin_token or not hmac.compare_digest(x_admin_token, rt.settings.admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin token required")
    return "admin"


# A class whose instances can be called like functions (__call__), so an instance can
# be used as a FastAPI dependency. One token bucket per client IP address.
class PerClientRateLimit:
    """Token bucket per client IP for the public submit endpoint. It protects the free LLM
    quota from one noisy client. In-memory, so per instance: good enough for a demo; a
    multi-instance deployment would keep the buckets in Redis."""

    def __init__(self, per_minute: float, burst: int = 3, max_clients: int = 10_000):
        self.per_minute = per_minute
        self.burst = burst                     # allow up to 3 quick submits, then throttle
        self.max_clients = max_clients
        self._buckets: dict[str, RateLimiter] = {}     # ip -> its own bucket
        self._lock = threading.Lock()

    def __call__(self, request: Request) -> None:
        # Behind Render/Vercel the real client is the first X-Forwarded-For hop.
        # (Proxies add the original IP in this header; otherwise we'd see the proxy's IP.)
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")
        with self._lock:
            # Memory safety valve: if too many IPs pile up, forget them all.
            if len(self._buckets) > self.max_clients:
                self._buckets.clear()
            bucket = self._buckets.setdefault(ip, RateLimiter(self.per_minute, burst=self.burst))
        # timeout=0: don't wait; no token available -> reject straight away with 429.
        if not bucket.acquire(timeout=0):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "slow down: submit rate limit exceeded")
