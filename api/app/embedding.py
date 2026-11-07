"""The question embedder (section 3.2, step 3): the same model that indexed the chunks.

EMBED_MODEL comes from the ConfigMap. `local-hash-v1` is computed here with no
network; any other name goes to Ollama's /api/embed. The indexer's embed stage
follows the same two rules, so the two sides cannot pick different models by
accident: the only way to disagree is to change the ConfigMap, and section 4.4's
readiness check catches that.
"""

from __future__ import annotations

import logging

import httpx

from .config import Settings
from .embedding_local import LOCAL_HASH_DIM, is_local_hash, local_hash_embed

log = logging.getLogger("rahbar.embedding")

KNOWN_DIMENSIONS = {"nomic-embed-text": 768, "mxbai-embed-large": 1024, "all-minilm": 384}


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.embed_model
        self.url = settings.ollama_url.rstrip("/")
        self.timeout = settings.embed_timeout_seconds
        self.local = is_local_hash(self.model)
        self.dimension: int | None = (
            LOCAL_HASH_DIM if self.local else KNOWN_DIMENSIONS.get(self.model)
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.local:
            return local_hash_embed(texts)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(f"{self.url}/api/embed",
                                       json={"model": self.model, "input": texts})
                response.raise_for_status()
                vectors = response.json().get("embeddings")
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding call to {self.url} failed: {exc}") from exc
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingError("embedding API returned the wrong number of vectors")
        out = [[float(x) for x in v] for v in vectors]
        if self.dimension is None and out:
            self.dimension = len(out[0])
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
