# ADR 0003: Redis holds the semantic cache, namespaced by index generation

## Status
Accepted · 2026-11-08

## Context
Section 3.3 asks for a semantic cache: a question whose vector is close enough to one
already answered should be served the stored answer without calling the model. The
entries must expire, must be wiped when the index changes (an answer grounded in an old
chunk must never outlive the chunk), and must never make a request fail when the cache
is down. Two stores were already in the cluster, so the question was which one holds
the entries: Chroma (a second collection) or Redis.

## Decision
**Redis** (`redis:7.4-alpine`, `k8s/base/redis-deployment.yaml`), through
`api/app/services/cache.py`.

* **Entry**: the question text, its vector and the serialised `AskResponse`, stored as
  one JSON string under `rahbar:cache:<index_generation>:<uuid>` with `EX=CACHE_TTL_SECONDS`
  (`cache.py:92-102`; TTL 86400 s from `k8s/base/configmap.yaml:26`).
* **Lookup**: `SCAN` the current generation's keys, `MGET` them, cosine over the stored
  vectors, hit when the best similarity is at or above `CACHE_THRESHOLD`
  (`cache.py:67-90`; threshold 0.92 from `configmap.yaml:25`, 0.95 in the ci overlay
  because hashed bag-of-words scores run differently, `k8s/overlays/ci/configmap-patch.yaml:14`).
  The question is embedded once and the same vector serves the cache and the retrieval
  (`api/app/services/answer.py:66-71`).
* **Invalidation by generation**: the indexer stamps `index_generation` (a UTC timestamp)
  into the collection metadata (`indexer/pipeline/store.py:58`); the API reads it
  (`api/app/retrieval/chroma.py:71-75`), uses it in every key, and deletes every key of
  another generation when it first notices a change, both on `/readyz`
  (`api/app/routes/health.py:32-37`) and on the next question
  (`answer.py:36-47`, `cache.py:104-117`). Entries from the previous index can
  therefore never hit again, even in the seconds before the delete runs, and the indexer
  never needs to reach Redis.
* **Never cached** (`answer.py:124-128`): follow-up questions (anaphora or a follow-up
  opener, `api/app/services/guards.py:15-19`, `:37-42`), abstentions
  (`answered_from_context: false`), answers whose citations were all dropped by the
  grounding check, and the canned fallback after two failed generations. A cached
  mistake is served to everyone, so only answers that passed every check are stored.
* **Visible to clients**: the additive `cached: bool = false` field on `AskResponse`
  (`api/app/schemas.py:69`, the one change to the starter contract, `:8-10`) and the
  `X-Cache: HIT|MISS` header (`api/app/routes/ask.py:18`). The UI shows a chip from either
  (`ui/index.html:114-116`); `eval/run_eval.py` records `cached` per question
  (`eval/run_eval.py:176`).
* **Failure mode**: any Redis error is logged and treated as a miss (`cache.py:87-90`,
  `:100-102`), so the cache can never turn a question into a 500. `/readyz` still lists
  Redis as a dependency (`api/app/services/readiness.py:93-98`), because a pod with no
  cache is a pod that will hammer the model, and we would rather know.
* **Local development**: an empty `REDIS_HOST` disables the cache entirely
  (`api/app/config.py:59-61`); `make dev` sets it that way (`Makefile:36`).

## Options considered
* **A second Chroma collection** (`rahbar_cache`) holding question vectors with the answer
  as metadata. It reuses the vector index, so lookup is an HNSW query rather than a
  scan. Against it: Chroma has no TTL (expiry becomes our cron), every write makes the
  API a second writer of the store whose single-writer property ADR 0001 exists to
  protect, invalidation on re-index is "delete a collection while the Job rewrites
  another one", and a Chroma outage would then take out both retrieval and the cache
  instead of one. The NetworkPolicy would also have to let the API write to Chroma, which
  today it only needs to read.
* **In-process memory** (a dict per pod). No dependency, but three to eight replicas
  would each warm their own cache, hit ratio divides by the replica count, and a rollout
  empties it. Fine for a single process, wrong for this Deployment.
* **PostgreSQL with pgvector.** A real vector index and a TTL via a timestamp column, but
  a third stateful system to run on a laptop cluster for a cache of hundreds of entries.
* **Exact-match cache** (hash of the normalised question). Trivial and safe, but it
  misses the point of the exercise: "what CGPA triggers a warning" and "when am I put on
  academic warning" should be one entry.

## Consequences
* **Lookup is O(entries) in the current generation**: one `SCAN`, one `MGET`, a Python
  cosine per entry. For the hundreds of entries a university help desk accumulates in a
  day this is a few milliseconds; at tens of thousands it would need a vector index
  (Redis Stack's `FT.SEARCH` with a `VECTOR` field, or the Chroma option above). The
  TTL bounds the growth.
* **Separate failure domain, separate PVC.** Redis runs `--appendonly yes` with a 1 Gi
  PVC and `Recreate` strategy (`redis-deployment.yaml:17-18`, `:40`), so a restart keeps
  the entries and two pods never share the AOF. Only the api pod may reach it
  (`k8s/base/networkpolicies.yaml:120-139`).
* **Threshold is a tuning decision.** 0.92 was chosen to avoid serving an answer about
  the 2023 rule to a question about the 2026 rule when both mention attendance; the
  measured false hit that ruled out a lower value, and the thresholds tried, are
  in `docs/PIPELINE.md` ("Semantic cache"): with the lexical embedder 0.35 is ruled out
  by a false hit between the "previous 2023 version" and "clause 1.21" questions (0.367),
  0.40 is the lowest safe value, and 0.92 is kept for the dense model, to be re-measured
  there with the same three rows.
* **Code changes do not invalidate the cache.** The key is namespaced by index
  generation, not by API version, so a fix to the guard regexes or the prompt leaves
  yesterday's answers serving until the TTL expires. That bit us during evaluation
  (`docs/ENGINEERING-NOTES.md`, question 7); the remedy is `redis-cli FLUSHALL` after a
  deploy that changes answer behaviour, or adding the image tag to the key prefix.
* **Follow-ups bypass the cache in both directions**, so a session that leans on
  anaphora pays the model each time; the alternative, caching with conversation state,
  needs a session id the contract does not have.
* **Metrics**: `rahbar_cache_lookups_total{result=hit|miss|skipped|error}` and
  `rahbar_cache_writes_total` (`api/app/routes/metrics.py:31-34`) give the hit ratio as
  `hit / (hit + miss)`.
