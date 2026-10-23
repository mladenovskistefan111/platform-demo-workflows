#!/usr/bin/env python3
"""Write the two caller files of an app repository: .github/workflows/ci.yaml (pull requests -> service-ci-<language>.yaml, check `ci / gate`,
then the `merge` job: the owner approves the `merge` environment on the run page and the pull request is squash-merged) and
publish.yaml (push to main, or a manual/merge-button run with `before` -> service-publish.yaml). Removes the old ci-*.yaml / release.yaml.
usage: gen-callers.py <repo-dir> <service> <wf-sha> <wf-ver>
<service> is the app name = repository name without the `app-` prefix = helm chart name = Docker Hub repository."""
import os, sys

repo_dir, svc, sha, ver = sys.argv[1:5]
WF = "platform-boutique/platform-demo-workflows/.github/workflows"
GO_CHECKOUT_ENV = ["PRODUCT_CATALOG_SERVICE_ADDR=localhost:3550", "CART_SERVICE_ADDR=localhost:7070",
    "CURRENCY_SERVICE_ADDR=localhost:7000", "SHIPPING_SERVICE_ADDR=localhost:50051",
    "PAYMENT_SERVICE_ADDR=localhost:50051", "EMAIL_SERVICE_ADDR=localhost:8080"]

CFG = {
  "checkout-service": dict(lang="go", with_={"test-packages": "./internal/...", "e2e-packages": "./tests/e2e/...", "smoke-port": '"5050"', "smoke-env": ("|", GO_CHECKOUT_ENV)}, trivy=""),
  "shipping-service": dict(lang="go", with_={"test-packages": "./internal/...", "smoke-port": '"50051"'}, trivy=".trivyignore"),
  "frontend": dict(lang="go", with_={}, trivy=""),
  "product-catalog-service": dict(lang="go", with_={"test-packages": "./internal/..."}, trivy=""),
  "currency-service": dict(lang="node", with_={"smoke-port": '"7000"', "smoke-script": ".github/smoke.sh"}, trivy=".trivyignore"),
  "payment-service": dict(lang="node", with_={"smoke-port": '"50051"'}, trivy=".trivyignore"),
  "email-service": dict(lang="python", with_={}, trivy=".trivyignore"),
  "recommendation-service": dict(lang="python", with_={}, trivy=".trivyignore"),
  "ad-service": dict(lang="java", with_={}, trivy=""),
  "product-catalog-db": dict(lang="none", with_={}, trivy=""),
  "cart-service": dict(lang="dotnet", with_={"solution": "cartservice.sln", "project": "src/cartservice.csproj", "test-project": "tests/cartservice.tests.csproj"}, trivy=""),
}[svc]


# inputs of service-ci-<language> (see its `on.workflow_call.inputs`); the keys in CFG["with_"] are passed through unchanged
def with_block(d, indent="      "):
    if not d:
        return ""  # an empty `with:` is a syntax error for actionlint
    out = "    with:\n"
    for k, v in d.items():
        if isinstance(v, tuple):
            out += f"{indent}{k}: {v[0]}\n" + "".join(f"{indent}  {l}\n" for l in v[1])
        else:
            out += f"{indent}{k}: {v}\n"
    return out

ci_inputs = dict(CFG["with_"])
if CFG["lang"] == "none":
    ci_inputs["image"] = "false"  # no image pipeline for this repository
if CFG["trivy"]:
    ci_inputs["trivyignores"] = CFG["trivy"]

# The service-ci-<language> workflows are flat (no nested workflow), so GitHub validates only their own jobs: every caller grants
# contents: read + packages: write (the image job reads the build cache and pushes ghcr.io/<repo>:tree-<hash> for a run that may; a
# fork's token is read-only whatever is asked); only the dotnet file has a job (the test reporter) that needs checks: write.
ci_perms = ("      contents: read\n"
            "      packages: write # the image job pushes ghcr.io/<repo>:tree-<hash> (same-repository runs; a fork's token stays read-only)\n")
if CFG["lang"] == "dotnet":
    ci_perms += "      checks: write # the test reporter publishes a check run\n"

# ── the merge job (runs after `ci`, in the caller: an environment can only be named on a job with steps) ──
# Approving the environment `merge` on the run page is the owner's merge button. Everything below runs only after that click.
MERGE_APPROVAL = r"""
set -euo pipefail
# GitHub creates a missing environment without any protection on its first use, and then this job would run (and merge) without
# anyone clicking. So ask the run itself whether a person approved the environment `merge`.
approved=$(gh api "repos/$GH_REPO/actions/runs/$RUN_ID/approvals" --jq '[.[] | select(.state == "approved") | select(any(.environments[]?; .name == "merge"))] | length')
if [ "$approved" -lt 1 ]; then
  echo "::error::nobody approved the environment 'merge' for this run. Create the environment 'merge' in this repository's settings with a required reviewer."
  exit 1
fi
echo "environment 'merge' approved ($approved approval(s))"
"""

