# Failure analysis: break one stage at a time

Four of the six stages were broken on purpose, one at a time, against the same golden set
(`eval/golden.yaml`, 24 questions) and the same shipped index (250/40). Runs are local
(8 November 2026): `local-hash-v1` embeddings, the stub LLM, `RELEVANCE_FLOOR=0.21`, OCR
off; the raw per-question output of each run is in `docs/evidence/failure-*.txt`. The
healthy numbers these are compared with are the committed `eval/baseline.json`:
recall@3 0.643, recall@5 0.714, citation validity 1.0, abstention correctness 1.0.

Stages D (prompt) and F (generation) need a real model to break; they are described at the
end with the exact knob and the metric that would show them, to be run on the cluster.

| Stage | How it was broken | recall@5 | abstention | What the gate said |
| --- | --- | ---: | ---: | --- |
| healthy | shipped configuration | 0.714 | 1.00 | pass |
| A · Extraction | scanned PDFs indexed without OCR | n/a (2 documents gone) | 1.00 | version-type recall 0.33; q15 unanswerable |
| B · Chunking | `CHUNK_SIZE=100`, `CHUNK_OVERLAP=0` | 0.643 | 0.67 | recall@5 7.1 points below baseline; abstention below 1.00 |
| C · Retrieval | `TOP_K=1` | 0.524 | 1.00 | recall@5 19.0 points below baseline |
| C · Retrieval | `EMBED_MODEL` changed on the api only | no traffic: pod not ready | | `/readyz` 503, `embedding pin mismatch` in the log |
| E · Source | `SUPERSEDED_FILTER=false`, version question | 1.0 (fixtures) | 1.00 | pass: the gate cannot see this one |

## A · Extraction: index the scanned PDF without OCR

**How.** `OCR_ENABLED=false` (or, as on this laptop, no tesseract binary) and index the
corpus. **Symptom.** The ORIC start-ups policy produces 0 chunks and the 2023
Undergraduate Academic Rules produce 1 chunk, the cover page; the indexer log shows
`"lost_pages": 8` and `"lost_pages": 24` for them
(`docs/evidence/failure-A-scanned-without-ocr.txt`). Any question about either document is
either declined ("the documents I have do not cover this") or answered from the wrong
document: the explicit "previous 2023 version" question (q15) retrieves the 2026 handbook
cover instead, which is a confident wrong-year answer waiting to happen once a model writes
prose around it. **Diagnosis.** Stage A, because the chunks do not exist: the retrieved
ids in the per-request log never include the source, and `chunked` lines in the indexer
log show `"chunks": 0` for it. **Remedy.** The extract stage's OCR path
(`indexer/pipeline/extract.py`, `_ocr_page`): detect pages under 25 characters of text,
rasterise with `pdf2image`, read with `pytesseract`, mark `ocr=True`; the indexer image
installs `tesseract-ocr` and `poppler-utils`. **Had I not caused it:** the first number to
look at is the indexer's `chunked` log line per source and `lost_pages` in `extracted pdf`;
the symptom at the API is a run of abstentions for one document while others answer fine.

## B · Chunking: fixed-size 100-token chunks, no overlap

