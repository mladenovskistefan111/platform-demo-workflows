# Release model

- `main` is the trunk; work happens on short-lived `feat/*`, `fix/*`, `docs/*`, `chore/*` branches merged by squash PR once `ci / gate` is green.
- **One artifact per commit.** A merge to `main` builds, scans, publishes and signs the image once (`service-publish`) and opens the
  promotion PR for `dev`. The same digest then moves forward and is never rebuilt.
- **Environments are directories** in `platform-demo-helm` (`envs/dev|stage|prod`), Argo CD watches `main`. `dev` is promoted
  automatically (PR with auto-merge after the helm gates). `stage` and `prod` are promoted by a click (`Promote` workflow in the helm
  repo): stage takes the digest that is in dev, prod the one that is in stage (the `Promotion policy` check enforces it), and prod waits
  for the `prod` environment's reviewer. The stage version `X.Y.Z` is typed at the click and recorded in the helm repo as `<app>-vX.Y.Z`.
- **Rollback** is a pull request too (`Rollback` workflow, `gitops-rollback.yaml`) back or forward to a digest that was deployed before;
  it passes the same gates.
