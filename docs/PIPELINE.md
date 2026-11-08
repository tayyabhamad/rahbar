# Indexing and retrieval pipeline

Every number below was measured, not guessed, with the 24-question golden set in
`eval/golden.yaml`. Two setups appear, and each row says which:

* **laptop**: the CI embedder (`EMBED_MODEL=local-hash-v1`, a deterministic hashed
  bag-of-words, see `docs/ARCHITECTURE.md`) with the **stub LLM**, OCR off (no tesseract
  on the laptop), Chroma 1.0.12 and Redis in Docker. Cheap, deterministic, and the
  setting CI uses; it exposes every stage but measures lexical overlap, not meaning.
* **cluster**: the `dev-hosted` overlay on minikube (Calico, 3 GB node): the full
  pipeline with OCR, `baai/bge-m3` embeddings (1024 dimensions) and
  `google/gemini-3.1-flash-lite` generation, both through OpenRouter with the key in
  `rahbar-secret`. **This is the shipped configuration and the source of the committed
  `eval/baseline.json` and `eval/results.json`** (8 November 2026).

The Ollama path (`nomic-embed-text` + `llama3.2:3b`, the `dev` overlay) is wired the same
way; re-run `make eval` there and refresh the baseline with `--update-baseline` if you
switch to it.

## Extraction

**Which documents needed OCR.** `pdfplumber` returned no text layer for:

| Document | Pages | Pages with no text layer |
| --- | ---: | ---: |
| Faculty Entrepreneurship and Technical Start-ups Policy (ORIC) | 8 | 8 (fully scanned) |
| Undergraduate Academic Rules, Aug 2023 | 26 | 24 (scanned; only the cover and one page have text) |
| Academic Rules Handbook, Jun 2026 | 58 | 5 (image pages: cover, dividers) |
| Student Handbook, Jun 2026 | 64 | 4 (image pages) |

(`docs/evidence/failure-A-scanned-without-ocr.txt`.) The extract stage treats any page with
fewer than 25 characters of text as image-only and sends it to `pytesseract` via
`pdf2image` (`indexer/pipeline/extract.py`, `_ocr_page`); the page is marked `ocr=True`.
Locally OCR is unavailable, so those pages are logged as
`page has no text layer and OCR is unavailable` and kept empty rather than dropped; the
indexer image installs `tesseract-ocr` and `poppler-utils` (`indexer/Dockerfile`), so on
the cluster both scanned documents are indexed.

**Two-column pages.** The 2026 handbook and the Student Handbook mix full-width
paragraphs with two-column blocks on the same page. pdfplumber's plain `extract_text`
interleaves the columns line by line, so clause 1.9 and clause 1.15 ended up on one line
and every heading dissolved: before the fix the whole first chapter of the handbook was
one section called "Preamble". A first attempt split each page at a vertical gutter
that no word crossed; it caught only 5 of the 58 pages because the column widths change
from block to block. The shipped extractor takes page text from pdfminer's layout
analysis instead (`extract.py`, `_miner_pages`): characters are grouped into lines,
lines into text boxes, and boxes are ordered in reading order (`boxes_flow=0.5`), so a
two-column block comes out column by column while a full-width paragraph stays whole.
pdfplumber still provides the tables and the "does this page have a text layer" check.
After the switch the handbook has the sections a reader expects ("Attendance
Requirements", "Grade Points", "3 Admission Policy", "Academic Warning/Probation",
"Freezing of Semester", "5 Academic Assessment", ...) and the Student Handbook its
chapters ("V - Examination Rules", "Code Of Conduct", "Academic Dishonesty").

**A mangled table, before and after.** Handbook page 12, the grade-point table is two
small tables side by side. Plain text extraction reads across both:

```
Grade Points Grade Points A+ 4.00 C+ 2.33 A 4.00 C 2.00 A- 3.67 C- 1.67 B+ 3.33 D+ 1.33 ...
```

so "A+ 4.00 C+ 2.33" looks like one row of four values. `page.extract_tables()` is run on
every page and each table is re-rendered as pipe rows under a `[table]` marker and appended
to the page, so the same data is also stored as

```
| C- | 1.67 | | B+ | 3.33 | D+ | 1.33 | | B | 3.00 | D | 1.00 | | B- | 2.67 | F | 0.00 |
```

