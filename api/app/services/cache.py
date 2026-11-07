"""The semantic cache on Redis (section 3.3, ADR 0003).

An entry is the question's vector plus the serialised AskResponse, stored under
`rahbar:cache:<index_generation>:<uuid>` with a TTL. A lookup scans the current
generation's keys and returns the entry whose vector has the highest cosine
similarity to the new question, if that similarity is at or above CACHE_THRESHOLD.

Invalidation on re-index needs no coordination with the indexer: the indexer stamps
a new `index_generation` into the collection metadata, the API namespaces keys by
it, so entries from the previous index can never hit again, and `invalidate_other`
deletes them as soon as the API notices the change.

Rules enforced by the caller (services/answer.py): never cache follow-ups,
abstentions, out-of-scope answers or anything that fell back after validation.
Any Redis failure is logged and treated as a miss; the cache must never make a
request fail. /readyz still reports Redis as a dependency.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from typing import Any

from ..config import Settings
from ..routes import metrics

log = logging.getLogger("rahbar.cache")

PREFIX = "rahbar:cache"


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class SemanticCache:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.threshold = settings.cache_threshold
        self.ttl = settings.cache_ttl_seconds
        if client is None:
            import redis

            client = redis.Redis(host=settings.redis_host, port=settings.redis_port,
                                 socket_timeout=1.0, socket_connect_timeout=1.0,
                                 decode_responses=True)
        self.client: Any = client

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def _keys(self, pattern: str) -> list[str]:
        return [str(k) for k in self.client.scan_iter(match=pattern, count=500)]

    def lookup(self, vector: list[float], generation: str) -> tuple[dict[str, Any], float] | None:
        """The best entry at or above the threshold, with its similarity, else None."""
        try:
            keys = self._keys(f"{PREFIX}:{generation}:*")
            if not keys:
                metrics.CACHE_LOOKUPS.labels(result="miss").inc()
                return None
            best: tuple[dict[str, Any], float] | None = None
            for raw in self.client.mget(keys):
                if not raw:
                    continue
                entry = json.loads(raw)
                score = cosine(vector, entry.get("vector") or [])
                if best is None or score > best[1]:
                    best = (entry, score)
            if best is not None and best[1] >= self.threshold:
                metrics.CACHE_LOOKUPS.labels(result="hit").inc()
                return best[0]["response"], best[1]
            metrics.CACHE_LOOKUPS.labels(result="miss").inc()
            return None
        except Exception as exc:
            metrics.CACHE_LOOKUPS.labels(result="error").inc()
            log.warning("cache lookup failed; treating as a miss", extra={"error": str(exc)})
            return None

    def store(self, question: str, vector: list[float], response: dict[str, Any],
              generation: str) -> bool:
        key = f"{PREFIX}:{generation}:{uuid.uuid4().hex}"
        payload = json.dumps({"question": question, "vector": vector, "response": response})
        try:
            self.client.set(key, payload, ex=self.ttl)
            metrics.CACHE_WRITES.inc()
            return True
        except Exception as exc:
            log.warning("cache write failed", extra={"error": str(exc)})
            return False

    def invalidate_other(self, generation: str) -> int:
        """Delete every entry that belongs to a different index generation."""
        try:
            stale = [k for k in self._keys(f"{PREFIX}:*")
                     if k.split(":")[2] != generation]
            for start in range(0, len(stale), 500):
                self.client.delete(*stale[start:start + 500])
            if stale:
                log.info("cache invalidated after re-index",
                         extra={"deleted": len(stale), "generation": generation})
            return len(stale)
        except Exception as exc:
            log.warning("cache invalidation failed", extra={"error": str(exc)})
            return 0
