"""Checks on the model's Answer before it reaches a user (section 3.4).

* `section_matches`: the one rule for "does this citation name that passage", shared
  in spirit with eval/run_eval.py (docs/ARCHITECTURE.md, "Evaluation matching rule").
* `filter_citations`: every citation must match a passage retrieved for this request;
  anything else is fabricated and is dropped. If nothing survives, the answer cannot
  claim to be grounded.
* `fallback_answer` and `abstention_answer`: the two canned, never-cached responses.
"""

from __future__ import annotations

import re

from ..schemas import Answer, Citation, Passage

ABSTAIN_TEXT = ("The documents I have do not cover this question, so I cannot answer it "
                "from the corpus. Try rephrasing, or ask the relevant office directly.")
FALLBACK_TEXT = ("I could not produce a reliable answer this time: the model's reply did not "
                 "pass validation. Please ask again in a moment.")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def section_matches(wanted: str, actual: str) -> bool:
    """`wanted` names `actual` when equal, or when one is a prefix of the other followed
    by a boundary: "5.4" matches "5.4 Academic Warning"; "5.4" does not match "5.41 ...".
    """
    w, a = _norm(wanted), _norm(actual)
    if not w or not a:
        return w == a
    if w == a:
        return True
    short, long = (w, a) if len(w) < len(a) else (a, w)
    return long.startswith(short) and long[len(short)] in " .:-)]"


def filter_citations(citations: list[Citation], passages: list[Passage]) -> tuple[list[Citation], int]:
    """Citations that match a retrieved passage (normalised to the passage's exact
    source, section and page), and how many were dropped."""
    kept: list[Citation] = []
    dropped = 0
    for citation in citations:
        match = next(
            (p for p in passages
             if _norm(p.source) == _norm(citation.source)
             and section_matches(citation.section, p.section)),
            None,
        )
        if match is None:
            dropped += 1
            continue
        normalised = Citation(source=match.source, section=match.section, page=match.page)
        if normalised not in kept:
            kept.append(normalised)
    return kept, dropped


def abstention_answer(reason: str) -> Answer:
    return Answer(reasoning=reason, answer=ABSTAIN_TEXT, citations=[],
                  answered_from_context=False, confidence="low")


def fallback_answer(reason: str) -> Answer:
    return Answer(reasoning=reason, answer=FALLBACK_TEXT, citations=[],
                  answered_from_context=False, confidence="low")
