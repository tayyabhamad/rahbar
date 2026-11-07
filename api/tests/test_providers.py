"""The real providers over a mocked HTTP transport: enforce, validate, retry once, fail."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import make_passage, settings_with

from app.providers.llm.common import GenerationError
from app.providers.llm.hosted import HostedProvider
from app.providers.llm.ollama import OllamaProvider

P = make_passage("c-001", "1.21 A student must attend at least 80% of classes.", 0.8)
VALID = json.dumps({"reasoning": "passage 1", "answer": "At least 80%.",
                    "citations": [{"source": P.source, "section": P.section, "page": P.page}],
                    "answered_from_context": True, "confidence": "high"})


def _mock_client(monkeypatch: pytest.MonkeyPatch, replies: list[Any]) -> list[dict[str, Any]]:
    """Patch httpx.Client so each POST pops the next reply; return the captured bodies."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json=reply)

    real_client = httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return bodies


def _ollama(content: str) -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": content},
            "prompt_eval_count": 120, "eval_count": 40}


def _openai(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30}}


def test_ollama_enforces_schema_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _mock_client(monkeypatch, [_ollama(VALID)])
    answer = OllamaProvider(settings_with(temperature=0.1)).generate("attendance?", [P])
    assert answer.answer == "At least 80%."
    assert bodies[0]["format"]["properties"]["confidence"]["enum"] == ["high", "medium", "low"]
    assert bodies[0]["options"]["temperature"] == 0.1
    assert bodies[0]["messages"][1]["content"].index("<evidence") < \
        bodies[0]["messages"][1]["content"].index("Question:")


def test_retry_once_then_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _mock_client(monkeypatch, [_ollama("Attendance is 80 percent."), _ollama(VALID)])
    answer = OllamaProvider(settings_with()).generate("attendance?", [P])
    assert answer.answered_from_context is True
    assert len(bodies) == 2
    assert "rejected" in bodies[1]["messages"][1]["content"]


def test_two_bad_replies_raise_generation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_client(monkeypatch, [_ollama("prose"), _ollama("{not json")])
    with pytest.raises(GenerationError):
        OllamaProvider(settings_with()).generate("attendance?", [P])


def test_timeout_becomes_generation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_client(monkeypatch, [httpx.ReadTimeout("slow model")])
    with pytest.raises(GenerationError, match="timed out"):
        OllamaProvider(settings_with()).generate("attendance?", [P])


def test_hosted_uses_json_mode_and_bearer_key(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _mock_client(monkeypatch, [_openai(VALID)])
    provider = HostedProvider(settings_with(hosted_base_url="https://api.example/v1",
                                            llm_api_key="k", llm_model="llama-3.1-8b"))
    answer = provider.generate("attendance?", [P])
    assert answer.citations[0].section == P.section
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert bodies[0]["model"] == "llama-3.1-8b"


def test_hosted_requires_base_url() -> None:
    with pytest.raises(ValueError):
        HostedProvider(settings_with(hosted_base_url=""))
