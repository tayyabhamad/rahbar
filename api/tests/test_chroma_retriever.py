"""ChromaRetriever over a fake collection: the superseded filter and the hybrid pass."""

from __future__ import annotations

from typing import Any

from conftest import settings_with

from app.retrieval.chroma import ChromaRetriever

CHUNKS = [
    {"id": "h26-0001", "text": "1.21 Attendance: 80% of lectures is required.",
     "meta": {"source": "handbook-2026.pdf", "section": "1.21 Attendance", "page": 4,
              "doc_version": "2026-06", "superseded": False}, "dist": 0.30},
    {"id": "r23-0001", "text": "4.2 Attendance: 75% of lectures is required.",
     "meta": {"source": "rules-2023.pdf", "section": "4.2 Attendance", "page": 9,
              "doc_version": "2023-08", "superseded": True}, "dist": 0.28},
    {"id": "h26-0040", "text": "5.4 Academic warning below CGPA 2.00.",
     "meta": {"source": "handbook-2026.pdf", "section": "5.4 Academic Warning", "page": 12,
              "doc_version": "2026-06", "superseded": False}, "dist": 0.55},
]


class FakeCollection:
    metadata = {"embed_model": "local-hash-v1", "dimension": 512, "index_generation": "g1"}

    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []

    def count(self) -> int:
        return len(CHUNKS)

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        rows = CHUNKS
        where = kwargs.get("where") or {}
        if "superseded" in where:
            rows = [c for c in rows if c["meta"]["superseded"] == where["superseded"]]
        contains = (kwargs.get("where_document") or {}).get("$contains")
        if contains:
            rows = [c for c in rows if contains in c["text"]]
        rows = sorted(rows, key=lambda c: c["dist"])[: kwargs["n_results"]]
        return {"ids": [[c["id"] for c in rows]], "documents": [[c["text"] for c in rows]],
                "metadatas": [[c["meta"] for c in rows]], "distances": [[c["dist"] for c in rows]]}


class FakeClient:
    def __init__(self) -> None:
        self.collection = FakeCollection()

    def heartbeat(self) -> int:
        return 1

    def get_collection(self, name: str) -> FakeCollection:
        return self.collection


def _retriever() -> ChromaRetriever:
    return ChromaRetriever(settings_with(), client=FakeClient())


def test_superseded_documents_are_filtered_by_default() -> None:
    passages = _retriever().retrieve("attendance requirement", 5)
    assert [p.chunk_id for p in passages] == ["h26-0001", "h26-0040"]
    assert passages[0].score == 0.7 and passages[0].page == 4


def test_older_version_lifts_the_filter() -> None:
    passages = _retriever().retrieve("attendance requirement", 5, include_superseded=True)
    assert "r23-0001" in [p.chunk_id for p in passages]


def test_superseded_filter_can_be_switched_off_for_failure_analysis() -> None:
    r = ChromaRetriever(settings_with(superseded_filter=False), client=FakeClient())
    assert "r23-0001" in [p.chunk_id for p in r.retrieve("attendance requirement", 5)]


def test_exact_term_hits_come_first() -> None:
    r = _retriever()
    passages = r.retrieve("What does clause 5.4 say?", 2)
    assert passages[0].chunk_id == "h26-0040", "the $contains pass outranks the vector pass"
    assert any("where_document" in q for q in r.client.collection.queries)


def test_collection_info_and_generation() -> None:
    r = _retriever()
    assert r.collection_info()["count"] == 3
    assert r.index_generation() == "g1"
