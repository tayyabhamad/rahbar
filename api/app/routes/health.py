"""Liveness and readiness (section 4.3). They answer different questions.

/healthz: is the process alive? It touches nothing. If it depended on Chroma, one
slow Chroma would get every API pod killed and restarted at once.
/readyz: can this pod take traffic? 200 only when Chroma is reachable, the
collection is non-empty, the embedding pin matches and Redis answers; otherwise
503 naming the failed dependency, and the pod is withheld from the Service.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ..services.readiness import check_readiness

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Is the process alive? Must never touch Chroma, Redis or the LLM."""
    return {"status": "alive"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    state = request.app.state
    # check_readiness: Chroma heartbeat, collection.count() > 0, embedding pin, Redis PING.
    result = check_readiness(state.settings, state.retriever, state.cache,
                             getattr(state, "expected_dimension", None))
    if result.ready:
        cache = state.cache
        if cache is not None and state.retriever.name != "stub":
            generation = state.retriever.index_generation()
            if generation not in ("unavailable", "none") and \
                    getattr(state, "last_generation", None) not in (None, generation):
                cache.invalidate_other(generation)
            state.last_generation = generation
    else:
        response.status_code = 503
    return result.body()