where every grade sits next to its own points. The HTML fee and grading pages are parsed
into the same pipe-row form directly (`_TextExtractor`), which is why the fee questions
(q05, q06, q17, q19) all score recall 1.0.

**What could not be recovered.** (1) The 5-page Prospectus "Academic Rules and
Regulations" is set in letter-spaced, multi-column display type; its text comes out as
`Fa S T- N U C E S ...` fragments. It stays in the corpus (it is the same rules with
different clause numbers, property 2/7) but the golden set does not rely on it.
(2) Without OCR the 2023 rules are one cover chunk, so the explicit "previous 2023
version" question (q15) cannot be answered locally; it is the reason the version-type
recall is 0.33 in the local numbers. On the cluster, where the indexer image has
tesseract, the same document yields 60 chunks and the ORIC policy 26
(`docs/evidence/indexer-job-indexer-log.txt`), and the same golden set with the same
embedder scores recall@5 0.714 overall and **0.667 on version questions** (q15 is now
answered); with the OCR'd vocabulary two must-abstain questions also climb above the
0.21 floor (best 0.266 and 0.272), which is the lexical embedder's limit again, not a
chunking change. (3) Table-of-contents pages produce chunks under the
"VISION Table of Contents" label; they are harmless noise.

## Cleaning

Rules, in order (`indexer/pipeline/clean.py`):

1. **Repeating headers and footers.** A line whose digit-blanked signature appears in the
   top or bottom three lines of at least 30 % of the pages (and on at least three pages) is
   removed everywhere. Blanking digits is what makes "Academic Rules Handbook 2026 | 7" and
   "... | 8" one pattern. This needed the two-column fix first: a header cut in half at the
   gutter has a different signature on every page and survived as a fake heading
   ("National University Of Computer And").
2. **Bare page numbers** ("7", "- 7 -", "Page 7 of 53") on a line of their own. A clause
   number alone on a line ("5.1", which pdfminer emits as its own box) is re-attached to
   the line that follows *before* rule 1 runs, and a signature with no letters is never
   counted as a running header: without both, "5.1", "5.2" and "1.9" shared the
   digit-blanked signature `#.#`, appeared at the edge of most pages, and were stripped
   as a "header", so every clause lost its number.
3. **Line-end hyphenation**: "regis-\ntration" becomes "registration", only when the next
   line starts with a lowercase letter.
4. **Soft line wraps** inside a paragraph are joined, unless the previous line ends a
   sentence or the next line looks like a heading, bullet, numbered clause or table row.
5. **Whitespace**: non-breaking spaces, tabs and runs of spaces collapse; three or more
   blank lines collapse to one.

**Deliberately not stripped:** clause numbers at the start of a line ("1.21", "5.4",
"3.1.2"), bullet markers and pipe-delimited table rows. Clause numbers are both headings
and exact-term content (corpus property 7): the hybrid retrieval pass and every citation
of the form `[handbook / 1.21]` depend on them being in the text.

## Chunking

Unit: a **regex word-token** (`\w+|[^\w\s]`, `indexer/pipeline/tokens.py`); for English
prose it tracks BPE counts within about 20 %. `CHUNK_SIZE` and `CHUNK_OVERLAP` in the
ConfigMap are in this unit. The chunker is structure-aware (ADR 0002): sections, then
paragraphs, then sentences, with the section heading prefixed to every chunk and `overlap`
tokens shared between neighbours in the same section. Table rows are never split.

**Measured configurations** (real corpus, 24 golden questions, local-hash-v1, OCR off):

| Size (tokens) | Overlap (tokens) | recall@3 | recall@5 | Chunks stored | Storage amplification |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 250 | 40 | 0.643 | **0.714** | 585 | 1.20 |
| 400 | 50 | 0.571 | 0.667 | 360 | 1.13 |
| 600 | 80 | 0.643 | 0.667 | 266 | 1.13 |
| 100 | 0 (failure mode B) | 0.595 | 0.643 | 1352 | 1.05 |

Storage amplification is stored tokens divided by original tokens (the lecture's
150 / 120); the heading prefix and the overlap add tokens, the removal of headers, footers
and page numbers takes some away (the fixture corpus comes out at 0.94).

