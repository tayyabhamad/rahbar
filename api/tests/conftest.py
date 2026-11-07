"""Shared fixtures. Every test runs with stubs: no cluster, no model, no network."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.schemas import Answer, Citation, Passage


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVER", "stub")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("REDIS_HOST", "")          # cache disabled unless a test enables it
    monkeypatch.setenv("EMBED_MODEL", "local-hash-v1")


def make_passage(chunk_id: str, text: str, score: float, source: str = "rules-2026.pdf",
                 section: str = "1.21 Attendance", page: int = 4) -> Passage:
    return Passage(chunk_id=chunk_id, text=text, source=source, section=section, page=page,
                   score=score)


class FakeRetriever:
    """Returns whatever passages the test configured; records how it was called."""

    name = "chroma"

    def __init__(self, passages: list[Passage], generation: str = "gen-1") -> None:
        self.passages = passages
        self.generation = generation
        self.calls: list[dict[str, Any]] = []

    def embed_question(self, question: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    def retrieve(self, question: str, k: int, *, include_superseded: bool = False,
                 vector: list[float] | None = None) -> list[Passage]:
        self.calls.append({"question": question, "k": k, "include_superseded": include_superseded,
                           "vector": vector})
        return [p.model_copy() for p in self.passages[:k]]

    def collection_info(self) -> dict[str, Any]:
        return {"count": len(self.passages),
                "metadata": {"embed_model": "local-hash-v1", "dimension": 512,
                             "index_generation": self.generation}}

    def index_generation(self) -> str:
        return self.generation

    def heartbeat(self) -> None:
        return None


class ScriptedProvider:
    """Returns the Answers it was given, in order, or raises what it was given."""

    name = "scripted"

    def __init__(self, *answers: Answer | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def generate(self, question: str, passages: list[Passage],
                 older_version_requested: bool = False) -> Answer:
        self.calls.append({"question": question, "passages": passages,
                           "older": older_version_requested})
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeRedis:
    """The five Redis calls the cache uses, over a dict."""

    def __init__(self, alive: bool = True) -> None:
        self.data: dict[str, str] = {}
        self.alive = alive

    def ping(self) -> bool:
        if not self.alive:
            raise ConnectionError("redis down")
        return True

    def scan_iter(self, match: str, count: int = 100):  # type: ignore[no-untyped-def]
        prefix = match.rstrip("*")
        return iter([k for k in list(self.data) if k.startswith(prefix)])

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.data.get(k) for k in keys]

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.data[key] = value
        return True

    def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            n += self.data.pop(k, None) is not None
        return n


def good_answer(passage: Passage, text: str = "You need 80% attendance.") -> Answer:
    return Answer(reasoning="passage 1 states it", answer=text,
                  citations=[Citation(source=passage.source, section=passage.section,
                                      page=passage.page)],
                  answered_from_context=True, confidence="high")


def settings_with(**overrides: Any) -> Settings:
    base = {"retriever": "chroma", "llm_provider": "stub", "embed_model": "local-hash-v1",
            "redis_host": "", "relevance_floor": 0.45, "cache_threshold": 0.9, "top_k": 3}
    base.update(overrides)
    return Settings(**base)
