# AI usage

A specific disclosure carries no penalty, so this file is specific.

## Tools

| Tool | Version / model | How it was used |
| --- | --- | --- |
| Claude Code (Anthropic's CLI agent) | model Claude Fable 5.1 | Agent sessions in the repository on 2026-11-07 and 2026-11-08, driven by Tayyab Hamad; Shayed Zain Ali reviewed and approved the pull request. It read the case study and the starter kit, wrote the code and documents listed below, and ran them on minikube and in CI. The section at the end says what changed after the first draft and why. |
| OpenRouter (`google/gemini-3.1-flash-lite`, `baai/bge-m3`), Ollama (`llama3.2:3b`, `nomic-embed-text`, `qwen2.5:0.5b` in the bonus run) | runtime models | Not authoring tools: these are the models the system itself runs. Listed so there is no confusion about which AI did what. |

No other AI tools (ChatGPT, Copilot, Gemini, Cursor) were used.

## What Claude wrote or shaped, by area

| Area | Files | What it did |
| --- | --- | --- |
| Design | `docs/ARCHITECTURE.md` | Drafted the architecture note (objects, ConfigMap keys, network paths, how the corpus reaches the Job, overlays, evaluation matching rule) from the brief and the starter kit; it proposed the shapes (Job + CronJob, StatefulSet for Chroma, Redis for the cache, `local-hash-v1` for CI) and we accepted them. |
| API | `api/app/retrieval/chroma.py`, `api/app/providers/llm/{prompt,common,ollama,hosted}.py`, `api/app/services/{answer,cache,guards,validate,readiness}.py`, `api/app/embedding.py`, `api/app/embedding_local.py`, `api/app/routes/health.py`, `api/app/routes/metrics.py` additions, `api/app/config.py` additions | Wrote the first version of every "yours" module in the starter table (`STARTER.md:19`): the Chroma retriever with the superseded filter and the hybrid exact-term pass, the two real providers with structured output and the retry, the semantic cache, the relevance floor and abstention, citation filtering and the fallback, `/readyz` with the embedding-pin check, the extra Prometheus metrics. Also the `cached` field and `X-Cache` header. |
| Indexer | `indexer/pipeline/{extract,clean,chunk,tokens,embed,store,embedding_local}.py` | Wrote the five pipeline stages: pdfplumber extraction with table re-rendering, two-column detection and the OCR path; the cleaning rules; structure-aware chunking with overlap and the heading prefix; batched Ollama embeddings with the dimension check; the upsert-then-delete-stale store with the collection metadata and `index_generation`. |
| Tests | `api/tests/*`, `indexer/tests/*` (beyond the starter's) | Wrote the unit tests for the modules above (67 api tests, 27 indexer tests), including the test that the two `embedding_local.py` copies are identical. |
| Containers | `api/Dockerfile`, `indexer/Dockerfile`, `ui/Dockerfile`, `.dockerignore` | Wrote all three Dockerfiles (non-root, pinned bases, tesseract and poppler in the indexer image). |
| Kubernetes | `k8s/base/*.yaml`, `k8s/overlays/{dev,dev-hosted,dev-ollama-incluster,prod,ci}/*`, `k8s/components/{ollama-host,ollama-incluster}/*` | Wrote every manifest: Namespace, ConfigMap, ServiceAccounts, Deployments, StatefulSet, Job, CronJob, Services, Ingress, HPA, NetworkPolicies, the overlays and the two Ollama components, with the resource numbers marked as starting points. |
| CI/CD | `.github/workflows/ci.yml`, `.github/workflows/cd.yml` | Wrote both workflows: lint, unit tests with coverage floor, kustomize + kubeconform, image build, Trivy scan, end-to-end eval on kind; and the main-branch build-push-deploy chain with the temporary cd overlay. |
| Evaluation | `eval/run_eval.py` (the four functions), `eval/golden.yaml` draft, `eval/fixtures/baseline.fixture.json` | Implemented `recall_at_k`, `citation_validity`, `summarise`, `gate`; drafted the 24-question golden set from the corpus text, which it then checked against the chunker output and corrected. |
| Corpus | `corpus/sources.yaml` edits | Helped choose which starter-listed documents to keep and which to leave out (the hostel/transport PDF) and recorded the reasons. |
| Documentation | `docs/CORPUS.md`, `docs/PIPELINE.md`, `docs/FAILURE-ANALYSIS.md`, `docs/ENGINEERING-NOTES.md`, `docs/adr/0001-0004`, `README.md`, this file, Makefile tweaks (`dev`, `eval-fixtures`) | Drafted every document from the templates, the code and the measured runs; the `TODO(cluster)` markers of the first draft were all replaced with numbers from the cluster. |

What Claude also did, beyond writing files: it ran the work. It started minikube with
Calico in WSL, built and loaded the images, applied the overlays, ran the indexer Job and
the eval through the Ingress, broke the stages for `docs/FAILURE-ANALYSIS.md`, captured
the files in `docs/evidence/`, pushed the `dev` branch, opened both pull requests and
merged the first from Tayyab's `gh` session after the reviewer approved it. Every
measured number in `docs/` comes from one of those runs.

Things Claude did not do, because we did them or because it refused to: it never typed or
stored the LLM API key (Tayyab created the Secret with `make secret` from a gitignored
`.env`), it did not create the Docker Hub token or the GitHub repository secrets (Tayyab
did), it did not sign in to or act from Shayed's GitHub account (the pull request
approval was clicked from that account by us), and it did not choose the hosted provider
or the model tier (we did).

## What we changed afterwards and why

Stated plainly: we did not hand-edit the code after Claude wrote it. The changes after
the first draft were made in the same sessions, by Claude, either because we asked for
them or because a run on the cluster or in CI showed the draft was wrong. They are listed
here so the history is honest about which was which.

Changes we asked for:

* **Hosted provider next to Ollama** (`api/app/providers/llm/hosted.py`,
  `api/app/embedding.py`, `indexer/pipeline/embed.py`, `k8s/overlays/dev-hosted/`). The
  draft was Ollama only. The laptop could not hold minikube and a 3B model together, so
  Tayyab asked for both paths, switched by one overlay, with OpenRouter
  (`google/gemini-3.1-flash-lite`, `baai/bge-m3`) as the one we ran.
* **The key in a gitignored `.env`** (`Makefile`, `.gitignore`). The draft read the key
  only from the shell; Tayyab asked for `.env` so `make secret` works in a new terminal.
* **Authors and the pull request flow** (`README.md`, this file, branch protection on
  `main`): one approval required, Shayed as the reviewer, no direct pushes to `main`.
* **The bonus mode** (`k8s/components/ollama-incluster/`, pull request 2), asked for
  after the main work was merged.

Changes forced by what the runs showed (found and fixed by Claude, reviewed by us in the
pull requests):

* `k8s/base/*`: `enableServiceLinks: false` on every pod, because Kubernetes injected
  `CHROMA_PORT=tcp://...` and Chroma crashed on it in kind.
* `api/app/retrieval/chroma.py`, `api/app/main.py`: the Chroma client is created lazily
  and the startup readiness check runs in a thread, because the api crash-looped when
  Chroma was down and the startupProbe killed it while the check blocked the port.
* `indexer/pipeline/run.py`, `k8s/base/indexer-job.yaml`: `wait_for_chroma` and
  `backoffLimit: 6`, because the Job burned its retries on a cold cluster.
* `indexer/pipeline/store.py`: strip `hnsw:*` keys before `collection.modify`, which
  Chroma 1.0.12 rejects.
* `indexer/pipeline/extract.py`, `clean.py`, `chunk.py`: pdfminer layout boxes in place
  of a per-page gutter split for the two-column handbook; clause numbers re-attached
  before the header filter that was eating them; table-of-contents lines no longer
  treated as headings. All three came from reading the chunker output against the PDFs.
* `api/app/services/guards.py`: the "older version" pattern tightened, because the word
  "earlier" in an injection question lifted the superseded filter.
* `eval/golden.yaml`: sections and expected sources corrected against the chunker dry
  run; the committed baseline is the hosted cluster run, not the first local one.
* `api/requirements.txt`, `indexer/requirements.txt`, the three Dockerfiles: versions
  raised and `pypdf` removed until Trivy reported no fixable HIGH or CRITICAL finding.
* `.github/workflows/*`: runner-preinstalled kustomize, kind and kubectl after release
  downloads answered 504; the Trivy action pinned to a tag that exists; branch-protection
  check names corrected; smoke-test retries for the Ingress endpoint lag.
* `k8s/components/*`, `scripts/check_namespace.sh`: component resources rendered with no
  namespace and landed in `default`; every one now sets `metadata.namespace`
  (`docs/evidence/ollama-incluster-namespace-bug.txt`).
* `docs/*`: every `TODO(cluster)` marker replaced with a measured number: chunk sizes,
  the relevance floor, the cache threshold, `kubectl top`, the failure stages.

Kept from the draft unchanged: the overall design (Job plus CronJob, StatefulSet for
Chroma, Redis cache keyed by index generation, the embedding pin on `/readyz`).
Not kept: Ollama as the path we demonstrate (it remains the base default and is
documented, but the evidence is from the hosted overlay), and `RELEVANCE_FLOOR=0.21`,
which was right for the lexical embedder and wrong for `bge-m3` (0.45 shipped).

## Statement

Most of this repository was written by Claude Code under our direction, as the tables
above say. Our own work was the direction (what to build, which provider, what to
measure), the accounts and secrets, the review and approval of the pull requests, and
reading the result. We are submitting it as AI-assisted work, not as code we typed, and
we understand that the viva examines whether we can explain it.

Signed: Tayyab Hamad, Shayed Zain Ali (24i-3061) · 2026-11-11