**Shipped: 250 / 40.** It is the only configuration that improves recall@5 (+4.8 points
over 400/50) and it also has the best recall@3 together with 600/80. The cost is 63 % more
chunks and 6 % more stored tokens, which at 585 chunks is irrelevant. The reason it wins
with this embedder is lexical: a smaller chunk has fewer unrelated words diluting the
bag-of-words vector. With `nomic-embed-text` the gap may narrow, so the cluster run should
repeat the three rows (change `CHUNK_SIZE`/`CHUNK_OVERLAP` in the ConfigMap, `make index`,
`make eval`) before the baseline is frozen. Fixed 100-token windows with no overlap (the
failure-mode B row) lose 7 points of recall@5 and let a must-abstain question clear the
relevance floor: see `docs/FAILURE-ANALYSIS.md`.

**recall@5 by question type at 250/40:** ordinary 0.708 (12), exact 1.000 (4), version
0.333 (3), injection 0.750 (2), abstain n/a (3). Exact-term questions are *not* the worst
here, which is the opposite of what the handout predicts for an embedding model, and the
reason is the hybrid pass: a question that carries a clause number, course code, amount or
percentage triggers a second Chroma query restricted with `where_document={"$contains":
term}` and its hits are merged in front of the semantic hits
(`api/app/retrieval/chroma.py`). Version questions are worst because two of the three
need the 2023 document, which has no text locally (see Extraction).

**Shipped baseline (cluster: OCR on, bge-m3, gemini-3.1-flash-lite, 250/40, floor 0.45,
`eval/baseline.json`):**

| | recall@3 | recall@5 | citation validity | abstention |
| --- | ---: | ---: | ---: | ---: |
| all 24 questions | 0.881 | **0.929** | 1.000 | 1.000 (3 of 3) |
| ordinary (12) | 0.792 | 0.875 | 1.000 | |
| version (3) | 1.000 | 1.000 | 1.000 | |
| exact (4) | 1.000 | 1.000 | 1.000 | |
| injection (2) | 1.000 | 1.000 | 1.000 | |

Mean 4.8 s per question. Two answerable questions were declined by the model
(`answered_from_context: false`, counted as false abstentions in `results.json`): q15,
which asks for the superseded 2023 wording and whose OCR'd chunk the model judged too
garbled to quote, and q11, a faculty head-count from an annual-report table. Both are
retrieval hits (recall 1.0); the honesty rule, not retrieval, held them back. The two
ordinary misses (q07 ChatGPT policy at 0.5, q12 campuses at 0) are discussed in
`docs/ENGINEERING-NOTES.md`, question 5. Against the lexical laptop numbers (recall@5
0.714, version 0.333, exact 1.0) the dense embedder lifts ordinary and version
questions; exact-term questions were already at 1.0 because the hybrid pass carries them.

## Embedding

| | Cluster, hosted (shipped) | Cluster, local Ollama | CI and laptop runs |
| --- | --- | --- | --- |
| Model | `baai/bge-m3` via OpenRouter `POST /embeddings` (`EMBED_PROVIDER=hosted`) | `nomic-embed-text` via Ollama `POST /api/embed` | `local-hash-v1` (hashed bag-of-words, no network) |
| Vector dimension | 1024 | 768 | 512 |
| Where the pin lives | ConfigMap `rahbar-config`, keys `EMBED_MODEL` and `EMBED_PROVIDER`, read by the api and the indexer; the `dev-hosted` overlay sets both (`k8s/overlays/dev-hosted/configmap-patch.yaml`) | base `k8s/base/configmap.yaml` | the `ci` overlay patches the same key |

Switching the pin was exercised on the cluster: the re-index Job logged `embedding pin
changed; recreating the collection` (`local-hash-v1`/512 to `baai/bge-m3`/1024), the api
pods restarted onto the new value and `/readyz` reported `embedding_pin: ok: baai/bge-m3`
(`docs/evidence/indexer-job-hosted-log.txt`).

