"""Stage 1: Extract. PDF, HTML, Markdown or plain text to text, page by page.

What this stage does (section 3.1):

* PDF pages are read with pdfplumber. Tables are pulled out separately and
  re-rendered as pipe-delimited rows, because pdfplumber's plain text puts a
  table's columns on top of each other (see docs/PIPELINE.md for the
  before-and-after). The row text is appended to the page under a `[table]`
  marker so the chunker and the embedder see the row as one unit.
* A page whose text layer yields fewer than MIN_TEXT_CHARS characters is
  treated as image-only. It is rasterised with pdf2image (poppler) and read
  with pytesseract (tesseract-ocr); the page is marked `ocr=True`. If OCR is
  disabled or the binaries are missing, the loss is logged and the page is
  kept empty rather than silently dropped.
* Page text comes from pdfminer's layout analysis (text boxes in reading order),
  which keeps two-column blocks column by column. pdfplumber's plain text
  interleaves the columns line by line and every clause number lands
  mid-sentence (the 2026 Academic Rules Handbook does this).
* HTML is reduced to headings, paragraphs and pipe-delimited table rows.
  Scripts, styles and navigation are discarded.
* Markdown has its front matter removed (discover() already read it) and is
  treated as a single page.

Hyphenation at line ends and header/footer repetition are *not* handled here;
that is the Clean stage's job, because it needs to see every page at once.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from collections.abc import Iterator
from pathlib import Path

from .models import Document, Page

MIN_TEXT_CHARS = int(os.environ.get("OCR_MIN_TEXT_CHARS", 25))
OCR_ENABLED = os.environ.get("OCR_ENABLED", "true").lower() in {"1", "true", "yes"}
OCR_LANG = os.environ.get("OCR_LANG", "eng")
OCR_DPI = int(os.environ.get("OCR_DPI", 200))


def _log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer.extract", "msg": msg, **fields}),
          file=sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _render_table(rows: list[list[str | None]]) -> str:
    """Render a pdfplumber table as pipe rows. Empty cells stay empty so the
    columns keep their position; multi-line cells are flattened."""
    out = []
    for row in rows:
        if not row or all(cell in (None, "") for cell in row):
            continue
        cells = [" ".join((cell or "").split()) for cell in row]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _ocr_page(path: str, page_number: int) -> str | None:
    """OCR one page. Returns None when OCR is unavailable so the caller can log it."""
    if not OCR_ENABLED:
        return None
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        return None
    try:
        images = convert_from_path(path, dpi=OCR_DPI, first_page=page_number,
                                   last_page=page_number)
        if not images:
            return ""
        return str(pytesseract.image_to_string(images[0], lang=OCR_LANG))
    except Exception as exc:  # poppler or tesseract binary missing, corrupt page, ...
        _log("ocr failed", source=Path(path).name, page=page_number, error=str(exc))
        return None


def _miner_pages(path: str) -> Iterator[str]:
    """Page text from pdfminer's layout analysis, one string per page.

    pdfminer groups characters into lines and lines into text boxes, then orders the
    boxes in reading order (`boxes_flow`), which is what makes two-column pages come
    out column by column instead of interleaved line by line. The FAST-NUCES handbooks
    mix full-width paragraphs with two-column blocks on one page, so no single gutter
    works; the box analysis does. pdfplumber is still used for tables and to decide
    whether a page has a text layer at all.
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTTextContainer

    laparams = LAParams(boxes_flow=0.5, line_margin=0.35, char_margin=2.0, word_margin=0.1)
    for layout in extract_pages(path, laparams=laparams):
        boxes = [el for el in layout if isinstance(el, LTTextContainer)]
        yield "\n".join(el.get_text().strip() for el in boxes if el.get_text().strip())


