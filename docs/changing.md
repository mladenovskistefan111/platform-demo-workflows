# Changing a workflow

1. Branch, edit (`tools/gen-lang-workflows.py` generates the node/python/java/dotnet files, `tools/gen-service-ci.py` the `service-ci-*` files from them, `tools/gen-service-publish.py` `service-publish.yaml`; `service-merge.yaml` and `release.yaml` are written by hand - edit the source or the generator, run them in that order; `tools/test-gate.py` runs the gate scenarios, `tools/test-shell.py` the shell of the image lookup, the Docker Hub copy, `before`, the prune, the merge job and the tag button),
   run `actionlint` **with shellcheck installed** (CI has it; without it you miss real findings).
2. Open a PR here (its CI is `workflow-lint`). To prove the change, open **throw-away PRs** in one repo per language whose
   `ci.yaml` pins the branch's commit SHA (`tools/gen-callers.py <repo-dir> <service> <sha> "test"`), wait for green, close them.
   The publish path can only be exercised from `main`: after merging and tagging, roll out to one service and merge a code change there.
3. Merge (squash), then **tag with the button**: Actions, Release, Run workflow, type `vX.Y.Z` (`release.yaml` makes the annotated tag on
   `main` and prints the commit SHA that callers pin; tags are immutable - a mistake means a new patch tag), then roll the new SHA out: `tools/gen-callers.py` per service (it removes the old `ci-*.yaml`), a
   `sed` for the repos with hand-written callers, one PR per repo (CI re-runs), merge when green.
4. Breaking input/secret changes get a major version.

Tools (all need the GitHub CLI logged in): `tools/bump-actions.py` (move every pin to the newest release older than N days,
`--hold gradle/actions=v5`), `tools/gen-lang-workflows.py`, `tools/gen-callers.py`, `tools/gen-guardrails.py` (CODEOWNERS,
Dependabot, `.pre-commit-config.yaml` for a repo), `tools/transfer-repo.sh` (move a repo to the org and re-apply what a transfer resets), `tools/required-checks.sh` + `tools/protect-repo.sh` (branch protection with the
required checks taken from a repo's last green PR, squash-only merges, secret scanning, tag ruleset).
