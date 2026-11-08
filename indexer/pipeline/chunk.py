"""Stage 3: Chunk. Structure-aware, with overlap.

The split order is the lecture's: sections, then paragraphs, then sentences,
then (only when one sentence is longer than the target) a hard cut on tokens.

* A *section* starts at a heading. Headings are Markdown `#` lines, numbered
  titles such as "5.4 Academic Warning" or "3 Admission Policy", short ALL-CAPS
  lines, and short Title-Case lines such as "Scheduling of Classes". A numbered
  line only counts when its title reads like a title (most words capitalised):
  "3.32 A new admission is closed if ..." is a clause, not a heading.
  A chunk never crosses a section boundary, so a definition and its clause
  number stay together (corpus property 5) and every chunk can carry the
  heading it sits under as its `section` metadata.
* Inside a section, paragraphs are packed into a chunk until the next one
  would push it over `size` tokens. A paragraph that is itself too long is
  split on sentence boundaries. Pipe-delimited table rows are treated as
  sentences, so a row is never cut in half.
* Neighbouring chunks in the same section share `overlap` tokens: the tail of
  the previous chunk is prepended to the next, so a rule that straddles a cut
  is whole in at least one of them.
* The section heading is prepended to every chunk's text. It costs a few
  tokens of storage amplification and buys exact-term recall: a question
  that quotes "clause 1.21" matches a chunk whose text starts with "1.21".

Sizes are in regex word-tokens (see tokens.py); docs/PIPELINE.md states the
unit next to the three measured configurations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Chunk, Document
from .tokens import count_tokens, tail_tokens, tokenize

_MD_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_NUMBERED_HEADING = re.compile(
    r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})[.)]?\s+(?P<title>[A-Z][^.!?]{1,90})$")
_BARE_CLAUSE = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){1,3})\.?\s+(?P<title>\S.{0,120})$")
_CAPS_HEADING = re.compile(r"^[A-Z][A-Z0-9 \-/&',()]{5,70}$")
_TITLE_LINE = re.compile(r"^[A-Z][A-Za-z0-9'’\-/&(),]*(?: [A-Za-z0-9'’\-/&(),]+){0,6}$")
_SMALL_WORDS = {"of", "and", "the", "a", "an", "for", "in", "on", "to", "or", "by", "with",
                "at", "from", "&", "vs", "per"}
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(\[])")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

MAX_HEADING_TOKENS = 16


@dataclass
class _Block:
    text: str
    page: int


def _titleish(title: str) -> bool:
    """Most words capitalised (small words excepted): a title, not a sentence."""
    words = title.split()
    if not words or len(words) > 10:
        return False
    capitalised = sum(1 for w in words
                      if w.lower().strip("(),") in _SMALL_WORDS or not w[0].isalpha()
                      or w[0].isupper())
    return capitalised / len(words) >= 0.75


def _heading_of(line: str) -> str | None:
    """Return the heading text when the line is a heading, else None."""
    line = line.strip()
    if not line:
        return None
    m = _MD_HEADING.match(line)
    if m:
        return m.group(1).strip()
    if count_tokens(line) > MAX_HEADING_TOKENS or _TABLE_ROW.match(line):
        return None
    m = _NUMBERED_HEADING.match(line)
    if m and _titleish(m.group("title")):
        return f"{m.group('num')} {m.group('title').strip()}"
    if _CAPS_HEADING.match(line):
        return line.title() if line.isupper() else line
    # A Title-Case line that ends in a bare number is a table-of-contents entry
    # ("Academic Warning/Probation 18"), not the heading itself.
    if _TITLE_LINE.match(line) and _titleish(line) and len(line) <= 48 \
            and not line.endswith((".", ",", ";", ":")) and not re.search(r"\s\d{1,3}$", line):
        return line
    return None


def _clause_label(line: str) -> str | None:
    """A numbered clause that is a full sentence ("5.4.1 A student whose ...") is
    not a heading, but its number is still the best section label we have."""
    m = _BARE_CLAUSE.match(line.strip())
    return m.group("num") if m else None


def _sentences(paragraph: str) -> list[str]:
    if _TABLE_ROW.match(paragraph.splitlines()[0] if paragraph else ""):
        return [row for row in paragraph.splitlines() if row.strip()]
    return [s for s in _SENTENCE_SPLIT.split(paragraph) if s.strip()]


def _hard_split(text: str, size: int) -> list[str]:
    toks = tokenize(text)
    pieces = []
    for i in range(0, len(toks), size):
        pieces.append(" ".join(toks[i:i + size]))
    return pieces


def _pack(units: list[_Block], size: int, overlap: int) -> list[tuple[str, int]]:
    """Pack text units into chunks under `size` tokens with `overlap` carried over."""
    chunks: list[tuple[str, int]] = []
    current: list[str] = []
    current_tokens = 0
    current_page = units[0].page if units else 1
    carry = ""

    def flush() -> None:
        nonlocal current, current_tokens, carry, current_page
        if not current:
            return
        body = "\n".join(current).strip()
        text = (carry + "\n" + body).strip() if carry else body
        chunks.append((text, current_page))
        carry = tail_tokens(body, overlap) if overlap > 0 else ""
        current, current_tokens = [], 0

    for unit in units:
        n = count_tokens(unit.text)
        if n > size:                       # one sentence longer than a chunk: hard cut
            flush()
            for piece in _hard_split(unit.text, size):
                current, current_tokens, current_page = [piece], count_tokens(piece), unit.page
                flush()
            continue
        if current and current_tokens + n + count_tokens(carry) > size:
            flush()
        if not current:
            current_page = unit.page
        current.append(unit.text)
        current_tokens += n
    flush()
    return chunks


def _units_for_section(blocks: list[_Block], size: int) -> list[_Block]:
    """Paragraphs that fit stay whole; longer ones become sentences."""
    units: list[_Block] = []
    for block in blocks:
        if count_tokens(block.text) <= size:
            units.append(block)
        else:
            units.extend(_Block(s, block.page) for s in _sentences(block.text))
    return units


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Split on sections, then paragraphs, then sentences, keeping each chunk under
    `size` tokens, with `overlap` tokens shared between neighbours. Every chunk keeps
    the section heading it sits under, its page, doc_version and superseded."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be between 0 and size - 1")

    # 1. Walk every page, building sections of paragraph blocks.
    sections: list[tuple[str, list[_Block]]] = []
    heading = "Preamble"
    blocks: list[_Block] = []
    for page in doc.pages:
        page_no = page.page
        for paragraph in re.split(r"\n\s*\n", page.text):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            lines = paragraph.split("\n")
            first = lines[0].strip()
            found = _heading_of(first)
            if found is not None:
                if blocks:
                    sections.append((heading, blocks))
                heading, blocks = found, []
                rest = "\n".join(lines[1:]).strip()
                if rest:
                    blocks.append(_Block(rest, page_no))
                continue
            if not blocks and heading == "Preamble":
                label = _clause_label(first)
                if label:
                    heading = label
            blocks.append(_Block(paragraph, page_no))
    if blocks:
        sections.append((heading, blocks))

    # 2. Pack each section on its own, then prefix the heading.
    chunks: list[Chunk] = []
    stem = re.sub(r"[^a-z0-9]+", "-", doc.source.lower()).strip("-")
    ordinal = 0
    heading_budget = size // 4
    for section, section_blocks in sections:
        prefix_tokens = min(count_tokens(section), heading_budget)
        body_size = max(size - prefix_tokens - 1, size // 2)
        units = _units_for_section(section_blocks, body_size)
        for text, first_page in _pack(units, body_size, overlap):
            if not text.strip():
                continue
            body = text if text.startswith(section) else f"{section}\n{text}"
            chunks.append(Chunk(
                id=f"{stem}-{ordinal:04d}",
                text=body,
                source=doc.source,
                section=section,
                page=first_page,
                doc_version=doc.doc_version,
                superseded=doc.superseded,
            ))
            ordinal += 1
    return chunks


def storage_amplification(doc: Document, chunks: list[Chunk]) -> float:
    """Stored tokens divided by original tokens (the lecture's 150 / 120 figure)."""
    original = sum(count_tokens(p.text) for p in doc.pages) or 1
    stored = sum(count_tokens(c.text) for c in chunks)
    return stored / original
