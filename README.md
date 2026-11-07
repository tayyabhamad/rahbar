# RAHBAR

![ci](https://github.com/tayyabhamad/rahbar/actions/workflows/ci.yml/badge.svg)

<!-- The badge assumes the repository lives at github.com/tayyabhamad/rahbar; adjust the
     path if the fork or organisation differs. -->

RAHBAR answers FAST-NUCES students' questions about the rules that govern them (attendance,
academic warning, fees, grading, the generative-AI policy) from the university's own public
documents, and says so when the documents do not cover a question. It is a grounded RAG
service: an indexer Job turns nine public PDFs and web pages into cited chunks in a Chroma
collection, a three-replica FastAPI service retrieves the relevant passages, filters out
superseded rules, asks a local or hosted LLM for a schema-constrained JSON answer, drops
any citation that does not point at a retrieved passage, and caches validated answers in
Redis. Everything runs in one Kubernetes namespace with default-deny network policies,
readiness that depends on the index existing and on the embedding model matching the one
that built it, and a CI gate that evaluates recall, citation validity and abstention on
every pull request. Authors: Tayyab Hamad and `<partner name>` (SCD Assignment 2, due
8 November 2026).

## Architecture

```
user ──> Ingress (rahbar.local) ──┬── /     ──> ui  (nginx, Deployment x1, :8080)
                                  └── /api  ──> api (FastAPI, Deployment x3, HPA 3..8, :8000)
                                                 │ reads             │ cache            │ generate
                                                 v                   v                  v
                                     chroma (StatefulSet x1, PVC)  redis (Deployment + PVC)   LLM provider
                                                 ^                                      (ollama-host | hosted)
                                                 │ writes (only writer)
                                     indexer (Job + weekly CronJob: fetch, extract, clean, chunk, embed, store)
```

One writer, one reader, one shared pin: the indexer Job is the only process that writes
the collection; the api only reads it; both read `EMBED_MODEL` from the same ConfigMap
key and the api refuses to become ready when the collection was built with a different
model. Embeddings (`nomic-embed-text`) and generation (`llama3.2:3b`) go through the same
Ollama host on the laptop; CI swaps them for a deterministic local embedder and a stub
provider. Design: `docs/ARCHITECTURE.md`; decisions: `docs/adr/`.

## Prerequisites

* Docker (Desktop on Windows/macOS), with at least 8 GB available to containers
* minikube with Calico (`make cluster` starts it with `--cni=calico --memory=6144 --cpus=4`
  and enables the `metrics-server` and `ingress` addons)
* kubectl; kustomize is optional (`kubectl apply -k` is enough; `make validate` and CI use
  the standalone `kustomize` plus `kubeconform`)
* Ollama on the laptop, listening on all interfaces so pods can reach it:
  `OLLAMA_HOST=0.0.0.0 ollama serve`, then `ollama pull llama3.2:3b` and
  `ollama pull nomic-embed-text`
* Python 3.12, `make`, `git`
* On Windows: WSL 2 (Ubuntu). The Makefile needs bash; run everything from the WSL shell
  with the repository on the Linux filesystem or mounted under `/mnt`.
* Optional: a free-tier OpenAI-compatible LLM endpoint and key for `LLM_PROVIDER=hosted`

## From a clean clone to an answered question

```bash
git clone https://github.com/tayyabhamad/rahbar.git && cd rahbar

make install          # .venv with api, indexer and tool dependencies
make test             # unit tests with stubs: no cluster, no model, no network
make cluster          # minikube + Calico + metrics-server + ingress
make images           # build rahbar-api:dev, rahbar-indexer:dev, rahbar-ui:dev and load them into minikube

# One command creates everything, the namespace included; no separate namespace apply.
make apply            # = kubectl apply -k k8s/overlays/dev

# Optional, only for LLM_PROVIDER=hosted (the key never touches a file):
export LLM_API_KEY=...; make secret

# Wait for the stores, then the index, then the api (which sits at 0/1 until the index exists).
kubectl rollout status statefulset/chroma -n rahbar --timeout=180s
kubectl rollout status deployment/redis -n rahbar --timeout=120s
kubectl wait --for=condition=complete job/indexer -n rahbar --timeout=600s
kubectl rollout status deployment/api -n rahbar --timeout=300s
kubectl rollout status deployment/ui -n rahbar
```

`kubectl wait` only returns early on success; if the Job fails it blocks until the
timeout, so in a second terminal use `kubectl get jobs,pods -n rahbar -w` and
`kubectl logs job/indexer -n rahbar --all-containers -f`. The Job's `fetch` initContainer
downloads the nine documents from nu.edu.pk one at a time (about 35 MB), so the first run
takes a few minutes before indexing starts.

Reach the Ingress:

