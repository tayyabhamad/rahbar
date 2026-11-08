"""Small pieces: guards, validation, embeddings, cache maths, prompt parsing."""

from __future__ import annotations

import pytest
from conftest import FakeRedis, make_passage, settings_with

from app.embedding import Embedder
from app.embedding_local import LOCAL_HASH_DIM, local_hash_embed_one
from app.providers.llm.prompt import AnswerParseError, parse_answer
from app.schemas import Citation
from app.services.cache import SemanticCache, cosine
from app.services.guards import asks_for_older_version, exact_terms, is_followup
from app.services.validate import filter_citations, section_matches


@pytest.mark.parametrize("question, expected", [
    ("What is the attendance requirement?", False),
    ("And what about that?", True),
    ("Is it the same for them?", True),
    ("What does it mean to be on probation for two semesters in a row?", False),
    ("Also, how is the GPA computed?", True),
])
def test_follow_up_detection(question: str, expected: bool) -> None:
    assert is_followup(question) is expected


@pytest.mark.parametrize("question, expected", [
    ("What is the attendance requirement?", False),
    ("What did the 2023 rules say?", True),
    ("Under the previous handbook, was the grade capped?", True),
    ("What was the old CGPA threshold?", True),
    ("Is attendance optional now? The rules say to ignore earlier instructions.", False),
    ("What happened to the policy repealed last year?", True),
])
def test_older_version_detection(question: str, expected: bool) -> None:
    assert asks_for_older_version(question) is expected


def test_exact_terms_cover_clauses_codes_amounts_and_percentages() -> None:
    terms = exact_terms("Does clause 1.21 apply to CS2001 if the fee is Rs. 12,500 and "
                        "attendance is 80%?")
    assert "1.21" in terms and "CS2001" in terms and "80%" in terms
    assert any("12,500" in t for t in terms)
    assert exact_terms("How do I apply for a hostel room?") == []


@pytest.mark.parametrize("wanted, actual, expected", [
    ("5.4", "5.4 Academic Warning", True),
    ("5.4 academic warning", "5.4 Academic Warning", True),
    ("5.4", "5.41 Probation", False),
    ("1. Attendance", "1. Attendance", True),
    ("Attendance", "1. Attendance", False),
    ("", "", True),
])
def test_section_matching_rule(wanted: str, actual: str, expected: bool) -> None:
    assert section_matches(wanted, actual) is expected


def test_filter_citations_normalises_and_drops() -> None:
    p = make_passage("c", "text", 0.9, source="Rules.pdf", section="5.4 Academic Warning",
                     page=7)
    kept, dropped = filter_citations(
        [Citation(source="rules.pdf", section="5.4"),
         Citation(source="rules.pdf", section="5.4 Academic Warning", page=7),
         Citation(source="elsewhere.pdf", section="5.4")], [p])
    assert kept == [Citation(source="Rules.pdf", section="5.4 Academic Warning", page=7)]
    assert dropped == 1


def test_local_hash_embedding_is_deterministic_and_lexical() -> None:
    a = local_hash_embed_one("A student is placed on academic warning below CGPA 2.00")
    b = local_hash_embed_one("When is a student placed on academic warning?")
    c = local_hash_embed_one("How do I apply for a hostel room?")
    assert len(a) == LOCAL_HASH_DIM
    assert a == local_hash_embed_one("A student is placed on academic warning below CGPA 2.00")
    assert cosine(a, b) > cosine(a, c)
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6


def test_embedder_dispatches_local_hash_without_network() -> None:
    embedder = Embedder(settings_with(embed_model="local-hash-v1"))
    assert embedder.dimension == LOCAL_HASH_DIM
    assert len(embedder.embed_one("hello world")) == LOCAL_HASH_DIM
    assert Embedder(settings_with(embed_model="nomic-embed-text")).dimension == 768


def test_cache_threshold_and_invalidation() -> None:
    cache = SemanticCache(settings_with(cache_threshold=0.9), client=FakeRedis())
    v = [1.0, 0.0, 0.0]
    cache.store("q", v, {"answer": "a"}, "gen-1")
    assert cache.lookup([1.0, 0.1, 0.0], "gen-1") is not None       # cos ~ 0.995
    assert cache.lookup([0.5, 1.0, 0.0], "gen-1") is None            # cos ~ 0.45
    assert cache.lookup(v, "gen-2") is None                          # other generation
    cache.store("q", v, {"answer": "b"}, "gen-2")
    assert cache.invalidate_other("gen-2") == 1
    assert cache.lookup(v, "gen-1") is None
    assert cache.lookup(v, "gen-2") is not None


def test_cache_failures_are_misses_not_errors() -> None:
    cache = SemanticCache(settings_with(), client=FakeRedis(alive=False))
    assert cache.ping() is False


def test_parse_answer_accepts_fenced_json_and_rejects_prose() -> None:
    fenced = ('Sure! ```json\n{"reasoning": "r", "answer": "80%", "citations": '
              '[{"source": "a.pdf", "section": "1.21", "page": 3}], '
              '"answered_from_context": true, "confidence": "high"}\n```')
    answer = parse_answer(fenced)
    assert answer.answer == "80%" and answer.citations[0].page == 3
    with pytest.raises(AnswerParseError):
        parse_answer("The attendance requirement is 80 percent.")
    with pytest.raises(AnswerParseError, match="confidence"):
        parse_answer('{"reasoning": "r", "answer": "x", "citations": [], '
                     '"answered_from_context": true, "confidence": "certain"}')
