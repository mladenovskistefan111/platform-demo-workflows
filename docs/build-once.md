# Build once per tree

The rule: **never redo the same work, and what was tested is what ships.** Until v3.3 the pull request built the image locally (not
pushed) and scanned it, and the publish after the merge built it again. From v3.4.0 the image is built once per *source tree*.

## The mechanism

The image is named after the content it was built from, not after a commit:

```
ghcr.io/<repo, lower case>:tree-<git tree hash of the checked-out commit>        TREE=$(git rev-parse 'HEAD^{tree}')
```

The pull request is checked out as its merge commit (the PR head merged into `main`); the squash commit that lands on `main` is another
commit but has the **same tree** when `main` did not move in between. So:

| Where | What happens |
|---|---|
| Pull request, `Image: Build + CVE scan` (job `image` of `service-ci-<language>`) | compute `TREE`; if `ghcr.io/<repo>:tree-<TREE>` exists, reuse its digest (no build); else build with the registry cache and push it under that tag; scan **that digest** with Trivy (same version, threshold and ignore file everywhere) |
| Pull request from a fork, or opened by Dependabot | the token cannot write packages: build into the local daemon and scan that, as before. Nothing is pushed, nothing is reused; the publish builds the tree itself after the merge |
| Publish, `Image: Find or build` | compute `TREE` of the main commit; **found** = reuse the digest, no build; **not found** = build it now (cache written) and push as `tree-<TREE>` (the same tag, so a re-run finds it) |
| Publish, `Image: Scan` | always scan the digest again, whether reused or built: new CVEs may have appeared since the pull request |
| Publish, `Publish: Copy, sign, verify` | copy that digest by digest to Docker Hub as `sha-<7 chars of the main commit>` (no `latest`, no semver), cosign sign (keyless), verify; **a Docker Hub digest that differs from the scanned one fails the run**; an existing `sha-<7>` tag with another digest is never moved |

**When the tree is not found:** another merge landed first (the tested merge result differs from what `main` became), the branch was
rebased with other changes, the PR image was pruned (more than `prune-keep-tree` newer `tree-*` versions exist), the pull request was from a fork or
Dependabot, or the lookup itself failed (retried three times, then the image is built: building is always safe because what is built is
what is scanned and shipped). The job summary says `reused` or `built`.

A re-run is cheap and consistent: the build pushed the tag, so the second run finds it.

## What is recorded where

- Job summary of `Image: ...`: tree, reused or built, digest. Job summary of `Publish: Copy, sign, verify`: commit, tree, reused or built,
  digest, Docker Hub tag, signature verified, link to the helm pull requests (the exact pull request is in the `Deploy: Open dev PR` summary).
