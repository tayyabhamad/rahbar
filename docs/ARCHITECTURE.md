# RAHBAR architecture

One writer, one reader, one shared pin. The **indexer** (a Kubernetes Job) is the only
process that writes the Chroma collection. The **api** (a Deployment with 3 replicas) only
reads it. Both read `EMBED_MODEL` from the same ConfigMap key, and the api refuses to
become ready when the collection was built with a different model (section 4.4).

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

Everything lives in the namespace `rahbar`.

## Objects and names

| Object | Name | Notes |
| --- | --- | --- |
| Namespace | `rahbar` | never `default` |
| ConfigMap | `rahbar-config` | every key below; read by api and indexer |
| Secret | `rahbar-secret` | key `LLM_API_KEY`; created with `make secret`, never committed |
| Deployment | `api` | image `rahbar-api`, port 8000, 3 replicas, probes on `/healthz` and `/readyz`, `maxSurge: 1`, `maxUnavailable: 0`, preStop sleep 5, `terminationGracePeriodSeconds: 20` |
| Deployment | `ui` | image `rahbar-ui`, port 8080, 1 replica, `emptyDir` at `/tmp` |
| StatefulSet | `chroma` | image `chromadb/chroma:1.0.12`, port 8000, data at `/data`, `volumeClaimTemplates` (`chroma-data`, 2Gi); heartbeat `GET /api/v2/heartbeat` |
| Deployment + PVC | `redis` | image `redis:7.4-alpine`, port 6379, `--appendonly yes`, PVC `redis-data` (1Gi) at `/data` |
| Job | `indexer` | image `rahbar-indexer`, `restartPolicy: OnFailure`, `backoffLimit: 3`, `ttlSecondsAfterFinished: 3600` |
| CronJob | `reindex` | weekly (`0 3 * * 1`), identical pod template to the Job; `concurrencyPolicy: Forbid` |
| Service x4 | `api`, `ui`, `chroma`, `redis` | all `ClusterIP`; chroma and redis are never NodePort or LoadBalancer |
| Ingress | `rahbar` | host `rahbar.local`; `/api` (Prefix) to `api:8000`, `/` (Prefix) to `ui:8080`; `ingressClassName: nginx` |
| HPA | `api` | `minReplicas: 3`, `maxReplicas: 8`, CPU 70 % |
| ServiceAccount x4 | `api`, `ui`, `chroma`, `redis`, `indexer` | `automountServiceAccountToken: false` everywhere |
| NetworkPolicy | `default-deny`, then one allow per path | see "Network paths" |

Labels: every workload carries `app.kubernetes.io/name: <component>` and
`app.kubernetes.io/part-of: rahbar`. Selectors use `app.kubernetes.io/name`.

## Why Chroma is a StatefulSet

Chroma owns one directory of HNSW segments and a SQLite file on disk. A StatefulSet gives it
a **stable identity** (`chroma-0`) and a PVC created from `volumeClaimTemplates` that
follows that identity: a rescheduled pod reattaches the same volume, and a scale-up never
produces a second pod writing the same claim. A Deployment with a PVC would let a rolling
update start a second pod against the same volume while the first is still running, and
Chroma's single-writer SQLite would corrupt or lock. `updateStrategy: RollingUpdate` with
one replica also guarantees the old pod is gone before the new one mounts the data.

## Configuration (ConfigMap `rahbar-config`)

Both images read environment variables only. Values below are the shipped defaults; the
dev, prod and ci overlays patch the ones that differ.

