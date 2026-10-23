# Release model

- `main` is the trunk; work happens on short-lived `feat/*`, `fix/*`, `docs/*`, `chore/*` branches merged by squash PR once `ci / gate` is green. The merge can be done with the `merge` job of the pull request's `CI` run (approve the environment `merge` on the run page) or with the normal GitHub Merge button.
- **One artifact per source tree.** The pull request builds and scans the image (`ghcr.io/<repo>:tree-<hash>`); the merge to `main` reuses it
  when `main`'s tree is the same (builds it once when not), scans the digest again, publishes it to Docker Hub, signs it and opens the
  promotion PR for `dev` (`service-publish`). The same digest then moves forward and is never rebuilt. See [Build once per tree](build-once.md).
- **Environments are directories** in `platform-demo-helm` (`envs/dev|stage|prod`), Argo CD watches `main`. `dev` is promoted
  automatically (PR with auto-merge after the helm gates). `stage` and `prod` are promoted by two clicks **in the same Publish run**
  of the app repository ([One pipeline](one-pipeline.md)): stage takes the digest that is in dev, prod the one that is in stage (the `Promotion policy` check enforces it).
  The stage version `X.Y.Z` is computed (next patch) and recorded in the helm repo as `<app>-vX.Y.Z`; prod takes stage's version.
- **Rollback** is a pull request too (`Rollback` workflow, `gitops-rollback.yaml`) back or forward to a digest that was deployed before;
  it passes the same gates.
- **Tags of this repository** are made with the button: Actions, Release, Run workflow (`release.yaml`), which creates the annotated tag on `main` and prints the commit SHA that callers pin.
