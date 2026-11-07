"""The local-hash embedder must be the same code in the indexer and the API (4.4)."""

from __future__ import annotations

from pathlib import Path

from pipeline.embedding_local import LOCAL_HASH_DIM, local_hash_embed_one

HERE = Path(__file__).resolve().parent


def test_indexer_and_api_copies_are_identical() -> None:
    ours = HERE.parent / "pipeline" / "embedding_local.py"
    theirs = HERE.parent.parent / "api" / "app" / "embedding_local.py"
    assert ours.read_text(encoding="utf-8") == theirs.read_text(encoding="utf-8"), (
        "indexer/pipeline/embedding_local.py and api/app/embedding_local.py differ: "
        "vectors from the two sides would not be comparable"
    )


def test_local_hash_shape() -> None:
    v = local_hash_embed_one("5.4 Academic Warning: CGPA below 2.00")
    assert len(v) == LOCAL_HASH_DIM
    assert abs(sum(x * x for x in v) - 1.0) < 1e-6
