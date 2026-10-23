# platform-demo-workflows

Reusable GitHub Actions workflows shared by the platform-demo repositories. Services call these instead of carrying their own
copy of a 300-line pipeline, so a fix or a version bump lands once. Current release: **v3.3.0** (history below). Callers pin the release they use by commit SHA: the developer portal calls v3.0.3; the service repositories call `service-ci-<language>`/`service-publish` (v3.3.0, the pilot still pins v3.2.1) or, until they move, v2.0.2.

| Workflow | Purpose | Used by |
|---|---|---|
| [`go-ci.yaml`](.github/workflows/go-ci.yaml) | stand-alone, for repositories not on the service pipeline (older pins, portal): lint/format, secret scan, build, unit tests, optional E2E and gRPC smoke test, then SCA (govulncheck) and SAST (gosec) | checkout, shipping, product-catalog, frontend |
| [`node-ci.yaml`](.github/workflows/node-ci.yaml) | format/typecheck/lint, secret scan, build, tests, `npm audit`, Semgrep, optional gRPC smoke test | currency, payment |
| [`python-ci.yaml`](.github/workflows/python-ci.yaml) | ruff, secret scan, entry-point parse check, pytest, pip-audit, bandit | email, recommendation |
| [`java-gradle-ci.yaml`](.github/workflows/java-gradle-ci.yaml) | compile, secret scan, build, tests, Trivy fs scan, Semgrep | ad |
| [`dotnet-ci.yaml`](.github/workflows/dotnet-ci.yaml) | `dotnet format` + analyzers, secret scan, build, tests, vulnerable-package audit, Semgrep | cart |
| [`docker-build.yaml`](.github/workflows/docker-build.yaml) | build once, stage on ghcr.io, Trivy-scan that exact digest (HIGH/CRITICAL fail) | every service |
| [`docker-promote.yaml`](.github/workflows/docker-promote.yaml) | copy the scanned digest to Docker Hub (no rebuild), tag, cosign-sign, self-verify, attach the SBOM as a signed `spdxjson` attestation, self-verify; refuses commits not on `main` | every service |
| [`secret-scan.yaml`](.github/workflows/secret-scan.yaml) | stand-alone gitleaks scan (the pull request's commits; full history on manual runs); also the source of the `Guard: Secrets` job of the service pipeline | helm, observability |
| [`workflow-lint.yaml`](.github/workflows/workflow-lint.yaml) | stand-alone actionlint (+ shellcheck on CI runners) and "every `uses:` is pinned to a full commit SHA"; also the source of `Guard: Workflows` | repositories not on the service pipeline (this one included) |
| [`service-ci-go.yaml`](.github/workflows/service-ci-go.yaml), [`-node`](.github/workflows/service-ci-node.yaml), [`-python`](.github/workflows/service-ci-python.yaml), [`-java`](.github/workflows/service-ci-java.yaml), [`-dotnet`](.github/workflows/service-ci-dotnet.yaml), [`-none`](.github/workflows/service-ci-none.yaml) | **generated** (tools/gen-service-ci.py), flat pull-request pipeline of an app repository, one file per language, only that language's jobs: guards (change detection, secrets, workflows), lint, build (+ optional smoke test), tests (+ optional E2E), SCA, SAST, unpushed image build + Trivy scan, one `gate`. `none` = guards + image scan + gate (developer portal; product-catalog-db with `image: false`) | every app repository |
| [`service-publish.yaml`](.github/workflows/service-publish.yaml) | merge to `main`: build once, scan the digest, publish `sha-<7>` to Docker Hub, sign, SBOM, dev promotion PR, prune staging images | every app repository |
| [`changed-paths.yaml`](.github/workflows/changed-paths.yaml) | tells whether a change touches code or only docs/metadata (plain git, no token); `service-ci-*` carry an identical inline copy (`Guard: Changes`) | service-publish |
| [`gitops-promote.yaml`](.github/workflows/gitops-promote.yaml) | changes only `image.tag` and `image.digest` of one app in one environment of platform-demo-helm through a pull request (GitHub App token scoped to the helm repo, branch `promote/<app>/<env>` force-updated so a newer build replaces an older open one, squash auto-merge); idempotent | app publish workflows (dev), helm `Promote` (stage, prod) |
| [`gitops-rollback.yaml`](.github/workflows/gitops-rollback.yaml) | moves an app back or forward to a digest that was deployed before (`previous`, `latest`, a digest or a helm commit) through a pull request (`rollback/<app>/<env>`, label `rollback`) that passes the same gates | helm `Rollback` |
| [`scheduled-image-scan.yaml`](.github/workflows/scheduled-image-scan.yaml) | weekly Trivy scan of every deployed image digest (Monday 08:00 UTC); SARIF to the Security tab and one updated issue "Scheduled image scan" | helm `Scheduled scans` |
| [`scheduled-code-scan.yaml`](.github/workflows/scheduled-code-scan.yaml) | weekly SCA and SAST of the default branch of the services, the portal and the plugins (own cron, Monday 08:00 UTC); one updated issue "Scheduled code scan" | this repository |
| [`ghcr-prune.yaml`](.github/workflows/ghcr-prune.yaml) | keeps the newest N `sha-*` versions of a ghcr.io staging package, protects image-index children, `dry-run` supported | app publish workflows |
| [`terraform-ci.yaml`](.github/workflows/terraform-ci.yaml) | `fmt` + `validate`, TFLint, Trivy config scan, Checkov, Infracost monthly estimate (needs a free `INFRACOST_API_KEY`), `terraform plan` as a PR comment (no refresh, never touches the running platform) | infra-terraform |
| [`terraform-plan.yaml`](.github/workflows/terraform-plan.yaml) | plans a root module (or its destruction), saves the plan encrypted in the state bucket (7 days), shows the reason and the changes on the run page; changes nothing | infra-terraform |
| [`terraform-apply.yaml`](.github/workflows/terraform-apply.yaml) | **Review** job (checks the saved plan belongs to this commit, is recent and unaltered; shows reason, an "at a glance" summary computed by script with anything destroyed or replaced first, and the plan), then an **Apply** job in a GitHub environment that waits for a reviewer and applies exactly the reviewed plan. Also runs destroy plans, with optional before/after scripts | infra-terraform |

The **stand-alone** `go-ci`, `node-ci`, `python-ci`, `java-gradle-ci`, `dotnet-ci`, `secret-scan` and `workflow-lint` are for repositories
that are not on the service pipeline (callers pinned to an older release, the developer portal). App repositories use `service-ci-<language>`.
All six `service-ci-*` files have the same job names (`Guard: Changes`, `Guard: Secrets`, `Guard: Workflows`, `Check: Lint`, `Check: Build`,
`Check: Tests`, `Security: SCA`, `Security: SAST`, `Image: Build + CVE scan`, `gate`), so branch protection looks the same everywhere. In a
caller the check names are `<caller job id> / <job name>`, e.g. `ci / Check: Build`; the required one is `ci / gate`.

## How a service calls them

Two small files per app repository (generated by `tools/gen-callers.py`): `ci.yaml` (job `ci`, runs on pull requests) and `publish.yaml`
(job `publish`, runs on a push to `main`). There are no tags and no `release.yaml`: everything deployed comes from `main`.

```yaml
# .github/workflows/ci.yaml (shape)
on: { pull_request: { branches: [main] }, workflow_dispatch: }
permissions: {}
jobs:
  ci: { permissions: { contents: read, packages: read }, uses: .../service-ci-go.yaml@<sha> # v3.3.0, with: { test-packages: ..., ... } }
  # service-ci-<go|node|python|java|dotnet|none>.yaml; only dotnet also needs checks: write (test reporter); product-catalog-db: -none with image: false
# .github/workflows/publish.yaml (shape)
on: { push: { branches: [main] }, workflow_dispatch: }
jobs:
  publish: { permissions: { contents: read, packages: write, id-token: write }, uses: .../service-publish.yaml@<sha> # v3.3.0,
             with: { app: checkout-service }, secrets: DOCKERHUB_USERNAME / DOCKERHUB_TOKEN / PROMOTE_APP_ID / PROMOTE_APP_PRIVATE_KEY }
```

**`service-ci-<language>`** (pull request): flat, one file per language, so the Actions graph shows only what the repository runs. Left to
right: the guards `Guard: Changes`, `Guard: Secrets`, `Guard: Workflows`; next to them `Check: Lint`, `Check: Build`, `Check: Tests`,
`Security: SCA`, `Security: SAST` and `Image: Build + CVE scan` (built into the local daemon, never pushed); at the end one `gate` job.
**The left column means "starts at t=0", not "must pass first"**: all nine jobs start together, the checks wait only for the change
detection (a docs-only change skips them), so a flaky gitleaks download or one lint complaint does not hide the test, SAST and Trivy
results. `gate` needs every job, always runs, lists the failed or cancelled jobs first (their consequent skips are not noise) and is strict
about the rest: **`ci / gate` is the only required check**. The optional smoke test (Go, Node: input `smoke-port`) is a step of
`Check: Build`, reusing the build; the optional E2E tests (Go: `e2e-packages`) a step of `Check: Tests`; there are no optional jobs that
would show up as skipped.
The files are generated from the stand-alone workflows by `tools/gen-service-ci.py` (job bodies extracted, not retyped) and
`tools/check-consistency.py` fails when they drift. Nothing is nested, so GitHub validates only the file's own jobs: the caller grants
`contents: read` and `packages: read` (and `checks: write` for dotnet alone), and there is no placeholder pin or two-step release.

**`service-publish`** (merge to `main`): build once, stage on ghcr.io, scan that exact digest, copy it to Docker Hub as `sha-<7>`, sign it
(keyless cosign), attach the SBOM, open the promotion PR for `dev` through `gitops-promote.yaml`, and prune old staging images.
A docs-only merge publishes nothing.

Callers always pin to a **commit SHA** with the version in a trailing comment. A reusable workflow can never get more permissions than
its caller grants, so each job lists what it needs (`id-token: write` only on `publish`). Secrets: `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN` (per service repository), `PROMOTE_APP_ID` and `PROMOTE_APP_PRIVATE_KEY` (organisation secrets, GitHub App scoped to
platform-demo-helm). Semgrep needs no token.

## Release model (trunk-based)

- `main` is the trunk; work happens on short-lived `feat/*`, `fix/*`, `docs/*`, `chore/*` branches merged by squash PR once `ci / gate` is green.
- **One artifact per commit.** A merge to `main` builds, scans, publishes and signs the image once (`service-publish`) and opens the
  promotion PR for `dev`. The same digest then moves forward and is never rebuilt.
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

1. Branch, edit (`tools/gen-lang-workflows.py` generates the node/python/java/dotnet files, `tools/gen-service-ci.py` the `service-ci-*` files from them - edit the source or the generator, run both, in that order),
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
`--hold gradle/actions=v5`), `tools/gen-lang-workflows.py`, `tools/gen-service-ci.py`, `tools/test-gate.py` (gate scenarios), `tools/check-consistency.py`, `tools/check-permissions.py`, `tools/gen-callers.py`, `tools/gen-guardrails.py` (CODEOWNERS,
Dependabot, `.pre-commit-config.yaml` for a repo), `tools/transfer-repo.sh` (move a repo to the org and re-apply what a transfer resets), `tools/required-checks.sh` + `tools/protect-repo.sh` (branch protection with the
required checks taken from a repo's last green PR, squash-only merges, secret scanning, tag ruleset).

## Things to know

- **Docker Hub has no OCI referrers API.** cosign 3 defaults to the new bundle format stored as a referrer: `cosign sign` succeeds
  and `cosign verify` then finds nothing. `docker-promote` therefore signs/attests/verifies with
  `--new-bundle-format=false --use-signing-config=false` (classic `sha256-<digest>.sig` tags), which Kyverno and most verifiers
  understand. The keyless identity of an image signed through a reusable workflow is
  `https://github.com/platform-boutique/platform-demo-workflows/.github/workflows/docker-promote.yaml@<sha-or-ref>` (images signed before the move to the organisation: `https://github.com/mladenovskistefan111/platform-demo-workflows/...`; both are accepted); images
  signed by the older per-repo pipelines carry `.../<service>/.github/workflows/ci-main.yaml@refs/heads/...`.
- **Staging on ghcr.io, publishing by digest:** what Trivy scanned is byte-for-byte what is published and signed.
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
