"""What /readyz checks (sections 4.3 and 4.4), kept out of the route so it is testable.

Ready means: Chroma answers its heartbeat, the collection exists and is non-empty,
the collection was built with this API's EMBED_MODEL (and vector dimension, when
known), and Redis answers PING. Anything else is 503 naming the failed dependency.
/healthz never calls this.

With RETRIEVER=stub there is no collection to check; the stub reports itself ready
so the week-1 cluster can come up, and Redis is still checked when configured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..retrieval.base import Retriever
from .cache import SemanticCache

log = logging.getLogger("rahbar.readiness")


@dataclass
class Readiness:
    ready: bool
    failed: str | None
    checks: dict[str, Any] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        return {"status": "ready" if self.ready else "not ready",
                "failed": self.failed, "checks": self.checks}


def check_pin(metadata: dict[str, Any], settings: Settings,
              expected_dimension: int | None) -> str | None:
    """None when the collection matches the pin; otherwise the conflict, in words."""
    stored_model = metadata.get("embed_model")
    if stored_model != settings.embed_model:
        return (f"collection was indexed with EMBED_MODEL={stored_model!r} but this API "
                f"is configured with EMBED_MODEL={settings.embed_model!r}")
    stored_dim = metadata.get("dimension")
    if expected_dimension is not None and stored_dim is not None \
            and int(stored_dim) != expected_dimension:
        return (f"collection vectors have dimension {stored_dim} but "
                f"{settings.embed_model} produces {expected_dimension}")
    return None


def check_readiness(settings: Settings, retriever: Retriever, cache: SemanticCache | None,
                    expected_dimension: int | None = None) -> Readiness:
    checks: dict[str, Any] = {}
    failed: str | None = None

    if retriever.name == "stub":
        checks["retriever"] = "stub (no collection to check)"
    else:
        try:
            heartbeat = getattr(retriever, "heartbeat", None)
            if heartbeat is not None:
                heartbeat()
            checks["chroma"] = "reachable"
        except Exception as exc:
            checks["chroma"] = f"unreachable: {exc}"
            failed = failed or "chroma"
        if failed is None:
            try:
                info = retriever.collection_info()
                count = int(info.get("count", 0))
                checks["collection"] = {"name": settings.collection, "count": count}
                if count <= 0:
                    checks["collection"]["error"] = "empty: run the indexer Job"
                    failed = "collection"
                else:
                    conflict = check_pin(info.get("metadata") or {}, settings,
                                         expected_dimension)
                    if conflict:
                        checks["embedding_pin"] = conflict
                        failed = "embedding_pin"
                        log.error("embedding pin mismatch", extra={
                            "collection_embed_model": info["metadata"].get("embed_model"),
                            "api_embed_model": settings.embed_model,
                            "collection_dimension": info["metadata"].get("dimension"),
                            "api_dimension": expected_dimension,
                        })
                    else:
                        checks["embedding_pin"] = f"ok: {settings.embed_model}"
            except Exception as exc:
                checks["collection"] = f"missing or unreadable: {exc}"
                failed = failed or "collection"

    if cache is not None:
        if cache.ping():
            checks["redis"] = "reachable"
        else:
            checks["redis"] = "unreachable"
            failed = failed or "redis"
    else:
        checks["redis"] = "disabled (REDIS_HOST empty)"

    return Readiness(ready=failed is None, failed=failed, checks=checks)
