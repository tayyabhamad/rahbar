"""Stage 4: Embed. One vector per chunk, from the pinned model only.

The model name comes from EMBED_MODEL, the same ConfigMap key the API reads.
Nothing here can pick a different model: there is no fallback and no default
other than the ConfigMap's. Section 4.4 asks for the pin to be enforced in
code; this file is half of that (store.py records the model, the API checks).

Ollama's POST /api/embed takes a list of inputs and returns a list of vectors,
so chunks go up in batches of EMBED_BATCH. Every vector's dimension is checked
against the first one: a mixed-dimension index is the silent failure the
handout warns about, so it is an error here, loudly, in the Job.
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
EMBED_RETRIES = int(os.environ.get("EMBED_RETRIES", 3))


def _log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer.embed", "msg": msg, **fields}),
          file=sys.stdout, flush=True)


def embed_batch(client: httpx.Client, url: str, model: str, texts: list[str]) -> list[list[float]]:
    last_error: Exception | None = None
    for attempt in range(1, EMBED_RETRIES + 1):
        try:
            response = client.post(f"{url.rstrip('/')}/api/embed",
                                   json={"model": model, "input": texts})
            response.raise_for_status()
            vectors = response.json().get("embeddings")
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise RuntimeError(f"embedding API returned {len(vectors or [])} vectors for "
                                   f"{len(texts)} inputs")
            return [[float(x) for x in v] for v in vectors]
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            last_error = exc
            if attempt < EMBED_RETRIES:
                wait = 2.0 * attempt
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
    with httpx.Client(timeout=EMBED_TIMEOUT) as client:
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start:start + EMBED_BATCH]
            vectors.extend(embed_batch(client, settings.ollama_url, settings.embed_model, batch))
            _log("embedded", done=len(vectors), total=len(texts), model=settings.embed_model)
    dimension = len(vectors[0])
    bad = [i for i, v in enumerate(vectors) if len(v) != dimension]
    if bad:
        raise RuntimeError(f"{len(bad)} vectors do not have dimension {dimension} "
                           f"(first bad index {bad[0]}); refusing to write a mixed index")
    _log("embedding finished", vectors=len(vectors), dimension=dimension,
         seconds=round(time.perf_counter() - started, 1))
    return vectors