- OCI labels on the image: `org.opencontainers.image.source`, `org.opencontainers.image.revision` (the commit the image was **built** at:
  a pull request's head commit, or the main commit when the publish built it), `io.platform-demo.tree`, `io.platform-demo.build-run`
  (the run that built it). The commit that was **shipped** is in the cosign certificate and in the `sha-<7>` tag.
- The keyless signature's identity is `service-publish.yaml@<sha>` of this repository.

## Limits worth knowing

- **Trust boundary.** The image that ships can be built by a same-repository pull request's workflow run, which can push any `tree-*` tag
  of the package. Write access to the repository is therefore the trust boundary (forks cannot push). The publish scans and signs what it
  finds; it does not prove who built it. Fine for a solo or small trusted team; revisit before external contributors get write access.
- **The registry build cache** (`ghcr.io/<repo>/buildcache`, mode=max) is written by the publish when it builds and by every pull-request
  run that pushes the image (same repository, not Dependabot), so the next build starts from the newest layers. Forks and Dependabot only
  read it. A run that reuses an existing tree image builds nothing and writes nothing.
- **A third merge replaces the pending one.** The caller's concurrency group `publish-<ref>` runs one publish at a time and keeps only the
  newest waiting run. The replaced run's tree is contained in the newest one, so it is published with it. Exception: if the replaced merge
  changed code and the last one is documentation-only, the last run's `before` is the replaced merge, the diff is docs-only and nothing is
  published. Start `Publish` by hand without `before` (Actions, Publish, Run workflow) to publish the code.
- **Dependabot pull requests** get a read-only token: they build locally, and have no `merge` job; use the normal Merge button.

## Staging package on ghcr.io

`ghcr.io/<repo>` holds `tree-<hash>` versions (and the older `sha-<commit>` versions of v3.3 and before). `Cleanup: Prune staging` keeps
the newest N versions of **each** tag family (`prune-keep`, default 10, for `sha-*`; `prune-keep-tree`, default 20, for `tree-*`: every pull-request push makes a version), deletes the older ones and untagged versions older than seven
days, but never a version with another tag (`latest`, `v1.2.3`), never an untagged manifest that a kept image index points to (the
attestations and platform manifests of a kept image) and never the `buildcache` package (a package name with a slash cannot be addressed).
It runs next to the helm pull request, has `continue-on-error: true` and never delays or fails a publish. Docker Hub is not pruned here.

## The merge button

`ci.yaml` of every app repository ends with a `merge` job (after `ci / gate`, not part of it): a short `uses:` job that calls
`service-merge.yaml`, where the logic lives once. The caller keeps the `if:` (same-repository pull request, not Dependabot) and grants
`contents`, `pull-requests` and `actions: write`; the called job sits in the environment `merge`. The job:

1. runs only for same-repository pull requests, in the environment `merge` (the owner is the required reviewer): on the run page,
   **Review deployments, Approve and deploy**;
2. checks that the run was really approved (a missing environment is created by GitHub without protection and would run the job at once);
3. checks that the pull request is open, targets `main` and still is at the commit that was tested (otherwise: nothing is merged, the new
   push started a new run), warns when `main` moved since the tests started;
4. squash-merges with the workflow token (`gh pr merge --squash --delete-branch --match-head-commit <tested sha>`, title and body of the
   pull request);
5. reads the commit `main` had before the merge (the parent of the squash commit) and starts `Publish` with it as `before`: events created
   with the workflow token start no workflows except `workflow_dispatch`, so the push trigger does not fire for this merge.

The normal GitHub Merge button still works and starts `Publish` by push. If both happened for the same tree, the concurrency group lets one
run after the other and the second finds the tree image (it signs and attests again; nothing else changes).

**Security.** The job runs the workflow files of the pull request (that is how `pull_request` workflows work), but only after a person
approved the environment, with a token that can write contents, pull requests and actions. Approving a pull request that changes
`.github/workflows` therefore means reading that change. Required checks stay what they were: **`ci / gate` only**; the `merge` check
shows as waiting on the pull request until it is approved and is not required.

**Failure modes.**

- Branch protection `required_status_checks` is `ci / gate` only; `merge` must never be added. `enforce_admins: true` is fine (the workflow
  token is not an admin and is not asked to bypass anything). Rules that block an API merge block this one too: required approving reviews
  above zero (the token cannot approve), unresolved conversations, "require branches to be up to date" when the branch is behind. The step
  then fails with "the merge was refused" and the pull request stays open; use the web button or update the branch.
- A documentation-only pull request merges normally; the `Publish` run starts with `before` and its `Guard: Changes` finds only docs and
  skips everything.
- A new push to the pull request cancels the old `CI` run (`cancel-in-progress`), including a waiting approval. Approve the new run.
- The branch moved while the approval was pending: refused, as above. `--match-head-commit` also covers the instant between the check and the merge.
- Merged but Publish could not be started (the dispatch is retried three times): the job is red and the message gives the way out: Actions,
  Publish, Run workflow, `before` = the commit it prints.
- Publish starts, and `publish.yaml` of `main` lacks the `before` input (an app repository not yet on v3.4.0): the dispatch is refused
  and the job is red. Roll the caller out first.

## Settings the owner must create

- In every app repository (the 10 that have a Publish and product-catalog-db, which has only the merge button): a GitHub **environment
  `merge`** with the owner as **required reviewer**; "Prevent self-review" **off** (the owner opens the pull requests); no deployment
  branch restriction (the job runs on the pull request's merge ref).
- The workflow token needs "Read and write permissions" or the explicit `permissions` the job asks for (it asks, so the default can stay
  restricted); the repository setting "Allow GitHub Actions to create and approve pull requests" is not needed.
- The ghcr.io packages need write access for the repository's workflows (they already have it: the publish has pushed there since v3.2).
