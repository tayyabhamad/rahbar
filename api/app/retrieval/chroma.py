"""The real retriever (sections 3.2 and 4.4). It reads the collection; it never writes.

Three things happen here beyond a plain vector query:

* **Metadata filter.** `where={"superseded": False}` unless the caller asks for an
  older version. This is what fixes failure mode E; a prompt cannot.
* **Hybrid exact-term pass.** Embeddings blur "1.21" and "CS2001". When the question
  carries an exact term, a second query restricted with `where_document={"$contains":
  term}` runs and its hits are merged in front of the semantic hits.
* **Collection metadata.** `collection_info()` exposes the indexer's `embed_model`,
  `dimension` and `index_generation` for the readiness pin check and the cache.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import Settings
from ..embedding import Embedder
from ..schemas import Passage
from ..services.guards import exact_terms

log = logging.getLogger("rahbar.retrieval")

_INFO_TTL_SECONDS = 15.0


class ChromaRetriever:
    name = "chroma"

    def __init__(self, settings: Settings, embedder: Embedder | None = None,
                 client: Any | None = None) -> None:
        self.settings = settings
        self.embedder = embedder or Embedder(settings)
        self._client: Any | None = client
        self._collection: Any | None = None
        self._info: dict[str, Any] | None = None
        self._info_at = 0.0

    # -- connection ---------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The Chroma client, created on first use. `chromadb.HttpClient` contacts the
        server when it is built, so building it in __init__ would crash the API process
        whenever Chroma is down; instead the process starts, /readyz reports Chroma
        unreachable, and the pod waits at 0/1 READY until Chroma is back."""
        if self._client is None:
            import chromadb  # the thin client, from chromadb-client in requirements.txt
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.HttpClient(
                host=self.settings.chroma_host, port=self.settings.chroma_port,
                settings=ChromaSettings(anonymized_telemetry=False))
        return self._client

    def heartbeat(self) -> None:
        self.client.heartbeat()

    def collection(self) -> Any:
        if self._collection is None:
            self._collection = self.client.get_collection(self.settings.collection)
        return self._collection

    def collection_info(self, max_age: float = _INFO_TTL_SECONDS) -> dict[str, Any]:
        now = time.monotonic()
        if self._info is None or now - self._info_at > max_age:
            try:
                collection = self.collection()
                self._info = {"count": int(collection.count()),
                              "metadata": dict(collection.metadata or {})}
            except Exception:
                self._collection = None     # force a fresh lookup next time
                raise
            self._info_at = now
        return self._info

    def index_generation(self) -> str:
        try:
            return str(self.collection_info()["metadata"].get("index_generation", "none"))
        except Exception:
            return "unavailable"

    # -- querying -----------------------------------------------------------------

    def embed_question(self, question: str) -> list[float]:
        return self.embedder.embed_one(question)

    def _query(self, vector: list[float], k: int, where: dict[str, Any] | None,
               where_document: dict[str, Any] | None = None) -> list[Passage]:
        kwargs: dict[str, Any] = {
            "query_embeddings": [vector],
            "n_results": k,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        if where_document:
            kwargs["where_document"] = where_document
        result = self.collection().query(**kwargs)
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        passages = []
        for chunk_id, text, meta, dist in zip(ids, docs, metas, dists):
            meta = meta or {}
            passages.append(Passage(
                chunk_id=str(chunk_id),
                text=text or "",
                source=str(meta.get("source", "")),
                section=str(meta.get("section", "")),
                page=int(meta["page"]) if meta.get("page") is not None else None,
                score=round(1.0 - float(dist), 6),   # cosine distance -> similarity
            ))
        return passages

    def retrieve(
        self,
        question: str,
        k: int,
        *,
        include_superseded: bool = False,
        vector: list[float] | None = None,
    ) -> list[Passage]:
        vector = vector or self.embed_question(question)
        filter_on = self.settings.superseded_filter and not include_superseded
        where = {"superseded": False} if filter_on else None
        semantic = self._query(vector, k, where)

        terms = exact_terms(question)
        if not terms:
            return semantic
        exact: list[Passage] = []
        for term in terms[:2]:
            try:
                exact.extend(self._query(vector, k, where, {"$contains": term}))
            except Exception as exc:  # an exact pass must never break the semantic one
                log.warning("exact-term query failed", extra={"term": term, "error": str(exc)})
        merged: list[Passage] = []
        seen: set[str] = set()
        for passage in exact + semantic:
            if passage.chunk_id in seen:
                continue
            seen.add(passage.chunk_id)
            merged.append(passage)
        log.info("hybrid retrieval", extra={"terms": terms[:2], "exact_hits": len(exact),
                                            "semantic_hits": len(semantic)})
        return merged[:k]
