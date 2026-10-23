#!/usr/bin/env bash
# Replace the placeholder pin in service-ci.yaml and service-publish.yaml with the release that contains the primitives they call.
#
#   tools/pin-service-workflows.sh <40-hex commit sha> <vX.Y.Z>
#
# The two files call changed-paths, secret-scan, workflow-lint, the language workflows, docker-build, docker-promote,
# gitops-promote and ghcr-prune of THIS repository by commit SHA. That SHA only exists after the primitives are merged and tagged,
# hence two steps: (1) merge the primitives, tag vX.Y.Z; (2) run this script with that merge commit and tag, commit, merge, tag
# the next version. Until then the files carry PLACEHOLDER_PIN_SHA / PLACEHOLDER_PIN_VERSION and workflow-lint fails on purpose.
set -euo pipefail
sha=${1:?usage: $0 <40-hex commit sha> <vX.Y.Z>}
ver=${2:?usage: $0 <40-hex commit sha> <vX.Y.Z>}
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "not a full commit sha: $sha" >&2; exit 1; }
[[ $ver =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "not a version tag: $ver" >&2; exit 1; }
cd "$(dirname "$0")/.."
files=(.github/workflows/service-ci.yaml .github/workflows/service-publish.yaml)
sed -i "s/PLACEHOLDER_PIN_SHA/${sha}/g; s/PLACEHOLDER_PIN_VERSION/${ver}/g" "${files[@]}"
if grep -n PLACEHOLDER_PIN "${files[@]}"; then echo "placeholder left over" >&2; exit 1; fi
echo "pinned $(grep -c "@${sha}" "${files[@]}" | paste -sd' ') uses: line(s) to ${sha} # ${ver}"
