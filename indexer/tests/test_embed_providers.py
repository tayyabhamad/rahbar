"""The embed stage follows the same provider rules as the API: ollama, hosted, local-hash."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pipeline import embed as embed_mod
from pipeline.config import IndexerSettings


def _settings(**over: Any) -> IndexerSettings:
    base: dict[str, Any] = dict(embed_model="nomic-embed-text", chroma_host="c", chroma_port=1,
                                collection="rahbar", corpus_dir="/c", chunk_size=400,
                                chunk_overlap=50, ollama_url="http://ollama:11434")
    base.update(over)
    return IndexerSettings(**base)


def _mock(monkeypatch: pytest.MonkeyPatch, replies: list[Any]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = replies.pop(0)
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    real = httpx.Client

    def fake(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake)
    monkeypatch.setattr(embed_mod.time, "sleep", lambda s: None)
    return seen


def test_ollama_batches_go_to_api_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock(monkeypatch, [{"embeddings": [[1.0, 0.0], [0.0, 1.0]]}])
    assert embed_mod.embed(["a", "b"], _settings()) == [[1.0, 0.0], [0.0, 1.0]]
    assert seen[0].url.path == "/api/embed"


def test_hosted_uses_the_secret_and_retries_429(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock(monkeypatch, [httpx.Response(429, headers={"retry-after": "2"}),
                               {"data": [{"index": 0, "embedding": [0.2, 0.4]}]}])
    s = _settings(embed_model="gemini-embedding-001", embed_provider="hosted",
                  hosted_base_url="https://llm.example/v1beta/openai", llm_api_key="k")
    assert embed_mod.embed(["a"], s) == [[0.2, 0.4]]
    assert seen[0].url.path.endswith("/embeddings")
    assert seen[0].headers["authorization"] == "Bearer k"
    assert json.loads(seen[0].content) == {"model": "gemini-embedding-001", "input": ["a"]}
    assert len(seen) == 2


def test_hosted_without_base_url_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="HOSTED_BASE_URL"):
        embed_mod.embed(["a"], _settings(embed_provider="hosted"))


def test_mixed_dimensions_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(monkeypatch, [{"embeddings": [[1.0, 0.0], [0.0]]}])
    with pytest.raises(RuntimeError, match="dimension"):
        embed_mod.embed(["a", "b"], _settings())
