#!/usr/bin/env python3
"""Score a running RAHBAR against a golden set, and gate CI on the result (section 5).

    python eval/run_eval.py --api http://<ingress>                 # your corpus
    python eval/run_eval.py --api ... --golden eval/fixtures/golden.fixture.yaml
    python eval/run_eval.py --api ... --update-baseline            # after a deliberate improvement

Exit code 0 when every gate passes, 1 when any fails. CI relies on that exit code.

Given: talking to the API, reading the golden set, writing results.json, the exit
code. The evaluation itself is recall_at_k, citation_validity, summarise and gate;
the matching rule is documented in docs/ARCHITECTURE.md ("Evaluation matching rule").
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import yaml  # type: ignore[import-untyped]  # PyYAML ships no stubs; types-PyYAML not installed

HERE = Path(__file__).resolve().parent

# The gate from section 5.4. Change these only with a reason written in docs/PIPELINE.md.
MAX_RECALL5_DROP = 0.05        # recall@5 may fall at most 5 points below the baseline
MIN_CITATION_VALIDITY = 0.95   # at least 95% of citations must match retrieved passages
REQUIRED_ABSTENTION = 1.0      # every must-abstain question must be declined


def load_golden(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    questions = data.get("questions") or []
    if not questions:
        sys.exit(f"no questions in {path}")
    return questions


def ask(client: httpx.Client, api: str, question: str) -> dict:
    """POST one question and return the AskResponse as a dict (see api/app/schemas.py)."""
    response = client.post(f"{api.rstrip('/')}/api/ask", json={"question": question})
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Matching rule (docs/ARCHITECTURE.md, "Evaluation matching rule")
# ---------------------------------------------------------------------------

_SECTION_BOUNDARY = (" ", ".", ":")


def _norm(value: object) -> str:
    """Lower-case and collapse whitespace; None and non-strings become ''."""
    if value is None:
        return ""
    return " ".join(str(value).split()).lower()


def _source_matches(golden: object, retrieved: object) -> bool:
    """Same file name, case-insensitive; a leading directory on either side is ignored."""
    a = _norm(golden).replace("\\", "/").rsplit("/", 1)[-1]
    b = _norm(retrieved).replace("\\", "/").rsplit("/", 1)[-1]
    return bool(a) and a == b


def _section_prefix(short: str, long: str) -> bool:
    """`short` equals `long`, or is a prefix of it followed by a space, '.', ':' or the end.
    Both must already be normalised. '5.4' matches '5.4 academic warning' and '5.4.1 ...'
    but not '5.41 ...'."""
    if short == long:
        return True
    if not long.startswith(short):
        return False
    return long[len(short):len(short) + 1] in _SECTION_BOUNDARY


def _golden_matches(golden: dict, hit: dict) -> bool:
    """Golden entry vs one retrieved passage. A `chunk_id` on the golden entry wins
    outright; otherwise same source and the golden section is a prefix of the retrieved
    section. A golden entry without a section matches on source alone."""
    golden_id = _norm(golden.get("chunk_id"))
    if golden_id and golden_id == _norm(hit.get("chunk_id")):
        return True
    if not _source_matches(golden.get("source"), hit.get("source")):
        return False
    wanted = _norm(golden.get("section"))
    if not wanted:
        return True
    return _section_prefix(wanted, _norm(hit.get("section")))


def _citation_matches(citation: dict, hit: dict) -> bool:
    """Citation vs one retrieved passage: same source, and the sections equal or one is a
    prefix of the other (the model may cite '5.4' for '5.4 Academic Warning', or quote the
    full heading for a chunk labelled '5.4')."""
    cited_id = _norm(citation.get("chunk_id"))
    if cited_id and cited_id == _norm(hit.get("chunk_id")):
        return True
    if not _source_matches(citation.get("source"), hit.get("source")):
        return False
    cited = _norm(citation.get("section"))
    found = _norm(hit.get("section"))
    if not cited or not found:
        return cited == found
    return _section_prefix(cited, found) or _section_prefix(found, cited)


def _dicts(value: object) -> list[dict]:
    """Only the dict entries of a list; anything else (None, a string) is empty."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Fraction of the relevant (source, section) pairs found in the top k retrieved.
    Return None when the question has no relevant passages (must-abstain questions)."""
    wanted = _dicts(relevant)
    if not wanted:
        return None
    top = _dicts(retrieved)[:max(k, 0)]
    matched = sum(1 for entry in wanted if any(_golden_matches(entry, hit) for hit in top))
    return matched / len(wanted)


def citation_validity(response: dict) -> float | None:
    """Fraction of the response's citations that match a passage in response['retrieved'].
    Return None when there are no citations."""
    if not isinstance(response, dict):
        return None
    citations = _dicts(response.get("citations"))
    if not citations:
        return None
    retrieved = _dicts(response.get("retrieved"))
    valid = sum(1 for c in citations if any(_citation_matches(c, hit) for hit in retrieved))
    return valid / len(citations)


# ---------------------------------------------------------------------------
# Aggregation and the gate
# ---------------------------------------------------------------------------

def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _per_question(row: dict) -> dict:
    """One results row for a golden question; never raises on a missing field."""
    raw_response = row.get("response")
    response: dict = raw_response if isinstance(raw_response, dict) else {}
    relevant = _dicts(row.get("relevant"))
    retrieved = _dicts(response.get("retrieved"))
    answer = response.get("answer")
    answer_text = answer if isinstance(answer, str) else ""
    expect = row.get("expect")
    expect_hit: bool | None = None
    if isinstance(expect, str) and expect.strip():
        expect_hit = expect.strip().lower() in answer_text.lower()
    return {
        "id": row.get("id"),
        "type": row.get("type") or "ordinary",
        "must_abstain": bool(row.get("must_abstain", False)),
        "recall_at_3": _round(recall_at_k(relevant, retrieved, 3)),
        "recall_at_5": _round(recall_at_k(relevant, retrieved, 5)),
        "citation_validity": _round(citation_validity(response)),
        "answered_from_context": response.get("answered_from_context"),
        "confidence": response.get("confidence"),
        "cached": bool(response.get("cached", False)),
        "seconds": row.get("seconds"),
        "expect": expect,
        "expect_hit": expect_hit,
        "retrieved": [
            {"chunk_id": hit.get("chunk_id"), "score": hit.get("score")} for hit in retrieved
        ],
        "citations": len(_dicts(response.get("citations"))),
        "answer": answer_text[:200],
    }


def summarise(rows: list[dict]) -> dict:
    """Overall recall@3, recall@5, citation validity and abstention correctness, plus
    recall@5 broken down by question type. This dict becomes results.json."""
    per_question = [_per_question(row) for row in rows if isinstance(row, dict)]

    recall3 = [q["recall_at_3"] for q in per_question if q["recall_at_3"] is not None]
    recall5 = [q["recall_at_5"] for q in per_question if q["recall_at_5"] is not None]
    validity = [
        q["citation_validity"] for q in per_question if q["citation_validity"] is not None
    ]
    must_abstain = [q for q in per_question if q["must_abstain"]]
    declined = [q for q in must_abstain if q["answered_from_context"] is False]
    false_abstentions = sum(
        1 for q in per_question
        if not q["must_abstain"] and q["answered_from_context"] is False
    )
    expected = [q for q in per_question if q["expect_hit"] is not None]
    seconds = [float(q["seconds"]) for q in per_question
               if isinstance(q["seconds"], (int, float))]

    by_type: dict[str, dict] = {}
    for name in sorted({str(q["type"]) for q in per_question}):
        group = [q for q in per_question if str(q["type"]) == name]
        by_type[name] = {
            "questions": len(group),
            "recall_at_3": _mean([q["recall_at_3"] for q in group
                                  if q["recall_at_3"] is not None]),
            "recall_at_5": _mean([q["recall_at_5"] for q in group
                                  if q["recall_at_5"] is not None]),
            "citation_validity": _mean([q["citation_validity"] for q in group
                                        if q["citation_validity"] is not None]),
        }

    return {
        "recall_at_3": _mean(recall3),
        "recall_at_5": _mean(recall5),
        "citation_validity": _mean(validity) if validity else 1.0,
        "abstention_correctness": (
            round(len(declined) / len(must_abstain), 4) if must_abstain else 1.0
        ),
        "false_abstentions": false_abstentions,
        "expect_hits": (
            round(sum(1 for q in expected if q["expect_hit"]) / len(expected), 4)
            if expected else None
        ),
        "mean_seconds": _mean(seconds),
        "by_type": by_type,
        "per_question": per_question,
    }


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


def gate(results: dict, baseline: dict | None) -> list[str]:
    """Return one message per failed gate (section 5.4); an empty list means pass."""
    failures: list[str] = []

    recall5 = _number(results.get("recall_at_5"))
    base5 = _number(baseline.get("recall_at_5")) if isinstance(baseline, dict) else None
    if base5 is not None and recall5 is not None:
        drop = base5 - recall5
        if drop > MAX_RECALL5_DROP + 1e-9:
            failures.append(
                f"recall@5 {recall5:.4f} is {drop:.4f} below the baseline {base5:.4f} "
                f"(allowed drop {MAX_RECALL5_DROP:.2f})"
            )
    elif base5 is not None and recall5 is None:
        failures.append(
            f"recall@5 could not be computed (no question had relevant passages) but the "
            f"baseline is {base5:.4f}"
        )

    abstention = _number(results.get("abstention_correctness"))
    if abstention is None or abstention < REQUIRED_ABSTENTION:
        shown = "missing" if abstention is None else f"{abstention:.4f}"
        failures.append(
            f"abstention correctness {shown} is below the required {REQUIRED_ABSTENTION:.2f}"
        )

    validity = _number(results.get("citation_validity"))
    if validity is None or validity < MIN_CITATION_VALIDITY:
        shown = "missing" if validity is None else f"{validity:.4f}"
        failures.append(
            f"citation validity {shown} is below the minimum {MIN_CITATION_VALIDITY:.2f}"
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAHBAR against a golden set.")
    parser.add_argument("--api", required=True, help="base URL, e.g. http://192.168.49.2")
    parser.add_argument("--host", help="Host header, when your Ingress rule names a host")
    parser.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write these results as the new baseline")
    args = parser.parse_args()

    questions = load_golden(args.golden)
    rows = []
    headers = {"Host": args.host} if args.host else {}
    with httpx.Client(timeout=120.0, headers=headers) as client:
        for q in questions:
            started = time.perf_counter()
            response = ask(client, args.api, q["question"])
            rows.append({
                "id": q["id"], "type": q.get("type", "ordinary"),
                "must_abstain": bool(q.get("must_abstain", False)),
                "relevant": q.get("relevant") or [],
                "expect": q.get("expect"),
                "response": response,
                "seconds": round(time.perf_counter() - started, 2),
            })
            print(f"  {q['id']:<5} {rows[-1]['seconds']:>6.2f}s  {q['question'][:60]}")

    try:
        results = summarise(rows)
        baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else None
        failures = gate(results, baseline)
    except NotImplementedError as exc:
        print(f"\nnot implemented yet: {exc}")
        return 2

    results["golden"] = str(args.golden)
    results["questions"] = len(rows)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.out}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {args.baseline}")

    if failures:
        print("\nGATE FAILED")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("\ngate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