MERGE_SCRIPT = r"""
set -euo pipefail
pr=$(gh pr view "$PR_NUMBER" --json state,headRefOid,baseRefName,title,body)
state=$(jq -r .state <<< "$pr")
head=$(jq -r .headRefOid <<< "$pr")
base_ref=$(jq -r .baseRefName <<< "$pr")
title=$(jq -r .title <<< "$pr")
body=$(jq -r .body <<< "$pr")

if [ "$state" != OPEN ]; then
  echo "::error::pull request #$PR_NUMBER is $state, there is nothing to merge"
  exit 1
fi
if [ "$base_ref" != main ]; then
  echo "::error::pull request #$PR_NUMBER targets $base_ref, not main"
  exit 1
fi
# What was tested is what gets merged: the branch must still be at the commit this run tested.
if [ "$head" != "$TESTED_SHA" ]; then
  echo "::error::the branch moved after the tests ran (tested ${TESTED_SHA:0:7}, the branch is now ${head:0:7}); nothing was merged. The new push started a new run: approve that one."
  exit 1
fi
main_now=$(gh api "repos/$GH_REPO/branches/main" --jq .commit.sha)
if [ "$main_now" != "$TESTED_BASE" ]; then
  echo "::warning::main moved after the tests started (${TESTED_BASE:0:7} -> ${main_now:0:7}); the merged tree may differ from the tested one, so Publish will build the final tree itself."
fi

# --match-head-commit makes GitHub refuse the merge if the branch moves between the check above and the merge itself.
if ! gh pr merge "$PR_NUMBER" --squash --delete-branch --match-head-commit "$TESTED_SHA" --subject "$title (#$PR_NUMBER)" --body "$body"; then
  state=$(gh pr view "$PR_NUMBER" --json state --jq .state)
  if [ "$state" != MERGED ]; then
    echo "::error::the merge was refused (branch protection, a conflict, an unresolved conversation, a moved branch); nothing was merged"
    exit 1
  fi
  echo "::warning::gh reported an error after the merge (probably deleting the branch); the pull request is merged"
fi

# The commit main had BEFORE this merge: Publish diffs before..HEAD, so a documentation-only merge publishes nothing.
merge_sha=""
for attempt in 1 2 3 4 5; do
  merge_sha=$(gh pr view "$PR_NUMBER" --json mergeCommit --jq '.mergeCommit.oid // empty')
  if [ -n "$merge_sha" ]; then break; fi
  sleep $((attempt * 3))
done
before=""
if [ -n "$merge_sha" ]; then
  before=$(gh api "repos/$GH_REPO/commits/$merge_sha" --jq '.parents[0].sha')
fi
for sha in "$merge_sha" "$before"; do
  if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "::error::merged, but the merge commit or its parent could not be read ('$merge_sha', '$before'). Start Publish by hand: Actions, Publish, Run workflow."
    exit 1
  fi
done
{
  echo "before=$before"
  echo "merge=$merge_sha"
} >> "$GITHUB_OUTPUT"
{
  echo "### Merged"
  echo
  echo "Pull request #$PR_NUMBER was squash-merged as \`${merge_sha:0:7}\` (main was at \`${before:0:7}\`)."
} >> "$GITHUB_STEP_SUMMARY"
"""

MERGE_PUBLISH = r"""
set -euo pipefail
# A merge made with the workflow token does not fire the push trigger of Publish (events created by GITHUB_TOKEN start no
# workflows, except workflow_dispatch), so start it explicitly. If the normal GitHub Merge button was used instead, the push
# trigger starts Publish by itself and the concurrency group keeps the two runs from overlapping; the second finds the tree image.
for attempt in 1 2 3; do
  if gh workflow run publish.yaml --ref main -f before="$BEFORE"; then
    echo "Publish started for main (before=${BEFORE:0:7})." >> "$GITHUB_STEP_SUMMARY"
    exit 0
  fi
  sleep $((attempt * 5))
done
echo "::error::the pull request is merged, but Publish could not be started. Start it by hand: Actions, Publish, Run workflow (before = $BEFORE)."
exit 1
"""


def block(text, indent):
    return "\n".join((" " * indent + l) if l else "" for l in text.strip("\n").split("\n"))


