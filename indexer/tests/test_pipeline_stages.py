"""Unit tests for the pure pipeline stages: extract (Markdown and HTML), clean and
chunk. No Chroma, no model, no network. The fixture corpus in eval/fixtures is
the input, so these tests also pin what the CI e2e job indexes."""

from __future__ import annotations

from pathlib import Path

import pytest
from pipeline.chunk import chunk, storage_amplification
from pipeline.clean import clean, clean_text
from pipeline.extract import extract
from pipeline.models import Document, Page
from pipeline.store import store
from pipeline.tokens import count_tokens, tail_tokens

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"


# ---------------------------------------------------------------- extract

def test_extract_markdown_strips_front_matter_and_keeps_headings() -> None:
    doc = extract(str(FIXTURES / "academic-rules-2026.md"), "2026", False)
    assert doc.source == "academic-rules-2026.md"
    assert len(doc.pages) == 1
    text = doc.pages[0].text
    assert "doc_version" not in text          # front matter removed
    assert "## 1. Attendance" in text          # headings kept


def test_extract_html_turns_tables_into_pipe_rows(tmp_path: Path) -> None:
    html = tmp_path / "fees.html"
    html.write_text(
        "<html><head><style>p{}</style><script>var x=1;</script></head><body>"
        "<nav>Home | About</nav><h2>Tuition Fee</h2><p>Per credit hour.</p>"
        "<table><tr><th>Programme</th><th>Fee</th></tr>"
        "<tr><td>BS</td><td>10,000</td></tr></table></body></html>", encoding="utf-8")
    doc = extract(str(html), "2026", False)
    text = doc.pages[0].text
    assert "## Tuition Fee" in text
    assert "| BS | 10,000 |" in text
    assert "var x" not in text and "Home | About" not in text


def test_extract_rejects_unknown_types(tmp_path: Path) -> None:
    bad = tmp_path / "x.docx"
    bad.write_bytes(b"not really")
    with pytest.raises(ValueError):
        extract(str(bad), "1", False)


# ---------------------------------------------------------------- clean

def _page(n: int, body: str) -> Page:
    return Page(source="h.pdf", page=n,
                text=f"Academic Rules Handbook 2026\n{body}\nPage {n} of 5")


def test_clean_removes_repeating_header_and_footer_but_keeps_clauses() -> None:
    bodies = ["5.1 Attendance below 80 percent earns an FA grade.",
              "5.2 The Head of Department may condone a documented absence.",
              "5.3 A student on warning must meet the advisor each month.",
              "5.4 A CGPA below 2.00 places a student on academic warning.",
              "5.5 Two consecutive warnings lead to dismissal."]
    doc = Document(source="h.pdf", doc_version="2026", superseded=False,
                   pages=[_page(i, body) for i, body in enumerate(bodies, start=1)])
    cleaned = clean(doc)
    for p in cleaned.pages:
        assert "Academic Rules Handbook" not in p.text
        assert "Page" not in p.text
    assert cleaned.pages[3].text == "5.4 A CGPA below 2.00 places a student on academic warning."


def test_clean_joins_hyphenated_and_wrapped_lines() -> None:
    text = "A student must complete regis-\ntration before the dead-\nline\nset by the office."
    out = clean_text(text)
    assert "registration" in out and "deadline" in out
    assert out.count("\n") == 0


def test_clean_does_not_join_headings_or_table_rows() -> None:
    text = "5.4 Academic Warning\nA student whose CGPA falls below 2.00\n| BS | 10,000 |\n| MS | 12,500 |"
    out = clean_text(text)
    assert out.splitlines()[0] == "5.4 Academic Warning"
    assert "| BS | 10,000 |" in out.splitlines()


# ---------------------------------------------------------------- chunk

def test_chunks_carry_section_page_version_and_never_cross_sections() -> None:
    doc = clean(extract(str(FIXTURES / "academic-rules-2026.md"), "2026", False))
    pieces = chunk(doc, size=120, overlap=20)
    sections = {c.section for c in pieces}
    assert "1. Attendance" in sections and "3. Repeating a course" in sections
    for c in pieces:
        assert c.doc_version == "2026" and c.superseded is False and c.page == 1
        assert c.text.startswith(c.section)
        assert set(c.metadata()) == {"source", "section", "page", "doc_version", "superseded"}
    attendance = [c for c in pieces if c.section == "1. Attendance"]
    assert all("Repeating" not in c.text for c in attendance)


def test_chunks_respect_size_and_overlap() -> None:
    long = " ".join(f"Sentence number {i} says something useful about rule {i}." for i in range(80))
    doc = Document(source="long.md", doc_version="1", superseded=False,
                   pages=[Page(source="long.md", page=1, text="# Section A\n\n" + long)])
    pieces = chunk(doc, size=100, overlap=15)
    assert len(pieces) > 3
    for c in pieces:
        assert count_tokens(c.text) <= 100 + 5      # heading prefix may add a few tokens
    # the overlap: the tail of chunk i reappears at the head of chunk i+1
    first_body = pieces[0].text.split("\n", 1)[1]
    assert tail_tokens(first_body, 15) in pieces[1].text


