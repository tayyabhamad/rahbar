# ADR 0001: The indexer is a Kubernetes Job, not API start-up code

## Status
Accepted · 2026-11-08

## Context
The API runs as a Deployment with three replicas (`k8s/base/api-deployment.yaml:13`) and
the HPA can take it to eight (`k8s/base/api-hpa.yaml:15-16`). Chroma is one pod with one
`ReadWriteOnce` volume (`k8s/base/chroma-statefulset.yaml:100-109`) and its storage is a
SQLite file plus HNSW segments, which tolerate exactly one writer
(`docs/ARCHITECTURE.md:43-51`).

The obvious shortcut, "build the index when the API starts", breaks in four ways once
there is more than one replica:

1. **Concurrent writers.** Every replica would run the same pipeline against the same
   collection at the same time. Three processes upserting the same ids into one
   single-writer store is a race: duplicated chunks, lock errors, or a half-written index
   depending on timing. A rolling update (`maxSurge: 1`, `api-deployment.yaml:20-24`)
   adds a fourth writer while three are still running.
2. **Every restart re-indexes.** A liveness restart, an HPA scale-up or a node reschedule
   would download ~35 MB of documents from the university site, OCR the scanned pages and call
   the embedding model for all 360+ chunks again. That is minutes of work, repeated per
   pod, for a corpus that changes a few times a year.
3. **Readiness lies.** While a pod indexes, it is either "ready" and answering from a
   partial collection, or "not ready" for minutes, and the probe cannot tell the two
   apart. The retrieval results a user gets would depend on which replica happened to
   finish first.
4. **Re-indexing needs a redeploy.** The only way to pick up a changed document would be
   to restart every API pod, which couples a data refresh to a code rollout.

## Decision
Indexing is a separate workload with the API as a pure reader
(`api/app/main.py:41`, `indexer/pipeline/store.py:1`):

* A **Job** `indexer` (`k8s/base/indexer-job.yaml:17`) runs the pipeline once and exits
  (`indexer/pipeline/run.py:107-128`). `restartPolicy: OnFailure` and `backoffLimit: 3`
  (`indexer-job.yaml:30`, `:23`) retry a failed run; `ttlSecondsAfterFinished: 3600`
  (`:24`) removes the finished pod. A `fetch` initContainer downloads the corpus into an
  `emptyDir` first (`indexer-job.yaml:40-63`, `docs/ARCHITECTURE.md:140-149`).
* A **CronJob** `reindex` (`k8s/base/reindex-cronjob.yaml:15`, Mondays 03:00) has the
  identical pod template and `concurrencyPolicy: Forbid` (`:17`), so there are never two
  writers even on a slow week. A manual re-index is
  `kubectl create job --from=cronjob/reindex reindex-$(date +%s) -n rahbar`
  (`Makefile:66`), never a redeploy of the API.
* The API's **readiness** depends on the index existing: `/readyz` returns 503 while the
  collection is empty (`api/app/services/readiness.py:71-74`, probe at
  `api-deployment.yaml:76-82`), so the pods sit at 0/1 until the Job has written, and
  the Service never routes to a pod with nothing to retrieve from.
* Only the indexer's ServiceAccount and pod label may reach Chroma for writes; the
  NetworkPolicy allows `api, indexer -> chroma:8000` and nothing else
  (`k8s/base/networkpolicies.yaml:96-118`).

## Options considered
* **Index in API start-up, guarded by a lock** (a Redis `SETNX` or a lease). Fixes the
  race but not the rest: every restart still waits on the lock holder, readiness still
  cannot distinguish "indexing" from "broken", and a re-index is still a rollout. The lock
  is one more thing to expire correctly.
* **Leader election among API pods.** One replica indexes, the others wait. Adds a
  coordination library and RBAC for Leases to a service whose only job is to read, and
  the leader still re-indexes on every restart.
* **An init container per API pod.** Each pod would build the index before uvicorn
  starts: three init containers racing the same volume, so it is option 1 with worse
  logging. The init-container pattern is right for a per-pod step (we use it for the
  fetch inside the Job), not for a shared resource.
* **A sidecar indexer in the Chroma pod.** Keeps the writer next to the store, but the
  Chroma pod is a StatefulSet that must restart as rarely as possible; a long-running
  sidecar with a model dependency and network egress to the university site makes it
  restart more, and a sidecar has no "finished" state for a probe to look at.
* **A Deployment that indexes in a loop.** Always-on pods for work that happens weekly;
  a Job with a CronJob is the native shape for run-to-completion work.

## Consequences
* **Order matters at deploy time.** The Job must complete before the API is useful; the
  README lists the order (chroma, redis, Job, then `kubectl rollout status
  deployment/api`). CI polls the Job for `Complete` or `Failed` before waiting on the API
  (`.github/workflows/ci.yml:364-386`).
* **A failed re-index leaves the old index serving.** `store.py` upserts into the
  existing collection in batches and only afterwards deletes ids this run did not produce
  (`indexer/pipeline/store.py:102-122`); it refuses to write an empty index (`:75-76`)
  and the embed stage refuses mixed dimensions (`indexer/pipeline/embed.py:77-81`). A
  crash mid-run therefore leaves the previous chunks in place; a query during a run can
  see a mix of old and new chunks for a few seconds (`store.py:7-10`), which we accept
  for a policy corpus.
* **Cache invalidation rides on the Job.** Every run stamps a new `index_generation`
  into the collection metadata (`store.py:58`); the API namespaces its cache keys by it
  and deletes other generations when it notices the change
  (`api/app/services/cache.py:104-117`, `api/app/routes/health.py:32-37`). The indexer
  never talks to Redis (`store.py:19-20`), so the "redis from api only" NetworkPolicy
  (`networkpolicies.yaml:120-139`) holds.
* **Two pod templates to keep in sync.** The Job and the CronJob carry the same spec
  twice (`indexer-job.yaml:25-98`, `reindex-cronjob.yaml:28-100`); the ci overlay has
  to patch both (`k8s/overlays/ci/kustomization.yaml:32-43`). A drift between them
  would be the thing to watch for in review.
* **The pin changes are visible.** If `EMBED_MODEL` differs from the collection's, the
  Job drops and recreates the collection (`store.py:93-100`) while the API is already
  failing readiness on the mismatch (ADR 0004), so no replica that was serving good
  answers is interrupted by the rebuild.
