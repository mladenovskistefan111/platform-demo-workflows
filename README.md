# platform-demo-workflows

Reusable GitHub Actions workflows shared by the platform-demo repositories. Services call these instead of
carrying their own copy of a 300-line pipeline, so a fix or a version bump lands once.

| Workflow | Purpose | Used by |
|---|---|---|
| [`go-ci.yaml`](.github/workflows/go-ci.yaml) | lint/format, secret scan, build, unit tests, then SCA (govulncheck) and SAST (gosec) | checkout, shipping, product-catalog, frontend |
| [`docker-build.yaml`](.github/workflows/docker-build.yaml) | build once, stage on ghcr.io, Trivy-scan that exact digest (HIGH/CRITICAL fail) | every service |
| [`docker-promote.yaml`](.github/workflows/docker-promote.yaml) | copy the scanned digest to Docker Hub (no rebuild), tag, cosign-sign, verify, attach SBOM; refuses commits not on `main` | every service |
| [`workflow-lint.yaml`](.github/workflows/workflow-lint.yaml) | actionlint + "every `uses:` is pinned to a full commit SHA" | every repo |

| [`node-ci.yaml`](.github/workflows/node-ci.yaml) | format/typecheck/lint, secret scan, build, tests, `npm audit`, Semgrep, optional gRPC smoke test | currency, payment |
| [`python-ci.yaml`](.github/workflows/python-ci.yaml) | ruff, secret scan, entry-point parse check, pytest, pip-audit, bandit | email, recommendation |
| [`java-gradle-ci.yaml`](.github/workflows/java-gradle-ci.yaml) | compile, secret scan, build, tests, Trivy fs scan, Semgrep | ad |
| [`dotnet-ci.yaml`](.github/workflows/dotnet-ci.yaml) | `dotnet format` + analyzers, secret scan, build, tests, vulnerable-package audit, Semgrep | cart |

Every language workflow has the same job names (`Lint & Format`, `Secret Scan`, `Build Check`, `Unit Tests`,
`SCA - Dependency Vulnerabilities`, `SAST - Static Security Analysis`), so branch protection looks the same everywhere.

## How a service calls them

Two small files per service. `ci.yaml` runs on pull requests, `release.yaml` runs when a `vX.Y.Z` tag is pushed
(or by hand). **Nothing runs on a plain push.**

```yaml
# .github/workflows/ci.yaml
on:
  pull_request:
    branches: [main]
  workflow_dispatch:
permissions: {}
jobs:
  go:
    permissions: { contents: read, pull-requests: read }
    uses: mladenovskistefan111/platform-demo-workflows/.github/workflows/go-ci.yaml@<sha> # v1.0.0
  image:
    needs: go
    permissions: { contents: read, packages: write }
    uses: mladenovskistefan111/platform-demo-workflows/.github/workflows/docker-build.yaml@<sha> # v1.0.0
```

Callers always pin to a **commit SHA** with the version in a trailing comment; Dependabot bumps both.
A reusable workflow can never get more permissions than its caller grants, so each job lists what it needs.

## Release model (trunk-based)

- `main` is the trunk; work happens on short-lived `feat/*`, `fix/*`, `docs/*`, `chore/*` branches merged by squash PR.
- A release is an annotated semver tag on `main`: `git tag -a v1.4.0 -m "..." && git push origin v1.4.0`.
  The tag triggers `release.yaml`: CI gates, build, scan, then promote and sign. Docker Hub gets
  `:1.4.0`, `:sha-<short>` and `:latest`.
- Environments are not branches. They are values files / directories in `platform-demo-helm`
  (Argo CD watches `main`), and promotion is a PR that bumps the image digest for that environment.
- This repo is versioned the same way: immutable `v1.x.y` tags. Callers pin the tag's commit SHA with the version in a trailing comment.