| Key | Value | Read by | Why |
| --- | --- | --- | --- |
| `RETRIEVER` | `chroma` | api | `stub` only for week-1 and unit tests |
| `LLM_PROVIDER` | `ollama-host` | api | `stub`, `ollama-host`, `ollama-incluster` or `hosted` |
| `EMBED_MODEL` | `nomic-embed-text` | api, indexer | **the pin**; `local-hash-v1` in CI (see below) |
| `LLM_MODEL` | `llama3.2:3b` | api | |
| `OLLAMA_URL` | `http://host.minikube.internal:11434` | api, indexer | embeddings and generation |
| `HOSTED_BASE_URL` | `` | api | OpenAI-compatible base URL for `hosted` |
| `CHROMA_HOST` / `CHROMA_PORT` | `chroma` / `8000` | api, indexer | the Service name |
| `CHROMA_COLLECTION` | `rahbar` | api, indexer | |
| `REDIS_HOST` / `REDIS_PORT` | `redis` / `6379` | api | semantic cache |
| `TOP_K` | `5` | api | |
| `RELEVANCE_FLOOR` | `0.45` | api | best similarity below this: abstain |
| `CACHE_THRESHOLD` | `0.92` | api | cosine similarity for a cache hit |
| `CACHE_TTL_SECONDS` | `86400` | api | |
| `TEMPERATURE` | `0.1` | api | facts and JSON: near-deterministic, non-zero so a 3B model does not loop |
| `LLM_TIMEOUT_SECONDS` | `120` | api | a hung model must not pin a worker forever |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `250` / `40` | indexer | regex word-tokens (see `indexer/pipeline/tokens.py`) |
| `CORPUS_DIR` | `/corpus/raw` | indexer | where the Job finds documents and `_index.json` |
| `OCR_ENABLED` | `true` | indexer | |
| `LOG_LEVEL` | `INFO` | api | |

## Embedding providers and the CI model

`EMBED_MODEL` selects the embedder in both the api and the indexer, from the same two rules:

* `local-hash-v1`: a deterministic, dependency-free **hashed bag-of-words** embedding,
  dimension 512, implemented identically in `api/app/embedding.py` and
  `indexer/pipeline/embedding_local.py`. Lower-cased word tokens minus stopwords are
  hashed into 512 buckets with log-scaled counts and L2-normalised, so cosine similarity
  is lexical overlap. CI uses it with the stub LLM provider: no model, no network, yet
  recall@k on the fixture corpus is meaningful and the must-abstain gate is real.
* anything else: Ollama `POST {OLLAMA_URL}/api/embed` with `{"model": EMBED_MODEL,
  "input": [...]}`.

The indexer writes `embed_model` and `dimension` into the collection metadata, plus
`index_generation` (a UTC timestamp). The api compares `embed_model` with its own and,
on mismatch, `/readyz` returns 503 with a body naming both values and a log line
`embedding pin mismatch`.

## Chunk metadata (every chunk)

`source` (file name), `section` (heading the chunk sits under, e.g. `5.4 Academic Warning`),
`page` (1-based, first page of the chunk), `doc_version` (e.g. `2026-06`), `superseded`
(bool). Chunk ids are `<source-slug>-<ordinal:04d>`.

## Query pipeline (api, every question)

1. Validate 3..500 characters (Pydantic, 422 otherwise).
2. Cache check in Redis unless the question is a follow-up (starts with or is dominated by
   anaphora such as "it", "that", "what about", "and").
3. Embed the question with `EMBED_MODEL`.
4. Retrieve `TOP_K` from Chroma with `where={"superseded": False}` unless the question asks
   for an older version (mentions "old", "previous", "2023", "earlier", "superseded"...).
   Hybrid step: when the question contains an exact term (clause number `1.21`, a course
   code `CS2001`, a CGPA like `2.00`, a rupee amount), a second query with
   `where_document={"$contains": term}` is merged in front of the semantic hits.
5. Abstain when the best similarity is below `RELEVANCE_FLOOR`.
6. Augment: instructions, numbered evidence delimited as `<evidence id="1" source=".." section="..">...</evidence>`, then the question. Retrieved text is data, never instructions.
7. Generate with constrained JSON (`format=<schema>` on Ollama, `response_format` on hosted), parse with Pydantic, retry once, then a canned fallback that is **not** cached.
8. Drop citations that match no retrieved passage; if none survive, mark not answered from context. Cache the result only if it was answered from context, validated on the first or second try, and not a follow-up.

