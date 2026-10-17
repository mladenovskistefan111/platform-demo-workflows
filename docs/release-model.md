# Release model

- `main` is the trunk; work happens on short-lived `feat/*`, `fix/*`, `docs/*`, `chore/*` branches merged by squash PR.
- A release is an annotated semver tag on `main`: `git tag -a v1.4.0 -m "..." && git push origin v1.4.0`. The tag triggers
  `release.yaml`: CI gates, build, scan, then promote and sign. Docker Hub gets `:1.4.0`, `:sha-<short>` and `:latest`.
  A manual `Release` run with `publish=true` publishes only `:sha-<short>` (no `latest`).
- Environments are not branches. They are directories in `platform-demo-helm` (`envs/<env>/`), Argo CD watches `main`, and
  promotion is a PR that bumps the image digest for that environment.
