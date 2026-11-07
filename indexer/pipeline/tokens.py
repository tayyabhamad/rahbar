"""Token counting for the chunker and for the storage-amplification figure.

The unit everywhere in this repository is a *regex word-token*: a run of word
characters, or a single punctuation mark. For English prose this tracks BPE
token counts within roughly 20 percent, needs no model download, and makes
the chunker deterministic and dependency-free. CHUNK_SIZE and CHUNK_OVERLAP
in the ConfigMap are expressed in this unit (docs/PIPELINE.md says so too).
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text)


def count_tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


def tail_tokens(text: str, n: int) -> str:
    """The last `n` tokens of `text`, as text, for overlap between neighbours.

    Cuts on a token boundary, never inside a word, and keeps the original
    spacing between the surviving tokens.
    """
    if n <= 0 or not text:
        return ""
    spans = [m.span() for m in _TOKEN.finditer(text)]
    if len(spans) <= n:
        return text.strip()
    start = spans[-n][0]
    return text[start:].strip()
