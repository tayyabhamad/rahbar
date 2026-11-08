# Corpus

Source: FAST-NUCES public website (nu.edu.pk) · Documents: 9 · Pages: 281 (279 PDF pages
plus two HTML pages; the per-document counts are in the table below)

The documents are listed with URL, size and SHA-256 in `corpus/MANIFEST.md`, which
`corpus/fetch.py` writes; the files themselves live in `corpus/raw/` and are not
committed. The list we fetch from is `corpus/sources.yaml`.

```bash
make fetch                                  # corpus/raw/*.pdf|*.html, _index.json, MANIFEST.md
python corpus/fetch.py --verify             # re-hash local files against MANIFEST.md
python corpus/fetch.py --list https://www.nu.edu.pk/Downloads   # find more documents
```

Ethics: every document is a public page that needs no login; the fetcher downloads one
file at a time with a 2 s pause and a clear User-Agent (`corpus/fetch.py:40`, `:102`,
`:135`), and forty teams share the same site, so that pause stays. The weekly CronJob
repeats the fetch once a week, not more.

## The documents

| Document | File (`corpus/raw/`) | `doc_version` | `superseded` | Pages | Chunks (local dry run) | Notes |
| --- | --- | --- | --- | ---: | ---: | --- |
| Undergraduate Academic Rules (Aug 2023) | `undergraduate-academic-rules-aug-2023.pdf` | `2023-08` | **true** | 26 | 1 | 24 of 26 pages are scanned; see property 3 |
| Academic Rules Handbook (Jun 2026) | `academic-rules-handbook-jun-2026.pdf` | `2026-06` | false | 58 | 60 | current rules; two-column layout; numbered clauses (1.21 attendance, 5.x academic warning) |
| Prospectus 2026 - Academic Rules and Regulations | `prospectus-2026-academic-rules-and-regulations.pdf` | `2026` | false | 5 | 17 | the same rules condensed, different clause numbers, multi-column text |
| Student Handbook (Jun 2026) | `student-handbook-jun-2026.pdf` | `2026-06` | false | 64 | 80 | Generative AI policy with numbered sub-sections, long definitions, mentions hostels exist |
| Annual Report 2019-20 | `annual-report-2019-20.pdf` | `2019-20` | false | 33 | 41 | faculty-by-campus tables |
| Annual Report 2023-24 | `annual-report-2023-24.pdf` | `2023-24` | false | 85 | 146 | faculty-by-campus and enrolment tables |
| Fee Structure page | `fee-structure-page.html` | `2026-27` | false | 1 | 8 | HTML tables: tuition per credit hour, admission fee Rs 30,000, refund schedule |
| Grading page | `grading-page.html` | `2026` | false | 1 | 7 | HTML grade-point table, warning/probation rules |
| Faculty Entrepreneurship and Technical Start-ups Policy | `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` | `2022-05` | false | 8 | 0 locally | fully image-only; needs OCR (property 3) |

Chunk counts are from the local dry run of extract → clean → chunk at `CHUNK_SIZE=400`,
`CHUNK_OVERLAP=50` (regex word-tokens) with OCR off: 360 chunks in total (585 at the
shipped 250/40; see `docs/PIPELINE.md`). In the indexer
image tesseract and poppler are installed (`indexer/Dockerfile`), so the two scanned
documents produce more chunks on the cluster. Measured on minikube on 8 November 2026
(`docs/evidence/indexer-job-indexer-log.txt`, the `"chunked"` lines of the re-index Job):
**703 chunks** at 250/40, storage amplification 1.18 (132,381 stored / 111,896 original
tokens):

| Document | Pages | OCR'd pages | Chunks (cluster, OCR on) | Chunks (laptop, no OCR) |
| --- | ---: | ---: | ---: | ---: |
| Undergraduate Academic Rules, Aug 2023 | 26 | 24 | 60 | 1 |
| Academic Rules Handbook, Jun 2026 | 58 | 5 | 111 | 63 |
| Prospectus 2026: Academic Rules and Regulations | 5 | 0 | 34 | 19 |
| Student Handbook, Jun 2026 | 64 | 4 | 157 | 89 |
| Annual Report 2019-20 | 33 | 0 | 66 | 41 |
| Annual Report 2023-24 | 85 | 0 | 232 | 145 |
| Fee Structure page | 1 | 0 | 8 | 8 |
| Grading page | 1 | 0 | 9 | 7 |
| Faculty Entrepreneurship and Technical Start-ups Policy | 8 | 8 | 26 | 0 |

## The seven properties