The indexer writes `embed_model` and `dimension` into the collection metadata
(`indexer/pipeline/store.py`, `collection_metadata`). The api compares them with its own
`EMBED_MODEL` and known dimension in `/readyz` (`api/app/services/readiness.py`,
`check_pin`) and refuses to become ready on a mismatch; `docs/evidence/embedding-pin-mismatch.txt`
shows the 503 body and the `embedding pin mismatch` log line from a local run. The two
copies of the local-hash code are kept byte-identical by a test
(`indexer/tests/test_embedding_local.py`).

## Store

585 chunks at the shipped configuration locally (360 at 400/50); the cluster count with
OCR will be higher, since the two scanned documents add about 32 pages of text. Metadata
schema per chunk, all required by section 3.1 and used by the retriever and the citations:

| Field | Type | Example |
| --- | --- | --- |
| `source` | str | `academic-rules-handbook-jun-2026.pdf` |
| `section` | str | `Attendance Requirements` |
| `page` | int | `10` (first page of the chunk) |
| `doc_version` | str | `2026-06` |
| `superseded` | bool | `false` |

Collection metadata: `embed_model`, `dimension`, `chunk_size`, `chunk_overlap`,
`chunk_count`, `index_generation` (a UTC stamp that changes on every run and namespaces
the cache). Chunk ids are `<source-slug>-<ordinal:04d>`; a re-index with the same pin
upserts in place and deletes the ids the new run did not produce, so the api never sees an
empty collection.

## Relevance floor

Chosen with the lecture's rule: the lowest value with zero must-abstain questions answered
from context. Best similarity per golden question at 250/40 (`results.json`), lowest
first: q22 0.120 (abstain), q10 0.151, q20 0.172 (abstain), q07 0.172, q23 0.177, q18
0.186, q16 0.201, q11 0.205, **q21 0.208 (abstain)**, q24 0.234, ...

| `RELEVANCE_FLOOR` | Must-abstain answered | False abstentions | Verdict |
| ---: | ---: | ---: | --- |
| 0.15 | 2 of 3 (q20, q21) | 1 | ruled out: the gate requires 100 % |
| 0.19 | 1 of 3 (q21 at 0.208) | 2 | ruled out by q21 |
| **0.21** | 0 of 3 | 6 (q07, q10, q11, q16, q18, q23) | lowest floor with zero false answers |

So with the lexical embedder the two populations overlap: the three questions the corpus
cannot answer score between 0.12 and 0.21, and six answerable questions score below 0.21.
That is a property of bag-of-words cosine, not of the floor rule, and it is why the floor is
only the first line of defence: the model's own `answered_from_context: false` is the
second, and the stub provider used locally has no second line. The committed
`eval/baseline.json` was produced at 0.21 so that the abstention gate is honest. The
ConfigMap ships `RELEVANCE_FLOOR=0.45` for `nomic-embed-text`, whose cosine scores run
higher and separate better; repeat this table on the cluster (`make eval` once per floor)
and put the chosen value in the ConfigMap with the same one-line justification.

**With the dense embedder (cluster, bge-m3, real model), from the shipped run's
per-question best scores:** must-abstain questions scored 0.492 (q22 hostel rent), 0.535
(q20 parking) and 0.622 (q21 laptop loan); the weakest answerable ones 0.467 (q16, clause
1.21), 0.535 (q23) and 0.561 (q10). What each floor would do to this run:

| `RELEVANCE_FLOOR` | Must-abstain caught by the floor | False abstentions caused by the floor | Verdict |
| ---: | ---: | ---: | --- |
| **0.45** (shipped) | 0 of 3 | 0 | the model declined all three itself: abstention 100 %, nothing lost |
| 0.50 | 1 of 3 (q22) | 1 (q16, an exact-term question at 0.467) | ruled out |
| 0.55 | 2 of 3 | 2 (q16, q23) | ruled out |
| 0.65 | 3 of 3 | 12 | ruled out |

So the floor cannot do the abstaining on its own with this embedder either: the absent
topics are semantically close to real university text (fees, facilities). The floor's job
is to stop obvious junk from reaching the model; honest refusal comes from the
`answered_from_context: false` contract, which held on all three (`eval/results.json`).
0.45 is kept because every higher value starts paying in exact-term recall before it buys
a single floor-caught abstention.

## Semantic cache

Redis, ADR 0003. A hit is cosine similarity between question vectors at or above
`CACHE_THRESHOLD`. The rule is again the lowest threshold with zero false hits on the test
questions. Pairwise similarity between the 24 golden questions with the CI embedder:

