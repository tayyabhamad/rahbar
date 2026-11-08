"""Stage 2: Clean. Remove artefacts without destroying structure.

Rules, in the order they run (and reported in docs/PIPELINE.md):

1. Repeating headers and footers. A line that appears, after digits are
   blanked, in the top or bottom three lines of at least REPEAT_FRACTION of
   the pages (and on at least REPEAT_MIN_PAGES pages) is a running header or
   footer and is removed everywhere. Blanking digits is what catches
   "Page 7 of 53" and "Academic Rules Handbook 2026 | 7" as one pattern.
2. Bare page numbers ("7", "- 7 -", "Page 7", "7/53") on their own line.
3. Line-end hyphenation: "regis-\ntration" becomes "registration". Only when the
   next line starts with a lowercase letter, so "re-\nEnrolment" and numbered
   lists are left alone.
4. Soft line wraps inside a paragraph are joined into one line, so a sentence
   is one unit for the chunker. A line is a wrap continuation only when the
   previous line does not end in sentence punctuation and the next line does
   not look like a heading, a bullet, a numbered clause or a table row.
5. Whitespace: non-breaking spaces, tabs and runs of spaces collapse to one
   space; three or more blank lines collapse to one.

What is deliberately NOT stripped: clause numbers at the start of a line
("5.4", "1.21", "3.1.2"), bullet markers and pipe-delimited table rows. Clause
numbers are headings and exact-term content (corpus property 7); the chunker
and the citations depend on them.
"""

from __future__ import annotations

import re
from collections import Counter

from .models import Document, Page

REPEAT_FRACTION = 0.3
REPEAT_MIN_PAGES = 3
EDGE_LINES = 3

_DIGITS = re.compile(r"\d+")
_PAGE_NUMBER = re.compile(r"^\s*(?:page\s*)?[-–—]?\s*\d{1,4}\s*(?:/|of)?\s*\d{0,4}\s*[-–—]?\s*$",
                          re.I)
_HEADING_LIKE = re.compile(r"^(#{1,6}\s|\d+(\.\d+)*[.)]?\s+\S|[A-Z][A-Z \-/&']{6,}$)")
_BULLET = re.compile(r"^\s*([-*•▪●○■◦]|\(?[a-zivx]{1,4}[.)]|\d{1,3}[.)])\s+")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SENTENCE_END = re.compile(r"[.!?:;…\"”’)]\s*$")
_HYPHEN_BREAK = re.compile(r"(\w)-\n([a-z])")
# A clause number that pdfminer put on a line of its own ("1.9\nA theory course ...") is
# re-attached to its sentence, so the clause stays one unit for the chunker.
_CLAUSE_ALONE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){1,3}\.?)\n(?=\S)", re.M)
_WS = re.compile(r"[ \t  -​]+")


def _signature(line: str) -> str:
    return _DIGITS.sub("#", " ".join(line.split())).strip().lower()


def _repeating_lines(pages: list[Page]) -> set[str]:
    """Signatures of lines that recur at the top or bottom of many pages."""
    if len(pages) < REPEAT_MIN_PAGES:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        lines = [ln for ln in page.text.splitlines() if ln.strip()]
        edge = lines[:EDGE_LINES] + lines[-EDGE_LINES:] if len(lines) > EDGE_LINES * 2 else lines
        for sig in {_signature(ln) for ln in edge}:
            # A signature with no letters ("#.#", "# - #") is a clause number or a page
            # number, not a running header; those are handled by their own rules.
            if sig and len(sig) >= 3 and re.search(r"[a-z]", sig):
                counts[sig] += 1
    threshold = max(REPEAT_MIN_PAGES, int(len(pages) * REPEAT_FRACTION))
    return {sig for sig, n in counts.items() if n >= threshold}


def _is_structural(line: str) -> bool:
    return bool(_HEADING_LIKE.match(line) or _BULLET.match(line) or _TABLE_ROW.match(line)
                or line.startswith("[table]"))


def _continues(previous: str, line: str) -> bool:
    """True when `line` is the soft-wrapped continuation of `previous`."""
    if not previous or _SENTENCE_END.search(previous):
        return False
    if _is_structural(line) or _is_structural(previous) or previous.startswith("[table]"):
        return False
    return line[0].islower() or line[0] in ",;)"


def _join_wrapped_lines(text: str) -> str:
    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if out and _continues(out[-1], stripped):
            out[-1] = out[-1] + " " + stripped
        else:
            out.append(stripped)
    return "\n".join(out)


def clean_text(text: str, repeating: set[str] | None = None) -> str:
    repeating = repeating or set()
    kept: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if _signature(stripped) in repeating and not _TABLE_ROW.match(stripped):
            continue
        if _PAGE_NUMBER.match(stripped) and not _TABLE_ROW.match(stripped):
            continue
        kept.append(_WS.sub(" ", stripped))
    text = "\n".join(kept)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _CLAUSE_ALONE.sub(r"\1 ", text)
    text = _join_wrapped_lines(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean(doc: Document) -> Document:
    """Strip headers and footers that repeat on every page; keep headings and
    section boundaries, because chunking and citations depend on them."""
    # Re-attach lone clause numbers first, so "5.1" on its own line never looks like a
    # repeating edge line and the clause reaches the chunker as one unit.
    joined = [Page(source=p.source, page=p.page, text=_CLAUSE_ALONE.sub(r"\1 ", p.text),
                   ocr=p.ocr) for p in doc.pages]
    repeating = _repeating_lines(joined)
    cleaned = [Page(source=p.source, page=p.page, text=clean_text(p.text, repeating), ocr=p.ocr)
               for p in joined]
    return Document(source=doc.source, doc_version=doc.doc_version, superseded=doc.superseded,
                    pages=cleaned)
