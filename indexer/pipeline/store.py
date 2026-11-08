"""Stage 5: Store. The only code in the whole system that writes to Chroma.

What happens to the old index while the API serves from it (ADR 0001):

* Same embedding model and dimension as the existing collection: the new
  chunks are *upserted* in batches, then every id that the new run did not
  produce is deleted. The collection never disappears and never empties, so
  the three API replicas keep answering throughout; a query during the run
  sees a mix of old and new chunks for a few seconds, which is acceptable for
  a policy corpus and far better than a 503.
* Different model or dimension (the pin changed): the vectors are not
  comparable, so the collection is dropped and recreated. The API's readiness
  probe will already be failing on the pin mismatch, so nothing that was
  serving good answers is interrupted.

The collection metadata records the embedding model, its dimension, the chunk
settings and an `index_generation` stamp. The API compares the model and the
dimension against its own EMBED_MODEL (section 4.4) and uses the generation
stamp to invalidate its semantic cache (section 3.3): the indexer never
touches Redis, which keeps the NetworkPolicy "redis from api only".
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from .config import IndexerSettings
from .models import Chunk

STORE_BATCH = 100


def _log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer.store", "msg": msg, **fields}),
          file=sys.stdout, flush=True)


def get_client(settings: IndexerSettings) -> Any:
    import chromadb  # the thin client, from chromadb-client in requirements.txt
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port,
                               settings=ChromaSettings(anonymized_telemetry=False))


def collection_metadata(settings: IndexerSettings, dimension: int, count: int) -> dict[str, Any]:
    return {
        "hnsw:space": "cosine",
        "embed_model": settings.embed_model,
        "dimension": dimension,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "chunk_count": count,
        "index_generation": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
    }


def _existing_collection(client: Any, name: str) -> Any | None:
    try:
        return client.get_collection(name)
    except Exception:
        return None


def store(chunks: list[Chunk], vectors: list[list[float]], settings: IndexerSettings,
          client: Any | None = None) -> int:
    """Write every chunk with its vector and metadata, and record the embedding model
    and vector dimension in the collection metadata (section 4.4). Return the count."""
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    if not chunks:
        raise ValueError("refusing to write an empty index")
    dimension = len(vectors[0])
    client = client or get_client(settings)
    metadata = collection_metadata(settings, dimension, len(chunks))

    existing = _existing_collection(client, settings.collection)
    compatible = (
        existing is not None
        and (existing.metadata or {}).get("embed_model") == settings.embed_model
        and int((existing.metadata or {}).get("dimension", -1)) == dimension
    )

    collection: Any
    if compatible and existing is not None:
        collection = existing
        _log("upserting into the existing collection", collection=settings.collection,
             embed_model=settings.embed_model, dimension=dimension)
    else:
        if existing is not None:
            _log("embedding pin changed; recreating the collection",
                 old_model=(existing.metadata or {}).get("embed_model"),
                 old_dimension=(existing.metadata or {}).get("dimension"),
                 new_model=settings.embed_model, new_dimension=dimension)
            client.delete_collection(settings.collection)
        collection = client.create_collection(name=settings.collection, metadata=metadata)

    new_ids = [c.id for c in chunks]
    for start in range(0, len(chunks), STORE_BATCH):
        batch = chunks[start:start + STORE_BATCH]
        collection.upsert(
            ids=[c.id for c in batch],
            documents=[c.text for c in batch],
            embeddings=vectors[start:start + STORE_BATCH],
            metadatas=[c.metadata() for c in batch],
        )
        _log("stored", done=min(start + STORE_BATCH, len(chunks)), total=len(chunks))

    if compatible:
        stale = _stale_ids(collection, set(new_ids))
        if stale:
            for start in range(0, len(stale), STORE_BATCH):
                collection.delete(ids=stale[start:start + STORE_BATCH])
            _log("deleted stale chunks", count=len(stale))
        # Chroma refuses any modify() that carries an hnsw:* key, even unchanged, so the
        # distance setting (fixed at creation) is left out of the metadata refresh.
        collection.modify(metadata={k: v for k, v in metadata.items()
                                    if not k.startswith("hnsw:")})

    count = int(collection.count())
    _log("index written", collection=settings.collection, chunks=count,
         embed_model=settings.embed_model, dimension=dimension,
         index_generation=metadata["index_generation"])
    return count


def _stale_ids(collection: Any, keep: set[str]) -> list[str]:
    """Ids in the collection that this run did not write."""
    stale: list[str] = []
    offset = 0
    while True:
        page = collection.get(limit=1000, offset=offset, include=[])
        ids = page.get("ids") or []
        if not ids:
            break
        stale.extend(i for i in ids if i not in keep)
        offset += len(ids)
        if len(ids) < 1000:
            break
    return stale