```bash
# Linux: the minikube IP is routable.
echo "$(minikube ip) rahbar.local" | sudo tee -a /etc/hosts
make ask Q="What CGPA puts a student on academic warning?"

# Windows / macOS with the docker driver: the minikube IP is not reachable.
minikube tunnel                                   # keep this running in its own terminal
echo "127.0.0.1 rahbar.local" | sudo tee -a /etc/hosts   # Windows: C:\Windows\System32\drivers\etc\hosts
make ask Q="What CGPA puts a student on academic warning?" INGRESS_IP=127.0.0.1
```

Then open <http://rahbar.local> for the chat UI. The response carries `citations`, the
`retrieved` passages, `answered_from_context` and a `cached` flag; the UI shows a
`cache: hit/miss` chip.

### Re-index

```bash
make index            # kubectl create job --from=cronjob/reindex reindex-<ts> -n rahbar
kubectl get jobs -n rahbar -w
```

A re-index fetches the documents again, upserts the chunks in place, deletes stale ones,
and stamps a new `index_generation`, which invalidates every cached answer. The CronJob
`reindex` does the same every Monday at 03:00 cluster time. The api is never restarted
for it.

### Evaluate

```bash
make eval                                  # eval/golden.yaml through the Ingress; exit 1 = gate failed
make eval INGRESS_IP=127.0.0.1             # behind minikube tunnel
# After a deliberate improvement only:
.venv/bin/python eval/run_eval.py --api http://127.0.0.1 --host rahbar.local --update-baseline

# Without a cluster: the fixture corpus against `make dev` (stubs) in another terminal
make eval-fixtures
```

The gate (`eval/run_eval.py`): recall@5 may not drop more than 0.05 below the baseline,
citation validity ≥ 0.95, every must-abstain question declined. `eval/results.json` and
`eval/baseline.json` are committed.

## Demonstrations

### Embedding pin (section 4.4)

Change the api's `EMBED_MODEL` without re-indexing and watch it refuse to become ready:

```bash
kubectl patch configmap rahbar-config -n rahbar --type merge -p '{"data":{"EMBED_MODEL":"dummy-768"}}'
kubectl rollout restart deployment/api -n rahbar
kubectl get pods -n rahbar -l app.kubernetes.io/name=api -w      # the new pod stays 0/1 READY
kubectl logs -n rahbar -l app.kubernetes.io/name=api --tail=50 | grep "embedding pin mismatch"
kubectl exec -n rahbar deploy/api -- python -c "import httpx;print(httpx.get('http://localhost:8000/readyz').text)"   # 503 naming both models
```

Because the rollout has `maxUnavailable: 0`, the three old pods keep serving while the
new one is parked; nothing restarts because liveness is `/healthz`. Revert:

```bash
kubectl patch configmap rahbar-config -n rahbar --type merge -p '{"data":{"EMBED_MODEL":"nomic-embed-text"}}'
kubectl rollout restart deployment/api -n rahbar
```

### Zero-downtime rollout (section 4.6)

```bash
# Terminal 1: keep asking
while true; do make ask Q="How much is the admission fee?" INGRESS_IP=127.0.0.1 | grep -c answer; sleep 1; done
# Terminal 2: roll
kubectl rollout restart deployment/api -n rahbar        # or: make images TAG=v2 && kubectl set image deployment/api api=rahbar-api:v2 -n rahbar
kubectl rollout status deployment/api -n rahbar
kubectl rollout history deployment/api -n rahbar
kubectl rollout undo deployment/api -n rahbar           # back to the previous template
```

`maxSurge: 1`, `maxUnavailable: 0`, a 5 s preStop sleep and a 20 s grace period mean a
new pod passes readiness before an old one is removed, and the old one leaves the
endpoints before uvicorn gets SIGTERM; the loop in terminal 1 should not print an error.

### Scaling (section 4.6)

```bash
kubectl get hpa api -n rahbar -w                        # TARGETS shows CPU % against 70 %
kubectl scale deployment/api --replicas=5 -n rahbar     # manual; the HPA scales back to 3 after its 5 min window if CPU is low
# Load: run several askers in parallel and watch the HPA and `kubectl top pods -n rahbar`.
for i in $(seq 1 20); do make ask Q="What attendance is required in lectures?" INGRESS_IP=127.0.0.1 >/dev/null & done; wait
```

`docs/ENGINEERING-NOTES.md` question 3 explains why CPU is the wrong signal for a service
that spends its time waiting on a model, and what to replace it with.

## API

The Ingress routes `/api` to the api Service and `/` to the ui Service; `/healthz`,
`/readyz` and `/metrics` are reachable inside the cluster (probes, Prometheus) and via
`kubectl port-forward deploy/api 8000:8000 -n rahbar`.

