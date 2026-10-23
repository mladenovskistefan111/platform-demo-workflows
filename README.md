# platform-demo-workflows

Reusable GitHub Actions workflows shared by the platform-demo repositories. Services call these instead of carrying their own
copy of a 300-line pipeline, so a fix or a version bump lands once. Current release: **v3.4.0** (history below). Callers pin the release they use by commit SHA: the developer portal calls v3.0.3; the service repositories call `service-ci-<language>`/`service-publish` (v3.4.0 once rolled out; until then v3.3.0, the pilot still v3.2.1) or, until they move, v2.0.2.

| Workflow | Purpose | Used by |
|---|---|---|
| [`go-ci.yaml`](.github/workflows/go-ci.yaml) | stand-alone, for repositories not on the service pipeline (older pins, portal): lint/format, secret scan, build, unit tests, optional E2E and gRPC smoke test, then SCA (govulncheck) and SAST (gosec) | checkout, shipping, product-catalog, frontend |
| [`node-ci.yaml`](.github/workflows/node-ci.yaml) | format/typecheck/lint, secret scan, build, tests, `npm audit`, Semgrep, optional gRPC smoke test | currency, payment |
| [`python-ci.yaml`](.github/workflows/python-ci.yaml) | ruff, secret scan, entry-point parse check, pytest, pip-audit, bandit | email, recommendation |
| [`java-gradle-ci.yaml`](.github/workflows/java-gradle-ci.yaml) | compile, secret scan, build, tests, Trivy fs scan, Semgrep | ad |
| [`dotnet-ci.yaml`](.github/workflows/dotnet-ci.yaml) | `dotnet format` + analyzers, secret scan, build, tests, vulnerable-package audit, Semgrep | cart |
| [`docker-build.yaml`](.github/workflows/docker-build.yaml) | build once, stage on ghcr.io, Trivy-scan that exact digest (HIGH/CRITICAL fail) | callers pinned to v3.2.x or older; `service-publish` no longer calls it (its steps are inline) |
| [`docker-promote.yaml`](.github/workflows/docker-promote.yaml) | copy the scanned digest to Docker Hub (no rebuild), tag, cosign-sign, self-verify, attach the SBOM as a signed `spdxjson` attestation, self-verify; refuses commits not on `main` | callers pinned to v3.2.x or older; `service-publish` has these steps inline (the copy now fails hard on a digest mismatch) |
| [`secret-scan.yaml`](.github/workflows/secret-scan.yaml) | stand-alone gitleaks scan (the pull request's commits; full history on manual runs); also the source of the `Guard: Secrets` job of the service pipeline | helm, observability |
| [`workflow-lint.yaml`](.github/workflows/workflow-lint.yaml) | stand-alone actionlint (+ shellcheck on CI runners) and "every `uses:` is pinned to a full commit SHA"; also the source of `Guard: Workflows` | repositories not on the service pipeline (this one included) |
| [`service-ci-go.yaml`](.github/workflows/service-ci-go.yaml), [`-node`](.github/workflows/service-ci-node.yaml), [`-python`](.github/workflows/service-ci-python.yaml), [`-java`](.github/workflows/service-ci-java.yaml), [`-dotnet`](.github/workflows/service-ci-dotnet.yaml), [`-none`](.github/workflows/service-ci-none.yaml) | **generated** (tools/gen-service-ci.py), flat pull-request pipeline of an app repository, one file per language, only that language's jobs: guards (change detection, secrets, workflows), lint, build (+ optional smoke test), tests (+ optional E2E), SCA, SAST, image build (pushed as `ghcr.io/<repo>:tree-<hash>` by same-repository runs, reused by the publish) + Trivy scan of that digest, one `gate`. `none` = guards + image scan + gate (developer portal; product-catalog-db with `image: false`) | every app repository |
| [`service-publish.yaml`](.github/workflows/service-publish.yaml) | **generated** (tools/gen-service-publish.py), ONE flat workflow, merge to `main`: find the image of this source tree (built by the pull request) or build it, scan the digest again, copy it to Docker Hub as `sha-<7>`, sign, verify (a digest mismatch fails the run); then in parallel the SBOM, the dev promotion PR and the staging prune. Graph below | every app repository |
| [`service-merge.yaml`](.github/workflows/service-merge.yaml) | the merge button, called by the `merge` job of every app repository's `ci.yaml` after `ci / gate`: waits for the approval of the environment `merge`, checks the approval and that the branch is still the tested commit, squash-merges, starts `Publish` with `before` | every app repository |
| [`release.yaml`](.github/workflows/release.yaml) | the tag button: Actions, Release, Run workflow, `vX.Y.Z`: creates the annotated tag on `main` and prints the commit SHA that callers pin | this repository |
| [`changed-paths.yaml`](.github/workflows/changed-paths.yaml) | tells whether a change touches code or only docs/metadata (plain git, no token); `service-ci-*` carry an identical inline copy (`Guard: Changes`), `service-publish` one with the extra `before` input | older callers |
| [`gitops-promote.yaml`](.github/workflows/gitops-promote.yaml) | changes only `image.tag` and `image.digest` of one app in one environment of platform-demo-helm through a pull request (GitHub App token scoped to the helm repo, branch `promote/<app>/<env>` force-updated so a newer build replaces an older open one, squash auto-merge); idempotent | app publish workflows (dev), helm `Promote` (stage, prod) |
| [`gitops-rollback.yaml`](.github/workflows/gitops-rollback.yaml) | moves an app back or forward to a digest that was deployed before (`previous`, `latest`, a digest or a helm commit) through a pull request (`rollback/<app>/<env>`, label `rollback`) that passes the same gates | helm `Rollback` |
| [`scheduled-image-scan.yaml`](.github/workflows/scheduled-image-scan.yaml) | weekly Trivy scan of every deployed image digest (Monday 08:00 UTC); SARIF to the Security tab and one updated issue "Scheduled image scan" | helm `Scheduled scans` |
| [`scheduled-code-scan.yaml`](.github/workflows/scheduled-code-scan.yaml) | weekly SCA and SAST of the default branch of the services, the portal and the plugins (own cron, Monday 08:00 UTC); one updated issue "Scheduled code scan" | this repository |
| [`ghcr-prune.yaml`](.github/workflows/ghcr-prune.yaml) | keeps the newest N `sha-*` versions of a ghcr.io staging package, protects image-index children, `dry-run` supported; `service-publish` carries an inline copy that also keeps the newest N `tree-*` versions (separate N) | callers pinned to v3.2.x or older |
| [`terraform-ci.yaml`](.github/workflows/terraform-ci.yaml) | `fmt` + `validate`, TFLint, Trivy config scan, Checkov, Infracost monthly estimate (needs a free `INFRACOST_API_KEY`), `terraform plan` as a PR comment (no refresh, never touches the running platform) | infra-terraform |
| [`terraform-plan.yaml`](.github/workflows/terraform-plan.yaml) | plans a root module (or its destruction), saves the plan encrypted in the state bucket (7 days), shows the reason and the changes on the run page; changes nothing | infra-terraform |
| [`terraform-apply.yaml`](.github/workflows/terraform-apply.yaml) | **Review** job (checks the saved plan belongs to this commit, is recent and unaltered; shows reason, an "at a glance" summary computed by script with anything destroyed or replaced first, and the plan), then an **Apply** job in a GitHub environment that waits for a reviewer and applies exactly the reviewed plan. Also runs destroy plans, with optional before/after scripts | infra-terraform |

The **stand-alone** `go-ci`, `node-ci`, `python-ci`, `java-gradle-ci`, `dotnet-ci`, `secret-scan` and `workflow-lint` are for repositories
that are not on the service pipeline (callers pinned to an older release, the developer portal). App repositories use `service-ci-<language>`.
All six `service-ci-*` files have the same job names (`Guard: Changes`, `Guard: Secrets`, `Guard: Workflows`, `Check: Lint`, `Check: Build`,
`Check: Tests`, `Security: SCA`, `Security: SAST`, `Image: Build + CVE scan`, `gate`), so branch protection looks the same everywhere. In a
caller the check names are `<caller job id> / <job name>`, e.g. `ci / Check: Build`; the required one is `ci / gate`.

## How a service calls them

Two small files per app repository (generated by `tools/gen-callers.py`): `ci.yaml` (jobs `ci` and `merge`, runs on pull requests) and
`publish.yaml` (job `publish`, runs on a push to `main` and by hand). There are no tags and no `release.yaml` in an app repository:
everything deployed comes from `main`.

```yaml
# .github/workflows/ci.yaml (shape)
on: { pull_request: { branches: [main] }, workflow_dispatch: }
permissions: {}
concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }
jobs:
  ci: { permissions: { contents: read, packages: write }, uses: .../service-ci-go.yaml@<sha> # v3.4.0, with: { test-packages: ..., ... } }
  # service-ci-<go|node|python|java|dotnet|none>.yaml; only dotnet also needs checks: write (test reporter); product-catalog-db: -none with image: false
  merge:                                  # the merge button: NOT part of `gate`, after it; the logic is service-merge.yaml
    needs: ci
    if: pull_request, same repository, not Dependabot
    permissions: { contents: write, pull-requests: write, actions: write }
    uses: .../service-merge.yaml@<sha> # v3.4.0   (product-catalog-db: with: { start-publish: false })
# .github/workflows/publish.yaml (shape)
on: { push: { branches: [main] }, workflow_dispatch: { inputs: { before: {type: string, default: ""} } } }
concurrency: { group: publish-${{ github.ref }}, cancel-in-progress: false }
jobs:
  publish: { permissions: { contents: read, packages: write, id-token: write }, uses: .../service-publish.yaml@<sha> # v3.4.0,
             with: { app: checkout-service, before: "${{ inputs.before }}" }, secrets: DOCKERHUB_USERNAME / DOCKERHUB_TOKEN / PROMOTE_APP_ID / PROMOTE_APP_PRIVATE_KEY }
```

**`service-ci-<language>`** (pull request): flat, one file per language, so the Actions graph shows only what the repository runs. Left to
right: the guards `Guard: Changes`, `Guard: Secrets`, `Guard: Workflows`; next to them `Check: Lint`, `Check: Build`, `Check: Tests`,
`Security: SCA`, `Security: SAST` and `Image: Build + CVE scan`; at the end one `gate` job:

```
Guard: Changes ---+--> Check: Lint | Check: Build | Check: Tests | Security: SCA | Security: SAST | Image: Build + CVE scan --+
Guard: Secrets  --+                                                                                                          +--> gate --> merge
Guard: Workflows -+------------------------------------------------------------------------------------------------------+      (approval)
```

**The left column means "starts at t=0", not "must pass first"**: all nine jobs start together, the checks wait only for the change
detection (a docs-only change skips them), so a flaky gitleaks download or one lint complaint does not hide the test, SAST and Trivy
results. `gate` needs every job, always runs, lists the failed or cancelled jobs first (their consequent skips are not noise) and is strict
about the rest: **`ci / gate` is the only required check**; `merge` is after it and is not part of it. The optional smoke test (Go, Node:
input `smoke-port`) is a step of `Check: Build`, reusing the build; the optional E2E tests (Go: `e2e-packages`) a step of `Check: Tests`;
there are no optional jobs that would show up as skipped.
The image job builds the image once per source tree and, for a same-repository run, pushes it as `ghcr.io/<repo>:tree-<hash>` and scans that
digest (see "Build once per tree" below); a fork's or Dependabot's run cannot push and builds into the local daemon as before.
The files are generated from the stand-alone workflows by `tools/gen-service-ci.py` (job bodies extracted, not retyped; the image steps
come from `tools/wfgen.py`, shared with the publish) and `tools/check-consistency.py` fails when they drift. Nothing is nested, so GitHub
validates only the file's own jobs: the caller grants `contents: read` and `packages: write` (and `checks: write` for dotnet alone), and there
is no placeholder pin or two-step release.

**`service-publish`** (merge to `main`, or `merge`/manual `workflow_dispatch`): ONE flat generated workflow
(`tools/gen-service-publish.py`; the only nested workflow is the unchanged `gitops-promote.yaml@341f641`, shared with the helm repository):

```
Guard: Changes -> Image: Find or build -> Image: Scan -> Publish: Copy, sign, verify -+-> Publish: SBOM
                                                                                      +-> Deploy: Open dev PR   (gitops-promote.yaml)
                                                                                      +-> Cleanup: Prune staging (best effort)
```

| Job | What it does |
|---|---|
| `Guard: Changes` | docs/metadata-only commit: nothing below runs. Push: diff `github.event.before..HEAD`. Manual run: with `before` it diffs `before..HEAD` the same way, without it everything runs |
| `Image: Find or build` | `TREE=$(git rev-parse HEAD^{tree})`; looks for `ghcr.io/<repo>:tree-<TREE>`. Found = its digest is reused, no build. Not found (main moved, the tree differs, the image was pruned) = built now, registry cache written, pushed under the same tag |
| `Image: Scan` | Trivy (HIGH/CRITICAL fail) on that digest, again: new CVEs may have appeared since the pull request |
| `Publish: Copy, sign, verify` | refuses commits not reachable from `main`; copies the digest to Docker Hub as `sha-<7 chars of the commit>` only (no `latest`, no semver), never moves an existing tag, **fails hard if the Docker Hub digest differs from the scanned one**, cosign signs (keyless) and verifies. Job summary: tree, reused or built, digest, tag, signature, link to the helm pull requests |
| `Publish: SBOM` | SPDX SBOM as a signed attestation, verified. Own job, so it is not on the way to the helm pull request; a failure fails the run visibly |
| `Deploy: Open dev PR` | the pull request in the helm repository that moves `dev` to this digest (`permissions: {}`, App token) |
| `Cleanup: Prune staging` | `continue-on-error: true`, never delays the helm pull request: keeps the newest `prune-keep` (10) `sha-*` and the newest `prune-keep-tree` (20) `tree-*` versions of the ghcr.io package, never the `buildcache` package, never an image-index child of a kept image |

Concurrency (`publish-<ref>`, no cancel) lives in the caller: one publish at a time. **A run that waits behind a running one is replaced by
a newer waiting one**, so of three quick merges the second never publishes by itself; the third publishes the tree of `main`, which contains
it. Callers always pin to a **commit SHA** with the version in a trailing comment. A reusable workflow can never get more permissions than
its caller grants, so each job lists what it needs (`id-token: write` only on the jobs that sign). Secrets: `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN` (per service repository), `PROMOTE_APP_ID` and `PROMOTE_APP_PRIVATE_KEY` (organisation secrets, GitHub App scoped to
platform-demo-helm). Semgrep needs no token.

### Build once per tree

The image is named after the content it was built from, not after a commit: `ghcr.io/<repo>:tree-<git tree hash of the checked-out commit>`.
The pull request's merge commit and the squash commit that lands on `main` are different commits with the **same tree** when `main` did not
move in between, so the image the pull request built and scanned is the image the publish ships. Nothing is built twice and what was
tested is what ships. If `main` did move (another merge in between), the tree differs, the lookup finds nothing and the publish builds the
final tree itself; the same happens when the PR image was pruned or the pull request came from a fork or from Dependabot (their token cannot
push). Only the Docker Hub copy, the signature, the SBOM and the helm pull request are new work after the merge; the scan is repeated on
purpose. Details, limits and the trust boundary: [docs/build-once.md](docs/build-once.md). The registry build cache is refreshed by the publish when it builds and by every pull-request run that pushes (same repository, not Dependabot); forks and Dependabot only read it.

### The merge button

After `ci / gate` the `merge` job of `ci.yaml` (a short `uses:` job, the logic is `service-merge.yaml`, defined once) waits in the GitHub
environment `merge` (required reviewer: the owner). On the run page: **Review deployments, Approve and deploy**; the job then checks that
the run was approved, that the branch still is the tested commit, squash-merges the pull request
(`gh pr merge --squash --delete-branch`, title and body from the pull request) with the workflow token and starts `Publish`
(`gh workflow run publish.yaml --ref main -f before=<main before the merge>`). A merge made with the workflow token does not fire the push
trigger, hence the explicit start; the normal GitHub Merge button still works and starts Publish by push, and the concurrency group keeps
the two apart (a second run finds the tree image). The caller file is the pull request's own: **read a change to `.github/workflows`
before you approve it.** Fork and Dependabot pull requests have no `merge` job (read-only token): use the normal button.

## Release model (trunk-based)

- `main` is the trunk; work happens on short-lived `feat/*`, `fix/*`, `docs/*`, `chore/*` branches merged by squash PR once `ci / gate` is green. The merge is the `merge` job of the pull request's `CI` run (approve the environment `merge` on the run page) or the normal GitHub Merge button.
- **One artifact per source tree.** The pull request builds and scans the image (`ghcr.io/<repo>:tree-<hash>`); the merge to `main` reuses it
  when `main`'s tree is the same (builds it once when not), scans the digest again, publishes it to Docker Hub, signs it and opens the
  promotion PR for `dev` (`service-publish`). The same digest then moves forward and is never rebuilt.
- **Environments are directories** in `platform-demo-helm` (`envs/dev|stage|prod`), Argo CD watches `main`. `dev` is promoted
  automatically (PR with auto-merge after the helm gates). `stage` and `prod` are promoted by a click (`Promote` workflow in the helm
  repo): stage takes the digest that is in dev, prod the one that is in stage (the `Promotion policy` check enforces it), and prod waits
  for the `prod` environment's reviewer. The stage version `X.Y.Z` is typed at the click and recorded in the helm repo as `<app>-vX.Y.Z`.
- **Rollback** is a pull request too (`Rollback` workflow, `gitops-rollback.yaml`) back or forward to a digest that was deployed before;
  it passes the same gates.

## Dependabot (quiet by design)

`tools/gen-guardrails.py <repo> --dependabot-only` writes each repository's `.github/dependabot.yml`:

- **at most one pull request per repository per month**: a single multi-ecosystem group (language packages, Docker, Actions together), schedule monthly, 7-day cooldown;
- **major versions are ignored** everywhere, and Docker base images (Go, Node, Python, Temurin, ...) ignore **minor** versions as well: a toolchain or major upgrade is a deliberate decision (a card on the project board), not a bot PR;
- the rules are **in the file**, not in Dependabot's per-repository memory (`@dependabot ignore ...`), which a repository transfer wipes (that is what flooded the organisation with ~25 PRs after the move);
- Dependabot **security** alerts and security updates are a separate mechanism, enabled per repository by `tools/transfer-repo.sh` / `tools/protect-repo.sh`.

To take a major upgrade deliberately: raise it by hand (or temporarily remove the ignore), test, merge.

## Changing a workflow (the procedure that worked)

1. Branch, edit (`tools/gen-lang-workflows.py` generates the node/python/java/dotnet files, `tools/gen-service-ci.py` the `service-ci-*` files from them, `tools/gen-service-publish.py` `service-publish.yaml` - edit the source or the generator, run them in that order),
   run `actionlint` **with shellcheck installed** (CI has it; without it you miss real findings).
2. Open a PR here (its CI is `workflow-lint`). To prove the change, open **throw-away PRs** in one repo per language whose
   `ci.yaml` pins the branch's commit SHA (`tools/gen-callers.py <repo-dir> <service> <sha> "test"`), wait for green, close them.
   The publish path can only be exercised from `main`: after merging and tagging, roll out to one service and merge a code change there.
3. Merge (squash), then **tag with the button**: Actions, Release, Run workflow, type `vX.Y.Z` (`release.yaml` makes the annotated tag on
   `main` and prints the commit SHA that callers pin; tags are immutable - a mistake means a new patch tag), then roll the new SHA out: `tools/gen-callers.py` per service (it removes the old `ci-*.yaml`), a
   `sed` for the repos with hand-written callers, one PR per repo (CI re-runs), merge when green.
4. Breaking input/secret changes get a major version.

Tools (all need the GitHub CLI logged in): `tools/bump-actions.py` (move every pin to the newest release older than N days,
`--hold gradle/actions=v5`), `tools/gen-lang-workflows.py`, `tools/gen-service-ci.py`, `tools/gen-service-publish.py` (with `tools/wfgen.py`, the helpers and image steps both share), `tools/test-gate.py` (gate scenarios), `tools/test-shell.py` (the shell of the image lookup, the Docker Hub copy, `before`, the prune, the merge job and the tag button against stubs and real git repositories), `tools/check-consistency.py`, `tools/check-permissions.py`, `tools/gen-callers.py`, `tools/gen-guardrails.py` (CODEOWNERS,
Dependabot, `.pre-commit-config.yaml` for a repo), `tools/transfer-repo.sh` (move a repo to the org and re-apply what a transfer resets), `tools/required-checks.sh` + `tools/protect-repo.sh` (branch protection with the
required checks taken from a repo's last green PR, squash-only merges, secret scanning, tag ruleset).

## Things to know

- **Docker Hub has no OCI referrers API.** cosign 3 defaults to the new bundle format stored as a referrer: `cosign sign` succeeds
  and `cosign verify` then finds nothing. `docker-promote` and, since v3.4.0, `service-publish` therefore sign/attest/verify with
  `--new-bundle-format=false --use-signing-config=false` (classic `sha256-<digest>.sig` tags), which Kyverno and most verifiers
  understand. The keyless identity of an image signed through a reusable workflow is
  `https://github.com/platform-boutique/platform-demo-workflows/.github/workflows/service-publish.yaml@<sha-or-ref>` since v3.4.0 (`docker-promote.yaml` before; images signed before the move to the organisation: `https://github.com/mladenovskistefan111/platform-demo-workflows/...`; both are accepted); images
  signed by the older per-repo pipelines carry `.../<service>/.github/workflows/ci-main.yaml@refs/heads/...`.
- **Staging on ghcr.io, publishing by digest:** what Trivy scanned is byte-for-byte what is published and signed; since v3.4.0 a Docker Hub digest that differs from the scanned one fails the run.
- **A job output that contains a secret is dropped by GitHub.** The Docker Hub image reference contains the account name (a secret), so `Publish: Copy, sign, verify` outputs only the digest and the tag; the SBOM job rebuilds the reference.
- **A missing GitHub environment is created without protection on first use.** The `merge` job therefore asks the run for an approval of the environment `merge` and refuses to merge without one.
- **`gradle/actions` is held at v5**: v6 puts its caching component under proprietary terms; nobody accepted them.
- **Pins use a 7-day cooldown** (a brand-new release is a supply-chain risk). Dependabot is configured with majors as separate PRs.
- Newer **bandit** (1.9) flags `random.sample` (B311); recommendation-service uses `random.SystemRandom().sample`.
- GitHub's runner image has shellcheck; a local `actionlint` without it passes things CI later rejects.

## Version history

| Tag | Change |
|---|---|
| v1.0.0 | Go CI, Docker build/scan, Docker promote/sign, workflow lint |
| v1.1.0 | Node, Python, Java/Gradle, .NET CI; shellcheck fixes |
| v1.2.0 | stand-alone secret-scan |
| v2.0.0 | all actions to their newest settled release (Node 24), cosign 3, SBOM as attestation, Semgrep via pip (breaking: callers no longer pass `SEMGREP_APP_TOKEN`) |
| v2.0.1 | classic cosign signature format (Docker Hub has no referrers API) |
| v2.0.2 | secret scan runs the open-source **gitleaks CLI** (pinned, SHA-256 checked) instead of `gitleaks-action`, whose license is paid for organisation-owned repositories; the cosign self-verification accepts the workflows repo under both owners (`mladenovskistefan111`, `platform-boutique`) |
| v2.1.0 | Terraform pipeline: `terraform-ci` (fmt, validate, TFLint, Trivy, Checkov, Infracost, plan comment), `terraform-apply`, `terraform-destroy` |
| v3.0.0 | Terraform pipeline rebuilt (breaking): `terraform-plan` + `terraform-apply` (computed at-a-glance summary) apply the exact saved plan after approval; `terraform-destroy` removed (a destroy is a plan with `destroy: true`); AWS access through GitHub OIDC roles only, no stored keys, no runner-address handling |
| v3.0.1 | `terraform-ci`: the plan and cost jobs read their variables correctly (a stray `}` broke them) |
| v3.0.2 | `terraform-ci`: variables are shell-quoted when loaded for the plan and cost jobs (a JSON list lost its double quotes) |
| v3.0.3 | Trivy is set to v0.76.1 explicitly in the three scan steps (`docker-build`, `java-gradle-ci`, `terraform-ci`) instead of the action's older default (0.70.0), so it is bumped in one reviewed place |
| v3.1.0 | every job has a `timeout-minutes`; the language workflows run all their jobs in parallel; one generated secret-scan implementation (shallow, scans the pull request's commits); `changed-paths.yaml` (docs-only change filter); registry build cache on ghcr.io instead of `type=gha`; `docker-promote` gets `sha-only`, outputs and retries on the cosign lookups; new `gitops-promote`, `gitops-rollback`, `scheduled-image-scan`, `scheduled-code-scan`, `ghcr-prune` and `tools/check-repo-settings.sh` (read-only settings drift check) |
| v3.2.0 | `service-ci` and `service-publish`: one pipeline for every app repository (pull request = verify, merge to `main` = publish once and open the dev promotion PR; no tags, no `release.yaml`); callers are two generated files (`ci.yaml`, `publish.yaml`); `ci / gate` is the single required check |
| v3.2.1 | fix: `service-ci` / `service-publish` failed at startup (`startup_failure`) because GitHub checks the permissions of every nested job, skipped ones too: generated `ci.yaml` callers now grant `checks: write` (the dotnet job), the prune job of `service-publish` gets `contents: read`; new `tools/check-permissions.py` (run in this repository's CI) walks the nested workflows against the callers' grants |
| v3.3.0 | `service-ci.yaml` is replaced by flat, generated, per-language pull-request workflows `service-ci-go`, `-node`, `-python`, `-java`, `-dotnet` and `-none` (tools/gen-service-ci.py). The Actions graph of a caller showed every language behind `if:` as hollow skipped boxes (and a duplicate Secret Scan, skipped E2E/smoke jobs, truncated names); now every file contains only the jobs its language runs, with no nested workflow, short names (`ci / Check: Build`) and the same shape everywhere: `Guard: Changes`, `Guard: Secrets`, `Guard: Workflows` start at t=0, the checks (`Check: Lint/Build/Tests`, `Security: SCA/SAST`, `Image: Build + CVE scan`) need only the change detection, `gate` waits for all and names the failed job first. The optional smoke test (Go, Node) is a step of `Check: Build` and the E2E tests (Go) a step of `Check: Tests` (no skipped jobs, one build). Because nothing is nested, a caller needs only `contents: read` + `packages: read` (`checks: write` for dotnet only) and there is no placeholder pin / two-step release (`tools/pin-service-workflows.sh` is gone). Callers: new `uses:` file name, no `language:` input, same `with:` otherwise; the required check stays `ci / gate`. `service-ci.yaml` is removed (v3.2.1 stays valid for callers pinned to it) |
| v3.4.0 | **Build once per source tree, flat publish, merge and tag buttons.** The pull request's `Image: Build + CVE scan` now pushes the image as `ghcr.io/<repo>:tree-<git tree hash>` for same-repository runs (forks and Dependabot build locally as before) and scans that digest; the publish after the merge looks for the image of the main commit's tree and ships it without building (found), or builds it once and pushes it under the same tag (not found: main moved, tree differs, image pruned), then scans the digest again. Why: the image was built twice (pull request, then publish) and what was tested was not literally what shipped. `service-publish.yaml` is one flat generated workflow (`tools/gen-service-publish.py`) instead of calling `docker-build`, `docker-promote` and `ghcr-prune` (no nested publish boxes in the graph, no two-step release): `Guard: Changes`, `Image: Find or build`, `Image: Scan`, `Publish: Copy, sign, verify`, then in parallel `Publish: SBOM`, `Deploy: Open dev PR` and `Cleanup: Prune staging` (best effort, keeps the newest N `sha-*` and `tree-*` versions). The Docker Hub copy fails hard when its digest differs from the scanned one and never moves an existing `sha-<7>` tag; the SBOM attestation is its own job, off the way to the helm pull request. New input `before` (a manual run diffs `before..HEAD`, so a docs-only merge publishes nothing); output `image` is dropped (it carried the secret account name). Callers: `ci.yaml` gets a short `merge` job that calls the new `service-merge.yaml` (environment `merge`, owner approves, squash-merge, starts Publish), the ci grant becomes `contents: read` + `packages: write`, `publish.yaml` accepts and passes `before`. The registry build cache is also refreshed by pull-request runs that push; `prune-keep` (10, `sha-*`) and new `prune-keep-tree` (20, `tree-*`) are separate. New `release.yaml` (Actions, Release, Run workflow: creates the tag, prints the SHA to pin). New `tools/wfgen.py`, `tools/gen-service-publish.py`, `tools/test-shell.py`; `check-consistency.py` and `check-permissions.py` cover the flat publish, the merge job and the shared image steps. The cosign identity of new images is `service-publish.yaml@<sha>` (every verifier accepts any workflow of the workflows repository). `docker-build`, `docker-promote`, `ghcr-prune`, `changed-paths` stay for older callers |

## The Terraform workflows

- **Credentials:** GitHub OIDC only. The caller passes the ARN of a read-only role for plans and reviews, and of an apply role for the
  Apply job; nothing secret is stored. The apply role's trust policy should admit only the GitHub environment the Apply job runs in.
- **Plan, then apply that plan:** `terraform-plan` saves the plan in the state bucket under `plans/<stack>/<plan id>/` (not as an artifact:
  artifacts of a public repository are public, and a plan file can hold secret values). `terraform-apply` is started with the plan id.
  Its Review job refuses a plan that is for another directory, another commit, older than `max_plan_age_hours`, of the wrong kind
  (`destroy` must match), or whose checksum differs; it shows the reason and the plan on the run page. The Apply job waits for the
  environment's reviewers, re-checks the checksum, applies, and deletes the plan. Terraform itself rejects a plan whose state changed.
- **Destroy** is a plan with `destroy: true` applied with `destroy: true`; `pre_apply_script` and `post_apply_script` (paths in the calling
  repository) empty the cluster before and list leftovers after. A deployment can never apply a destroy plan, nor the reverse.
- **Accepted risks** are written down next to the scanners' configuration in the calling repository (`.trivyignore.yaml`, `.checkov.yaml`,
  each entry with its reason); a scan that fails is never silenced without one.
- **The pull-request plan** does not refresh, so it never touches the running platform.
- **Cost estimate:** Infracost shows on-demand prices and no usage-based items (NAT data, load balancer traffic, logs).
