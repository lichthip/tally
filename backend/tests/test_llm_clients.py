"""LLM HTTP clients against a mocked transport: request shape, parsing, error taxonomy."""
import json

import httpx
import pytest

from tally.extraction.llm_clients import (
    GeminiClient,
    LLMBadOutput,
    LLMPermanentError,
    LLMRateLimited,
    LLMTransientError,
    OpenAICompatClient,
)


def gemini_with(handler):
    c = GeminiClient("key-123", "gemini-flash-latest")
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_gemini_request_and_response():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["key"] = req.headers["x-goog-api-key"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": '{"is_trade_idea": false, "reason": "q", "ideas": []}'}]}}]})

    out = gemini_with(handler).complete_json("sys", "user", {"type": "OBJECT"})
    assert out["is_trade_idea"] is False
    assert seen["url"].endswith("/models/gemini-flash-latest:generateContent")
    assert seen["key"] == "key-123"                                   # key in header, not URL
    cfg = seen["body"]["generationConfig"]
    assert cfg["responseMimeType"] == "application/json" and cfg["temperature"] == 0
    assert cfg["responseSchema"] == {"type": "OBJECT"}


@pytest.mark.parametrize("status,exc", [(429, LLMRateLimited), (503, LLMTransientError),
                                        (400, LLMPermanentError), (401, LLMPermanentError)])
def test_gemini_error_taxonomy(status, exc):
    c = gemini_with(lambda req: httpx.Response(status, headers={"retry-after": "7"}, text="nope"))
    with pytest.raises(exc) as info:
        c.complete_json("s", "u")
    if status == 429:
        assert info.value.retry_after == 7


def test_gemini_blocked_prompt_is_bad_output():
    c = gemini_with(lambda req: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(LLMBadOutput):
        c.complete_json("s", "u")


def test_timeout_is_transient():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(LLMTransientError):
        gemini_with(handler).complete_json("s", "u")


def test_openai_compatible_client():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"is_trade_idea": true, "ideas": []}\n```'}}]})

    c = OpenAICompatClient("groq", "https://api.groq.com/openai/v1", "gk", "llama-3.3-70b-versatile")
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert c.complete_json("sys", "usr") == {"is_trade_idea": True, "ideas": []}
    assert seen["auth"] == "Bearer gk"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert c.model_id == "groq:llama-3.3-70b-versatile"