| # | Required property | Document(s) that provide it | Evidence or notes |
| --- | --- | --- | --- |
| 1 | At least 6 documents, 40 pages | all nine | 9 documents, 281 pages, 360 chunks locally (`corpus/MANIFEST.md`, dry-run log `corpus chunked`) |
| 2 | Two versions of one policy | Undergraduate Academic Rules (Aug 2023) vs Academic Rules Handbook (Jun 2026); the Prospectus 2026 is a third rendering of the current rules with its own clause numbers | The 2026 handbook supersedes the 2023 rules. `corpus/sources.yaml:15-25` sets `doc_version` and `superseded`; `fetch.py` copies them into `corpus/raw/_index.json` and `MANIFEST.md`; `indexer/pipeline/run.py:49-53` reads `_index.json`; every chunk carries `doc_version` and `superseded` (`indexer/pipeline/chunk.py:208-216`); the retriever filters `where={"superseded": False}` unless the question asks for an older version (`api/app/retrieval/chroma.py:120-121`, `api/app/services/guards.py:20-29`; the filter is on by default, `api/app/config.py:47-49`). Golden questions q13-q15 (`eval/golden.yaml:118-140`) test it. |
| 3 | Scanned, image-only PDF | Faculty Entrepreneurship and Technical Start-ups Policy (all 8 pages); Undergraduate Academic Rules Aug 2023 (24 of 26 pages) | `pdffonts` on the ORIC policy prints an empty font list (run via the indexer image, which has poppler: `docker run --rm -v "$PWD/corpus/raw:/corpus/raw:ro" --entrypoint pdffonts rahbar-indexer:dev /corpus/raw/faculty-entrepreneurship-and-technical-start-ups-policy.pdf`), while the 2026 handbook lists embedded MyriadPro, Montserrat and Times fonts. On the laptop (no tesseract) the pipeline logs `page has no text layer and OCR is unavailable` for all 8 and 24 pages and the two documents yield 0 and 1 chunks; on the cluster the indexer image OCRs them (`ocr_pages: 8` and `ocr_pages: 24`, `lost_pages: 0`) and they yield 26 and 60 chunks (`docs/evidence/indexer-job-indexer-log.txt`). |
| 4 | A table with real information | Fee Structure page (tuition per credit hour, admission fee, refund schedule); Grading page (grade-point table); Annual Reports (faculty by campus and department) | HTML tables become pipe rows (`extract.py:199-281`), PDF tables are re-rendered under a `[table]` marker (`extract.py:57-66`, `:163-171`), and a table row is never split (`chunk.py:96-99`). Golden q04, q05, q06, q11, q17, q19 ask for table values. |
| 5 | Long definitions | Student Handbook (Generative AI policy: what counts as generative AI use, permitted and prohibited uses, numbered sub-sections); Academic Rules Handbook (definitions of academic warning, probation, W grade, condonable absence) | A chunk never crosses a section boundary and sub-clauses stay with their heading (`chunk.py:166-193`); paragraphs longer than the budget split on sentences, not mid-sentence (`chunk.py:146-154`). Golden q07 and q14 target the AI policy. |
| 6 | A deliberately absent topic | Hostel and transport rules: the prospectus "Transport and Hostel Facilities" PDF is left out (`corpus/sources.yaml:68-71`). The Student Handbook only mentions that in-campus hostels exist at two campuses | A student plausibly asks about hostel allotment, hostel fee or rent, application deadlines, or bus routes and transport fees; none of that is in the corpus. Golden q22 (`eval/golden.yaml:185-191`) is the near miss on purpose; q20 and q21 (parking, laptop scheme) are clean absences. All three carry `must_abstain: true`, and the gate requires every one to be declined (`eval/run_eval.py:31`). |
| 7 | Exact-term content | Academic Rules Handbook (clause numbers such as 1.21, 4.25; CGPA 2.00); Fee Structure page (Rs 30,000 admission fee, Rs 500 rechecking fee, 80 % refund by day 14); Grading page (grade points per letter grade) | Embeddings blur numbers, so questions carrying a clause number, course code, amount or percentage (`api/app/services/guards.py:31-34`) trigger a second query with `where_document={"$contains": term}` merged in front of the semantic hits (`chroma.py:124-132`); the heading prefix on every chunk (`chunk.py:207`) puts the clause number inside the chunk text. Golden q16-q19 (`eval/golden.yaml:142-169`) are the exact-term set. |

## Known gaps

* The 2023 rules are almost entirely scanned, so locally they contribute one chunk (the
  two pages with a text layer). The "older version" questions therefore depend on OCR
  having run in the Job; until then q15 is expected to retrieve little from that file.
* The Prospectus is multi-column marketing layout; its extracted text is partly garbled
  and it is kept as a secondary source of the current rules, not the primary one. The
  golden set points at the handbook for rules questions.
* Sizes in `MANIFEST.md` were recorded on 2026-11-08; the university site changes, so
  `python corpus/fetch.py --verify` after a fetch is the check that the documents are the
  ones this corpus was built and evaluated on.
