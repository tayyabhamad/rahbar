# Indexing and retrieval pipeline

Every number below was measured, not guessed. Unless a row says otherwise it comes from a
local run on 8 November 2026 with the **CI embedder** (`EMBED_MODEL=local-hash-v1`, a
deterministic hashed bag-of-words, see `docs/ARCHITECTURE.md`), the **stub LLM**, OCR
off (no tesseract on the laptop), Chroma 1.0.12 and Redis in Docker, and the 24-question
golden set in `eval/golden.yaml`. The same scripts (`make index`, `make eval`) produce the
cluster numbers with `nomic-embed-text` and Ollama; re-run them there and refresh
`eval/baseline.json` with `--update-baseline`. Where the two embedders are expected to
differ, the text says so.

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
recall is 0.33 in the local numbers. (3) Table-of-contents pages produce chunks under the
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

## Embedding

| | Cluster | CI and local runs |
| --- | --- | --- |
| Model | `nomic-embed-text` via Ollama `POST /api/embed` | `local-hash-v1` (hashed bag-of-words, no network) |
| Vector dimension | 768 | 512 |
| Where the pin lives | ConfigMap `rahbar-config`, key `EMBED_MODEL`, read by the api and the indexer (`k8s/base/configmap.yaml`) | the `ci` overlay patches the same key |

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
0.95 and the local runs stay well above. The ConfigMap ships 0.92 for `nomic-embed-text`,
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