def _extract_pdf(path: str, source: str) -> list[Page]:
    import pdfplumber

    pages: list[Page] = []
    ocr_pages = 0
    lost_pages = 0
    layout_pages = 0
    miner = _miner_pages(path)
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            plain = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
            try:
                text = next(miner)
            except (StopIteration, Exception):   # pdfminer gave up on this page: keep plain
                text = plain
            else:
                # Prefer the layout text; fall back when it is empty but plain is not.
                if len(text.strip()) < MIN_TEXT_CHARS <= len(plain.strip()):
                    text = plain
                else:
                    layout_pages += 1
            tables_text = ""
            try:
                tables = page.extract_tables()
            except Exception:  # a malformed page should not kill the whole document
                tables = []
            rendered = [_render_table(t) for t in tables if t]
            rendered = [r for r in rendered if r.count("\n") >= 1]   # at least two rows
            if rendered:
                tables_text = "\n\n[table]\n" + "\n\n[table]\n".join(rendered)

            ocr = False
            if len(text.strip()) < MIN_TEXT_CHARS:
                recovered = _ocr_page(path, number)
                if recovered is None:
                    lost_pages += 1
                    _log("page has no text layer and OCR is unavailable",
                         source=source, page=number)
                else:
                    text, ocr = recovered, True
                    ocr_pages += 1
            pages.append(Page(source=source, page=number, text=(text + tables_text).strip(),
                              ocr=ocr))
    _log("extracted pdf", source=source, pages=len(pages), ocr_pages=ocr_pages,
         lost_pages=lost_pages, layout_pages=layout_pages)
    return pages


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "li", "br", "section", "article", "ul", "ol", "tr", "table",
               "header", "footer", "nav", "form", "blockquote", "pre"}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "head", "nav", "footer"}
_HEADING = re.compile(r"^h([1-6])$")


class _TextExtractor(HTMLParser):
    """Headings become Markdown headings, table rows become pipe rows, and the
    rest becomes paragraphs. Good enough for the FAST-NUCES fee and grading pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._heading: int | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._table_rows = 0

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if not text:
            return
        if self._heading:
            self.lines.append("#" * self._heading + " " + text)
        else:
            self.lines.append(text)
        self.lines.append("")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        m = _HEADING.match(tag)
        if m:
            self._flush()
            self._heading = int(m.group(1))
        elif tag == "table":
            self._flush()
            self.lines.append("[table]")
            self._table_rows = 0
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if _HEADING.match(tag):
            self._flush()
            self._heading = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.lines.append("| " + " | ".join(self._row) + " |")
                self._table_rows += 1
            self._row = None
        elif tag == "table":
            if self._table_rows == 0 and self.lines and self.lines[-1] == "[table]":
                self.lines.pop()
            self.lines.append("")
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._row is None:
            self._buf.append(data)

    def text(self) -> str:
        self._flush()
        out = "\n".join(self.lines)
        return re.sub(r"\n{3,}", "\n\n", out).strip()


def _extract_html(path: str, source: str) -> list[Page]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    parser = _TextExtractor()
    parser.feed(raw)
    text = parser.text()
    _log("extracted html", source=source, chars=len(text))
    return [Page(source=source, page=1, text=text)]


# ---------------------------------------------------------------------------
# Markdown and text
# ---------------------------------------------------------------------------

def _strip_front_matter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:])
    return text


def _extract_markdown(path: str, source: str) -> list[Page]:
    text = _strip_front_matter(Path(path).read_text(encoding="utf-8", errors="replace"))
    return [Page(source=source, page=1, text=text.strip())]


# ---------------------------------------------------------------------------

def extract(path: str, doc_version: str, superseded: bool) -> Document:
    """Return the document's text page by page, with image-only pages OCR'd and
    marked `ocr=True`."""
    source = Path(path).name
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        pages = _extract_pdf(path, source)
    elif suffix in {".html", ".htm"}:
        pages = _extract_html(path, source)
    elif suffix in {".md", ".txt"}:
        pages = _extract_markdown(path, source)
    else:
        raise ValueError(f"unsupported document type: {source}")
    return Document(source=source, doc_version=doc_version, superseded=superseded, pages=pages)
