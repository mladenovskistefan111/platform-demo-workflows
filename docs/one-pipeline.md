# One pipeline: merge -> dev -> stage -> prod

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

## Where to click

**Repository `app-<name>` -> Actions -> Publish -> the run of your merge -> Review deployments** (yellow banner at the top of the run page)
-> tick the environment (`stage`, later `prod`) -> **Approve and deploy** (or **Reject**). The run summary of every job says what it did
and what comes next.

## Settings each app repository needs (once)

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

## The jobs after `Open dev PR`

| Job | What it does | Ends |
|---|---|---|
| `Wait for dev` | polls the dev pull request (every 15 s, up to 25 min) until `envs/dev` on helm `main` holds **this build's digest**. The pull request is only the signal: its branch is shared by all builds of the app, so "merged" alone does not mean "mine" | green: landed, or *superseded by a newer build*. Red: closed without merging, or not merged in 25 min |
| `Approve stage deployment` | environment `stage`. **After** the click: the approval is verified; dev must **still** hold this build's digest (else *superseded*: nothing is promoted); the version is chosen | green, or red (no approval, tags cannot be listed) |
| `Promote to stage` | the same pinned `gitops-promote.yaml` as `Open dev PR`: env `stage`, this run's digest, the chosen version, auto-merge | |
| `Wait for stage` | as `Wait for dev`; also checks that the merged file records the expected version | |
| `Release stage version` | creates the tag and release `<app>-vX.Y.Z` in `platform-demo-helm` (below). **Best effort**: `continue-on-error`, so a refusal never blocks the prod approval | green; when GitHub refuses: a red step, a warning on the run and the reason in the summary, the run itself stays green |
| `Approve prod deployment` | environment `prod`; same checks after the click, with stage as the previous environment | |
| `Promote to prod` | `gitops-promote.yaml`, env `prod`, this run's digest, **stage's version** | |
| `Wait for prod` | as above; the last line of the chain | |

The digest is the one of **this run**; the registry is never asked for "the newest". The helm repository's promotion policy stays the
safety net: stage may only take dev's digest, prod only stage's, and a promotion pull request changes one environment only.

## The version

Stage: the next **patch** after the highest `<app>-vX.Y.Z` tag of `platform-demo-helm`; none yet: `1.0.0`. The highest version wins (by
number: `1.0.10` is above `1.0.9`), tags that are not plain `X.Y.Z` (`-rc1`, extra dots, look-alikes) are ignored, and a version already
written in `envs/stage/values/<app>.yaml` counts (its tag is made right after the merge). If the stage file already holds this build's
digest with a version, that version is kept. Prod: **the version of stage, unchanged**. These are the rules of `promote.sh` in the helm
repository. The version is chosen **after the stage click**, so it is the latest. If the tags cannot be listed the job fails; it never
guesses `1.0.0`. **A minor or major bump is not part of this pipeline** (no input for it): use the manual **Promote** form of the helm repository,
which takes an explicit version.

## The release `<app>-vX.Y.Z`

`Release stage version` runs after `Wait for stage`, with a short-lived **GitHub App token** limited to `platform-demo-helm` and *Contents:
write* (not the run's own token). It (1) checks that the helm commit really holds this version for this digest, (2) creates the tag on that
commit when it does not exist (**an existing tag is never moved or touched**; one that exists for this digest is fine; one that exists for
another digest fails the job), (3) creates the release (notes: digest, image tag, source commit, helm commit, run link; not marked
*latest*) when it does not exist. Run it twice and the second run changes nothing. The tag and the release are two separate calls, so a
refusal names the call that failed.

**The job is best effort** (`continue-on-error: true`, like the staging prune): if GitHub refuses, the job's result is still *success* for the jobs
after it, so **the prod approval is not blocked**. The failure is visible as a red step, a warning annotation on the run and a summary that says
which call failed and quotes the answer. The version is already recorded in the stage file, so nothing is lost. *"Resource not accessible by integration"* means the token has no
permission (check the App's *Contents: write* and that it is installed on `platform-demo-helm`); *"Repository rule violations"* means a
ruleset refused it: add the App `platform-demo-promote` to the bypass list of the ruleset `protect-app-release-tags` (Settings -> Rules). Fix it and use
**Re-run failed jobs** to make the release (it is idempotent); it does not matter whether the prod click already happened.

## Several builds at once

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

Known and accepted: two stage promotions whose version is chosen within the minutes between a click and the merge of its pull request can pick the
same version; the second `Release stage version` then fails loudly (a warning and a red step: "the tag exists for another image") and the tag is not moved. It needs two clicks
on two different runs within minutes, which the replace-older approval makes unlikely.

**Dev pull request without auto-merge.** `Wait for dev` waits for the dev pull request to merge. If `Publish` is started with `auto-merge: false`
(a manual dispatch input of the shared workflow; the callers never set it), the pull request waits for a person, `Wait for dev` times out after 25
minutes **by design** and the chain does not go on. Merge the pull request and use **Re-run failed jobs**: the wait sees the merge.

## What stops the chain, and what to do

| Message in the summary | Meaning | Do |
|---|---|---|
| *no approval* (approve job red) | the environment has no required reviewer | create/fix the environment `stage` or `prod`, re-run the failed job |
| *the pull request was closed without merging* | someone closed the helm pull request, or a helm check failed and it was closed | read the pull request; start Publish by hand or merge the next change |
| *waited 25 minutes, not merged yet* | helm checks are red or slow | fix the checks; when the pull request merges, **Re-run failed jobs**: the wait sees the merge and the chain goes on |
| *superseded by a newer build* | a newer build took over dev/stage | nothing; approve in the newer run |
| *the version is not the expected one* | the merged file records another version | look at the pull request and the file |
| *creating the tag ... failed* | see the release section | |

## What is tested, and what is not

`tools/test-shell.py` runs every script of the chain against a stub of `gh` and a dictionary that plays platform-demo-helm; `tools/check-consistency.py`
checks names, `needs`, environments, permissions, concurrency groups and the re-check of the supersede rule;
`tools/check-permissions.py` checks the callers (no workflow-level concurrency, all secrets passed, `actions: read`). What they cannot show is
how GitHub itself treats a cancelled waiting approval or an environment without protection; the first real run in a throw-away app repository is the proof.
