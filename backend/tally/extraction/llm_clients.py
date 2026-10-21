# =============================================================================
# extraction/llm_clients.py: TALKING TO THE LLM PROVIDERS, AND THE RATE LIMITER
# -----------------------------------------------------------------------------
# Contents:
#   1. An ERROR FAMILY (taxonomy). Different failures need different reactions:
#        LLMRateLimited    "too many requests"  -> wait, retry
#        LLMTransientError server hiccup/timeout -> retry later
#        LLMBadOutput      model returned junk  -> human review, retrying won't help
#        LLMPermanentError bad API key / model  -> config problem, stop
#   2. LLMClient (abstract) + two implementations:
#        GeminiClient       Google's API format
#        OpenAICompatClient the OpenAI format, which Groq and OpenRouter both copy.
#                           One class serves both: only the URL and key differ.
#      This is what "provider-agnostic" means: the rest of the code only calls
#      client.complete_json(system, user) and never knows which company answered.
#   3. RateLimiter: a "token bucket" that keeps us under the free tier's limit.
#
# We call the providers' REST APIs directly with httpx (no vendor SDKs): fewer
# dependencies, and we control exactly which errors count as retryable.
# =============================================================================
"""Thin HTTP clients for free-tier LLM providers, plus a rate limiter.

We call the REST APIs directly with httpx instead of vendor SDKs: one less dependency per
provider, and the error handling (which status codes are retryable) is explicit and visible.

Error taxonomy drives what the worker does next:
  LLMRateLimited / LLMTransientError -> retry later (queue backoff)
  LLMBadOutput                       -> deterministic problem, send post to human review
  LLMPermanentError                  -> configuration problem (bad key/model); retries
                                        won't help, the job eventually dead-letters
"""
from __future__ import annotations

import json
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx


# --- The error family. Inheritance lets callers catch broad or narrow:
#     `except LLMError` catches all of them; `except LLMRateLimited` only that one.
#     LLMRateLimited is a KIND of LLMTransientError (both mean "try again later").
class LLMError(Exception):
    pass


class LLMTransientError(LLMError):
    pass


class LLMRateLimited(LLMTransientError):
    # Also remembers how long the provider asked us to wait (the Retry-After header).
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class LLMBadOutput(LLMError):
    pass


class LLMPermanentError(LLMError):
    pass


# Turn an HTTP error status into the right exception type. HTTP status codes:
#   2xx/3xx = fine, 429 = too many requests, 408 = timeout, 5xx = provider's fault,
#   other 4xx (401 bad key, 404 unknown model...) = our fault, won't fix itself.
def _classify(resp: httpx.Response, provider: str) -> None:
    if resp.status_code < 400:
        return                       # success: nothing to do
    body = resp.text[:300]           # first 300 chars of the error, for the message
    if resp.status_code == 429:
        ra = resp.headers.get("retry-after")
        raise LLMRateLimited(f"{provider} 429: {body}", float(ra) if ra and ra.isdigit() else None)
    if resp.status_code >= 500 or resp.status_code == 408:
        raise LLMTransientError(f"{provider} {resp.status_code}: {body}")
    raise LLMPermanentError(f"{provider} {resp.status_code}: {body}")


