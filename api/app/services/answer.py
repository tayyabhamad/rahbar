"""Turns a question into an AskResponse: the query pipeline, section 3.2, steps 2 to 8.

    1. validate         AskRequest (3..500 chars) did it before we got here
    2. cache check      skipped for follow-up questions
    3. embed            the same EMBED_MODEL the indexer used
    4. retrieve         top_k from Chroma, superseded excluded unless asked for
    5. floor            best similarity below RELEVANCE_FLOOR: abstain, honestly
    6. augment+generate the provider builds the prompt and enforces the schema
    7. validate         parse, retry once (inside the provider), else fall back;
                        then drop every citation that names nothing we retrieved
    8. store            the cache entry, only when it is safe to cache

Each step lives in its own module so it can be unit-tested; this file only
orders them and writes the one log line per request that section 4.8 asks for.
"""

from __future__ import annotations

import logging
import time

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..providers.llm.common import GenerationError
from ..retrieval.base import Retriever
from ..routes import metrics
from ..schemas import Answer, AskResponse, Passage, RetrievedRef
from .cache import SemanticCache
from .guards import asks_for_older_version, is_followup
from .validate import abstention_answer, fallback_answer, filter_citations

log = logging.getLogger("rahbar.answer")


class Pipeline:
    """Holds the generation stamp between requests so cache invalidation is one compare."""

    def __init__(self) -> None:
        self.last_generation: str | None = None

    def note_generation(self, generation: str, cache: SemanticCache | None) -> None:
        if cache is not None and generation not in ("unavailable", "none") \
                and self.last_generation not in (None, generation):
            cache.invalidate_other(generation)
        if generation not in ("unavailable", "none"):
            self.last_generation = generation


_pipeline = Pipeline()


def answer_question(
    question: str,
    retriever: Retriever,
    provider: LLMProvider,
    settings: Settings,
    cache: SemanticCache | None = None,
) -> AskResponse:
    rid = request_id_var.get()
    followup = is_followup(question)
    older = asks_for_older_version(question)
    generation = retriever.index_generation()
    _pipeline.note_generation(generation, cache)

    # 2 + 3. Embed once; the vector serves the cache lookup and the retrieval.
    retrieval_started = time.perf_counter()
    vector = retriever.embed_question(question)
    if cache is not None and not followup:
        hit = cache.lookup(vector, generation)
        if hit is not None:
            cached_response, similarity = hit
            response = AskResponse(**{**cached_response, "request_id": rid, "cached": True})
            log.info("answered from cache", extra={"similarity": round(similarity, 4),
                                                   "generation": generation})
            return response
    elif cache is not None:
        metrics.CACHE_LOOKUPS.labels(result="skipped").inc()

    # 4. Retrieve.
    passages = retriever.retrieve(question, settings.top_k, include_superseded=older,
                                  vector=vector)
    metrics.RETRIEVAL_LATENCY.observe(time.perf_counter() - retrieval_started)
    best = max((p.score for p in passages), default=0.0)

    # 5. Relevance floor.
    fallback = False
    dropped = 0
    abstain_reason: str | None = None
    if not passages or best < settings.relevance_floor:
        # Abstain, but still report what came closest: the eval scores retrieval and
        # abstention separately, and the log line needs the scores to diagnose stage C.
        answer = abstention_answer(f"best similarity {best:.3f} is below the floor "
                                   f"{settings.relevance_floor}")
        abstain_reason = "floor"
    else:
        # 6 + 7. Generate, validate, retry once, else fall back.
        generation_started = time.perf_counter()
        try:
            answer = provider.generate(question, passages, older_version_requested=older)
        except GenerationError as exc:
            metrics.FALLBACKS.inc()
            fallback = True
            answer = fallback_answer(str(exc))
            log.error("generation failed twice; returning the fallback",
                      extra={"error": str(exc)})
        metrics.GENERATION_LATENCY.observe(time.perf_counter() - generation_started)
        answer, dropped, abstain_reason = _ground(answer, passages)

    if abstain_reason:
        metrics.ABSTENTIONS.labels(reason=abstain_reason).inc()

    response = AskResponse(
        request_id=rid,
        answer=answer.answer,
        citations=answer.citations,
        answered_from_context=answer.answered_from_context,
        confidence=answer.confidence,
        retrieved=[RetrievedRef(chunk_id=p.chunk_id, source=p.source, section=p.section,
                                page=p.page, score=p.score) for p in passages],
        provider=provider.name,
    )

    # 8. Store, only when it is safe to serve this answer to everyone.
    cacheable = (cache is not None and not followup and not fallback
                 and response.answered_from_context and bool(response.citations))
    if cacheable and cache is not None:
        cache.store(question, vector, response.model_dump(), generation)

    log.info(
        "answered",
        extra={
            "retriever": retriever.name,
            "provider": provider.name,
            "top_k": settings.top_k,
            "chunk_ids": [p.chunk_id for p in passages],
            "scores": [round(p.score, 4) for p in passages],
            "best_score": round(best, 4),
            "relevance_floor": settings.relevance_floor,
            "answered_from_context": response.answered_from_context,
            "abstention": not response.answered_from_context,
            "abstention_reason": abstain_reason,
            "fallback": fallback,
            "citations_dropped": dropped,
            "followup": followup,
            "older_version": older,
            "cached_write": cacheable,
            "generation": generation,
        },
    )
    return response


def _ground(answer: Answer, passages: list[Passage]) -> tuple[Answer, int, str | None]:
    """Keep only citations that name a retrieved passage; demote an uncited answer."""
    kept, dropped = filter_citations(answer.citations, passages)
    reason: str | None = None
    if answer.answered_from_context and not kept:
        reason = "no_valid_citation"
        answer = Answer(
            reasoning=answer.reasoning + " [demoted: no citation matched a retrieved passage]",
            answer=("I found related passages but could not ground an answer in them with a "
                    "valid citation, so I am not treating this as answered from the corpus. "
                    "Draft answer, unverified: " + answer.answer),
            citations=[],
            answered_from_context=False,
            confidence="low",
        )
    elif not answer.answered_from_context:
        reason = "model"
        answer = answer.model_copy(update={"citations": kept, "confidence": "low"})
    else:
        answer = answer.model_copy(update={"citations": kept})
    return answer, dropped, reason