**How.** `CHUNK_SIZE=100`, `CHUNK_OVERLAP=0` in the ConfigMap, re-index. **Symptom.**
1352 chunks instead of 585; recall@5 falls from 0.714 to 0.643 and recall@3 from 0.643 to
0.595, and the gate fails on the 7.1-point drop (`docs/evidence/failure-B-chunks-100-0.txt`).
Worse, abstention correctness drops to 0.67: one must-abstain question now clears the
floor, because a 100-token fragment that happens to share a few words with "parking
permit" scores as high as a real answer does. Version-type recall goes to 0: the clause
and the heading that identify the version are no longer in the same chunk. **Diagnosis.**
Stage B: the retrieved chunk ids in the log change from one id per rule to runs of
neighbouring ids, the similarity scores flatten (best scores cluster around 0.2), and the
chunk count in the collection metadata triples. **Remedy.** Structure-aware chunking with
overlap (ADR 0002): sections, then paragraphs, then sentences, heading prefixed, 40 tokens
shared between neighbours. **Had I not caused it:** the `chunk_count` and `chunk_size`
in the collection metadata (`kubectl exec` into chroma or `/readyz`'s `collection.count`)
and the flattened `scores` array in the `answered` log line give it away before any user
complains.

## C · Retrieval: `TOP_K=1`, and the embedding pin

**How (1).** `TOP_K=1` in the ConfigMap, restart the api. **Symptom.** recall@5 collapses
to 0.524 (a 19-point drop; the gate fails loudly, `docs/evidence/failure-C-top-k-1.txt`),
ordinary questions fall to 0.458 and version questions to 0. Answers that need two
passages (a rule and its exception) cannot be grounded; the model sees one chunk and either
answers from half the evidence or abstains. **Diagnosis.** Stage C: the `answered` log line
shows `"top_k": 1` and a one-element `chunk_ids` list on every request. **Remedy.** `TOP_K`
back to 5; the floor and the model's own refusal handle the irrelevant tail.

**How (2).** Change `EMBED_MODEL` in the ConfigMap to another model, restart the api
only (no re-index). **Symptom.** The pods never become ready: `/readyz` returns 503 with
`"failed": "embedding_pin"` and the body `collection was indexed with
EMBED_MODEL='local-hash-v1' but this API is configured with EMBED_MODEL='nomic-embed-text'`;
the log carries an ERROR line `embedding pin mismatch` with both model names and both
dimensions (512 vs 768); `/healthz` keeps returning 200 so the pod is withheld from the
Service, not restarted (`docs/evidence/embedding-pin-mismatch.txt`). **Without the
guardrail** this would be the silent version of stage C: query vectors from one model
against chunk vectors from another, cosine scores that look plausible, retrieval that
returns nonsense, no error anywhere. **Diagnosis.** `kubectl get pods` shows `0/1 READY`,
`kubectl describe pod` shows the readiness probe failing, `kubectl logs` names the conflict.
**Remedy.** Re-run the indexer Job (which recreates the collection under the new pin) or
revert the ConfigMap.

## E · Source: remove the superseded filter

**How.** `SUPERSEDED_FILTER=false` on the api (a setting that exists only for this
experiment, `api/app/config.py`) and ask a version-sensitive question. Measured on the
fixture corpus, whose 2023 and 2026 editions both have text (locally the real 2023 rules
are scanned, see A). **Symptom.** For "What is the minimum attendance needed to sit the
final exam?" the 2023 chunk (75 %) is retrieved *first*, score 0.350 against 0.315 for the
2026 chunk (80 %); for the grade-cap question the 2023 chunk leads 0.522 to 0.334. The
stub cites the first passage, so the answer quotes the repealed rule with a correct-looking
citation (`docs/evidence/failure-E-superseded-filter-off.txt`). **The gate passes:**
recall@5 is still 1.0 because the 2026 chunk is also in the top 5, and citation validity is
1.0 because the 2023 chunk really was retrieved. This is the failure mode the handout
warns cannot be fixed by a prompt, and it is also the one the retrieval metrics cannot see.
**Diagnosis.** Stage E: the `answered` log line's `chunk_ids` carry the 2023 source for a
question that did not ask for it, and `older_version` is `false` in the same line.
**Remedy.** The metadata filter `where={"superseded": False}` on every query unless the
question explicitly asks for an older version (`api/app/retrieval/chroma.py`,
`api/app/services/guards.py`). **Had I not caused it:** add a check to the eval that a
version-type question's first citation carries `doc_version` of the current document; the
recall gate alone will never catch it.

## D · Prompt and F · Generation: to run on the cluster

Both need a real model; the stub never reads the prompt.

* **D · Prompt.** Remove rule 1 ("use the supplied context") from `SYSTEM_PROMPT` in
  `api/app/providers/llm/prompt.py`. Expected symptom: `answered_from_context` stays `true`
  on the must-abstain questions (the model answers from its training data about some other
  university), citations that name no retrieved passage are dropped by `filter_citations`
  and the answer is demoted, so `rahbar_abstentions_total{reason="no_valid_citation"}`
  climbs while retrieval scores are unchanged. Diagnosis: scores healthy, citations
  dropped: the fault is between retrieval and the reply, i.e. the prompt.
* **F · Generation.** `TEMPERATURE=1.2`. Expected symptom: the same question gives
  different answers on repeat, JSON validation failures rise
  (`rahbar_llm_retries_total`, then `rahbar_fallbacks_total`), and confidence labels stop
  matching the evidence. Diagnosis: retrieval identical across repeats (same `chunk_ids`
  and `scores`), only the answer text moves; by the lecture's rule, check A to E first and
  blame the model last.