# Parse the model's text answer as JSON. Even in "JSON mode", models sometimes wrap the
# answer in ```json ... ``` fences, so we strip those first.
def parse_json_text(raw: str) -> dict[str, Any]:
    """Models sometimes wrap JSON in ``` fences even in JSON mode; tolerate that."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        # `from e` keeps the original error attached (useful when debugging).
        raise LLMBadOutput(f"model returned non-JSON: {raw[:200]!r}") from e
    if not isinstance(data, dict):
        raise LLMBadOutput("model returned JSON that is not an object")
    return data


# The interface every provider client implements.
class LLMClient(ABC):
    provider: str
    model: str

    # e.g. "groq:openai/gpt-oss-120b". Stored on every idea, and part of the cache key.
    @property
    def model_id(self) -> str:
        return f"{self.provider}:{self.model}"

    # Send instructions + post, get back a parsed JSON dict. gemini_schema is only used by Gemini.
    @abstractmethod
    def complete_json(self, system: str, user: str, gemini_schema: dict | None = None) -> dict[str, Any]:
        ...

    def list_models(self) -> list[str]:
        """Model ids this key can use (for `python -m tally check-llm`)."""
        return []


class GeminiClient(LLMClient):
    """Google AI Studio (Gemini API). Free tier available with an AI Studio key."""

    provider = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str, model: str, timeout_s: float = 30.0):
        self.api_key = api_key
        self.model = model
        self._http = httpx.Client(timeout=timeout_s)

    def complete_json(self, system: str, user: str, gemini_schema: dict | None = None) -> dict[str, Any]:
        # temperature 0 = as deterministic as possible (same input -> same output).
        gen_cfg: dict[str, Any] = {"temperature": 0, "responseMimeType": "application/json"}
        if gemini_schema:
            gen_cfg["responseSchema"] = gemini_schema    # constrained decoding to our schema
        # The request body in Gemini's format.
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen_cfg,
        }
        try:
            resp = self._http.post(f"{self.BASE}/models/{self.model}:generateContent",
                                   json=body, headers={"x-goog-api-key": self.api_key})
        # Network problems (timeout, connection dropped) are transient: worth retrying.
        except httpx.TimeoutException as e:
            raise LLMTransientError(f"gemini timeout: {e}") from e
        except httpx.HTTPError as e:
            raise LLMTransientError(f"gemini transport error: {e}") from e
        _classify(resp, "gemini")         # raises if the status code is an error
        data = resp.json()
        # Gemini's safety filter can refuse a prompt; treat that as bad output (-> review).
        if data.get("promptFeedback", {}).get("blockReason"):
            raise LLMBadOutput(f"gemini blocked prompt: {data['promptFeedback']['blockReason']}")
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError) as e:
            raise LLMBadOutput(f"gemini returned no content: {str(data)[:200]}") from e
        # Join the text parts, skipping any "thought" parts (the model's private reasoning).
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return parse_json_text(text)

    def list_models(self) -> list[str]:
        try:
            resp = self._http.get(f"{self.BASE}/models", headers={"x-goog-api-key": self.api_key})
        except httpx.HTTPError as e:
            raise LLMTransientError(f"gemini unreachable: {e}") from e
        _classify(resp, "gemini")
        return sorted(m["name"].removeprefix("models/") for m in resp.json().get("models", [])
                      if "generateContent" in m.get("supportedGenerationMethods", []))


# One class for every provider that copies OpenAI's API format (Groq, OpenRouter, ...).
class OpenAICompatClient(LLMClient):
    """Any OpenAI-compatible /chat/completions endpoint: Groq, OpenRouter, Cerebras..."""

    def __init__(self, provider: str, base_url: str, api_key: str, model: str,
                 timeout_s: float = 30.0, extra_headers: dict[str, str] | None = None):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.model = model
        # "Bearer <key>" is the standard way to send an API key. **(...) merges any
        # extra headers into the dict (OpenRouter likes an X-Title header).
        self._headers = {"Authorization": f"Bearer {api_key}", **(extra_headers or {})}
        self._http = httpx.Client(timeout=timeout_s)

    def complete_json(self, system: str, user: str, gemini_schema: dict | None = None) -> dict[str, Any]:
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},   # JSON mode; schema is in the prompt
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        try:
            resp = self._http.post(f"{self.base_url}/chat/completions", json=body,
                                   headers=self._headers)
        except httpx.TimeoutException as e:
            raise LLMTransientError(f"{self.provider} timeout: {e}") from e
        except httpx.HTTPError as e:
            raise LLMTransientError(f"{self.provider} transport error: {e}") from e
        _classify(resp, self.provider)
        try:
            # OpenAI format: {"choices": [{"message": {"content": "<the answer text>"}}]}
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMBadOutput(f"{self.provider} returned no content") from e
        return parse_json_text(content or "")

    def list_models(self) -> list[str]:
        try:
            resp = self._http.get(f"{self.base_url}/models", headers=self._headers)
        except httpx.HTTPError as e:
            raise LLMTransientError(f"{self.provider} unreachable: {e}") from e
        _classify(resp, self.provider)
        return sorted(m["id"] for m in resp.json().get("data", []))


# TOKEN BUCKET, explained: picture a bucket holding at most `capacity` tokens. Tokens
# drip in at `rate` per second (6/min = 0.1/s). Each LLM call needs one token. If the
# bucket is empty, you wait until one drips in. Result: never more than ~6 calls/minute.
class RateLimiter:
    """Thread-safe token bucket.

    Free LLM tiers allow only a handful of requests per minute. Workers acquire a token
    BEFORE claiming a job, so they never sit on a job lease while waiting for quota.
    (Per-process only; a fleet would need a shared limiter, e.g. in Redis.)
    """

    def __init__(self, rate_per_minute: float, burst: int = 1):
        self.rate = rate_per_minute / 60.0       # tokens per second
        self.capacity = float(max(1, burst))     # bucket size (burst=1: no bursts allowed)
        self.tokens = self.capacity              # start full
        self.updated = time.monotonic()          # last time we topped up
        self._lock = threading.Lock()            # several worker threads share one limiter

    # Add the tokens that "dripped in" since the last top-up (never above capacity).
    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    # Take one token, waiting if needed. Returns False if we gave up (timeout reached, or
    # the app is shutting down: `stop` is set).
    def acquire(self, timeout: float | None = None, stop: threading.Event | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
                wait = (1 - self.tokens) / self.rate    # seconds until the next whole token
            if deadline is not None:
                wait = min(wait, deadline - time.monotonic())
                if wait <= 0:
                    return False
            # Sleep in chunks of at most 1 s, so a shutdown signal is noticed quickly.
            if stop is not None:
                if stop.wait(min(wait, 1.0)):    # returns True if stop was set while waiting
                    return False
            else:
                time.sleep(min(wait, 1.0))

    # Give a token back (e.g. we took one but there was no job, so no LLM call was made).
    def refund(self) -> None:
        with self._lock:
            self.tokens = min(self.capacity, self.tokens + 1)
