"""Prometheus metrics at /metrics (section 4.8).

Request count and latency were given. The rest covers what section 4.8 asks for:
retrieval latency, generation latency, cache hits (hit ratio = hits / lookups),
abstentions, fallbacks, and tokens in and out, both as running totals and per
request.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

router = APIRouter()

REQUESTS = Counter("rahbar_requests_total", "HTTP requests", ["path", "status"])
LATENCY = Histogram(
    "rahbar_request_seconds",
    "HTTP request latency",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

_STAGE_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)
RETRIEVAL_LATENCY = Histogram("rahbar_retrieval_seconds",
                              "Embed the question and query the vector store",
                              buckets=_STAGE_BUCKETS)
GENERATION_LATENCY = Histogram("rahbar_generation_seconds",
                               "LLM call(s) for one question, including the retry",
                               buckets=_STAGE_BUCKETS)
CACHE_LOOKUPS = Counter("rahbar_cache_lookups_total",
                        "Semantic cache lookups by result; hit ratio = hit / (hit + miss)",
                        ["result"])   # hit | miss | skipped | error
CACHE_WRITES = Counter("rahbar_cache_writes_total", "Entries written to the semantic cache")
ABSTENTIONS = Counter("rahbar_abstentions_total",
                      "Answers with answered_from_context=false, by reason",
                      ["reason"])   # floor | model | no_valid_citation
FALLBACKS = Counter("rahbar_fallbacks_total",
                    "Canned responses returned because generation failed twice")
LLM_RETRIES = Counter("rahbar_llm_retries_total", "Second attempts after a validation failure")
TOKENS_IN = Counter("rahbar_tokens_in_total", "Prompt tokens sent to the model")
TOKENS_OUT = Counter("rahbar_tokens_out_total", "Completion tokens received from the model")
TOKENS_PER_REQUEST = Histogram("rahbar_tokens_per_request", "Tokens per model call",
                               ["direction"],
                               buckets=(64, 128, 256, 512, 1024, 2048, 4096, 8192))

for _result in ("hit", "miss", "skipped", "error"):
    CACHE_LOOKUPS.labels(result=_result)
for _reason in ("floor", "model", "no_valid_citation"):
    ABSTENTIONS.labels(reason=_reason)


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
