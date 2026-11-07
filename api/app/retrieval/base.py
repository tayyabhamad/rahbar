"""The retriever interface. The rest of the API only ever sees this."""

from __future__ import annotations

from typing import Any, Protocol

from ..schemas import Passage


class Retriever(Protocol):
    name: str

    def embed_question(self, question: str) -> list[float]:
        """The question's vector, from the pinned embedding model."""
        ...

    def retrieve(
        self,
        question: str,
        k: int,
        *,
        include_superseded: bool = False,
        vector: list[float] | None = None,
    ) -> list[Passage]:
        """Return up to k passages, best first. Superseded documents are excluded unless
        the caller asks for them (section 3.2, step 4)."""
        ...

    def collection_info(self) -> dict[str, Any]:
        """{"count": int, "metadata": {...}} for the readiness and pin checks (4.3, 4.4).
        Raises when the store is unreachable or the collection does not exist."""
        ...

    def index_generation(self) -> str:
        """A stamp that changes on every re-index; the cache namespaces entries by it."""
        ...
