"""EMBED_PROVIDER=hosted: OpenAI-compatible /embeddings with the Secret's key, 429 retried."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import settings_with

from app.embedding import Embedder, EmbeddingError


def _mock(monkeypatch: pytest.MonkeyPatch, replies: list[Any]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    real = httpx.Client

    def fake(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake)
    monkeypatch.setattr("app.embedding.time.sleep", lambda s: None)
    return seen


def _hosted() -> Embedder:
    return Embedder(settings_with(embed_model="gemini-embedding-001", embed_provider="hosted",
                                  hosted_base_url="https://llm.example/v1beta/openai",
                                  llm_api_key="k"))


def test_hosted_embeddings_call_the_openai_endpoint_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock(monkeypatch, [{"data": [{"index": 1, "embedding": [0.0, 1.0]},
                                         {"index": 0, "embedding": [1.0, 0.0]}]}])
    vectors = _hosted().embed(["a", "b"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]], "rows are re-ordered by index"
    assert seen[0].url.path.endswith("/embeddings")
    assert seen[0].headers["authorization"] == "Bearer k"
    assert json.loads(seen[0].content)["model"] == "gemini-embedding-001"


def test_rate_limit_is_retried_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock(monkeypatch, [httpx.Response(429, headers={"retry-after": "1"}),
                               {"data": [{"index": 0, "embedding": [0.5]}]}])
    assert _hosted().embed(["a"]) == [[0.5]]
    assert len(seen) == 2


def test_persistent_rate_limit_becomes_an_embedding_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(monkeypatch, [httpx.Response(429)] * 4)
    with pytest.raises(EmbeddingError, match="HTTP 429"):
        _hosted().embed(["a"])


def test_ollama_remains_the_default_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock(monkeypatch, [{"embeddings": [[0.1, 0.2, 0.3]]}])
    e = Embedder(settings_with(embed_model="nomic-embed-text"))
    assert e.provider == "ollama"
    assert e.embed(["q"]) == [[0.1, 0.2, 0.3]]
    assert seen[0].url.path == "/api/embed"


def test_hosted_requires_a_base_url() -> None:
    with pytest.raises(ValueError):
        Embedder(settings_with(embed_model="gemini-embedding-001", embed_provider="hosted"))
