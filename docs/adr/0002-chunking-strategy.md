# ADR 0002: Structure-aware chunking with overlap and a heading prefix

## Status
Accepted · 2026-11-08

## Context
The corpus is rule books, a student handbook, two annual reports and two HTML pages
(`docs/CORPUS.md`). Its value sits in small, exact units: a clause number and the
sentence that follows it ("1.21 ... attendance"), a fee row in a table, a definition that
runs over several numbered sub-sections. Retrieval is scored by `(source, section)`
(`docs/ARCHITECTURE.md:175-182`), so the `section` metadata on every chunk must be the
heading a reader would cite, and a chunk that cuts a clause in half or merges two
sections produces a citation that is wrong even when the text is right.

Three extraction facts shaped the design: the 2026 Academic Rules Handbook is set in two
columns that `pdfplumber` interleaves line by line; tables come out with their columns
stacked; the per-chunk budget is read from the ConfigMap as `CHUNK_SIZE`/`CHUNK_OVERLAP`
(`k8s/base/configmap.yaml:31-32`) and had to be expressed in a unit that needs no model
download inside the Job.

## Decision
`indexer/pipeline/chunk.py` splits **sections → paragraphs → sentences → hard cut**, in
that order, never crossing a section boundary (`chunk.py:3-5`, `:157-218`):

* **Section detection** (`chunk.py:68-86`): Markdown `#` lines, numbered titles such as
  `5.4 Academic Warning`, short ALL-CAPS lines and short Title-Case lines. A numbered
  line counts as a heading only when its title reads like a title (`_titleish`,
  `:57-65`); `3.32 A new admission is closed if ...` is a clause, not a heading. A
  section that opens with a clause-shaped sentence and has no heading yet takes the
  clause number as its label (`_clause_label`, `:89-93`, used at `:187-190`), so
  "Preamble" is the label only when nothing better exists.
* **Packing** (`_pack`, `:110-143`): paragraphs that fit stay whole; a paragraph longer
  than the budget is split on sentence boundaries (`_units_for_section`, `:146-154`;
  pipe-delimited table rows are the "sentences" of a table, `:96-99`); only a single
  sentence longer than the budget is hard-cut on tokens (`:130-135`).
* **Overlap**: neighbouring chunks in the same section share the last `overlap` tokens
  of the previous chunk (`:125`, `tokens.py:25-36`), so a rule that straddles a cut is
  whole in at least one of them.
* **Heading prefix**: every chunk's text starts with its section heading
  (`chunk.py:207`), with the heading's tokens taken out of the body budget
  (`chunk.py:199-202`). It costs a few tokens
  of storage and makes the exact term that users quote ("clause 1.21") part of the
  embedded text and of the `$contains` target used by the hybrid pass
  (`api/app/retrieval/chroma.py:124-132`).
* **Unit**: a regex word-token, `\w+|[^\w\s]` (`indexer/pipeline/tokens.py:14`). It
  tracks BPE counts within roughly 20 % for English prose, is deterministic and needs no
  tokenizer download. Shipped values: `CHUNK_SIZE=250`, `CHUNK_OVERLAP=40`
  (`configmap.yaml:31-32`), chosen from the three measured configurations in
  `docs/PIPELINE.md`; the code defaults stay at the starter's 400/50.
* **Upstream support**: two-column pages are detected by a vertical gutter that almost
  no word crosses and extracted column by column, left before right, with page-wide
  lines (running titles) pulled out first (`indexer/pipeline/extract.py:94-148`);
  tables are re-rendered as pipe rows under a `[table]` marker (`extract.py:57-66`,
  `:163-171`); the Clean stage removes repeating headers, footers and page numbers,
  re-joins hyphenated words and soft wraps, and deliberately keeps clause numbers, bullets
  and table rows (`indexer/pipeline/clean.py:5-24`).

Chunk ids are `<source-slug>-<ordinal:04d>` (`chunk.py:209`), and every chunk carries
`source`, `section`, `page`, `doc_version`, `superseded` (`:208-216`).

## Options considered
* **Fixed-size token windows** (e.g. 400 tokens, stride 350). Simplest and what most
  tutorials do. It ignores structure: a window routinely starts mid-clause and ends
  mid-table, and there is no honest `section` to put on it, so citations would point at
  "whatever heading was last seen", which fails the evaluation rule and misleads users.
* **One chunk per page.** Pages are a printing artefact. The handbook puts several
  clauses on one page and splits definitions across two; a page-chunk averages far more
  than 400 tokens, which dilutes the embedding, and cites a page rather than a rule.
* **One chunk per clause.** Attractive for the rule books, meaningless for the annual
  reports, the handbook prose and the HTML tables, which have no clauses. It also makes
  very short chunks (a one-line clause) that embed poorly on their own. The clause-aware
  heading rule gives us the clause label where it exists without forcing the rest of the
  corpus into that shape.
* **Semantic chunking** (split where embedding similarity between sentences drops).
  Needs an embedding call per sentence inside the Job, is non-deterministic across model
  versions, and the gains over heading-aware splitting on documents that already have
  headings did not justify the cost.

## Consequences
* **What was measured.** Locally (local-hash-v1 embeddings, OCR off) three
  configurations were indexed and scored on the 24-question golden set: 250/40 gave
  recall@5 0.714 with 585 chunks, 400/50 gave 0.667 with 360 chunks, 600/80 gave 0.667
  with 266 chunks, so 250/40 shipped. Storage amplification was 1.20 at 250/40 and 0.94 on
  the fixture corpus (stored tokens / original tokens; the heading prefix and overlap add
  tokens, the removal of headers, footers and page numbers takes more away). The table
  with the three measured size/overlap configurations, their recall@5 and chunk counts
  lives in `docs/PIPELINE.md` ("Chunking"), together with the before/after of a table
  extraction and the OCR losses; this ADR does not repeat it.
* **Heading detection is heuristic.** Title-Case lines that are not headings (a short
  sentence fragment at the top of a paragraph) can open a spurious section; ALL-CAPS
  table captions can too. The evaluation rule's prefix match (`5.4` matches `5.4 Academic
  Warning`) tolerates a heading that is slightly longer than the golden one but not a
  wrong one, so the golden set is also a test of the heading rule.
* **Column detection can misfire** on pages with a wide table (the gutter test is
  satisfied by the table's column gap). The two thresholds (`extract.py:89-91`) are the
  knobs; the `two_column_pages` count in the `extracted pdf` log line
  (`extract.py:184-185`) is how to see it.
* **Changing `CHUNK_SIZE` or `CHUNK_OVERLAP` requires a re-index**, not an API change;
  the values are recorded in the collection metadata (`indexer/pipeline/store.py:55-56`)
  so an index built with other settings is visible.
* **The token unit is not the model's.** A 400-word-token chunk is roughly 450 to 500
  BPE tokens for `nomic-embed-text`, well inside its 8192 context; for the generator,
  five chunks plus the prompt stay under the `num_ctx` of 8192 set in
  `api/app/providers/llm/ollama.py:32`.
