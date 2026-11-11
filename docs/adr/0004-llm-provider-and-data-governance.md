# ADR 0004: LLM provider interface, structured output, and data governance

## Status
Accepted · 2026-11-08

## Context
Section 3.5 lets us generate with a local Ollama model or a hosted free-tier endpoint,
and the choice must be a configuration change, not a code change. Whatever generates, the
answer must be a validated JSON object with citations (section 3.4), a user must never
see a 500 because the model wrote prose, and the system handles documents and
questions about students, so section 2 and section 4.7 ask what we do with the data: what
leaves the cluster, what is stored, what is logged, and how retrieved text is kept from
acting as instructions.

## Decision

### Provider interface and modes
`LLM_PROVIDER` selects the implementation in `api/app/providers/llm/__init__.py:7-23`:

| Value | Class | Transport | Structure enforcement |
| --- | --- | --- | --- |
| `stub` | `StubProvider` | none | n/a; unit tests and CI (`k8s/overlays/ci/configmap-patch.yaml:11`) |
| `ollama-host`, `ollama-incluster` | `OllamaProvider` (`ollama.py:19`) | `POST {OLLAMA_URL}/api/chat` (`ollama.py:35`) | `format=<JSON schema of Answer>` (`ollama.py:30`): constrained decoding |
| `hosted` | `HostedProvider` (`hosted.py:21`) | OpenAI-compatible `POST {HOSTED_BASE_URL}/chat/completions` (`hosted.py:39`) | `response_format: {"type": "json_object"}` (`hosted.py:35`) with the schema in the system prompt |

Both modes share one path: the provider implements `complete(messages, retry)` and
returns text plus token counts (`common.py:40-44`); `generate_checked` builds the
prompt, parses the reply into the Pydantic `Answer` (`api/app/schemas.py:41-48`), and on
a parse or validation failure calls once more with the error quoted back
(`common.py:47-74`, `prompt.py:53-54`). A second failure raises `GenerationError`,
which the answer service turns into a canned, never-cached fallback
(`api/app/services/answer.py:97-106`, `validate.py:66-67`). Timeouts
(`LLM_TIMEOUT_SECONDS=120`, `configmap.yaml:30`) raise the same error so a hung model
cannot pin a worker (`common.py:54-57`). After generation, every citation that names no
retrieved passage is dropped and an answer left without citations is demoted to
`answered_from_context: false` (`answer.py:153-173`, `validate.py:40-58`).

`ollama-host` and `ollama-incluster` differ only in `OLLAMA_URL` and the NetworkPolicy
path: the host variant reaches the laptop through
`k8s/components/ollama-host/networkpolicy.yaml:22-27`; the in-cluster variant (the
brief's bonus mode) is the component `k8s/components/ollama-incluster`: an Ollama
Deployment with a PVC for the weights pulled by an initContainer, a Service `ollama`,
and its own allow rules (`networkpolicy.yaml` there). The dev overlay ships `ollama-host`
with `llama3.2:3b` (`configmap.yaml:13-16`); `dev-ollama-incluster` ships the component.

### The embedding pin
Embeddings are not a provider choice: `EMBED_MODEL` is one ConfigMap key read by both
the indexer and the API (`configmap.yaml:14`, `indexer/pipeline/config.py:23`,
`api/app/config.py:29`). Two values exist: `local-hash-v1`, a deterministic hashed
bag-of-words embedding (dimension 512) implemented byte-for-byte identically in
`indexer/pipeline/embedding_local.py` and `api/app/embedding_local.py` and enforced by a
test (`indexer/tests/test_embedding_local.py:12-18`), used in CI; and anything else,
which goes to Ollama's `/api/embed` from both sides (`indexer/pipeline/embed.py:41-42`,
`api/app/embedding.py:44-46`), `nomic-embed-text` (768) in dev and prod. Generation and
embeddings therefore share the same Ollama host. The indexer writes `embed_model` and
`dimension` into the collection metadata (`indexer/pipeline/store.py:53-54`); the API
refuses readiness on a mismatch (`api/app/services/readiness.py:36-48`, `:76-86`).

### Data governance
* **Only public documents.** Every source is a public FAST-NUCES page reachable without
  a login (`corpus/sources.yaml`, `corpus/MANIFEST.md`). The PDFs are not committed;
  the repository carries the manifest with SHA-256 hashes and the fetch script, and the
  Job downloads them at run time, one request at a time with a 2 s pause and a clear
  User-Agent (`corpus/fetch.py:40`, `:102`, `:135`).
* **No PII in the corpus.** The documents are rules, fees, grading tables and annual
  reports with aggregate numbers; staff names in the annual reports are already
  published by the university. We did not include transcripts, student lists or anything
  behind the portal.
* **Questions are not kept.** A question lives in the Redis cache entry for at most
  `CACHE_TTL_SECONDS` (86400 s) and only when it was answered from context
  (`api/app/services/cache.py:94-97`, `answer.py:124-128`), and in the JSON log line
  for the request. Nothing writes questions to a database or a file. There is no
  account, so a question cannot be tied to a person except by whatever the Ingress logs.
* **What leaves the cluster.** With `ollama-host` the prompt (question plus retrieved
  passages) goes to the laptop's Ollama over the host network. With `hosted` it goes to
  the configured third-party endpoint over 443; that is the one mode in which student
  questions reach an external company, and it is off by default
  (`configmap.yaml:13`, `:17`).
