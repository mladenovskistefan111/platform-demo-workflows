# Things to know

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
