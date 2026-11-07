"""Stage 4: Embed. One vector per chunk, from the pinned model only.

The model name comes from EMBED_MODEL, the same ConfigMap key the API reads, and
EMBED_PROVIDER says where it runs (the API applies the same rules in
api/app/embedding.py):

* `local-hash-v1` as the model name: deterministic, dependency-free, no network (CI).
* `EMBED_PROVIDER=ollama` (default): Ollama's POST /api/embed at OLLAMA_URL.
* `EMBED_PROVIDER=hosted`: an OpenAI-compatible POST {HOSTED_BASE_URL}/embeddings with
  LLM_API_KEY, e.g. Google AI Studio's gemini-embedding-001. Free tiers rate-limit,
  so 429 and 5xx answers are retried with backoff (Retry-After when sent).

Nothing here can pick a different model: there is no fallback and no default other
than the ConfigMap's. Section 4.4 asks for the pin to be enforced in code; this file
is half of that (store.py records the model, the API checks). Chunks go up in batches
of EMBED_BATCH, and every vector's dimension is checked against the first one: a
mixed-dimension index is the silent failure the handout warns about, so it is an
error here, loudly, in the Job.
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

from .config import IndexerSettings
from .embedding_local import is_local_hash, local_hash_embed

EMBED_BATCH = int(os.environ.get("EMBED_BATCH", 32))
EMBED_TIMEOUT = float(os.environ.get("EMBED_TIMEOUT_SECONDS", 180))
EMBED_RETRIES = int(os.environ.get("EMBED_RETRIES", 5))


def _log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer.embed", "msg": msg, **fields}),
          file=sys.stdout, flush=True)


def _retry_wait(response: httpx.Response | None, attempt: int) -> float:
    header = response.headers.get("retry-after") if response is not None else None
    if header and header.isdigit():
        return min(float(header), 60.0)
    return min(2.0 * 2 ** (attempt - 1), 30.0)


def _parse(provider: str, body: dict[str, object], n: int) -> list[list[float]]:
    if provider == "hosted":
        data = body.get("data")
        if not isinstance(data, list):
            raise RuntimeError("embeddings response has no data list")
        rows = sorted(data, key=lambda d: int(d.get("index", 0)))
        vectors = [[float(x) for x in row["embedding"]] for row in rows]
    else:
        raw = body.get("embeddings")
        if not isinstance(raw, list):
            raise RuntimeError("embeddings response has no embeddings list")
        vectors = [[float(x) for x in v] for v in raw]
    if len(vectors) != n:
        raise RuntimeError(f"embedding API returned {len(vectors)} vectors for {n} inputs")
    return vectors


def embed_batch(client: httpx.Client, settings: IndexerSettings,
                texts: list[str]) -> list[list[float]]:
    provider = settings.embed_provider
    if provider == "hosted":
        url = f"{settings.hosted_base_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    else:
        url = f"{settings.ollama_url.rstrip('/')}/api/embed"
        headers = {}
    payload = {"model": settings.embed_model, "input": texts}
    last_error: Exception | str | None = None
    for attempt in range(1, EMBED_RETRIES + 1):
        response: httpx.Response | None = None
        try:
            response = client.post(url, json=payload, headers=headers)
            if response.status_code == 429 or response.status_code >= 500:
                raise RuntimeError(f"HTTP {response.status_code}")
            response.raise_for_status()
            return _parse(provider, response.json(), len(texts))
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            last_error = exc
            if attempt < EMBED_RETRIES:
                wait = _retry_wait(response, attempt)
                _log("embedding call failed; retrying", attempt=attempt, wait=wait,
                     error=str(exc))
                time.sleep(wait)
    raise RuntimeError(f"embedding failed after {EMBED_RETRIES} attempts: {last_error}")


def embed(texts: list[str], settings: IndexerSettings) -> list[list[float]]:
    """Embed with settings.embed_model and nothing else."""
    if not texts:
        return []
    vectors: list[list[float]] = []
    started = time.perf_counter()
    if is_local_hash(settings.embed_model):
        # CI's model: deterministic, dependency-free, identical code in the API
        # (docs/ARCHITECTURE.md). The pin check treats it like any other model name.
        vectors = local_hash_embed(texts)
        _log("embedding finished", vectors=len(vectors), dimension=len(vectors[0]),
             model=settings.embed_model, seconds=round(time.perf_counter() - started, 1))
        return vectors
    if settings.embed_provider == "hosted" and not settings.hosted_base_url:
        raise RuntimeError("EMBED_PROVIDER=hosted needs HOSTED_BASE_URL in the ConfigMap")
    with httpx.Client(timeout=EMBED_TIMEOUT) as client:
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start:start + EMBED_BATCH]
            vectors.extend(embed_batch(client, settings, batch))
            _log("embedded", done=len(vectors), total=len(texts), model=settings.embed_model,
                 provider=settings.embed_provider)
    dimension = len(vectors[0])
    bad = [i for i, v in enumerate(vectors) if len(v) != dimension]
    if bad:
        raise RuntimeError(f"{len(bad)} vectors do not have dimension {dimension} "
                           f"(first bad index {bad[0]}); refusing to write a mixed index")
    _log("embedding finished", vectors=len(vectors), dimension=dimension,
         seconds=round(time.perf_counter() - started, 1))
    return vectors
