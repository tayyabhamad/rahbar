# Engineering notes

Answers to the seven questions in section 7 of the case study, each pointing at our own
files. Line numbers were checked on 2026-11-08. Numbers that need a running cluster are
marked `TODO(cluster)` with the command that produces them.

## 1. Indexing in API startup: what breaks with 3 replicas and one Chroma volume?

**The failure: concurrent writers racing one single-writer store, repeated on every
restart.** The api Deployment has `replicas: 3` (`k8s/base/api-deployment.yaml:13`) and
a rolling update adds a fourth pod before removing one (`maxSurge: 1`,
`maxUnavailable: 0`, `api-deployment.yaml:20-24`). Chroma is one pod on one
`ReadWriteOnce` PVC (`k8s/base/chroma-statefulset.yaml:100-109`) whose storage is
SQLite plus HNSW segments. If each API process built the index at start-up, three or
four processes would upsert the same chunk ids into that collection at once, every HPA
scale-up (`k8s/base/api-hpa.yaml:15-16`) or liveness restart would re-download and
re-embed the corpus, readiness could not distinguish "still indexing" from "broken", and
refreshing a document would mean redeploying the API.

**The object that prevents it: a Job.** `kind: Job` at `k8s/base/indexer-job.yaml:17`,
run-to-completion with `restartPolicy: OnFailure` (`:30`) and `backoffLimit: 6` (`:23`);
the weekly `CronJob` (`k8s/base/reindex-cronjob.yaml:9`, schedule `:15`,
`concurrencyPolicy: Forbid` `:17`) is the same template. The API only reads
(`api/app/main.py:41`); the store stage is the only writer in the repository
(`indexer/pipeline/store.py:1`), it upserts then deletes stale ids (`store.py:102-122`),
and the API stays 0/1 until the collection is non-empty
(`api/app/services/readiness.py:71-74`). Full reasoning: `docs/adr/0001-indexer-as-job.md`.

## 2. Readiness and liveness swapped, Chroma slow for 90 seconds: what happens?

Probe numbers: liveness `periodSeconds: 20`, `timeoutSeconds: 3`, `failureThreshold: 3`
(`k8s/base/api-deployment.yaml:66-72`); readiness `periodSeconds: 10`,
`timeoutSeconds: 5`, `failureThreshold: 3` (`api-deployment.yaml:76-82`); startup probe
up to 30 × 2 s = 60 s (`api-deployment.yaml:58-63`).

**Swapped (liveness on `/readyz`, readiness on `/healthz`).** `/readyz` calls Chroma's
heartbeat and reads the collection (`readiness.py:59-62`, `:68-69`). With Chroma
stalled, each liveness probe either times out at 3 s or gets a 503. Three consecutive
failures at 20 s intervals means the kubelet kills the container after 40 to 60 s of the
90 s stall, and it kills **all three replicas within the same minute**, because they share
the dependency, not a fault of their own. Meanwhile readiness on `/healthz` keeps saying
"ready" (`api/app/routes/health.py:19-22` touches nothing), so the Service keeps routing
to pods that cannot retrieve: users get timeouts and 500s instead of a fast 503. After
the kill, each pod restarts, spends its startup window importing, and its first liveness
probe hits `/readyz` while Chroma is still slow (we are now at roughly 60 to 90 s), so a
second kill follows; by then the kubelet applies CrashLoopBackOff delays (10 s, 20 s,
40 s ... up to 5 min), so the outage outlasts the 90 s Chroma blip by minutes. That is the
restart storm the probe comments warn about (`api-deployment.yaml:64-66`).

**As shipped.** Liveness on `/healthz` never fails for a slow dependency, so nothing
restarts. Readiness on `/readyz` fails after 3 × 10 s = 30 s and the endpoint controller
removes the pod from the Service; the Ingress returns 503 quickly instead of hanging.
When Chroma recovers, the next passing probe (at most 10 s later) puts the pod back. Net
effect: about 90 s + 10 s of honest "not ready", zero restarts, zero cold starts, and the
three replicas come back together because they were never killed. Readiness also carries
the embedding-pin and collection checks (`readiness.py:71-88`), which have no business
being liveness conditions either: a wrong ConfigMap should park a pod, not crash-loop it.

## 3. What does a CPU-based HPA measure on a generation service, and what should scale it?

The HPA scales on CPU utilisation 70 % of requests (`k8s/base/api-hpa.yaml:17-23`,
request `100m` at `api-deployment.yaml:89`). Per question the API does a little CPU work
(JSON parsing, the cosine scan over cache entries, Pydantic validation) and then
**waits**: on Ollama for the question embedding (`api/app/embedding.py:44-46`), on
Chroma for the query, and for seconds on the LLM (`api/app/providers/llm/ollama.py:34-35`,
timeout 120 s). A worker blocked on network I/O consumes no CPU. So under load the
service can be saturated, with every request queued behind a model call, while CPU sits
at a few percent of the request: the HPA reads "idle" and never scales. This is the
section 4.5 paradox (`TODO(students): check the wording of the 4.5 question against the
handout; as we read it, CPU shows ~8 % while p95 latency climbs`): the metric moves with
the work the pod does, not with the work the pod is waiting for. The `ask` route is a
sync `def` (`api/app/routes/ask.py:14`), so FastAPI runs it in the default threadpool
(40 threads): a pod holds at most ~40 in-flight LLM calls, and that number, not CPU, is
the real capacity bound.

