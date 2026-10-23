# platform-demo-workflows

Reusable GitHub Actions workflows shared by the platform-demo repositories. Services call these instead of carrying their own
copy of a 300-line pipeline, so a fix or a version bump lands once. Current release: **v3.6.0** (history below). Callers pin the release they use by commit SHA: the developer portal calls v3.0.3; the service repositories call `service-ci-<language>`/`service-publish` (v3.6.0 once rolled out; until then the release each repository pins: v3.5.0, v3.4.0, v3.3.0, the pilot still v3.2.1) or, until they move, v2.0.2.

| Workflow | Purpose | Used by |
|---|---|---|
| [`go-ci.yaml`](.github/workflows/go-ci.yaml) | stand-alone, for repositories not on the service pipeline (older pins, portal): lint/format, secret scan, build, unit tests, optional E2E and gRPC smoke test, then SCA (govulncheck) and SAST (gosec) | checkout, shipping, product-catalog, frontend |
| [`node-ci.yaml`](.github/workflows/node-ci.yaml) | format/typecheck/lint, secret scan, build, tests, `npm audit`, Semgrep, optional gRPC smoke test | currency, payment |
| [`python-ci.yaml`](.github/workflows/python-ci.yaml) | ruff, secret scan, entry-point parse check, pytest, pip-audit, bandit | email, recommendation |
| [`java-gradle-ci.yaml`](.github/workflows/java-gradle-ci.yaml) | compile, secret scan, build, tests, Trivy fs scan, Semgrep | ad |
| [`dotnet-ci.yaml`](.github/workflows/dotnet-ci.yaml) | `dotnet format` + analyzers, secret scan, build, tests, vulnerable-package audit, Semgrep | cart |
| [`docker-build.yaml`](.github/workflows/docker-build.yaml) | build once, stage on ghcr.io, Trivy-scan that exact digest (HIGH/CRITICAL fail) | callers pinned to v3.2.x or older; `service-publish` no longer calls it (its steps are inline) |
| [`docker-promote.yaml`](.github/workflows/docker-promote.yaml) | copy the scanned digest to Docker Hub (no rebuild), tag, cosign-sign, self-verify, attach the SBOM as a signed `spdxjson` attestation, self-verify; refuses commits not on `main` | callers pinned to v3.2.x or older; `service-publish` has these steps inline (the copy now fails hard on a digest mismatch) |
| [`secret-scan.yaml`](.github/workflows/secret-scan.yaml) | stand-alone gitleaks scan (the pull request's commits; full history on manual runs); also the source of the `Secret scan` job of the service pipeline | helm, observability |
| [`workflow-lint.yaml`](.github/workflows/workflow-lint.yaml) | stand-alone actionlint (+ shellcheck on CI runners) and "every `uses:` is pinned to a full commit SHA"; also the source of `Workflow lint` | repositories not on the service pipeline (this one included) |
| [`service-ci-go.yaml`](.github/workflows/service-ci-go.yaml), [`-node`](.github/workflows/service-ci-node.yaml), [`-python`](.github/workflows/service-ci-python.yaml), [`-java`](.github/workflows/service-ci-java.yaml), [`-dotnet`](.github/workflows/service-ci-dotnet.yaml), [`-none`](.github/workflows/service-ci-none.yaml) | **generated** (tools/gen-service-ci.py), flat pull-request pipeline of an app repository, one file per language, only that language's jobs: guards (change detection, secrets, workflows), lint, build (+ optional smoke test), tests (+ optional E2E), dependency scan (SCA), code scan (SAST), image build (pushed as `ghcr.io/<repo>:tree-<hash>` by same-repository runs, reused by the publish) + Trivy scan of that digest, one `gate`. `none` = guards + image scan + gate (developer portal; product-catalog-db with `image: false`) | every app repository |
| [`service-publish.yaml`](.github/workflows/service-publish.yaml) | **generated** (tools/gen-service-publish.py), ONE flat workflow, merge to `main`: find the image of this source tree (built by the pull request) or build it, scan the digest again, copy it to Docker Hub as `sha-<7>`, sign, verify (a digest mismatch fails the run); then in parallel the SBOM, the staging prune and the dev promotion PR, which the same run follows through stage and prod with your two approval clicks (v3.6.0, [One pipeline](#one-pipeline-merge---dev---stage---prod)). Graph below | every app repository |
| [`service-merge.yaml`](.github/workflows/service-merge.yaml) | the merge button, called by the `merge` job of every app repository's `ci.yaml` after `ci / gate`: waits for the approval of the environment `merge`, checks the approval and that the branch is still the tested commit, squash-merges, starts `Publish` with `before` | every app repository |
| [`release.yaml`](.github/workflows/release.yaml) | the tag button: Actions, Release, Run workflow, `vX.Y.Z`: creates the annotated tag on `main` and prints the commit SHA that callers pin | this repository |
| [`changed-paths.yaml`](.github/workflows/changed-paths.yaml) | tells whether a change touches code or only docs/metadata (plain git, no token); `service-ci-*` carry an identical inline copy (`Detect changes`, which also writes the "What changed" run summary), `service-publish` one with the extra `before` input | older callers |
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
All six `service-ci-*` files have the same job names (`Detect changes`, `Secret scan`, `Workflow lint`, `Lint`, `Build`, `Tests`,
`Dependency scan`, `Code scan`, `Image build and scan`, `gate`), so branch protection looks the same everywhere. In a caller the check
names are `<caller job id> / <job name>`, e.g. `ci / Build`; the required one is `ci / gate` (see "Job names and run summaries" below).

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
  ci: { permissions: { contents: read, packages: write }, uses: .../service-ci-go.yaml@<sha> # v3.6.0, with: { test-packages: ..., ... } }
  # service-ci-<go|node|python|java|dotnet|none>.yaml; only dotnet also needs checks: write (test reporter); product-catalog-db: -none with image: false
  merge:                                  # the merge button: NOT part of `gate`, after it; the logic is service-merge.yaml
    needs: ci
    if: pull_request, same repository, not Dependabot
    permissions: { contents: write, pull-requests: write, actions: write }
    uses: .../service-merge.yaml@<sha> # v3.6.0   (product-catalog-db: with: { start-publish: false })
# .github/workflows/publish.yaml (shape)
on: { push: { branches: [main] }, workflow_dispatch: { inputs: { before: {type: string, default: ""} } } }
# no workflow-level concurrency: the run waits days for the stage / prod click (see "One pipeline" below)
jobs:
  publish: { permissions: { contents: read, packages: write, id-token: write, actions: read }, uses: .../service-publish.yaml@<sha> # v3.6.0,
             with: { app: checkout-service, before: "${{ inputs.before }}" }, secrets: DOCKERHUB_USERNAME / DOCKERHUB_TOKEN / PROMOTE_APP_ID / PROMOTE_APP_PRIVATE_KEY }
```

**`service-ci-<language>`** (pull request): flat, one file per language, so the Actions graph shows only what the repository runs. Left to
right: `Detect changes`, `Secret scan`, `Workflow lint`; next to them `Lint`, `Build`, `Tests`, `Dependency scan` (SCA),
`Code scan` (SAST) and `Image build and scan`; at the end one `gate` job:

```
Detect changes --+--> Lint | Build | Tests | Dependency scan | Code scan | Image build and scan --+
Secret scan -----+                                                                                +--> gate --> merge
Workflow lint ---+--------------------------------------------------------------------------------+      (approval)
```

**The left column means "starts at t=0", not "must pass first"**: all nine jobs start together, the checks wait only for the change
detection (a docs-only change skips them), so a flaky gitleaks download or one lint complaint does not hide the test, SAST and Trivy
results. `gate` needs every job, always runs, names the failed or cancelled job first (the skips that follow from it are not noise) and is strict
about the rest; it works on job ids, never on the names: **`ci / gate` is the only required check**; `merge` is after it and is not part of it. The optional smoke test (Go, Node:
input `smoke-port`) is a step of `Build`, reusing the build; the optional E2E tests (Go: `e2e-packages`) a step of `Tests`;
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
Detect changes -> Find or build image -> Scan image -> Publish image -+-> Create SBOM
                                                                      +-> Clean up staging images (best effort)
                                                                      +-> Open dev PR   (gitops-promote.yaml) -> Wait for dev
                                                                            -> Approve stage deployment -> Promote to stage -> Wait for stage
                                                                            -> Release stage version
                                                                            -> Approve prod deployment -> Promote to prod -> Wait for prod
```

| Job | What it does |
|---|---|
| `Detect changes` | docs/metadata-only commit: nothing below runs. Push: diff `github.event.before..HEAD`. Manual run: with `before` it diffs `before..HEAD` the same way, without it everything runs |
| `Find or build image` | `TREE=$(git rev-parse HEAD^{tree})`; looks for `ghcr.io/<repo>:tree-<TREE>`. Found = its digest is reused, no build. Not found (main moved, the tree differs, the image was pruned) = built now, registry cache written, pushed under the same tag |
| `Scan image` | Trivy (HIGH/CRITICAL fail) on that digest, again: new CVEs may have appeared since the pull request |
| `Publish image` | refuses commits not reachable from `main`; copies the digest to Docker Hub as `sha-<7 chars of the commit>` only (no `latest`, no semver), never moves an existing tag, **fails hard if the Docker Hub digest differs from the scanned one**, cosign signs (keyless) and verifies. Its run summary answers "what was published": tag, commit, digest, reused or built, signature, where the dev change goes |
| `Create SBOM` | SPDX SBOM as a signed attestation, verified. Own job, so it is not on the way to the helm pull request; a failure fails the run visibly |
| `Open dev PR` | the pull request in the helm repository that moves `dev` to this digest (`permissions: {}`, App token); skipped when dev already runs a newer commit (last step of `Publish image`) |
| `Wait for dev` ... `Wait for prod` | the promotion to stage and prod in the same run, with your clicks: see [One pipeline](#one-pipeline-merge---dev---stage---prod) |
| `Clean up staging images` | `continue-on-error: true`, never delays the helm pull request: keeps the newest `prune-keep` (10) `sha-*` and the newest `prune-keep-tree` (20) `tree-*` versions of the ghcr.io package, never the `buildcache` package, never an image-index child of a kept image |

Concurrency: since v3.6.0 there is no workflow-level group (a run waits days for a click and would block every later merge); the jobs
that must not overlap have their own groups per app (see "One pipeline" below). **A build that waits behind a running one is replaced by
a newer waiting one**, so of three quick merges the second never builds by itself; the third builds the tree of `main`, which contains
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
trigger, hence the explicit start; the normal GitHub Merge button still works and starts Publish by push (if both ever happen for the same
commit, the build group lets the second run wait and find the tree image). The caller file is the pull request's own: **read a change to `.github/workflows`
before you approve it.** Fork and Dependabot pull requests have no `merge` job (read-only token): use the normal button.

### One pipeline: merge -> dev -> stage -> prod

Since v3.6.0 the whole way of a change to production is **one run** in the Actions tab of the application repository: the **Publish** run
that starts when the pull request is merged. There is no second place to go, nothing to type and no version to choose.

```
Detect changes -> Find or build image -> Scan image -> Publish image -+-> Create SBOM
                                                                      +-> Clean up staging images
                                                                      +-> Open dev PR -> Wait for dev -> Approve stage deployment
                                                                            -> Promote to stage -> Wait for stage -> Release stage version
                                                                            -> Approve prod deployment -> Promote to prod -> Wait for prod
```

You merge (the `merge` button of the pull request, or the normal Merge button). **Dev lands by itself.** Then the run **waits for you**:

1. **Approve stage deployment**: open the Publish run, press **Review deployments**, tick `stage`, **Approve and deploy**. The run opens the
   stage pull request in `platform-demo-helm`, waits until it is merged and makes the release `<app>-vX.Y.Z`.
2. **Approve prod deployment**: the same click with `prod`. The run opens the prod pull request and waits until it is merged. The last job,
   `Wait for prod`, ends with "The chain is complete: dev, stage and prod run this build."

The clicks can come minutes or days apart. GitHub notifies the reviewers of an environment when a job waits for them (their notification
settings decide how). A job that nobody approves for 30 days is stopped by GitHub.

#### Where to click

**Repository `app-<name>` -> Actions -> Publish -> the run of your merge -> Review deployments** (yellow banner at the top of the run page)
-> tick the environment (`stage`, later `prod`) -> **Approve and deploy** (or **Reject**). The run summary of every job says what it did
and what comes next.

#### Settings each app repository needs (once)

| Environment | Required reviewers | Deployment branches | Why |
|---|---|---|---|
| `merge` (exists) | the owner; *Prevent self-review* **off** | no restriction (it runs on the pull request) | the merge button |
| `stage` | the owner; *Prevent self-review* **off** (the owner merged the change) | **Selected branches: `main`** | the click for stage |
| `prod` | the owner; *Prevent self-review* **off** | **Selected branches: `main`** | the click for prod |

The environments are `dev` (no click), `stage` and `prod`, nothing else. **A missing environment is created by GitHub without any
protection when a job first mentions it, and then the job would run without anybody clicking.** The approval jobs therefore ask the run
itself whether a person approved the environment and **fail without promoting anything** when nobody did (the message says to create the
environment with a required reviewer). The caller grants `actions: read` for that check.

Organisation secrets `PROMOTE_APP_ID` and `PROMOTE_APP_PRIVATE_KEY` (the GitHub App `platform-demo-promote`) are the same ones `Open dev PR`
uses; the App needs *Contents: write* and *Pull requests: write* on `platform-demo-helm`. Nothing else changes in the callers.

#### The jobs after `Open dev PR`

| Job | What it does | Ends |
|---|---|---|
| `Wait for dev` | polls the dev pull request (every 15 s, up to 25 min) until `envs/dev` on helm `main` holds **this build's digest**. The pull request is only the signal: its branch is shared by all builds of the app, so "merged" alone does not mean "mine" | green: landed, or *superseded by a newer build*. Red: closed without merging, or not merged in 25 min |
| `Approve stage deployment` | environment `stage`. **After** the click: the approval is verified; dev must **still** hold this build's digest (else *superseded*: nothing is promoted); the version is chosen | green, or red (no approval, tags cannot be listed) |
| `Promote to stage` | the same pinned `gitops-promote.yaml` as `Open dev PR`: env `stage`, this run's digest, the chosen version, auto-merge | |
| `Wait for stage` | as `Wait for dev`; also checks that the merged file records the expected version | |
| `Release stage version` | creates the tag and release `<app>-vX.Y.Z` in `platform-demo-helm` (below) | red when GitHub refuses |
| `Approve prod deployment` | environment `prod`; same checks after the click, with stage as the previous environment | |
| `Promote to prod` | `gitops-promote.yaml`, env `prod`, this run's digest, **stage's version** | |
| `Wait for prod` | as above; the last line of the chain | |

The digest is the one of **this run**; the registry is never asked for "the newest". The helm repository's promotion policy stays the
safety net: stage may only take dev's digest, prod only stage's, and a promotion pull request changes one environment only.

#### The version

Stage: the next **patch** after the highest `<app>-vX.Y.Z` tag of `platform-demo-helm`; none yet: `1.0.0`. The highest version wins (by
number: `1.0.10` is above `1.0.9`), tags that are not plain `X.Y.Z` (`-rc1`, extra dots, look-alikes) are ignored, and a version already
written in `envs/stage/values/<app>.yaml` counts (its tag is made right after the merge). If the stage file already holds this build's
digest with a version, that version is kept. Prod: **the version of stage, unchanged**. These are the rules of `promote.sh` in the helm
repository. The version is chosen **after the stage click**, so it is the latest. If the tags cannot be listed the job fails; it never
guesses `1.0.0`. A minor or major bump is not part of this pipeline.

#### The release `<app>-vX.Y.Z`

`Release stage version` runs after `Wait for stage`, with a short-lived **GitHub App token** limited to `platform-demo-helm` and *Contents:
write* (not the run's own token). It (1) checks that the helm commit really holds this version for this digest, (2) creates the tag on that
commit when it does not exist (**an existing tag is never moved or touched**; one that exists for this digest is fine; one that exists for
another digest fails the job), (3) creates the release (notes: digest, image tag, source commit, helm commit, run link; not marked
*latest*) when it does not exist. Run it twice and the second run changes nothing. The tag and the release are two separate calls, so a
refusal names the call that failed.

If GitHub refuses, the run summary says which call and quotes the answer. *"Resource not accessible by integration"* means the token has no
permission (check the App's *Contents: write* and that it is installed on `platform-demo-helm`); *"Repository rule violations"* means a
ruleset refused it: add the App `platform-demo-promote` to the bypass list of the ruleset `protect-app-release-tags` (Settings -> Rules). Fix it and use
**Re-run failed jobs**: the chain continues from the release.

#### Several builds at once

There is **no workflow-level `concurrency`** in `publish.yaml` any more. A run that waits days for a click would otherwise make every later
merge queue behind it. The jobs that must not overlap have their own groups (per app, in `service-publish.yaml`):

| Group | Job | Effect |
|---|---|---|
| `publish-build-<app>` | `Find or build image` | one build at a time; a second build waits, a waiting one is replaced by a newer one |
| `publish-prune-<app>` | `Clean up staging images` | two prunes never run together |
| `promote-stage-<app>`, `promote-prod-<app>`, **cancel-in-progress** | the two approval jobs | a NEWER build's waiting approval **replaces** an older waiting one. The older run ends as **cancelled** (grey), not failed |
| `gitops-promote-<app>-<env>` (inside `gitops-promote.yaml`) | the dev, stage and prod pull requests | one at a time per app and environment; a newer build updates the shared branch `promote/<app>/<env>` |

The groups are scoped to the repository by GitHub and the group names contain the app, so apps never interfere with each other.

What happens:

- **Two merges in quick succession (A, then B).** Both runs start. Their builds go one after the other (B waits at `Find or build image`;
  of three quick merges the middle one is replaced by the last and its run is cancelled: the last build contains it). The dev pull
  requests: if A's is still open when B's job runs, B updates it (same branch) and A's `Wait for dev` reports *superseded by a newer
  build* (green); if A's already merged, B opens its own, A's run reaches its stage approval first, B's approval then **replaces** it and
  A's run ends as cancelled. Either way **one approval waits: B's**.
- **A merge while an older run waits for the stage click.** The new run reaches `Approve stage deployment` once its dev pull request is
  merged; GitHub then cancels the older waiting approval. The older run is cancelled, the new one waits. Until then the old click still works.
- **A click on an old approval.** After the click the job re-reads helm `main`. If dev (for prod: stage) no longer holds this build's digest
  the run says *superseded by a newer build* and promotes nothing (green): approve the newer run. If the newer build lands in dev between
  that check and the pull request being checked in helm, the helm promotion policy refuses the old pull request, and `Wait for stage`
  reports *superseded* when it cannot merge.
- **A slower older run never moves dev back.** The last step of `Publish image` (`Check that dev does not already run a newer commit`) compares
  dev's `sha-<7>` tag with this commit; when dev runs a descendant of this commit, `Open dev PR` and everything after it are skipped (green,
  summary says why). This also covers *Re-run all jobs* of an old run. Anything unclear (another tag format, GitHub errors) lets the run go on.
- **A documentation-only merge** publishes nothing (as before) and none of the chain runs. It no longer waits behind, or replaces, anything:
  an older run waiting for a click is not touched.
- **Merge, then Reject at the stage prompt.** The run ends red at `Approve stage deployment`; the build stays in dev; the next merge starts
  its own run.

Not covered: two stage promotions whose version is chosen within the minutes between a click and the merge of its pull request can pick the
same version; the second `Release stage version` then fails ("the tag exists for another image"), the tag is not moved. It needs two clicks
on two different runs within minutes, which the replace-older approval makes unlikely.

#### What stops the chain, and what to do

| Message in the summary | Meaning | Do |
|---|---|---|
| *no approval* (approve job red) | the environment has no required reviewer | create/fix the environment `stage` or `prod`, re-run the failed job |
| *the pull request was closed without merging* | someone closed the helm pull request, or a helm check failed and it was closed | read the pull request; start Publish by hand or merge the next change |
| *waited 25 minutes, not merged yet* | helm checks are red or slow | fix the checks; when the pull request merges, **Re-run failed jobs**: the wait sees the merge and the chain goes on |
| *superseded by a newer build* | a newer build took over dev/stage | nothing; approve in the newer run |
| *the version is not the expected one* | the merged file records another version | look at the pull request and the file |
| *creating the tag ... failed* | see the release section | |

#### What is tested, and what is not

`tools/test-shell.py` runs every script of the chain against a stub of `gh` and a dictionary that plays platform-demo-helm; `tools/check-consistency.py`
checks names, `needs`, environments, permissions, concurrency groups and the re-check of the supersede rule;
`tools/check-permissions.py` checks the callers (no workflow-level concurrency, all secrets passed, `actions: read`). What they cannot show is
how GitHub itself treats a cancelled waiting approval or an environment without protection; the first real run in a throw-away app repository is the proof.

### Job names and run summaries

**Job names** (since v3.5.0): sentence case, what the job does, no group prefixes (no `Guard:`, `Check:`, `Security:`, `Image:`, `Publish:`,
`Deploy:`, `Cleanup:`). Every job has an explicit, stable `name:`; the job id (`image`, `sca`, ...) does not change. GitHub shows a called
job as `<caller job id> / <job name>`, so the caller's job id is the prefix: `ci / Build`, `ci / Code scan`, `publish / Publish image`.
Language detail belongs in the step names (`Run golangci-lint`, `Run gosec`). The required check is `ci / gate`: `gate` stays lowercase
because it is the one name branch protection pins. `tools/check-consistency.py` fails on any other name, on a `Group:` prefix and on a
missing `name:`; the gate reads job **ids** from `needs`, never names (it only prints the names).

| Job id | Name in `service-ci-<language>` | Was (v3.4.0) |
|---|---|---|
| `changes` | Detect changes | Guard: Changes |
| `secret-scan` | Secret scan | Guard: Secrets |
| `workflow-lint` | Workflow lint | Guard: Workflows |
| `lint` | Lint | Check: Lint |
| `build` | Build | Check: Build |
| `test` | Tests | Check: Tests |
| `sca` | Dependency scan | Security: SCA |
| `sast` | Code scan | Security: SAST |
| `image` | Image build and scan | Image: Build + CVE scan |
| `gate` | gate | gate |

| Job id | Name in `service-publish` | Was (v3.4.0) |
|---|---|---|
| `changes` | Detect changes | Guard: Changes |
| `image` | Find or build image | Image: Find or build |
| `scan` | Scan image | Image: Scan |
| `publish` | Publish image | Publish: Copy, sign, verify |
| `sbom` | Create SBOM | Publish: SBOM |
| `deploy` | Open dev PR | Deploy: Open dev PR |
| `prune` | Clean up staging images | Cleanup: Prune staging |
| `wait-dev` | Wait for dev | (new in v3.6.0) |
| `approve-stage` | Approve stage deployment | (new) |
| `stage` | Promote to stage | (new) |
| `wait-stage` | Wait for stage | (new) |
| `release` | Release stage version | (new) |
| `approve-prod` | Approve prod deployment | (new) |
| `prod` | Promote to prod | (new) |
| `wait-prod` | Wait for prod | (new) |

`service-merge` keeps `Approve and merge`.

**Run summaries** (the markdown on the run page, `GITHUB_STEP_SUMMARY`): status first, only the key facts, details folded in `<details>`,
ids in code, no emojis in headings. A summary belongs to one job, so every job writes its own short section and none passes data around:

| Run | Job | Section |
|---|---|---|
| pull request | `Detect changes` | **What changed**: code or documentation only, the number of changed files, the list (first 50) in `<details>`, a warning when workflow files are part of the change |
| pull request | `Image build and scan` | **Image**: reused, built or built locally; the `tree-<hash>` tag; the digest; whether the Trivy scan passed (the findings are in the step's log) |
| pull request | `gate` | the verdict first (`Ready to merge`, or `Blocked: <job> failed`), what changed, one row per job, and what the `merge` job will do |
| publish | `Detect changes` | the same **What changed**; for a documentation-only commit: "Nothing is published." |
| publish | `Find or build image`, `Scan image` | **Image** (reused or built, tag, digest), **Image scan** (Trivy result) |
| publish | `Publish image` | **Published `sha-<7>`**: commit, Docker Hub tag, digest, reused or built, signature verified, where the dev change goes |
| publish | `Create SBOM` | **SBOM**: attached to the digest as a signed attestation, verified |
| publish | `Open dev PR` | the section of `gitops-promote.yaml` (unchanged): link to the helm pull request, tag, digest, source commit |
| publish | `Clean up staging images` | versions before, deleted, remaining, failures; the deleted list folded in `<details>` |
| publish | `Wait for dev` / `Wait for stage` / `Wait for prod` | **Dev deployed** / **Stage deployed: version X.Y.Z** / **Prod deployed** with the helm pull request link; or *superseded by a newer build* (green), or why the chain stopped; `Wait for prod` ends with "The chain is complete" |
| publish | `Approve stage deployment` / `Approve prod deployment` | **Stage approved by <who>**: version (and how it was chosen), old -> new digest; or *not promoted: superseded by a newer build* |
| publish | `Release stage version` | **Released `<app>-vX.Y.Z`**: tag, release link, digest, helm commit |

Limits and care: GitHub rejects a step summary over 1 MiB, so lists are cut (50 file names, at most 100 deleted versions). File names,
tags and caller inputs are untrusted text: they appear only inside code spans, as printable ASCII (anything else becomes `?`), without
backticks, at most 200 characters. The Trivy counts are not in the summary: the action prints a table in the log, and counting would need a
second scan. `gitops-promote.yaml` is shared with the helm repository and pinned by SHA, so `Open dev PR` keeps its own summary.

Example, a green pull request (`gate`):

```
### Ready to merge

Change: **code**. All checks apply.

| Job | Result |
|---|---|
| Detect changes | passed |
| Secret scan | passed |
| ... | passed |
| Image build and scan | passed |

**Merge button:** approve the `merge` environment on this run page (Review deployments) to squash-merge this pull request and start Publish.
```

Example, the publish job:

```
### Published `sha-1234567`

| | |
|---|---|
| Commit | [`1234567`](https://github.com/<org>/<repo>/commit/<sha>) |
| Docker Hub tag | `sha-1234567` |
| Digest | `sha256:...` |
| Image | reused: built by the pull request from source tree `<tree>`, not built again |
| Signature | verified (keyless cosign; the certificate names this workflow and commit `1234567`) |
| Dev change | the pull request in platform-demo-helm that moves `dev` of `<app>` to this digest (link), opened by the `Open dev PR` job (its summary links the exact one) |
```

## Release model (trunk-based)

- `main` is the trunk; work happens on short-lived `feat/*`, `fix/*`, `docs/*`, `chore/*` branches merged by squash PR once `ci / gate` is green. The merge is the `merge` job of the pull request's `CI` run (approve the environment `merge` on the run page) or the normal GitHub Merge button.
- **One artifact per source tree.** The pull request builds and scans the image (`ghcr.io/<repo>:tree-<hash>`); the merge to `main` reuses it
  when `main`'s tree is the same (builds it once when not), scans the digest again, publishes it to Docker Hub, signs it and opens the
  promotion PR for `dev` (`service-publish`). The same digest then moves forward and is never rebuilt.
- **Environments are directories** in `platform-demo-helm` (`envs/dev|stage|prod`), Argo CD watches `main`. `dev` is promoted
  automatically (PR with auto-merge after the helm gates). `stage` and `prod` are promoted by two clicks **in the same Publish run** (v3.6.0,
  [One pipeline](#one-pipeline-merge---dev---stage---prod)): stage takes the digest that is in dev, prod the one that is in stage (the `Promotion policy` check enforces it).
  The stage version `X.Y.Z` is computed (next patch) and recorded in the helm repo as `<app>-vX.Y.Z`; prod takes stage's version.
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
- **A job output that contains a secret is dropped by GitHub.** The Docker Hub image reference contains the account name (a secret), so `Publish image` outputs only the digest and the tag; the SBOM job rebuilds the reference.
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
| v3.5.0 | **Plain job names and useful run summaries** (no change to the graph, the job ids, the inputs, the permissions or the callers except the pin). Job names have no group prefixes any more: sentence case, what the job does. `service-ci-*`: `Detect changes`, `Secret scan`, `Workflow lint`, `Lint`, `Build`, `Tests`, `Dependency scan` (was SCA), `Code scan` (was SAST), `Image build and scan`, `gate` (unchanged; the required check stays `ci / gate`). `service-publish`: `Detect changes`, `Find or build image`, `Scan image`, `Publish image`, `Create SBOM`, `Open dev PR`, `Clean up staging images`. `service-merge`: `Approve and merge` (unchanged). Every job has an explicit `name:`; `check-consistency.py` pins the names and rejects a `Group:` prefix. Run summaries: the pull request's `Detect changes` writes what changed (code or documentation only, number of files, the list folded away, a warning when workflow files are part of the change), `Image build and scan` the image (reused or built, tree tag, digest, Trivy passed or failed), and `gate` the verdict first (`Ready to merge` / `Blocked: <job> failed`), the results and what the Merge button will do. The publish run: `Publish image` states what was published (tag, commit, digest, reused or built, signature, where the dev change goes), `Create SBOM`, `Scan image` and `Clean up staging images` write a short section each. File names, tags and caller inputs are escaped (code spans, printable ASCII, capped lists). `changed-paths.yaml` writes the same summary for the older callers. `test-gate.py` and `test-shell.py` cover the summaries |
| v3.6.0 | **One pipeline: merge -> dev -> stage -> prod in the Publish run of the app repository.** `service-publish.yaml` goes on after `Open dev PR` with `Wait for dev`, `Approve stage deployment` (GitHub environment `stage` of the APP repository: Review deployments on the run page), `Promote to stage`, `Wait for stage`, `Release stage version`, `Approve prod deployment` (environment `prod`), `Promote to prod`, `Wait for prod`; the two promotions call the same pinned `gitops-promote.yaml` as `Open dev PR` with this run's digest. After each click the job re-reads helm `main`: when a newer build moved the previous environment the run ends green as *superseded by a newer build* and promotes nothing. The stage version is computed in the pipeline with the rules of the helm repository's `promote.sh` (next patch after the highest `<app>-vX.Y.Z` tag, none: 1.0.0, a version in the stage file counts); prod takes stage's. The tag and release `<app>-vX.Y.Z` are created by the pipeline with the GitHub App token (idempotent, never moves a tag). **Concurrency:** the callers' workflow-level `publish-<ref>` group is removed (a run waits days for a click); job groups per app instead (`Find or build image`, `Clean up staging images`, the two approvals with cancel-in-progress so a newer waiting approval replaces an older one). New last step of `Publish image` skips `Open dev PR` when dev already runs a newer commit. Callers: `publish.yaml` also grants `actions: read` (who approved the environment); each app repository needs the environments `stage` and `prod` (owner as required reviewer, deployment branch main). `check-consistency.py`, `check-permissions.py` and `test-shell.py` cover the new jobs |

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
