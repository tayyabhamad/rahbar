"""The indexer Job's entry point. It runs once and exits (section 4.2).

    python -m pipeline.run --dummy    # week 1: three fake chunks, no model needed
    python -m pipeline.run            # the real pipeline: extract, clean, chunk, embed, store

Exit code 0 means the index was written; anything else fails the Job, and
Kubernetes retries it up to the Job's backoffLimit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from typing import Any

from .config import IndexerSettings, get_settings
from .models import Chunk

CHROMA_WAIT_SECONDS = int(os.environ.get("CHROMA_WAIT_SECONDS", 300))


def log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer", "msg": msg, **fields}), flush=True)


def _front_matter(path: Path) -> dict[str, str]:
    """Read `key: value` lines between leading '---' markers (used by eval/fixtures)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index.

    corpus/fetch.py writes _index.json next to the files it downloads; use it when
    present. Otherwise read Markdown front matter (the CI fixtures work this way).
    """
    root = Path(corpus_dir)
    index = root / "_index.json"
    if index.exists():
        entries = json.loads(index.read_text(encoding="utf-8"))
        return [(root / e["file"], str(e.get("doc_version", "unknown")),
                 bool(e.get("superseded", False))) for e in entries]
    found = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".pdf", ".md", ".html", ".txt"}:
            continue
        if path.name.lower().startswith("readme"):
            continue
        meta = _front_matter(path) if path.suffix.lower() == ".md" else {}
        found.append((path, meta.get("doc_version", "unknown"),
                      meta.get("superseded", "false").lower() == "true"))
    return found


def wait_for_chroma(settings: IndexerSettings, timeout: float = CHROMA_WAIT_SECONDS,
                    pause: float = 5.0) -> Any:
    """A connected Chroma client, retrying until `timeout` seconds have passed.

    On a fresh cluster the Job and the chroma StatefulSet are applied together, so
    the first attempts land before Chroma listens. Waiting here, instead of failing
    and letting the Job's backoff retry, keeps the backoffLimit for real failures."""
    from .store import get_client

    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            return get_client(settings)
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise SystemExit(f"chroma at {settings.chroma_host}:{settings.chroma_port} "
                                 f"not reachable after {timeout:.0f}s: {exc}") from exc
            log("waiting for chroma", attempt=attempt, error=str(exc)[:120])
            time.sleep(pause)


def run_pipeline(settings: IndexerSettings, client: Any | None = None) -> int:
    from .chunk import chunk
    from .clean import clean
    from .embed import embed
    from .extract import extract
    from .store import store

    from .chunk import storage_amplification
    from .tokens import count_tokens

    documents = discover(settings.corpus_dir)
    if not documents:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    all_chunks: list[Chunk] = []
    original_tokens = stored_tokens = 0
    for path, doc_version, superseded in documents:
        if not path.exists():
            log("document missing; skipping", source=path.name)
            continue
        doc = clean(extract(str(path), doc_version, superseded))
        pieces = chunk(doc, settings.chunk_size, settings.chunk_overlap)
        original = sum(count_tokens(p.text) for p in doc.pages)
        stored = sum(count_tokens(c.text) for c in pieces)
        original_tokens += original
        stored_tokens += stored
        log("chunked", source=path.name, pages=len(doc.pages),
            ocr_pages=sum(1 for p in doc.pages if p.ocr), chunks=len(pieces),
            original_tokens=original, stored_tokens=stored,
            amplification=round(storage_amplification(doc, pieces), 3),
            doc_version=doc_version, superseded=superseded)
        all_chunks.extend(pieces)
    if not all_chunks:
        raise SystemExit("every document produced zero chunks; nothing to index")
    log("corpus chunked", documents=len(documents), chunks=len(all_chunks),
        chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap,
        original_tokens=original_tokens, stored_tokens=stored_tokens,
        amplification=round(stored_tokens / max(original_tokens, 1), 3))
    vectors = embed([c.text for c in all_chunks], settings)
    return store(all_chunks, vectors, settings, client)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    parser.add_argument("--dummy", action="store_true", help="write three fake chunks")
    args = parser.parse_args()
    settings = get_settings()
    log("indexer starting", dummy=args.dummy, embed_model=settings.embed_model,
        collection=settings.collection, chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        pod=os.environ.get("HOSTNAME", "-"))
    started = time.perf_counter()
    try:
        client = wait_for_chroma(settings)
        if args.dummy:
            from .dummy import write_dummy_index

            count = write_dummy_index(settings, client)
        else:
            count = run_pipeline(settings, client)
    except NotImplementedError as exc:
        log("indexer stage not implemented yet", error=str(exc))
        return 2
    log("indexer finished", chunks=count, seconds=round(time.perf_counter() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