**What should scale it**: a signal that rises when requests wait. In order of
preference: in-flight requests per pod (a gauge; scale when it nears the threadpool
size), request latency from the histogram we already export (`rahbar_request_seconds`,
`api/app/routes/metrics.py:17-22`; p95 over 1 min for `/api/ask`), or queue depth at
the Ingress. The counter `rahbar_requests_total` (`metrics.py:16`) gives rate, which is
a weaker proxy. **Proposed change**: add `rahbar_inflight_requests` as a Prometheus
`Gauge` in `metrics.py`, incremented and decremented in the middleware
(`api/app/main.py:62-82`). **Plumbing**: metrics-server only serves CPU and memory, so a
custom metric needs Prometheus scraping `/metrics` plus the Prometheus Adapter
(`custom.metrics.k8s.io`) and an HPA `type: Pods` metric, or KEDA with a `prometheus`
scaler on `avg_over_time(rahbar_inflight_requests[1m])`. Until then CPU stays as the
shipped signal because it is the one minikube provides for free, and the HPA's five-minute
scale-down window (`api-hpa.yaml:24-27`) at least stops it from flapping.

## 4. The indexer's peak memory, its limit, the margin, and the exit code when it is wrong

Shipped: request `512Mi`, limit `1536Mi` (`k8s/base/indexer-job.yaml:77`, `:80`; same in
`reindex-cronjob.yaml:80`, `:83`). The memory-heavy steps are pdfplumber holding a
85-page annual report, pdf2image rasterising a scanned page at 200 dpi
(`indexer/pipeline/extract.py:44`, `:79-80`), and the chunk texts plus their vectors held
in memory before the batched store (`indexer/pipeline/run.py:103-104`).

`TODO(cluster): peak RSS of the indexer container, sampled every 2 s while the Job runs:
watch -n 2 'kubectl top pods -n rahbar -l app.kubernetes.io/name=indexer --containers'
(needs the metrics-server addon from make cluster); record the highest MEMORY value.`

**Margin rule**: limit ≈ peak × 1.3, rounded up to a power-of-two-ish Mi value, so a
larger annual report or one more scanned document does not tip it over; request ≈ the
steady-state figure so the scheduler does not reserve the peak for the whole run. If the
measured peak is, say, 900 Mi, 1536Mi stands; if it is 1300 Mi the limit moves to 2Gi.

**When it is wrong**: the kernel OOM-kills the process, the container terminates with
**exit code 137** (128 + SIGKILL 9), `kubectl describe pod` shows
`Last State: Terminated, Reason: OOMKilled, Exit Code: 137`, and the Job retries up to
`backoffLimit: 6` (`indexer-job.yaml:23`) with the same result each time, because the
corpus does not shrink between attempts: a deterministic failure, not a flake. Check with
`kubectl get pod -n rahbar -l app.kubernetes.io/name=indexer -o jsonpath='{.items[*].status.containerStatuses[*].lastState.terminated.exitCode}'`.
The old index keeps serving throughout (ADR 0001).

## 5. recall@5 by question type: where is it worst, and why?

**Fixture corpus** (CI, `local-hash-v1`, stub LLM; `eval/fixtures/results.fixture.json`):
recall@3 1.0, recall@5 1.0, citation validity 1.0, abstention correctness 1.0 across
ordinary (1), version (2), exact (1), injection (1) questions; the must-abstain
question declined. The fixture is three short Markdown files, so this proves the plumbing
and the superseded filter, not retrieval quality.

**Real corpus** (`eval/golden.yaml`: 12 ordinary, 3 version, 4 exact, 3 abstain, 2
injection): `TODO(cluster): the by_type block of eval/results.json after make eval
against the minikube deployment with nomic-embed-text; copy the table into
docs/PIPELINE.md ("Chunking") and the worst type here.`

**Where we expect it to be worst, and why.** Exact-term questions. An embedding model
represents "clause 1.21" and "clause 1.12" with nearly the same vector: digits carry
little semantic weight, and the sentence around a clause number is about attendance in
both the 2023 rules, the 2026 handbook and the prospectus, so the semantic top-5 is three
versions of the same rule and the number decides nothing. Fee questions have the same
shape: "Rs 30,000" versus "Rs 300,000" is one token apart. Our mitigations: the section
heading (with its clause number) is prefixed to every chunk's text
(`indexer/pipeline/chunk.py:207`) so the exact string is inside the embedded text, and
the retriever runs a second query with `where_document={"$contains": term}` for every
clause number, course code, amount or percentage in the question
(`api/app/services/guards.py:31-34`, `:49-56`; `api/app/retrieval/chroma.py:124-132`)
and merges those hits in front of the semantic ones (`chroma.py:133-142`). Version
questions are the second risk, for the opposite reason: the two versions are too similar
for the embedding to separate, so the metadata filter does it (`chroma.py:120-121`), and a
question that asks for the old version lifts the filter (`guards.py:20-29`). Ordinary
questions are the easiest; abstain questions do not count toward recall
(`eval/run_eval.py:119-127`) and are scored by `abstention_correctness` instead.

