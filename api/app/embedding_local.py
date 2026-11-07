"""`local-hash-v1`: a deterministic, dependency-free lexical embedding.

This file is byte-for-byte identical in `api/app/embedding_local.py` and
`indexer/pipeline/embedding_local.py`; a test in the indexer package asserts it.
It is the embedding model CI uses (docs/ARCHITECTURE.md): hashed bag-of-words,
so cosine similarity means lexical overlap. No model, no network, and the same
input always gives the same vector, which is what a test corpus needs.

It is *not* a semantic embedding. It is the pin's alternative value, chosen by
EMBED_MODEL, and the pin check treats it like any other model name.
"""

from __future__ import annotations

import hashlib
import math
import re

LOCAL_HASH_MODEL = "local-hash-v1"
LOCAL_HASH_DIM = 512

_TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+(?:[.,]\d+)*%?")
_STOPWORDS = frozenset(
    "a an the and or of to in on for with by at from as is are was were be been being "
    "this that these those it its into than then there their they them he she his her "
    "we our you your i me my do does did not no yes if so such which who whom whose what "
    "when where why how all any each both more most other some only own same too very can "
    "will would should could may might must shall have has had having also about above "
    "after before between during under over up down out off again further once here".split()
)


def is_local_hash(model: str) -> bool:
    return model == LOCAL_HASH_MODEL


def _tokens(text: str) -> list[str]:
    out = []
    for tok in _TOKEN.findall(text.lower()):
        if tok in _STOPWORDS:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        out.append(tok)
    return out


def _bucket(token: str) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "big") % LOCAL_HASH_DIM
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


def local_hash_embed_one(text: str) -> list[float]:
    counts: dict[str, int] = {}
    toks = _tokens(text)
    for tok in toks:
        counts[tok] = counts.get(tok, 0) + 1
    # Word bigrams carry phrase identity ("academic warning") at a small cost.
    for a, b in zip(toks, toks[1:]):
        key = f"{a}_{b}"
        counts[key] = counts.get(key, 0) + 1
    vector = [0.0] * LOCAL_HASH_DIM
    for tok, n in counts.items():
        index, sign = _bucket(tok)
        weight = 1.0 + math.log(n)
        if "_" in tok:
            weight *= 0.5
        vector[index] += sign * weight
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def local_hash_embed(texts: list[str]) -> list[list[float]]:
    return [local_hash_embed_one(t) for t in texts]
