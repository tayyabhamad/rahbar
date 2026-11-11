#!/usr/bin/env bash
# Every rendered object except the Namespace itself must carry `namespace: rahbar`.
# Why: the base kustomization's `namespace:` transformer does not reach resources that a
# *component* adds at the overlay level, so the Ollama objects once rendered without a
# namespace and landed in `default`, where the api could not resolve the Service
# (docs/evidence/ollama-incluster-namespace-bug.txt). Components now set metadata.namespace
# on every resource; this check keeps it that way.
#   usage: kustomize build k8s/overlays/dev | scripts/check_namespace.sh
set -euo pipefail
rendered=$(cat)
kinds=$(grep -cE '^kind: ' <<<"$rendered")
namespaced=$(grep -cE '^  namespace: rahbar$' <<<"$rendered")
if [ "$namespaced" -ne $((kinds - 1)) ]; then
  echo "namespace check: $kinds objects, $namespaced in namespace rahbar (expected $((kinds - 1)))" >&2
  grep -nE '^kind: |^  name: |^  namespace: ' <<<"$rendered" | grep -B2 -A0 'kind:' | head -60 >&2
  exit 1
fi
echo "namespace check: $namespaced of $kinds objects in rahbar (plus the Namespace)"