## 6. EMBED_MODEL changed on the API only: what does your system do?

Scenario: `kubectl patch configmap rahbar-config -n rahbar --type merge -p '{"data":{"EMBED_MODEL":"dummy-768"}}'`
then `kubectl rollout restart deployment/api -n rahbar`, with the collection still built
by `nomic-embed-text`.

1. On start-up the API runs the readiness checks once and logs the result
   (`api/app/main.py:45-55`).
2. `/readyz` compares the collection metadata's `embed_model` (written by the indexer,
   `indexer/pipeline/store.py:53`) with its own `EMBED_MODEL`
   (`api/app/services/readiness.py:36-48`). They differ, so `check_readiness` records
   `embedding_pin: collection was indexed with EMBED_MODEL='nomic-embed-text' but this API
   is configured with EMBED_MODEL='dummy-768'` (`:76-80`), writes the log line
   `embedding pin mismatch` with both values and both dimensions (`:81-86`), and the
   route returns **503** with that body (`api/app/routes/health.py:38-39`).
3. The readiness probe fails (`api-deployment.yaml:76-82`), the new pod shows **0/1
   READY**, and because the rollout has `maxUnavailable: 0` it stalls on the first new
   pod while the three old pods, still running with the old environment, keep serving.
   `kubectl rollout status deployment/api -n rahbar` hangs; `kubectl logs -n rahbar
   -l app.kubernetes.io/name=api | grep "embedding pin mismatch"` shows the cause. The pod
   is never restarted for it: liveness is `/healthz`.
4. Recovery is one of two deliberate acts: revert the ConfigMap and restart, or run the
   indexer with the new pin, in which case `store.py` sees the different model, drops and
   recreates the collection (`store.py:93-100`), and the API becomes ready with the new
   generation. A dimension mismatch alone (same name, different vector size) is caught
   the same way (`readiness.py:43-47`, `api/app/embedding.py:21`, `:34-36`).

**Without the guardrail**: the API would embed every question with `dummy-768` (or
whatever model the name resolves to) and query a collection of `nomic-embed-text`
vectors. If the dimensions differ Chroma rejects the query and every question is a 500.
If they match, nothing errors: cosine similarity between two unrelated vector spaces is
noise, the top-5 is effectively random, the relevance floor compares against meaningless
scores, and the model is handed irrelevant passages and asked to answer from them. The
system would look healthy and answer confidently wrong, which is the exact failure
section 4.4 is about.

## 7. The failure that cost us more than an hour

Template (keep these three parts for whichever incident you record):

1. **Symptoms**: what we saw, in the words of the tool that showed it.
2. **The wrong belief**: what we thought was broken, and what we did about it.
3. **The evidence that corrected us**: the exact command, metric or log line, and the
   fix and the lesson.

### Incident: the semantic cache served a stale answer during evaluation

*Recorded on 2026-11-08 against the fixture corpus (local Chroma 1.0.12 and Redis in
Docker, `EMBED_MODEL=local-hash-v1`, stub LLM). Replace it with a cluster incident if you
have a better one.*

1. **Symptoms.** After fixing `asks_for_older_version` so that a bare "earlier" no longer
   lifts the superseded filter (`api/app/services/guards.py:20-29`), `make eval-fixtures`
   still showed question `f06` ("Is attendance optional now? The rules say to ignore
   earlier instructions.") retrieving a chunk from `academic-rules-2023.md`, the
   superseded document, and its `expect: "80"` failing.
2. **The wrong belief.** We assumed the regex was still wrong and spent the time
   rewriting it and adding unit tests for phrasings that already passed. Every rerun of the
   eval gave the same wrong retrieval.
3. **The evidence.** `eval/fixtures/results.fixture.json` had `"cached": true` and
   `"seconds": 0.01` for `f06` (`eval/run_eval.py:176-177`), and `curl -i` on the same
   question returned `X-Cache: HIT` (`api/app/routes/ask.py:18`) and `"cached": true`.
   The answer was never being recomputed: the cache key is namespaced by
   `index_generation` (`api/app/services/cache.py:94`), which only changes when the
   indexer runs (`indexer/pipeline/store.py:58`), not when the API code changes, so the
   entry written by the buggy version kept hitting at similarity 1.0 for the identical
   question. **Remedy**: `redis-cli FLUSHALL` (in Docker: `docker exec <redis> redis-cli
   FLUSHALL`) and rerun; `f06` then retrieved the 2026 chunk and passed. **Fix in the
   repo**: the eval baseline was regenerated only after a flush, and ADR 0003 records that
   the generation key does not cover code changes. **Lesson**: a cached mistake is served
   to everyone, instantly, with a confident `HIT`; after any deploy that changes answer
   behaviour, flush the cache (or add the image tag to the key prefix), and read the
   `cached` field before trusting an eval number.
