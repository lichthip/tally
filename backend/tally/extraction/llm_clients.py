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


class LLMError(Exception):
    pass


class LLMTransientError(LLMError):
    pass


class LLMRateLimited(LLMTransientError):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class LLMBadOutput(LLMError):
    pass


class LLMPermanentError(LLMError):
    pass


def _classify(resp: httpx.Response, provider: str) -> None:
    if resp.status_code < 400:
        return
    body = resp.text[:300]
    if resp.status_code == 429:
        ra = resp.headers.get("retry-after")
        raise LLMRateLimited(f"{provider} 429: {body}", float(ra) if ra and ra.isdigit() else None)
    if resp.status_code >= 500 or resp.status_code == 408:
        raise LLMTransientError(f"{provider} {resp.status_code}: {body}")
    raise LLMPermanentError(f"{provider} {resp.status_code}: {body}")


def parse_json_text(raw: str) -> dict[str, Any]:
    """Models sometimes wrap JSON in ``` fences even in JSON mode; tolerate that."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise LLMBadOutput(f"model returned non-JSON: {raw[:200]!r}") from e
    if not isinstance(data, dict):
        raise LLMBadOutput("model returned JSON that is not an object")
    return data


class LLMClient(ABC):
    provider: str
    model: str

    @property
    def model_id(self) -> str:
        return f"{self.provider}:{self.model}"

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
        gen_cfg: dict[str, Any] = {"temperature": 0, "responseMimeType": "application/json"}
        if gemini_schema:
            gen_cfg["responseSchema"] = gemini_schema    # constrained decoding to our schema
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen_cfg,
        }
        try:
            resp = self._http.post(f"{self.BASE}/models/{self.model}:generateContent",
                                   json=body, headers={"x-goog-api-key": self.api_key})
        except httpx.TimeoutException as e:
            raise LLMTransientError(f"gemini timeout: {e}") from e
        except httpx.HTTPError as e:
            raise LLMTransientError(f"gemini transport error: {e}") from e
        _classify(resp, "gemini")
        data = resp.json()
        if data.get("promptFeedback", {}).get("blockReason"):
            raise LLMBadOutput(f"gemini blocked prompt: {data['promptFeedback']['blockReason']}")
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError) as e:
            raise LLMBadOutput(f"gemini returned no content: {str(data)[:200]}") from e
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


class OpenAICompatClient(LLMClient):
    """Any OpenAI-compatible /chat/completions endpoint: Groq, OpenRouter, Cerebras..."""

    def __init__(self, provider: str, base_url: str, api_key: str, model: str,
                 timeout_s: float = 30.0, extra_headers: dict[str, str] | None = None):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.model = model
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


class RateLimiter:
    """Thread-safe token bucket.

    Free LLM tiers allow only a handful of requests per minute. Workers acquire a token
    BEFORE claiming a job, so they never sit on a job lease while waiting for quota.
    (Per-process only; a fleet would need a shared limiter, e.g. in Redis.)
    """

    def __init__(self, rate_per_minute: float, burst: int = 1):
        self.rate = rate_per_minute / 60.0
        self.capacity = float(max(1, burst))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def acquire(self, timeout: float | None = None, stop: threading.Event | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
                wait = (1 - self.tokens) / self.rate
            if deadline is not None:
                wait = min(wait, deadline - time.monotonic())
                if wait <= 0:
                    return False
            if stop is not None:
                if stop.wait(min(wait, 1.0)):
                    return False
            else:
                time.sleep(min(wait, 1.0))

    def refund(self) -> None:
        with self._lock:
            self.tokens = min(self.capacity, self.tokens + 1)