Cache keys are `rahbar:cache:<index_generation>:<uuid>`; a re-index changes the generation
so old entries can never hit again, and the api deletes the previous generation's keys when
it notices the change. Entries carry the question vector and the serialised response.

## Network paths (NetworkPolicy)

Default deny ingress and egress for every pod in the namespace, then:

| From | To | Port |
| --- | --- | --- |
| ingress-nginx (namespace `ingress-nginx`) | ui | 8080 |
| ingress-nginx, ui | api | 8000 |
| api, indexer | chroma | 8000 |
| api | redis | 6379 |
| every pod | kube-dns in `kube-system` | 53 UDP/TCP |
| api, indexer | the host (Ollama on the laptop), `ipBlock` for the minikube host IP | 11434 |
| api, indexer | the internet (hosted provider, corpus fetch) | 443, 80 |

The ui pod can reach nothing but the Ingress controller's requests coming in.

## How the corpus reaches the Job

The documents are not committed (section 2). The indexer image carries `corpus/fetch.py`
and `corpus/sources.yaml` at `/app/corpus-src/`. The Job runs an **initContainer** `fetch`
(same image) that copies them into an `emptyDir` mounted at `/corpus`, runs
`python /corpus/fetch.py` (one request at a time, 2 s pause) and so produces
`/corpus/raw/*.pdf` and `/corpus/raw/_index.json`. The main container then indexes
`CORPUS_DIR=/corpus/raw`. The weekly CronJob repeats the fetch, which is the "re-index when
the material changes" case. The `ci` overlay replaces this with a ConfigMap of the three
fixture documents mounted at `/corpus/raw` and no initContainer.

## Images

| Image | Build context | Dockerfile | Base |
| --- | --- | --- | --- |
| `rahbar-api` | `api/` | `api/Dockerfile` | `python:3.12-slim`, non-root uid 10001 |
| `rahbar-indexer` | repository root (needs `corpus/`) | `indexer/Dockerfile` | `python:3.12-slim` + `tesseract-ocr`, `poppler-utils`, non-root |
| `rahbar-ui` | `ui/` | `ui/Dockerfile` | `nginxinc/nginx-unprivileged:1.27-alpine` |

Locally: tag `dev`, loaded with `minikube image load`, `imagePullPolicy: IfNotPresent`.
CD: `docker.io/<DOCKERHUB_USERNAME>/rahbar-{api,indexer,ui}:<git sha>`; the prod overlay's
image names are set with `kustomize edit set image` in `cd.yml`. Never `:latest`.

## Overlays

| Overlay | Purpose | Differences from base |
| --- | --- | --- |
| `k8s/overlays/dev` | minikube on a laptop | images `:dev`, `IfNotPresent`; Ollama host egress ipBlock |
| `k8s/overlays/prod` | the CD deploy target | images from Docker Hub by SHA |
| `k8s/overlays/ci` | the `e2e-eval` job on kind | `EMBED_MODEL=local-hash-v1`, `LLM_PROVIDER=stub`, `RETRIEVER=chroma`, fixtures ConfigMap at `/corpus/raw`, no initContainer, smaller requests |

Apply order (also in the README): namespace, then the overlay (`kubectl apply -k`), wait
for chroma and redis, run the indexer Job, wait for it, then `kubectl rollout status
deployment/api`; the api pods sit at 0/1 until the collection is non-empty.

## Evaluation matching rule

A retrieved passage matches a golden `relevant` entry when `source` is equal and the
golden `section`, lower-cased, equals the retrieved section or is a prefix of it followed
by a space or end of string (so golden `5.4` matches `5.4 Academic Warning` but not
`5.41 ...`). recall@k is the fraction of a question's relevant entries matched by any of
its top-k retrieved passages; questions with no relevant entries contribute nothing to
recall. Citation validity uses the same rule against the response's own `retrieved` list.