| Pair | Similarity | Same answer? |
| --- | ---: | --- |
| q15 "previous 2023 version of the rules" / q16 "clause 1.21" | 0.367 | no: a false hit |
| q02 "CGPA to stay off warning" / q24 (injection) | 0.345 | no |
| q16 "clause 1.21" / q18 "clause 4.25" | 0.333 | no |
| paraphrase: "What CGPA puts a student on academic warning?" / "Which CGPA places a student on academic warning?" | 0.750 | yes |
| paraphrase: "How much is the admission fee?" / "What is the admission fee amount?" | 0.643 | yes |
| paraphrase: "What is the attendance requirement?" / "How much attendance is required?" | 0.356 | yes, but below any safe threshold |

| `CACHE_THRESHOLD` | Result with local-hash-v1 |
| ---: | --- |
| 0.35 | false hit: q15 would be served q16's answer (0.367) |
| 0.40 | zero false hits; two of three paraphrases hit |
| 0.92 (shipped) | zero false hits; only near-identical wording hits |

0.40 is the lowest safe value for the lexical embedder, which is what the `ci` overlay's
0.95 and the local runs stay well above.

**With the dense embedder** (the 24 golden questions embedded with `baai/bge-m3` inside an
api pod on the cluster, same code path as the cache):

| Pair | Similarity | Same answer? |
| --- | ---: | --- |
| q13 "current minimum attendance" / q15 "previous 2023 version of the rules" | 0.780 | no: a false hit |
| q17 "one-time admission fee" / q20 "parking permit cost" | 0.693 | no |
| q01 "attendance in lectures and labs" / q13 "current minimum attendance" | 0.692 | partly |
| paraphrase: "What CGPA puts a student on academic warning?" / "Which CGPA places a student on academic warning?" | 0.987 | yes |
| paraphrase: "How much is the admission fee?" / "What is the admission fee amount?" | 0.987 | yes |
| paraphrase: "What is the attendance requirement?" / "How much attendance is required?" | 0.937 | yes |

| `CACHE_THRESHOLD` | Result with bge-m3 |
| ---: | --- |
| 0.78 | false hit: a question about the 2023 rules would be served the 2026 answer |
| 0.80 | lowest value with zero false hits; all three paraphrases hit |
| **0.92** (shipped) | zero false hits, all three paraphrases still hit, 0.14 of margin above the worst pair |

The lecture's rule picks 0.80; with only 24 test questions a 0.02 margin is one unseen
question away from a cached wrong answer, so 0.92 ships and the margin is the
justification. The ConfigMap ships 0.92 for `nomic-embed-text`,
where unrelated questions typically score 0.6 to 0.8 and paraphrases above 0.9; measure the
same three rows on the cluster before trusting it. Entries are never written for
follow-ups, abstentions, fallbacks or answers without a valid citation, and a re-index
invalidates the previous generation (`api/app/services/cache.py`). The eval run itself
showed why the rules matter: during the local measurements a cached answer from before a
code fix was served back (`X-Cache: HIT`, 0.01 s) until Redis was flushed.

## Evaluation method

`eval/run_eval.py`. A retrieved passage matches a golden `relevant` entry when the `source`
file name is equal (case-insensitive) and the golden `section`, lower-cased and
whitespace-collapsed, equals the retrieved section or is a prefix of it followed by a
space, `.`, `:` or the end (`5.4` matches `5.4 Academic Warning`, not `5.41 ...`). A golden
`chunk_id` equal to a retrieved id also matches, and a golden entry without a section
matches on source alone (used for the scanned 2023 document). recall@k is the fraction of a
question's relevant entries matched by any of its top-k retrieved passages, averaged over
questions that have relevant entries. Citation validity applies the same rule between a
response's `citations` and its own `retrieved` list; abstention correctness is the fraction
of must-abstain questions with `answered_from_context: false`. The gate fails on a
recall@5 drop of more than 5 points against `eval/baseline.json`, any must-abstain question
answered, or citation validity below 95 %. The section labels in `eval/golden.yaml` were
taken from a dry run of the chunker on 8 November 2026 after the two-column fix, before any
tuning.
