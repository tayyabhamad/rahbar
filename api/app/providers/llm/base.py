"""The LLM provider interface (section 3.5). Swapping providers is one file."""

from __future__ import annotations

from typing import Protocol

from ...schemas import Answer, Passage


class LLMProvider(Protocol):
    name: str

    def generate(self, question: str, passages: list[Passage],
                 older_version_requested: bool = False) -> Answer:
        """Answer the question from the passages, as an `Answer`. Raise
        `GenerationError` (providers/llm/common.py) when no valid Answer could be
        produced; the caller falls back."""
        ...
