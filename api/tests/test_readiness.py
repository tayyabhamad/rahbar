"""Readiness and the embedding pin (sections 4.3 and 4.4)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeRedis, FakeRetriever, make_passage, settings_with
from fastapi.testclient import TestClient

from app.services.cache import SemanticCache
from app.services.readiness import check_pin, check_readiness

P = make_passage("c-1", "text", 0.9)


class BrokenChroma(FakeRetriever):
    def heartbeat(self) -> None:
        raise ConnectionError("connection refused")


class WrongPin(FakeRetriever):
    def collection_info(self) -> dict[str, Any]:
        return {"count": 3, "metadata": {"embed_model": "dummy-768", "dimension": 768,
                                         "index_generation": "g"}}


def test_ready_when_everything_matches() -> None:
    cache = SemanticCache(settings_with(), client=FakeRedis())
    result = check_readiness(settings_with(), FakeRetriever([P]), cache, 512)
    assert result.ready and result.failed is None
    assert result.checks["embedding_pin"].startswith("ok")


def test_not_ready_names_chroma_when_unreachable() -> None:
    result = check_readiness(settings_with(), BrokenChroma([P]), None)
    assert not result.ready and result.failed == "chroma"


def test_not_ready_when_collection_is_empty() -> None:
    result = check_readiness(settings_with(), FakeRetriever([]), None)
    assert not result.ready and result.failed == "collection"
    assert "run the indexer" in result.checks["collection"]["error"]


def test_not_ready_on_embedding_pin_mismatch() -> None:
    result = check_readiness(settings_with(embed_model="nomic-embed-text"), WrongPin([P]), None)
    assert not result.ready and result.failed == "embedding_pin"
    assert "dummy-768" in result.checks["embedding_pin"]
    assert "nomic-embed-text" in result.checks["embedding_pin"]


def test_pin_check_also_compares_dimension() -> None:
    s = settings_with(embed_model="nomic-embed-text")
    assert check_pin({"embed_model": "nomic-embed-text", "dimension": 768}, s, 768) is None
    assert "dimension" in (check_pin({"embed_model": "nomic-embed-text", "dimension": 384},
                                     s, 768) or "")


def test_not_ready_when_redis_is_down() -> None:
    cache = SemanticCache(settings_with(), client=FakeRedis(alive=False))
    result = check_readiness(settings_with(), FakeRetriever([P]), cache)
    assert not result.ready and result.failed == "redis"


def test_readyz_route_returns_503_with_the_failed_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200       # stub retriever, cache disabled
        client.app.state.retriever = WrongPin([P])             # type: ignore[attr-defined]
        client.app.state.settings = settings_with()            # type: ignore[attr-defined]
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["failed"] == "embedding_pin"
        assert client.get("/healthz").status_code == 200, "liveness never depends on Chroma"