| Method | Path | Purpose | Response |
| --- | --- | --- | --- |
| GET | `/healthz` | Liveness: is the process alive? Touches no dependency. | `{"status": "alive"}` |
| GET | `/readyz` | Readiness: Chroma reachable, collection non-empty, embedding pin matches, Redis reachable. | 200 `{"status":"ready","failed":null,"checks":{...}}` or 503 with `failed` naming the dependency |
| GET | `/metrics` | Prometheus text format: requests by route and status, latency histograms, retrieval and generation latency, cache hits, abstentions, fallbacks, tokens. | `text/plain` |
| POST | `/api/ask` | The question endpoint. | `AskResponse`, see below; header `X-Cache: HIT|MISS`; `X-Request-ID` echoed or generated |

Request: `{"question": "..."}`, 3 to 500 characters (422 otherwise). Response:

```json
{
  "request_id": "f3a1...",
  "answer": "A student is placed on academic warning when the CGPA falls below 2.00 ...",
  "citations": [
    {"source": "academic-rules-handbook-jun-2026.pdf", "section": "5 Academic Assessment", "page": 21}
  ],
  "answered_from_context": true,
  "confidence": "high",
  "retrieved": [
    {"chunk_id": "academic-rules-handbook-jun-2026-pdf-0031", "source": "academic-rules-handbook-jun-2026.pdf",
     "section": "5 Academic Assessment", "page": 21, "score": 0.81}
  ],
  "provider": "ollama",
  "cached": false
}
```

Every citation matches a passage in `retrieved`, or it is dropped. `answered_from_context:
false` means the service declined: the best similarity was below `RELEVANCE_FLOOR`, the
model said the evidence does not cover the question, or no citation survived. The canned
fallback after two invalid model replies is also `false` and is never cached.

## Corpus policy

The nine documents (academic rules 2023 and 2026, prospectus rules, student handbook, two
annual reports, fee and grading pages, a scanned ORIC policy) are public FAST-NUCES pages.
They are **not committed**: `corpus/MANIFEST.md` records URL, size and SHA-256 for each,
`corpus/sources.yaml` is the list, and `make fetch` / `python corpus/fetch.py --verify`
download and check them one request at a time. The indexer Job fetches them itself at run
time. Details, the seven required properties and the deliberately absent topic (hostel and
transport rules): `docs/CORPUS.md`.

## CI/CD

* **CI** (`.github/workflows/ci.yml`, on pull requests to `main` and pushes to `dev`): ruff
  and mypy; unit tests with a 60 % coverage floor on the api; kustomize + kubeconform on
  every overlay; the three images built once and shared as an artifact; Trivy
  (HIGH/CRITICAL, fixed only) on those exact images; an end-to-end job on kind that applies
  the `ci` overlay, runs the indexer Job over the fixture corpus, asks a question through
  the Ingress and runs `eval/run_eval.py` against the fixture golden set and baseline. Its
  exit code is the gate.
* **CD** (`.github/workflows/cd.yml`, on push to `main`): unit tests again, build and push
  `rahbar-{api,indexer,ui}:<git sha>` to Docker Hub (never `latest`), then deploy the
  `prod` overlay with the images rewritten to that SHA to a kind cluster on the runner and
  smoke-test `POST /api/ask` through the Ingress.
* **GitHub secrets**: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` (an access token, not the
  password), optional `LLM_API_KEY`.
* **Branch model**: work on `dev`, open a pull request to `main`; `main` is protected
  (required CI checks, no direct pushes) and every push to it is a deploy.

## Documentation

| File | What it holds |
| --- | --- |
| `docs/ARCHITECTURE.md` | Objects, names, ConfigMap keys, network paths, how the corpus reaches the Job, overlays, the evaluation matching rule |
| `docs/adr/0001-indexer-as-job.md` | Why indexing is a Job and not API start-up code |
| `docs/adr/0002-chunking-strategy.md` | Structure-aware chunking, overlap, heading prefix, the token unit |
| `docs/adr/0003-semantic-cache-store.md` | Redis for the cache, generation-namespaced invalidation, the never-cache rules |
| `docs/adr/0004-llm-provider-and-data-governance.md` | Provider modes, structured output, the embedding pin, what is stored and logged |
| `docs/CORPUS.md` | The nine documents and the seven required properties |
| `docs/PIPELINE.md` | Extraction, cleaning, chunking measurements, embedding, cache thresholds, evaluation method |
| `docs/FAILURE-ANALYSIS.md` | The failure modes and where each is caught |
| `docs/ENGINEERING-NOTES.md` | The seven section-7 questions with file:line pointers |
| `docs/AI-USAGE.md` | What AI tools were used, for what, and what we changed |
| `STARTER.md` | The starter kit, as handed out |

## Screenshot

![The chat UI answering a question with citations](docs/evidence/ui-answer.png)

<!-- TODO: capture docs/evidence/ui-answer.png from http://rahbar.local after the first
     cluster run: one answered question with its citations and the cache chip visible. -->