* **The key.** `LLM_API_KEY` exists only in the Secret `rahbar-secret`, created from the
  shell with `make secret` (`Makefile:60-63`) or by CD from a GitHub secret
  (`.github/workflows/cd.yml:387-389`); the placeholder manifest is never applied
  (`k8s/base/kustomization.yaml:4-5`). Only the api Deployment mounts it, as optional
  (`api-deployment.yaml:53-56`). The settings object hides it from `repr`
  (`api/app/config.py:55`), the only place it is used is the `Authorization` header
  (`hosted.py:37`), and no log call includes it.
* **What is logged.** One JSON line per request with request id, path, status and
  latency (`api/app/main.py:79-82`) and one `answered` line with retriever, provider,
  chunk ids, scores, floor, abstention reason, fallback flag and cache decision
  (`answer.py:129-149`). The model's `reasoning` field is never returned to the user
  (`schemas.py:5-6`). Logs go to stdout only (`api/app/logjson.py:3`). The question text
  is not in the `answered` line; a failed model reply logs its first 200 characters
  (`common.py:67-69`) so a broken schema can be debugged.
* **Prompt injection.** Retrieved text is data: each passage is wrapped in an
  `<evidence>` tag whose own tag text has been neutralised inside the passage
  (`prompt.py:63-65`, `:73-76`), the system prompt says in plain words that nothing
  inside those tags is an instruction (`prompt.py:30-33`), the question is escaped the
  same way (`:83`), and the structure is enforced by the decoder, not by asking nicely.
  The superseded filter is lifted only for an explicit request about an older version,
  so "ignore earlier instructions" does not surface the 2023 rules
  (`api/app/services/guards.py:20-29`). The golden sets carry injection questions
  (`eval/fixtures/golden.fixture.yaml:48-53`, `eval/golden.yaml:193-206`). The filter
  itself can be switched off only through `SUPERSEDED_FILTER=false`
  (`api/app/config.py:47-49`), which exists to reproduce failure mode E for
  `docs/FAILURE-ANALYSIS.md` and is never set in any overlay.

## Options considered
* **A single hosted provider only.** Simplest, but every student question would leave
  the cluster, the demo would depend on a free-tier quota, and the assignment asks for
  the local option to be real.
* **Ollama in the cluster as a Deployment.** Keeps everything inside the namespace, but
  a 3B model wants 3 to 4 GB of RAM and `make cluster` gives minikube 6 GB in total
  (`Makefile:39`) for Chroma, Redis, three API pods and the ingress controller. Not the
  default for that reason, but shipped as the optional component
  `k8s/components/ollama-incluster` (overlay `dev-ollama-incluster`) and exercised on this
  laptop's 3 GB node with `qwen2.5:0.5b` (`docs/evidence/ollama-incluster-*.txt`): Ready
  in 2 min 3 s, grounded answers in 7 to 14 s on CPU, 913 Mi resident; the startupProbe
  with `failureThreshold: 30` is what lets the weights load without the liveness probe
  killing the pod. The same run showed why the model size matters more than where it
  runs: the 0.5B model answered the must-abstain question from its training data
  (docs/ARCHITECTURE.md, "In-cluster Ollama").
* **Ask for JSON in the prompt and parse leniently.** Small models are known to add
  prose or a trailing comment around the object, which makes the retry routine rather
  than rare. Constrained decoding (`format=`) prevents that at the decoder; the Pydantic
  validation and the retry stay as the backstop, and `parse_answer` still tolerates
  fences and chatter (`prompt.py:91-113`) for the hosted JSON mode, which only
  guarantees "some JSON object".
* **Function calling / tool use for the schema.** Not uniformly available on the free
  endpoints we tried; `response_format` JSON mode is.
* **Store questions for later analysis.** Useful for improving the golden set, but it
  would need a retention policy and a notice to users; out of scope, so we store
  nothing beyond the cache TTL.

## Addendum: embeddings on the hosted path (8 Nov 2026)

`EMBED_PROVIDER` (ConfigMap) selects where `EMBED_MODEL` runs: `ollama` (default) or
`hosted`, an OpenAI-compatible `POST /embeddings` at `HOSTED_BASE_URL` with the Secret's
key. Google AI Studio is the one free tier that serves embeddings as well as chat, so the
`dev-hosted` overlay pins `gemini-embedding-001` (3072 dimensions) next to
`gemini-2.5-flash-lite`; the indexer Job therefore also mounts `rahbar-secret`
(`optional: true`, read only on this path). Both sides share the same three rules
(`api/app/embedding.py`, `indexer/pipeline/embed.py`), and the pin check is unchanged:
model name and dimension in the collection metadata versus the API's ConfigMap. Free
tiers rate-limit, so 429 and 5xx answers are retried with backoff, honouring Retry-After.

## Consequences
* Switching provider is `kubectl patch configmap rahbar-config` plus a rollout; no image
  changes. Switching to `hosted` also needs `HOSTED_BASE_URL`, `LLM_MODEL` and the
  Secret, and makes the privacy statement above weaker; say so in the deployment notes.
* The retry doubles worst-case latency (two model calls up to 120 s each behind an
  Ingress timeout of 180 s, `k8s/base/ingress.yaml:13-14`); the fallback path is what
  keeps that bounded.
* `rahbar_llm_retries_total`, `rahbar_fallbacks_total` and the token counters
  (`api/app/routes/metrics.py:38-45`) are how we would notice a model that stopped
  honouring the schema after an Ollama upgrade.
* The pin check compares names, not weights: a re-pulled `nomic-embed-text` with
  different weights would pass. Pinning the Ollama model digest in `EMBED_MODEL`
  (`nomic-embed-text@sha256:...`) would close that, at the cost of a less readable
  ConfigMap.
* `local-hash-v1` is lexical, so CI's recall numbers measure the pipeline's plumbing and
  the metadata filter, not semantic quality; the real-corpus numbers come from the
  cluster run and are recorded in `docs/PIPELINE.md`.