def test_chunk_ids_are_stable_and_unique() -> None:
    doc = clean(extract(str(FIXTURES / "fees-2026.md"), "2026", False))
    a = chunk(doc, 200, 30)
    b = chunk(doc, 200, 30)
    assert [c.id for c in a] == [c.id for c in b]
    assert len({c.id for c in a}) == len(a)


def test_table_rows_are_never_split() -> None:
    doc = clean(extract(str(FIXTURES / "fees-2026.md"), "2026", False))
    pieces = chunk(doc, 40, 5)
    for c in pieces:
        for line in c.text.splitlines():
            if line.startswith("|"):
                assert line.endswith("|")


def test_storage_amplification_grows_with_overlap() -> None:
    long = " ".join(f"Rule {i} states that a student must do thing number {i}." for i in range(60))
    doc = Document(source="long.md", doc_version="1", superseded=False,
                   pages=[Page(source="long.md", page=1, text="# Rules\n\n" + long)])
    without = storage_amplification(doc, chunk(doc, 80, 0))
    with_overlap = storage_amplification(doc, chunk(doc, 80, 20))
    assert 0.9 <= without <= 1.3
    assert with_overlap > without


def test_chunk_rejects_bad_settings() -> None:
    doc = Document(source="x.md", doc_version="1", superseded=False, pages=[])
    with pytest.raises(ValueError):
        chunk(doc, 100, 100)


# ---------------------------------------------------------------- store

class _FakeCollection:
    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata
        self.rows: dict[str, tuple] = {}

    def upsert(self, ids, documents, embeddings, metadatas) -> None:
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self.rows[i] = (d, e, m)

    def get(self, limit, offset, include):
        ids = list(self.rows)[offset:offset + limit]
        return {"ids": ids}

    def delete(self, ids) -> None:
        for i in ids:
            self.rows.pop(i, None)

    def modify(self, metadata) -> None:
        self.last_modify = dict(metadata)
        self.metadata = {**self.metadata, **metadata}

    def count(self) -> int:
        return len(self.rows)


class _FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, _FakeCollection] = {}
        self.deleted: list[str] = []

    def get_collection(self, name):
        if name not in self.collections:
            raise ValueError("missing")
        return self.collections[name]

    def delete_collection(self, name) -> None:
        self.deleted.append(name)
        del self.collections[name]

    def create_collection(self, name, metadata):
        self.collections[name] = _FakeCollection(metadata)
        return self.collections[name]


def _settings(model: str = "nomic-embed-text"):
    from pipeline.config import IndexerSettings
    return IndexerSettings(embed_model=model, chroma_host="x", chroma_port=1, collection="rahbar",
                           corpus_dir="/c", chunk_size=400, chunk_overlap=50, ollama_url="http://o")


def _chunks(n: int):
    from pipeline.models import Chunk
    return [Chunk(id=f"c-{i}", text=f"t{i}", source="s.md", section="1", page=1,
                  doc_version="1", superseded=False) for i in range(n)]


def test_store_records_the_pin_and_upserts_in_place_when_compatible() -> None:
    client = _FakeClient()
    assert store(_chunks(3), [[0.1, 0.2]] * 3, _settings(), client) == 3
    meta = client.collections["rahbar"].metadata
    assert meta["embed_model"] == "nomic-embed-text" and meta["dimension"] == 2
    # second run with fewer chunks: same model, so upsert + delete stale, no drop
    assert store(_chunks(2), [[0.1, 0.2]] * 2, _settings(), client) == 2
    assert client.deleted == []
    assert set(client.collections["rahbar"].rows) == {"c-0", "c-1"}
    refreshed = client.collections["rahbar"].metadata
    assert refreshed["chunk_count"] == 2
    assert "hnsw:space" not in client.collections["rahbar"].last_modify, (
        "Chroma 1.0.12 rejects modify() with an hnsw:* key: the weekly re-index would fail")


def test_store_recreates_the_collection_when_the_pin_changes() -> None:
    client = _FakeClient()
    store(_chunks(2), [[0.1, 0.2]] * 2, _settings("old-model"), client)
    store(_chunks(2), [[0.1, 0.2, 0.3]] * 2, _settings("new-model"), client)
    assert client.deleted == ["rahbar"]
    assert client.collections["rahbar"].metadata["dimension"] == 3


def test_store_refuses_an_empty_index() -> None:
    with pytest.raises(ValueError):
        store([], [], _settings(), _FakeClient())
