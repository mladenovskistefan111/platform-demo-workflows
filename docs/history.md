# Version history

| Tag | Change |
|---|---|
| v1.0.0 | Go CI, Docker build/scan, Docker promote/sign, workflow lint |
| v1.1.0 | Node, Python, Java/Gradle, .NET CI; shellcheck fixes |
| v1.2.0 | stand-alone secret-scan |
| v2.0.0 | all actions to their newest settled release (Node 24), cosign 3, SBOM as attestation, Semgrep via pip (breaking: callers no longer pass `SEMGREP_APP_TOKEN`) |
| v2.0.1 | classic cosign signature format (Docker Hub has no referrers API) |
| v2.0.2 | secret scan runs the open-source **gitleaks CLI** (pinned, SHA-256 checked) instead of `gitleaks-action`, whose license is paid for organisation-owned repositories; the cosign self-verification accepts the workflows repo under both owners (`mladenovskistefan111`, `platform-boutique`) |