MERGE_JOB = f"""
  # The merge button. NOT part of `gate` (`ci / gate` stays the only required check). It waits for the owner to approve the
  # environment `merge` (run page: Review deployments, Approve and deploy), then squash-merges the pull request that was tested
  # and starts Publish. It runs the workflow files of the pull request, so what you approve includes them: read a change to
  # .github/workflows before approving. Fork and Dependabot pull requests are skipped (their token is read-only): use the normal
  # Merge button for those.
  merge:
    name: merge
    needs: ci
    if: ${{{{ github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository && github.actor != 'dependabot[bot]' }}}}
    runs-on: ubuntu-latest
    timeout-minutes: 10
    environment: merge
    permissions:
      contents: write # squash-merge, delete the branch
      pull-requests: write
      actions: write # start Publish (workflow_dispatch); also lets the approval check read the run
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      GH_REPO: ${{{{ github.repository }}}}
      PR_NUMBER: ${{{{ github.event.pull_request.number }}}}
      TESTED_SHA: ${{{{ github.event.pull_request.head.sha }}}}
      TESTED_BASE: ${{{{ github.event.pull_request.base.sha }}}}
      RUN_ID: ${{{{ github.run_id }}}}
    steps:
      - name: Check that the environment was approved
        run: |
{block(MERGE_APPROVAL, 10)}

      - name: Squash-merge the pull request that was tested
        id: merge
        run: |
{block(MERGE_SCRIPT, 10)}
"""
if CFG["lang"] != "none":  # product-catalog-db has no image pipeline, hence no Publish to start
    MERGE_JOB += f"""
      - name: Start Publish
        env:
          BEFORE: ${{{{ steps.merge.outputs.before }}}}
        run: |
{block(MERGE_PUBLISH, 10)}
"""

CI = f"""name: CI

# Pull-request gate: guards (change detection, secrets, workflows) start at t=0; when code changed, lint, build, tests, SCA, SAST
# and an image build + CVE scan run in parallel with them (the image is pushed as ghcr.io/<repo>:tree-<hash> and reused by Publish
# after the merge). The only required status check is `ci / gate`; `merge` comes after it and is the merge button.
on:
  pull_request:
    branches: [main]
  workflow_dispatch:

permissions: {{}}

concurrency:
  group: ci-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  ci:
    permissions:
{ci_perms}    uses: {WF}/service-ci-{CFG['lang']}.yaml@{sha} # {ver}
{with_block(ci_inputs)}{MERGE_JOB}"""

pub_inputs = {"app": svc}
if CFG["trivy"]:
    pub_inputs["trivyignores"] = CFG["trivy"]
pub_inputs["before"] = "${{ inputs.before }}"

PUB = f"""name: Publish

# A merge to main publishes the image ONCE (the image the pull request built is reused when main's tree is the same; Trivy, Docker
# Hub as sha-<commit>, cosign, SBOM) and opens the pull request in platform-demo-helm that moves `dev` to the new digest.
# No tags, no rebuilds. The `merge` job of ci.yaml starts this workflow by hand-dispatch (a merge made with the workflow token
# fires no push trigger) and passes `before` = main's commit before the merge, so a documentation-only merge publishes nothing.
# Started by hand without `before`, it publishes.
on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      before:
        description: Commit main was at before the change (empty = publish; set = a documentation-only change since then publishes nothing)
        type: string
        required: false
        default: ""

permissions: {{}}

concurrency:
  group: publish-${{{{ github.ref }}}}
  cancel-in-progress: false

jobs:
  publish:
    permissions:
      contents: read
      packages: write
      id-token: write # keyless cosign signing
    uses: {WF}/service-publish.yaml@{sha} # {ver}
{with_block(pub_inputs)}    secrets:
      DOCKERHUB_USERNAME: ${{{{ secrets.DOCKERHUB_USERNAME }}}}
      DOCKERHUB_TOKEN: ${{{{ secrets.DOCKERHUB_TOKEN }}}}
      PROMOTE_APP_ID: ${{{{ secrets.PROMOTE_APP_ID }}}}
      PROMOTE_APP_PRIVATE_KEY: ${{{{ secrets.PROMOTE_APP_PRIVATE_KEY }}}}
"""

d = os.path.join(repo_dir, ".github", "workflows")
os.makedirs(d, exist_ok=True)
for old in ("ci-feature.yaml", "ci-main.yaml", "release.yaml"):
    p = os.path.join(d, old)
    if os.path.exists(p): os.remove(p)
open(os.path.join(d, "ci.yaml"), "w").write(CI)
if CFG["lang"] != "none":  # product-catalog-db has no image pipeline: nothing to publish
    open(os.path.join(d, "publish.yaml"), "w").write(PUB)
elif os.path.exists(os.path.join(d, "publish.yaml")):
    os.remove(os.path.join(d, "publish.yaml"))
print("wrote callers for", svc)
