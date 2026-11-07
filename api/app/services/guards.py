"""Small, testable decisions about a question's text (section 3.2).

* `is_followup`: a question that leans on the previous turn ("and what about that?")
  must not be served from or written to the cache.
* `asks_for_older_version`: the superseded filter is lifted only when the user
  explicitly asks about an older version.
* `exact_terms`: clause numbers, course codes, thresholds and amounts that embeddings
  blur, for the hybrid retrieval pass.
"""

from __future__ import annotations

import re

_FOLLOWUP_OPENERS = re.compile(
    r"^\s*(and|also|but|then|so|what about|how about|and what|what else|ok|okay)\b", re.I)
_ANAPHORA = re.compile(
    r"\b(it|that|this|these|those|they|them|the same|above|previous answer|you said|"
    r"your answer|the one)\b", re.I)
# Only an explicit request for an older version lifts the superseded filter: a version
# word next to a document noun, a past year, or "superseded"/"repealed" themselves. A
# bare "earlier" or "previous" is not enough ("ignore earlier instructions").
_OLDER = re.compile(
    r"\b(?:old|older|previous|prior|earlier|former|outdated|superseded|repealed)\s+"
    r"(?:version|edition|rules?|policy|policies|handbook|regulations?|requirement|"
    r"threshold|cgpa|rule book)\b"
    r"|\b20(?:1\d|2[0-5])\b"
    r"|\b(?:superseded|repealed)\b"
    r"|\bbefore (?:the )?(?:2026|revision|update)\b", re.I)

_CLAUSE = re.compile(r"\b\d{1,2}(?:\.\d{1,2}){1,3}\b")
_COURSE_CODE = re.compile(r"\b[A-Z]{2,4}\s?-?\d{3,4}[A-Z]?\b")
_AMOUNT = re.compile(r"\b(?:Rs\.?|PKR)\s?\d[\d,]*\b|\b\d{1,3}(?:,\d{3})+\b")
_PERCENT = re.compile(r"\b\d{1,3}\s?%")


def is_followup(question: str) -> bool:
    text = question.strip()
    if _FOLLOWUP_OPENERS.match(text):
        return True
    words = len(text.split())
    return words <= 8 and bool(_ANAPHORA.search(text))


def asks_for_older_version(question: str) -> bool:
    return bool(_OLDER.search(question))


def exact_terms(question: str) -> list[str]:
    found: list[str] = []
    for pattern in (_CLAUSE, _COURSE_CODE, _AMOUNT, _PERCENT):
        for match in pattern.findall(question):
            term = match.strip()
            if term and term not in found:
                found.append(term)
    return found
