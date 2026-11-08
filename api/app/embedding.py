"""The question embedder (section 3.2, step 3): the same model that indexed the chunks.

EMBED_MODEL comes from the ConfigMap, and EMBED_PROVIDER says where that model runs:

* `local-hash-v1` as the model name: computed here with no network (CI).
* `EMBED_PROVIDER=ollama` (default): Ollama's `POST /api/embed` at OLLAMA_URL.
* `EMBED_PROVIDER=hosted`: an OpenAI-compatible `POST {HOSTED_BASE_URL}/embeddings`
  with the LLM_API_KEY from the Secret, e.g. Google AI Studio's
  `gemini-embedding-001`. Free tiers rate-limit, so 429 and 5xx are retried.

The indexer's embed stage follows the same rules (indexer/pipeline/embed.py), so the
two sides cannot pick different models by accident: the only way to disagree is to
change the ConfigMap, and section 4.4's readiness check catches that.
"""

from __future__ import annotations

import logging
import time

import httpx

from .config import Settings
from .embedding_local import LOCAL_HASH_DIM, is_local_hash, local_hash_embed

log = logging.getLogger("rahbar.embedding")

KNOWN_DIMENSIONS = {"nomic-embed-text": 768, "mxbai-embed-large": 1024, "all-minilm": 384,
                    "gemini-embedding-001": 3072, "text-embedding-004": 768, "baai/bge-m3": 1024,
                    "openai/text-embedding-3-small": 1536}
RETRIES = 4


class EmbeddingError(RuntimeError):
    pass


def retry_wait(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429/5xx: Retry-After when given, else backoff."""
    header = response.headers.get("retry-after")
    if header and header.isdigit():
        return min(float(header), 60.0)
    return min(2.0 * 2 ** (attempt - 1), 30.0)


class Embedder:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.embed_model
        self.provider = "local-hash" if is_local_hash(self.model) else settings.embed_provider
        self.ollama_url = settings.ollama_url.rstrip("/")
        self.hosted_url = settings.hosted_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.timeout = settings.embed_timeout_seconds
        self.local = self.provider == "local-hash"
        self.dimension: int | None = (
            LOCAL_HASH_DIM if self.local else KNOWN_DIMENSIONS.get(self.model)
        )
        if self.provider == "hosted" and not self.hosted_url:
            raise ValueError("EMBED_PROVIDER=hosted needs HOSTED_BASE_URL in the ConfigMap")

    def _request(self, texts: list[str]) -> tuple[str, dict[str, object], dict[str, str]]:
        if self.provider == "hosted":
            return (f"{self.hosted_url}/embeddings", {"model": self.model, "input": texts},
                    {"Authorization": f"Bearer {self.api_key}"})
        return f"{self.ollama_url}/api/embed", {"model": self.model, "input": texts}, {}

    @staticmethod
    def _vectors(provider: str, body: dict[str, object]) -> list[list[float]]:
        if provider == "hosted":
            data = body.get("data")
            if not isinstance(data, list):
                raise EmbeddingError("embeddings response has no data list")
            rows = sorted(data, key=lambda d: int(d.get("index", 0)))
            return [[float(x) for x in row["embedding"]] for row in rows]
        vectors = body.get("embeddings")
        if not isinstance(vectors, list):
            raise EmbeddingError("embeddings response has no embeddings list")
        return [[float(x) for x in v] for v in vectors]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.local:
            return local_hash_embed(texts)
        url, payload, headers = self._request(texts)
        last = ""
        for attempt in range(1, RETRIES + 1):
            try:
                with httpx.Client(timeout=self.timeout, headers=headers) as client:
                    response = client.post(url, json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    wait = retry_wait(response, attempt)
                    last = f"HTTP {response.status_code}"
                    log.warning("embedding call rate-limited or failed; retrying",
                                extra={"status": response.status_code, "wait": wait,
                                       "attempt": attempt})
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                out = self._vectors(self.provider, response.json())
            except httpx.HTTPError as exc:
                raise EmbeddingError(f"embedding call to {url} failed: {exc}") from exc
            if len(out) != len(texts):
                raise EmbeddingError("embedding API returned the wrong number of vectors")
            if self.dimension is None and out:
                self.dimension = len(out[0])
            return out
        raise EmbeddingError(f"embedding call to {url} failed after {RETRIES} attempts: {last}")

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
