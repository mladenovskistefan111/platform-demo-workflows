# Changing a workflow

1. Branch, edit (`tools/gen-lang-workflows.py` generates the node/python/java/dotnet files, `tools/gen-service-ci.py` the `service-ci-*` files from them - edit the source or the generator, run both, in that order; `tools/test-gate.py` runs the gate scenarios),
   run `actionlint` **with shellcheck installed** (CI has it; without it you miss real findings).
2. Open a PR here (its CI is `workflow-lint`). To prove the change, open **throw-away PRs** in one repo per language whose
   `ci.yaml` pins the branch's commit SHA (`tools/gen-callers.py <repo-dir> <service> <sha> "test"`), wait for green, close them.
   The publish path can only be exercised from `main`: after merging and tagging, roll out and run `Release` with `publish=true`
   on one service.
3. Merge (squash), tag `vX.Y.Z` on the merge commit (`git tag -a ... && git push origin vX.Y.Z`; tags are immutable - a mistake
   means a new patch tag), then roll the new SHA out: `tools/gen-callers.py` per service (it removes the old `ci-*.yaml`), a
   `sed` for the repos with hand-written callers, one PR per repo (CI re-runs), merge when green.
4. Breaking input/secret changes get a major version.

Tools (all need the GitHub CLI logged in): `tools/bump-actions.py` (move every pin to the newest release older than N days,
`--hold gradle/actions=v5`), `tools/gen-lang-workflows.py`, `tools/gen-callers.py`, `tools/gen-guardrails.py` (CODEOWNERS,
Dependabot, `.pre-commit-config.yaml` for a repo), `tools/transfer-repo.sh` (move a repo to the org and re-apply what a transfer resets), `tools/required-checks.sh` + `tools/protect-repo.sh` (branch protection with the
required checks taken from a repo's last green PR, squash-only merges, secret scanning, tag ruleset).
