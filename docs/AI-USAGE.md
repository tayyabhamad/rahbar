# AI usage

A specific disclosure carries no penalty, so this file is specific.

## Tools

| Tool | Version / model | How it was used |
| --- | --- | --- |
| Claude Code (Anthropic's CLI agent) | model Claude Fable 5.1 | Pair-programming sessions in the repository on 2026-11-08, driven by Tayyab Hamad and Shayed Zain Ali. It read the starter kit, the case study summary and our design notes, and wrote or shaped the code and documents listed below. We reviewed, ran and edited the output; where we changed it, the section at the end says what and why. |
| Ollama (`llama3.2:3b`, `nomic-embed-text`) | runtime models | Not an authoring tool: these are the models the system itself runs. Listed so there is no confusion about which AI did what. |

No other AI tools (ChatGPT, Copilot, Gemini, Cursor) were used.

## What Claude wrote or shaped, by area

| Area | Files | What it did |
| --- | --- | --- |
| Design | `docs/ARCHITECTURE.md` | Drafted the architecture note (objects, ConfigMap keys, network paths, how the corpus reaches the Job, overlays, evaluation matching rule) from our decisions in conversation; we chose the shapes (Job + CronJob, StatefulSet for Chroma, Redis for the cache, `local-hash-v1` for CI). |
| API | `api/app/retrieval/chroma.py`, `api/app/providers/llm/{prompt,common,ollama,hosted}.py`, `api/app/services/{answer,cache,guards,validate,readiness}.py`, `api/app/embedding.py`, `api/app/embedding_local.py`, `api/app/routes/health.py`, `api/app/routes/metrics.py` additions, `api/app/config.py` additions | Wrote the first version of every "yours" module in the starter table (`STARTER.md:19`): the Chroma retriever with the superseded filter and the hybrid exact-term pass, the two real providers with structured output and the retry, the semantic cache, the relevance floor and abstention, citation filtering and the fallback, `/readyz` with the embedding-pin check, the extra Prometheus metrics. Also the `cached` field and `X-Cache` header. |
| Indexer | `indexer/pipeline/{extract,clean,chunk,tokens,embed,store,embedding_local}.py` | Wrote the five pipeline stages: pdfplumber extraction with table re-rendering, two-column detection and the OCR path; the cleaning rules; structure-aware chunking with overlap and the heading prefix; batched Ollama embeddings with the dimension check; the upsert-then-delete-stale store with the collection metadata and `index_generation`. |
| Tests | `api/tests/*`, `indexer/tests/*` (beyond the starter's) | Wrote the unit tests for the modules above (60 api tests, 21 indexer tests at the time of writing), including the test that the two `embedding_local.py` copies are identical. |
| Containers | `api/Dockerfile`, `indexer/Dockerfile`, `ui/Dockerfile`, `.dockerignore` | Wrote all three Dockerfiles (non-root, pinned bases, tesseract and poppler in the indexer image). |
| Kubernetes | `k8s/base/*.yaml`, `k8s/overlays/{dev,prod,ci}/*`, `k8s/components/ollama-host/*` | Wrote every manifest: Namespace, ConfigMap, ServiceAccounts, Deployments, StatefulSet, Job, CronJob, Services, Ingress, HPA, NetworkPolicies, the three overlays and the Ollama-host component, with the resource numbers marked as starting points. |
| CI/CD | `.github/workflows/ci.yml`, `.github/workflows/cd.yml` | Wrote both workflows: lint, unit tests with coverage floor, kustomize + kubeconform, image build, Trivy scan, end-to-end eval on kind; and the main-branch build-push-deploy chain with the temporary cd overlay. |
| Evaluation | `eval/run_eval.py` (the four functions), `eval/golden.yaml` draft, `eval/fixtures/baseline.fixture.json` | Implemented `recall_at_k`, `citation_validity`, `summarise`, `gate`; drafted the 24-question golden set from the corpus text, which we then checked against the documents and edited. |
| Corpus | `corpus/sources.yaml` edits | Helped choose which starter-listed documents to keep and which to leave out (the hostel/transport PDF) and recorded the reasons. |
| Documentation | `docs/CORPUS.md`, `docs/PIPELINE.md`, `docs/FAILURE-ANALYSIS.md`, `docs/ENGINEERING-NOTES.md`, `docs/adr/0001-0004`, `README.md`, this file, Makefile tweaks (`dev`, `eval-fixtures`) | Drafted every document from the templates, our verified facts and the code, with `TODO(cluster)` markers wherever a number has to come from a real cluster run. |

Things Claude did not do: it did not run anything on minikube or kind (neither tool is on
the laptop the session ran on), it did not fetch the documents from the university site
(we did, with `make fetch`), and it did not decide the grades of evidence we report:
every measured number in `docs/` either comes from a run we executed (the local dry run,
the fixture end-to-end run, the unit tests) or is marked `TODO(cluster)`.

## What we changed afterwards and why

<!-- Fill this in truthfully before submission. One bullet per change, with the file and
     the reason. Keep the bullets that are true, delete the ones that are not. -->

* `api/app/...` — <what we changed> because <why: a bug we found, a behaviour we
  disagreed with, a simpler way>.
* `indexer/pipeline/...` — <what we changed> because <why>.
* `k8s/...` — <resource numbers, probe timings or NetworkPolicy CIDR we adjusted after
  running on minikube> because <what we measured: kubectl top, describe, events>.
* `eval/golden.yaml` — <questions we rewrote or dropped after reading the documents>
  because <the drafted answer or section was wrong, or the question was ambiguous>.
* `docs/...` — <numbers that replaced `TODO(cluster)` markers and anything we corrected>.
* Things we decided not to keep from the draft: <list, with the reason>.

## Statement

The submission reflects work we understand and can explain. Every module above was read
by both of us, we can describe what each function does and why it is there, and the
design decisions in the ADRs are ours; Claude's role was to write the first version
quickly and argue with us about it. If a marker asks either of us to walk through
`api/app/services/answer.py` or `indexer/pipeline/chunk.py` without notes, we can.

Signed: Tayyab Hamad, Shayed Zain Ali (24i-3061) · 2026-11-08
