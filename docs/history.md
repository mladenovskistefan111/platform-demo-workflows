# Version history

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
