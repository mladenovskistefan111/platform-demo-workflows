#!/usr/bin/env python3
"""Generate service-publish.yaml: the merge-to-main pipeline of an app repository, ONE flat workflow.

usage: gen-service-publish.py [repo-root]      (writes <repo-root>/.github/workflows/service-publish.yaml)

    Detect changes -> Find or build image -> Scan image -> Publish image -+-> Create SBOM
                                                                          +-> Clean up staging images
                                                                          +-> Open dev PR -> Wait for dev
                                                                                -> Approve stage deployment -> Promote to stage -> Wait for stage
                                                                                -> Release stage version
                                                                                -> Approve prod deployment -> Promote to prod -> Wait for prod

Since v3.6 the run does not end at the dev pull request: it goes on, in the same run, with the promotion to stage and prod. The two
approvals are jobs in the GitHub environments `stage` and `prod` of the app repository ("Review deployments" on the run page).

Run summaries (GITHUB_STEP_SUMMARY, one section per job on the run page): `Detect changes` what changed (changed-paths.yaml),
`Find or build image` reused or built + digest (tools/wfgen.py), `Scan image` the Trivy result, `Publish image` the answer to "what was
published": tag, commit, digest, signature, where the dev change goes; `Create SBOM`; `Open dev PR` (gitops-promote.yaml's own section
with the pull request link); `Clean up staging images` kept / deleted counts; and one section per chain job (what it waits for, the helm
pull request, old -> new digest, the version, "superseded by a newer build", the last line of the chain).

Why generated and flat: until v3.3 the publish workflow called docker-build, docker-promote and ghcr-prune (nested boxes in the
Actions graph, each with its own job inside) and had to be released in two steps whenever one of them changed. Now every job is in
this one file; the only nested workflow is gitops-promote.yaml, which the helm repository shares and which is unchanged.

Source of truth (extracted as text, never retyped, every transformation asserts that it matched):
  * the change detection is changed-paths.yaml:detect (plus the `before` input);
  * checkout, the main-reachability check, the registry logins, buildx, cosign install / sign / verify and the SBOM steps are
    docker-promote.yaml's steps;
  * the prune script is ghcr-prune.yaml's (plus the `tree-*` tag family and no concurrency block: the caller's `publish-<ref>`
    group already runs one publish at a time);
  * the tree lookup, the build, the digest record and the Trivy scan are tools/wfgen.py's, the same text as in the pull-request
    workflows' image job.
What is written here: the copy to Docker Hub (digest-checked, a mismatch is a hard failure), the summary, the job wiring.
tools/check-consistency.py fails when the committed file differs from this output.
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(HERE)
sys.path.insert(0, HERE)
import wfgen  # noqa: E402
from wfgen import Job, render_job, script, sub  # noqa: E402

wfgen.set_root(ROOT)
OUT = "service-publish.yaml"
GITOPS_PIN = "platform-boutique/platform-demo-workflows/.github/workflows/gitops-promote.yaml@341f641320553ad73969b637ff125466e6b2d99c # v3.1.0"
# Job names: sentence case, what the job does, no group prefixes. In the caller's run GitHub shows them as `publish / <name>`.
NAMES = {"changes": "Detect changes", "image": "Find or build image", "scan": "Scan image", "publish": "Publish image",
         "sbom": "Create SBOM", "deploy": "Open dev PR", "prune": "Clean up staging images",
         "wait-dev": "Wait for dev", "approve-stage": "Approve stage deployment", "stage": "Promote to stage",
         "wait-stage": "Wait for stage", "release": "Release stage version", "approve-prod": "Approve prod deployment",
         "prod": "Promote to prod", "wait-prod": "Wait for prod"}


def name_of(key):
    return NAMES[key]


# ───────────────────────── Detect changes ─────────────────────────

def changes_job():
    j = Job("changed-paths.yaml", "detect")
    src = j.src
    compare = j.step("Compare with the base")
    # `before` (workflow_dispatch input): a manual run diffs before..HEAD like a push does; without it a manual run is code.
    compare = sub(compare, "          BEFORE_SHA: ${{ github.event.before }}\n",
                  "          BEFORE_SHA: ${{ inputs.before || github.event.before }}\n", src)
    compare = sub(compare, "          is_docs() {\n",
                  "          if [ -n \"$BEFORE_SHA\" ] && ! [[ \"$BEFORE_SHA\" =~ ^[0-9a-f]{40}$ ]]; then\n"
                  "            echo \"::error::before must be a full 40-character commit SHA (got '$BEFORE_SHA')\"\n"
                  "            exit 1\n"
                  "          fi\n\n"
                  "          is_docs() {\n", src)
    compare = sub(compare, "            push)\n              base=\"\"\n",
                  "            push | workflow_dispatch)\n"
                  "              [ \"$EVENT_NAME\" = push ] || [ -n \"$BEFORE_SHA\" ] || decide true \"manual run without before: everything runs\"\n"
                  "              base=\"\"\n", src)
    compare = sub(compare, "              [ -n \"$base\" ] || { git rev-parse --verify --quiet HEAD^1",
                  "              if [ -z \"$base\" ] && [ \"$EVENT_NAME\" = workflow_dispatch ]; then\n"
                  "                decide true \"the commit given as before is not available; assuming code\"\n"
                  "              fi\n"
                  "              [ -n \"$base\" ] || { git rev-parse --verify --quiet HEAD^1", src)
    compare = sub(compare, '          skipped="Tests, scans and the image build are skipped."\n',
                  '          skipped="Nothing is published."\n', src)
    return render_job("changes", name_of("changes"), [j.steps[0], compare], timeout=j.timeout(), perms=j.raw("permissions"),
                      outputs=j.raw("outputs"))


# ───────────────────────── Find or build image, Scan image ─────────────────────────

def image_job():
    steps = [
        wfgen.checkout_step(),
        wfgen.buildx_step(),
        wfgen.login_step("Log in to ghcr.io"),
        wfgen.cache_step(),
        wfgen.find_step("true"),
        wfgen.build_step(push="true", cache_write=True),
        wfgen.record_step("true"),
        wfgen.summary_step(scan=False),
    ]
    outputs = ("    outputs:\n"
               "      tree: ${{ steps.find.outputs.tree }}\n"
               "      digest: ${{ steps.image.outputs.digest }}\n"
               "      ref: ${{ steps.image.outputs.ref }}\n"
               "      how: ${{ steps.image.outputs.how }} # reused | built")
    perms = "    permissions:\n      contents: read\n      packages: write # pushes ghcr.io/<repo>:tree-<hash> when it has to build"
    return render_job("image", name_of("image"), steps, timeout=45, perms=perms, needs=["changes"], outputs=outputs,
                      # only commits on main are published (the publish job checks again from the git history)
                      cond="${{ needs.changes.outputs.code == 'true' && github.ref == 'refs/heads/main' }}",
                      # one build of this repository at a time (the run is no longer serialised end to end: it may wait days for a click).
                      # A second run waits here; of three quick merges the waiting second is replaced by the third.
                      extra=["    concurrency:\n      group: publish-build-${{ inputs.app }}\n      cancel-in-progress: false"])


SCAN_SUMMARY_SCRIPT = r"""
set -euo pipefail
{
  echo "### Image scan"
  echo
  case "$SCAN" in
    success) echo "Trivy found no HIGH or CRITICAL vulnerabilities in \`$DIGEST\`." ;;
    failure) echo "**Trivy failed** on \`$DIGEST\`: HIGH or CRITICAL vulnerabilities, or the scan could not run. The table is in the log of the step \"Run Trivy vulnerability scanner\". Nothing is published." ;;
    *) echo "Trivy did not run." ;;
  esac
} >> "$GITHUB_STEP_SUMMARY"
"""


def scan_job():
    first, rest = wfgen.checkout_step().split("\n", 1)
    steps = [
        # only the ignore file is needed from the checkout
        f"{first}\n        if: inputs.trivyignores != ''\n{rest}",
        wfgen.login_step("Log in to ghcr.io"),
        wfgen.trivy_step("${{ needs.image.outputs.ref }}",
                         "same version, threshold and ignore file as the image job of service-ci-<language>; new CVEs may have appeared since the pull request"),
        ("      - name: Write the run summary\n"
         "        if: ${{ !cancelled() }}\n"
         "        env:\n"
         "          DIGEST: ${{ needs.image.outputs.digest }}\n"
         "          SCAN: ${{ steps.trivy.outcome }}\n"
         "        run: |\n" + script(SCAN_SUMMARY_SCRIPT)),
    ]
    perms = "    permissions:\n      contents: read\n      packages: read"
    return render_job("scan", name_of("scan"), steps, timeout=15, perms=perms, needs=["image"])


# ───────────────────────── Publish image ─────────────────────────

COPY_SCRIPT = r"""
set -euo pipefail
tag="sha-${GITHUB_SHA::7}"
hub="$HUB_REPO:$tag"
raw="$RUNNER_TEMP/hub-manifest.raw"

if ! [[ "$SOURCE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "::error::the scanned digest '$SOURCE_DIGEST' is not sha256:<64 hex>"
  exit 1
fi

# Prints the digest $1 points to. Returns 3 when the tag does not exist (or the repository does not exist yet), 1 when it could not
# be read after 5 attempts.
hub_digest() {
  local ref=$1 attempt
  for attempt in 1 2 3 4 5; do
    if docker buildx imagetools inspect --raw "$ref" > "$raw" 2> "$raw.err" && [ -s "$raw" ]; then
      echo "sha256:$(sha256sum "$raw" | cut -d' ' -f1)"
      return 0
    fi
    if grep -qiE 'not found|manifest_unknown|name_unknown|404|denied|does not exist' "$raw.err"; then
      return 3
    fi
    echo "::warning::reading $ref failed (attempt $attempt of 5); retrying" >&2
    sleep $((attempt * 5))
  done
  return 1
}

# A published tag is never moved: sha-<commit> names one digest for good (a re-run of this commit finds the same tree image).
rc=0
existing=$(hub_digest "$hub") || rc=$?
if [ "$rc" = 0 ]; then
  if [ "$existing" != "$SOURCE_DIGEST" ]; then
    echo "::error::$tag already exists on Docker Hub with digest $existing; the image to publish is $SOURCE_DIGEST. A published tag is never moved."
    exit 1
  fi
  echo "$tag already points to $SOURCE_DIGEST (a re-run); copying again changes nothing"
elif [ "$rc" = 1 ]; then
  echo "::warning::could not check whether $tag exists on Docker Hub; the read-back below still verifies the digest"
fi

docker buildx imagetools create --tag "$hub" "$SOURCE"

# What is on Docker Hub must be the image Trivy scanned, byte for byte: anything else is a hard failure, nothing gets signed.
rc=0
digest=$(hub_digest "$hub") || rc=$?
if [ "$rc" != 0 ]; then
  echo "::error::could not read $tag from Docker Hub after the copy"
  exit 1
fi
if [ "$digest" != "$SOURCE_DIGEST" ]; then
  echo "::error::Docker Hub has $digest for $tag but the scanned ghcr.io image is $SOURCE_DIGEST; refusing to sign an image that was not scanned"
  exit 1
fi
{
  echo "ref=${HUB_REPO}@${digest}"
  echo "digest=${digest}"
  echo "tag=${tag}"
} >> "$GITHUB_OUTPUT"
"""

SUMMARY_SCRIPT = r"""
set -euo pipefail
short=${GITHUB_SHA:0:7}
# The names come from the caller's file; they are only printed (and made into a link) when they are plain names.
helm="the pull request in platform-demo-helm that moves the environment to this digest"
if [[ "$APP" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] && [[ "$TARGET_ENV" =~ ^[a-z][a-z0-9-]*$ ]]; then
  helm="[the pull request in platform-demo-helm that moves \`$TARGET_ENV\` of \`$APP\` to this digest]($HELM_URL/pulls?q=is%3Apr+head%3Apromote%2F$APP%2F$TARGET_ENV), opened by the \`Open dev PR\` job (its summary links the exact one)"
fi
{
  echo "### Published \`$TAG\`"
  echo
  echo "| | |"
  echo "|---|---|"
  echo "| Commit | [\`$short\`]($GITHUB_SERVER_URL/$GITHUB_REPOSITORY/commit/$GITHUB_SHA) |"
  echo "| Docker Hub tag | \`$TAG\` |"
  echo "| Digest | \`$DIGEST\` |"
  case "$HOW" in
    reused) echo "| Image | reused: built by the pull request from source tree \`$TREE\`, not built again |" ;;
    *) echo "| Image | built in this run from source tree \`$TREE\` (no image of this tree existed) |" ;;
  esac
  echo "| Signature | verified (keyless cosign; the certificate names this workflow and commit \`$short\`) |"
  echo "| Dev change | $helm |"
} >> "$GITHUB_STEP_SUMMARY"
"""


def publish_job():
    d = Job("docker-promote.yaml", "promote")
    checkout = d.steps[0]
    assert "actions/checkout@" in checkout, d.src
    copy = ("      - name: Copy the scanned digest to Docker Hub (no rebuild)\n"
            "        id: promote\n"
            "        env:\n"
            "          SOURCE: ${{ needs.image.outputs.ref }}\n"
            "          SOURCE_DIGEST: ${{ needs.image.outputs.digest }}\n"
            "          HUB_REPO: ${{ secrets.DOCKERHUB_USERNAME }}/${{ inputs.image-name || inputs.app }}\n"
            "        run: |\n" + script(COPY_SCRIPT))
    summary = ("      - name: Write the run summary\n"
               "        env:\n"
               "          TREE: ${{ needs.image.outputs.tree }}\n"
               "          HOW: ${{ needs.image.outputs.how }}\n"
               "          DIGEST: ${{ steps.promote.outputs.digest }}\n"
               "          TAG: ${{ steps.promote.outputs.tag }}\n"
               "          APP: ${{ inputs.app }}\n"
               "          TARGET_ENV: ${{ inputs.env }}\n"
               "          HELM_URL: https://github.com/platform-boutique/platform-demo-helm\n"
               "        run: |\n" + script(SUMMARY_SCRIPT))
    steps = [
        checkout,
        d.step("Publishing is only allowed from commits on main"),
        d.step("Log in to ghcr.io"),
        d.step("Log in to Docker Hub"),
        d.step("Set up Docker Buildx"),
        copy,
        d.step("Install Cosign"),
        d.step("Sign image"),
        d.step("Verify the signature we just made"),
        summary,
        ("      - name: Check that dev does not already run a newer commit\n"
         "        id: dev-guard\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          APP: ${{ inputs.app }}\n"
         "          TARGET_ENV: ${{ inputs.env }}\n"
         "          DIGEST: ${{ steps.promote.outputs.digest }}\n"
         "        run: |\n" + script(DEV_GUARD_SCRIPT)),
    ]
    outputs = ("    outputs:\n"
               "      digest: ${{ steps.promote.outputs.digest }}\n"
               "      tag: ${{ steps.promote.outputs.tag }}\n"
               "      # 'true': dev already runs a newer commit, so `Open dev PR` is skipped (see the step)\n"
               "      stale: ${{ steps.dev-guard.outputs.stale }}\n"
               "      # no image reference here: it contains the Docker Hub account name, a secret, and GitHub drops outputs that contain secrets")
    perms = ("    permissions:\n      id-token: write # keyless cosign signing\n      contents: read\n"
             "      packages: read")
    return render_job("publish", name_of("publish"), steps, timeout=d.timeout(), perms=perms, needs=["image", "scan"], outputs=outputs)


# ───────────────────────── Create SBOM ─────────────────────────

SBOM_SUMMARY_SCRIPT = r"""
{
  echo "### SBOM"
  echo
  echo "An SPDX SBOM is attached to \`$DIGEST\` as a signed attestation, and the attestation was verified."
} >> "$GITHUB_STEP_SUMMARY"
"""


def sbom_job():
    d = Job("docker-promote.yaml", "promote")
    ref = ("      - name: Name the published image\n"
           "        id: published\n"
           "        env:\n"
           "          HUB_REPO: ${{ secrets.DOCKERHUB_USERNAME }}/${{ inputs.image-name || inputs.app }}\n"
           "          DIGEST: ${{ needs.publish.outputs.digest }}\n"
           "        run: |\n"
           '          echo "ref=${HUB_REPO}@${DIGEST}" >> "$GITHUB_OUTPUT"')
    moved = []
    for title in ("Generate SBOM", "Attach SBOM to image as a signed attestation", "Verify the SBOM attestation we just made"):
        moved.append(sub(d.step(title), "steps.promote.outputs.ref", "steps.published.outputs.ref", f"{d.src}: {title}", count=1))
    summary = ("      - name: Write the run summary\n"
               "        env:\n"
               "          DIGEST: ${{ needs.publish.outputs.digest }}\n"
               "        run: |\n" + script(SBOM_SUMMARY_SCRIPT))
    steps = [d.step("Log in to Docker Hub"), ref, d.step("Install Cosign")] + moved + [summary]
    perms = "    permissions:\n      id-token: write # keyless cosign attestation\n      contents: read"
    return render_job("sbom", name_of("sbom"), steps, timeout=15, perms=perms, needs=["publish"])


# ───────────────────────── Open dev PR ─────────────────────────

def deploy_job():
    return f"""\
  deploy:
    name: "{name_of('deploy')}"
    needs: [publish]
    if: ${{{{ needs.publish.outputs.stale != 'true' }}}}
    # the nested workflow: gitops-promote.yaml is shared with the helm repository and unchanged (works with the GitHub App token, not this one).
    # `Promote to stage` and `Promote to prod` call the same pinned file. It serialises itself per app and environment (its own concurrency group).
    permissions: {{}}
    uses: {GITOPS_PIN}
    with:
      app: ${{{{ inputs.app }}}}
      env: ${{{{ inputs.env }}}}
      image-tag: ${{{{ needs.publish.outputs.tag }}}}
      image-digest: ${{{{ needs.publish.outputs.digest }}}}
      source-repository: ${{{{ github.repository }}}}
      source-sha: ${{{{ github.sha }}}}
      version: ${{{{ inputs.version }}}}
      auto-merge: ${{{{ inputs.auto-merge }}}}
    secrets:
      PROMOTE_APP_ID: ${{{{ secrets.PROMOTE_APP_ID }}}}
      PROMOTE_APP_PRIVATE_KEY: ${{{{ secrets.PROMOTE_APP_PRIVATE_KEY }}}}
"""


# ───────────────────────── Clean up staging images ─────────────────────────

PRUNE_SUMMARY = '''
def plain(text):                             # tags come from the registry: plain characters only inside the table
    return re.sub(r"[^A-Za-z0-9._,:@/ +-]", "?", str(text))


with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as s:
    s.write(f"### Clean up staging images{' (dry run)' if dry_run else ''}\\n\\n")
    handled = len(todo) if dry_run else deleted
    s.write(f"`ghcr.io/{owner}/{package}` had {len(versions)} versions; {'would delete' if dry_run else 'deleted'} {handled}, "
            f"{len(versions) - handled} {'would remain' if dry_run else 'remain'}"
            f"{'; ' + str(failures) + ' deletion(s) failed, see the log' if failures else ''}"
            f"{'; more qualify, the next run continues' if truncated else ''}.\\n")
    if todo:
        s.write(f"\\n<details><summary>{'Versions that would be deleted' if dry_run else 'Versions selected for deletion'} ({len(todo)})</summary>\\n\\n")
        s.write("| Version | Digest | Created | Tags | Reason |\\n|---|---|---|---|---|\\n")
        for v, reason in todo:
            s.write(f"| {v['id']} | `{plain(v['name'][:19])}` | {v['_created']:%Y-%m-%d} | {plain(','.join(v['_tags']) or '-')} | {reason} |\\n")
        s.write("\\n</details>\\n")
'''

def prune_job():
    j = Job("ghcr-prune.yaml", "prune")
    step = j.step("Select and delete")
    src = j.src + " Select and delete"

    def ind(text):  # the python lives in a heredoc indented by 10
        return "\n".join(("          " + l) if l else "" for l in text.strip("\n").split("\n"))

    step = sub(step, ind('''
def sha_tagged(v):
    return any(t.startswith("sha-") for t in v["_tags"])


def protected(v):                           # a tag that is not sha-*: latest, v1.2.3, ...
    return any(not t.startswith("sha-") for t in v["_tags"])


ranked = [v for v in versions if sha_tagged(v)]
kept = ranked[:keep]
old_tagged = ranked[keep:]
anchors = kept + [v for v in versions if protected(v) and v not in kept]   # their manifests must stay complete
'''), ind('''
FAMILIES = ("sha-", "tree-")                # the tags our images carry on ghcr.io: sha-<commit> (before v3.4) and tree-<hash>


def protected(v):                           # a tag outside the families: latest, v1.2.3, ...
    return any(not t.startswith(FAMILIES) for t in v["_tags"])


KEEP_OF = {"sha-": keep, "tree-": keep_tree}  # every pull-request push makes a tree- version, so that family keeps more
kept, old_tagged = [], []                   # the newest N of EACH family stay; a version carrying both tags stays if either keeps it
for prefix in FAMILIES:
    ranked = [v for v in versions if any(t.startswith(prefix) for t in v["_tags"])]
    kept += [v for v in ranked[:KEEP_OF[prefix]] if v not in kept]
    old_tagged += [v for v in ranked[KEEP_OF[prefix]:] if v not in old_tagged]
old_tagged = [v for v in old_tagged if v not in kept]
anchors = kept + [v for v in versions if protected(v) and v not in kept]   # their manifests must stay complete
'''), src)
    step = sub(step, "{len(kept)} kept sha-* (newest {keep}), ", "{len(kept)} kept (newest {keep} sha-*, newest {keep_tree} tree-*), ", src)
    step = sub(step, '              keep = int(float(os.environ["KEEP"]))\n',
               '              keep = int(float(os.environ["KEEP"]))\n              keep_tree = int(float(os.environ["KEEP_TREE"]))\n', src)
    step = sub(step, "if keep < 1 or untagged_days < 1:", "if keep < 1 or keep_tree < 1 or untagged_days < 1:", src)
    step = sub(step, 'die("keep and untagged-days must be numbers")', 'die("keep, keep-tree and untagged-days must be numbers")', src)
    step = sub(step, 'die("keep and untagged-days must be at least 1")', 'die("keep, keep-tree and untagged-days must be at least 1")', src)
    step = sub(step, 'die("no sha-* tagged version to keep; refusing to delete anything (is the tag format still sha-<commit>?)")',
               'die("no sha-* or tree-* tagged version to keep; refusing to delete anything (did the tag format change?)")', src)
    step = sub(step, 'f"older than the newest {keep} sha-* versions"', 'f"older than the newest of its tag family (keeping {keep} sha-*, {keep_tree} tree-*)"', src)
    # the run summary: counts first, the table of what was handled folded away, registry text reduced to plain characters
    step = sub(step, ind('''
failures = 0
for v, reason in todo:
'''), ind('''
failures = 0
deleted = 0
for v, reason in todo:
'''), src)
    step = sub(step, ind('''
    r = gh("api", "-X", "DELETE", f"{api}/{v['id']}", check=False)
    if r.returncode != 0:
'''), ind('''
    r = gh("api", "-X", "DELETE", f"{api}/{v['id']}", check=False)
    if r.returncode == 0:
        deleted += 1
    if r.returncode != 0:
'''), src)
    step = sub(step, ind('''
with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as s:
    s.write(f"### ghcr.io/{owner}/{package} {'(dry run)' if dry_run else ''}\\n\\n")
    s.write(f"{len(versions)} versions; {len(kept)} newest sha-* kept; {len(todo)} {'would be deleted' if dry_run else 'deleted'}"
            f"{' (limit reached, the next run continues)' if truncated else ''}.\\n\\n")
    if todo:
        s.write("| Version | Digest | Created | Tags | Reason |\\n|---|---|---|---|---|\\n")
        for v, reason in todo:
            s.write(f"| {v['id']} | `{v['name'][:19]}` | {v['_created']:%Y-%m-%d} | {','.join(v['_tags']) or '-'} | {reason} |\\n")
'''), ind(PRUNE_SUMMARY), src)
    env = j.raw("env")
    env = sub(env, "      PACKAGE: ${{ inputs.package }}", "      PACKAGE: ${{ github.event.repository.name }}", j.src)
    env = sub(env, "      KEEP: ${{ inputs.keep }}", "      KEEP: ${{ inputs.prune-keep }}\n      KEEP_TREE: ${{ inputs.prune-keep-tree }}", j.src)
    env = sub(env, "      UNTAGGED_DAYS: ${{ inputs.untagged-days }}", "      UNTAGGED_DAYS: 7", j.src)
    env = sub(env, "      DRY_RUN: ${{ inputs.dry-run }}", '      DRY_RUN: "false"', j.src)
    assert j.raw("permissions").split("\n")[:3] == ["    permissions:", "      packages: write", "      contents: read"], j.raw("permissions")
    perms = "    permissions:\n      contents: read\n      packages: write # deletes old versions of ghcr.io/<repo>; never the buildcache package"
    return render_job("prune", name_of("prune"), [step], timeout=j.timeout(), perms=perms, needs=["publish"],
                      cond="${{ inputs.prune }}",
                      # best effort: a failed prune is a notice on the run, never a red publish, and it runs next to the helm pull request
                      extra=["    continue-on-error: true",
                             # two prunes of one package never run together (a pending one is replaced by a newer one)
                             "    concurrency:\n      group: publish-prune-${{ inputs.app }}\n      cancel-in-progress: false"], env=env)


# ───────────────────────── One pipeline: dev -> stage -> prod ─────────────────────────
# After `Open dev PR` the run goes on as a straight line. Every step of it is a job of this file:
#
#   Wait for dev -> Approve stage deployment -> Promote to stage -> Wait for stage -> Release stage version
#                -> Approve prod deployment -> Promote to prod -> Wait for prod
#
# The two approvals are jobs in the GitHub environments `stage` and `prod` of the APP repository (required reviewer = the owner):
# the click is "Review deployments" on the run page of this very run. The jobs before the click only wait; the checks that matter
# run AFTER the click (a click may come days later), in the approval job itself.

# The shell every chain job shares, written once. The three awk functions are the ones of platform-demo-helm/.github/scripts/
# promotion-policy.sh (img_get / img_version): the same parsing of a values file as the helm repository's own checks.
CHAIN_PRE = "set -euo pipefail\n"

CHAIN_LIB = r"""
HELM=platform-boutique/platform-demo-helm
# shellcheck disable=SC2034
HELM_URL=https://github.com/$HELM
# shellcheck disable=SC2034
SEMVER='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
DIGEST_RE='^sha256:[0-9a-f]{64}$'
APP_RE='^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'

die() { echo "::error::$1"; exit 1; }

# The value of image.<key> (tag | digest) of a values file on stdin, quotes removed (promotion-policy.sh: img_get).
img_get() {
  awk -v key="$1" '
    /^image:[ \t]*(#.*)?$/ { inimg = 1; ind = -1; next }
    inimg {
      if ($0 ~ /^[ \t]*(#.*)?$/) next
      match($0, /^[ \t]*/); n = RLENGTH
      if (n == 0) { inimg = 0; next }
      if (ind < 0) ind = n
      if (n == ind && $0 ~ ("^[ \t]+" key ":")) {
        v = $0; sub("^[ \t]+" key ":[ \t]*", "", v); sub(/[ \t]+#.*$/, "", v); sub(/[ \t]+$/, "", v); print v; exit
      }
    }' | tr -d "\"'"
}
img_digest() { img_get digest; }
# The `# vX.Y.Z` comment on the image.digest line (promotion-policy.sh: img_version).
img_version() {
  awk '
    /^image:[ \t]*(#.*)?$/ { inimg = 1; ind = -1; next }
    inimg {
      if ($0 ~ /^[ \t]*(#.*)?$/) next
      match($0, /^[ \t]*/); n = RLENGTH
      if (n == 0) { inimg = 0; next }
      if (ind < 0) ind = n
      if (n == ind && $0 ~ /^[ \t]+digest:/ && match($0, /#[ \t]*v[0-9]+\.[0-9]+\.[0-9]+[ \t]*$/)) {
        v = substr($0, RSTART, RLENGTH); gsub(/[# \tv]/, "", v); print v; exit
      }
    }'
}

# envs/<env>/values/$APP.yaml of platform-demo-helm at a ref (default main). The repository is public: any token reads it.
# Returns 44 when the file does not exist, 1 when GitHub could not be asked.
values_file() {
  local out rc=0
  out=$(gh api -H 'Accept: application/vnd.github.raw' "repos/$HELM/contents/envs/$1/values/$APP.yaml?ref=${2:-main}" 2>&1) || rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '%s\n' "$out"
    return 0
  fi
  if grep -q 'HTTP 404' <<< "$out"; then return 44; fi
  echo "::warning::could not read envs/$1/values/$APP.yaml at ${2:-main}: $(head -c 200 <<< "$out" | tr '\n' ' ')" >&2
  return 1
}
# The same, asking up to three times when GitHub itself fails (a missing file is not retried).
values_retry() {
  local attempt rc=0
  for attempt in 1 2 3; do
    rc=0
    values_file "$@" && return 0 || rc=$?
    [ "$rc" -eq 1 ] || return "$rc"
    sleep $((attempt * 3))
  done
  return 1
}
"""

WAIT_SCRIPT = CHAIN_PRE + CHAIN_LIB + r"""
# What the job waits for: the pull request of this build in platform-demo-helm to be merged, so that envs/$TARGET_ENV on main holds
# this build's digest. The pull request is only the signal. The truth is the values file: the pull request branch is shared by
# every build of the app (a newer build REPLACES the content of an open one), so "merged" alone does not mean "this build".
#   landed      this build's digest was merged into the environment (now, or it already was there)
#   current     ... and main still has it (no newer build or rollback moved the environment since)
title=${TARGET_ENV^}
interval=${POLL_SECONDS:-15}
summary() { cat >> "$GITHUB_STEP_SUMMARY"; }
stop() { # $1 = headline, $2 = what happened and what to do: the chain stops here
  echo "::error::$2"
  { echo "### $title: $1"; echo; echo "$2"; } | summary
  exit 1
}

[[ "$APP" =~ $APP_RE ]] || die "app '$APP' is not a chart name"
case "$TARGET_ENV" in dev | stage | prod) ;; *) die "env must be dev, stage or prod (got '$TARGET_ENV')" ;; esac
[[ "$DIGEST" =~ $DIGEST_RE ]] || die "the digest must be sha256:<64 hex> (got '$DIGEST')"
[[ "$PR_NUMBER" =~ ^[0-9]*$ ]] || die "pull request number '$PR_NUMBER' is not a number"
if ! [[ "$WAIT_MINUTES" =~ ^[0-9]+$ ]] || [ "$WAIT_MINUTES" -lt 1 ] || [ "$WAIT_MINUTES" -gt 30 ]; then die "wait-minutes must be 1 to 30 (got '$WAIT_MINUTES')"; fi
if [ -n "$EXPECT_VERSION" ] && ! [[ "$EXPECT_VERSION" =~ $SEMVER ]]; then die "version '$EXPECT_VERSION' is not X.Y.Z"; fi
file="envs/$TARGET_ENV/values/$APP.yaml"
pr_url=""
[ -z "$PR_NUMBER" ] || pr_url="$HELM_URL/pull/$PR_NUMBER"
tries=$((WAIT_MINUTES * 60 / interval))

# digest_now: the digest main has now in $cur (empty when unreadable); stops the job when the file does not exist
digest_now() {
  local rc=0
  cur=$(values_file "$TARGET_ENV" | img_digest) || rc=$? # pipefail: rc is values_file's (44 = no such file, 1 = GitHub failed)
  if [ "$rc" -eq 44 ]; then
    stop "$file does not exist" "$file does not exist in platform-demo-helm, so there is nothing to wait for. Create it first (copy it from the previous environment) in a normal pull request."
  fi
  if [ "$rc" -ne 0 ]; then cur=""; fi
  return 0
}

state=""; how=""; sha=""; cur=""; merged=""
for ((i = 1; i <= tries; i++)); do
  if [ -z "$PR_NUMBER" ]; then
    # No pull request was opened for this build: gitops-promote found nothing to change, or an older build did not replace a newer open promotion.
    digest_now
    if [ "$cur" = "$DIGEST" ]; then
      state=landed; how=present
      break
    fi
    if [ -n "$cur" ]; then
      state=superseded; how=no-pr
      break
    fi
    sleep "$interval"
    continue
  fi
  rc=0
  pr=$(gh api "repos/$HELM/pulls/$PR_NUMBER") || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "::warning::could not read pull request #$PR_NUMBER (attempt $i of $tries)"
    sleep "$interval"
    continue
  fi
  if [ "$(jq -r .merged <<< "$pr")" = true ]; then
    sha=$(jq -r '.merge_commit_sha // ""' <<< "$pr")
    rc=0
    merged=$(values_retry "$TARGET_ENV" "${sha:-main}" | img_digest) || rc=$?
    [ "$rc" -eq 0 ] || stop "could not read what was merged" "Pull request #$PR_NUMBER ($pr_url) is merged, but $file could not be read from GitHub. Re-run the failed jobs of this run."
    if [ "$merged" = "$DIGEST" ]; then state=landed; how=merged; else state=superseded; how=replaced; fi
    break
  fi
  if [ "$(jq -r .state <<< "$pr")" = closed ]; then
    digest_now
    if [ "$cur" = "$DIGEST" ]; then
      state=landed; how=present # closed, but the environment has this build anyway (another pull request carried it)
      break
    fi
    stop "the pull request was closed without merging" "Pull request #$PR_NUMBER ($pr_url) was closed without being merged, and $TARGET_ENV does not have this build ($DIGEST). Nothing was changed in $TARGET_ENV. Look at the pull request for the reason (a failed check, or someone closed it). To try again, start Publish for this commit by hand (Actions, Publish, Run workflow) or merge the next change."
  fi
  echo "waiting ($i of $tries): pull request #$PR_NUMBER is open; it merges itself when the helm checks pass"
  sleep "$interval"
done

if [ -z "$state" ] && [ -n "$pr_url" ] && [ "$TARGET_ENV" != dev ]; then
  # Not merged in time. The helm checks refuse a promotion whose digest is no longer the one of the previous environment (the promotion
  # policy), so when a newer build moved the previous environment meanwhile, this pull request can never merge: that is "superseded".
  case "$TARGET_ENV" in stage) prev=dev ;; *) prev=stage ;; esac
  rc=0
  prev_digest=$(values_retry "$prev" | img_digest) || rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$prev_digest" ] && [ "$prev_digest" != "$DIGEST" ]; then
    state=superseded; how=stalled
  fi
fi

if [ -z "$state" ]; then
  if [ -n "$pr_url" ]; then
    stop "waited $WAIT_MINUTES minutes, not merged yet" "Pull request #$PR_NUMBER ($pr_url) is still open after $WAIT_MINUTES minutes. It merges itself when the helm checks pass; look at its checks. If it merges later, use Re-run failed jobs on this run: this job then sees the merge and the chain goes on."
  fi
  stop "waited $WAIT_MINUTES minutes, nothing to wait for" "No pull request was opened for this build and $TARGET_ENV does not have its digest ($DIGEST). Look at the job before this one."
fi

landed=false; current=false; version=""
if [ "$state" = landed ]; then
  landed=true; current=true
  if [ -z "$sha" ]; then
    sha=$(gh api "repos/$HELM/commits?path=$file&sha=main&per_page=1" --jq '.[0].sha // empty') || sha=""
  fi
  if [ "$TARGET_ENV" != dev ]; then
    rc=0
    version=$(values_retry "$TARGET_ENV" "${sha:-main}" | img_version) || rc=$?
    [ "$rc" -eq 0 ] || stop "could not read the version" "$file could not be read from GitHub. Re-run the failed jobs of this run."
    if [ -n "$EXPECT_VERSION" ] && [ "$version" != "$EXPECT_VERSION" ]; then
      stop "the version is not the expected one" "$file records the version '${version:-none}' for this digest, the pipeline expected '$EXPECT_VERSION'. Nothing more is promoted. Check $pr_url and the file."
    fi
  fi
  if [ "$how" = merged ]; then
    rc=0
    now=$(values_retry "$TARGET_ENV" | img_digest) || rc=$?
    # when main cannot be read the approval job asks again after the click, so the chain goes on
    if [ "$rc" -eq 0 ] && [ "$now" != "$DIGEST" ]; then current=false; fi
  fi
fi

{
  echo "landed=$landed"
  echo "current=$current"
  echo "sha=$sha"
  echo "version=$version"
  echo "digest=$DIGEST"
  echo "tag=$TAG"
} >> "$GITHUB_OUTPUT"

via=""
[ -z "$pr_url" ] || via=" ([pull request #$PR_NUMBER]($pr_url))"
case "$TARGET_ENV" in
  dev) next="Next: **Approve stage deployment**. Open this run's page, **Review deployments**, tick \`stage\`. If a newer build gets that far first, its approval replaces this one and this run ends as cancelled." ;;
  stage) next="Next: the release \`$APP-v$version\` is created, then **Approve prod deployment** (Review deployments, tick \`prod\`). A newer build that gets that far first replaces this waiting approval." ;;
  *) next="The chain is complete: dev, stage and prod run this build." ;;
esac
{
  if [ "$state" = superseded ]; then
    echo "### $title: superseded by a newer build"
    echo
    if [ "$how" = no-pr ]; then
      echo "No pull request was opened for this build: $TARGET_ENV already has another digest (\`${cur:0:19}\`), and an older build never replaces a newer promotion."
    elif [ "$how" = stalled ]; then
      echo "The pull request$via is still open, and it can no longer merge: \`$prev\` now holds \`${prev_digest:0:19}\`, not this build's digest, and the promotion policy lets $TARGET_ENV take only the digest of \`$prev\`. It stays open until the promotion of the newer build replaces its content (same branch)."
    else
      echo "The pull request$via was merged with another digest than this build's (\`${merged:0:19}\`): a newer build took over the branch. $title now has that one."
    fi
    echo
    echo "This is not a failure. Nothing more happens for this build; the run of the newer build carries on."
  elif [ "$current" = true ]; then
    echo "### $title deployed${version:+: version \`$version\`}"
    echo
    if [ "$how" = present ]; then
      echo "\`$APP\` already runs \`$DIGEST\` in $TARGET_ENV; nothing had to change."
    else
      echo "\`$APP\` runs \`$DIGEST\` in $TARGET_ENV$via."
    fi
    echo
    echo "$next"
  else
    echo "### $title: merged, then replaced by a newer build"
    echo
    echo "This build's digest was merged into $TARGET_ENV$via, and $TARGET_ENV has moved on to another digest since (a newer build or a rollback)."
    echo
    echo "This is not a failure. The chain stops here for this build."
  fi
} | summary
"""

APPROVAL_SCRIPT = r"""
set -euo pipefail
summary() { cat >> "$GITHUB_STEP_SUMMARY"; }
# GitHub creates a missing environment WITHOUT protection on its first use, and then this job would run (and the promotion would start)
# without anyone clicking. So ask the run itself whether a person approved the environment.
approvals=$(gh api "repos/$GH_REPO/actions/runs/$RUN_ID/approvals")
who=$(jq -r --arg env "$ENVIRONMENT" '[.[] | select(.state == "approved") | select(any(.environments[]?; .name == $env)) | .user.login] | unique | join(", ")' <<< "$approvals")
count=$(jq --arg env "$ENVIRONMENT" '[.[] | select(.state == "approved") | select(any(.environments[]?; .name == $env))] | length' <<< "$approvals")
if [ "$count" -lt 1 ]; then
  msg="nobody approved the environment '$ENVIRONMENT' for this run. Create the environment '$ENVIRONMENT' in this repository's settings (Settings, Environments) with a required reviewer and the deployment branch main."
  echo "::error::$msg"
  { echo "### ${ENVIRONMENT^} not promoted: no approval"; echo; echo "Nothing was promoted: $msg"; } | summary
  exit 1
fi
echo "environment '$ENVIRONMENT' approved by $who"
echo "by=$who" >> "$GITHUB_OUTPUT"
"""

DECIDE_SCRIPT = CHAIN_PRE + CHAIN_LIB + r"""
# Runs AFTER the click, so it works with what main says now, not with what the plan said when the run started.
#  1. the previous environment (dev for stage, stage for prod) must STILL hold this build's digest: the promotion policy of the helm
#     repository lets stage take dev's current digest only and prod stage's only. If a newer build moved it meanwhile this build is
#     "superseded by a newer build": nothing is promoted and the run ends green.
#  2. the version. Stage: the next PATCH after the highest <app>-vX.Y.Z tag of the helm repository (none: 1.0.0), a version already in
#     the stage file counts (its tag is made after the merge), the same rule as promote.sh in the helm repository. Prod: the version of stage, unchanged.
title=${TARGET_ENV^}
summary() { cat >> "$GITHUB_STEP_SUMMARY"; }
stop() {
  echo "::error::$2"
  { echo "### $title not promoted: $1"; echo; echo "$2"; } | summary
  exit 1
}
short() { echo "${1:0:19}"; }

# next_version <app> [version already in the stage file]
next_version() {
  local tags newest maj min pat
  tags=$(gh api --paginate "repos/$HELM/git/matching-refs/tags/$1-v" --jq '.[].ref') || return 1
  newest=$({ printf '%s\n' "$tags" | sed -n "s|^refs/tags/$1-v||p"; [ -z "${2:-}" ] || echo "$2"; } | grep -E "$SEMVER" | LC_ALL=C sort -V | tail -n 1 || true)
  if [ -z "$newest" ]; then
    echo 1.0.0
    return 0
  fi
  IFS=. read -r maj min pat <<< "$newest"
  echo "$maj.$min.$((pat + 1))"
}

[[ "$APP" =~ $APP_RE ]] || die "app '$APP' is not a chart name"
[[ "$DIGEST" =~ $DIGEST_RE ]] || die "the digest must be sha256:<64 hex> (got '$DIGEST')"
[[ "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$ ]] || die "image tag '$TAG' is not a valid image tag"
case "$TARGET_ENV" in
  stage) prev=dev ;;
  prod) prev=stage ;;
  *) die "env must be stage or prod (got '$TARGET_ENV')" ;;
esac

rc=0
prev_file=$(values_retry "$prev") || rc=$?
[ "$rc" -eq 0 ] || stop "cannot read $prev" "envs/$prev/values/$APP.yaml could not be read from platform-demo-helm (exit $rc). Re-run the failed jobs of this run."
rc=0
target_file=$(values_retry "$TARGET_ENV") || rc=$?
[ "$rc" -eq 0 ] || stop "cannot read $TARGET_ENV" "envs/$TARGET_ENV/values/$APP.yaml could not be read from platform-demo-helm (exit $rc). A promotion only changes an existing values file: create it first (copy it from $prev) in a normal pull request, or re-run the failed jobs."
prev_digest=$(img_digest <<< "$prev_file")
old_digest=$(img_digest <<< "$target_file")
old_version=$(img_version <<< "$target_file")

if [ "$prev_digest" != "$DIGEST" ]; then
  {
    echo "go=false"
    echo "digest=$DIGEST"
    echo "tag=$TAG"
    echo "version="
  } >> "$GITHUB_OUTPUT"
  {
    echo "### $title not promoted: superseded by a newer build"
    echo
    echo "$prev now runs \`$(short "$prev_digest")\`, not this build's \`$(short "$DIGEST")\`. The promotion policy lets $TARGET_ENV take only the digest that $prev has now, so this build stops here."
    echo
    echo "This is not a failure. Approve $TARGET_ENV in the run of the newer build. Your approval of this run changed nothing."
  } | summary
  exit 0
fi

how=""
if [ "$TARGET_ENV" = stage ]; then
  if [ "$old_digest" = "$DIGEST" ] && [ -n "$old_version" ]; then
    version=$old_version
    how="already in stage as this version"
  else
    rc=0
    version=$(next_version "$APP" "$old_version") || rc=$?
    [ "$rc" -eq 0 ] || stop "cannot choose the version" "The tags of $HELM could not be listed, so the next version is unknown. Re-run the failed jobs of this run."
    how="next patch after the newest ${APP}-vX.Y.Z tag${old_version:+ or the version in the stage file}"
  fi
else
  version=$(img_version <<< "$prev_file")
  [ -n "$version" ] || stop "stage has no version for this digest" "envs/stage/values/$APP.yaml has no '# vX.Y.Z' on its image.digest line, so there is no version for prod. Promote the build to stage first."
  how="the version of stage, unchanged"
fi
[[ "$version" =~ $SEMVER ]] || die "version '$version' is not X.Y.Z"

{
  echo "go=true"
  echo "digest=$DIGEST"
  echo "tag=$TAG"
  echo "version=$version"
} >> "$GITHUB_OUTPUT"
{
  echo "### $title approved${BY:+ by $BY}"
  echo
  echo "| | |"
  echo "|---|---|"
  echo "| App | \`$APP\` |"
  echo "| Version | \`$version\` ($how) |"
  echo "| $title now | \`${old_digest:-none}\` |"
  echo "| $title becomes | \`$DIGEST\` |"
  echo "| Image tag | \`$TAG\` |"
  echo
  echo "Next: **Promote to $TARGET_ENV** opens [the pull request \`promote/$APP/$TARGET_ENV\`]($HELM_URL/pulls?q=is%3Apr+head%3Apromote%2F$APP%2F$TARGET_ENV) in platform-demo-helm."
} | summary
"""

RELEASE_SCRIPT = CHAIN_PRE + CHAIN_LIB + r"""
# The tag and release <app>-vX.Y.Z of platform-demo-helm for the version that stage now runs, on the commit that holds it. The notes
# hold the digest, so the version stays answerable after stage has moved on. The token is the GitHub App's (scoped to platform-demo-helm).
# Idempotent: an existing tag is NEVER moved or touched; when it exists for this digest only a missing release is added.
# Best effort: the JOB is continue-on-error, a refusal never blocks the prod approval (it is a warning + this summary).
summary() { cat >> "$GITHUB_STEP_SUMMARY"; }
stop() {
  # best effort: the job is continue-on-error, so a refusal is a warning on a green run and a red job in the graph; the prod approval is not blocked
  echo "::warning title=Release not made::$1. $2 The pipeline goes on; fix the cause and Re-run failed jobs of this run to make the release."
  { echo "### Release not made: $1"; echo; echo "$2"; echo; echo "This does not stop the pipeline: the prod approval is not blocked. The version \`$VERSION\` is already recorded in stage. Fix the cause, then use Re-run failed jobs on this run; the release is idempotent."; } | summary
  exit 1
}
# $1 = what failed, $2 = what GitHub answered: the answer tells the cause apart
explain() {
  case "$2" in
    *"rule violations"* | *"Rule violations"*) echo "A repository ruleset of platform-demo-helm refused it (the answer names the rule). The ruleset protecting '*-v*' tags has no bypass actor: add the GitHub App platform-demo-promote to its bypass list (Settings, Rules, Rulesets) if it restricts creating tags." ;;
    *"not accessible by integration"*) echo "The GitHub App token has no permission for this: check that the App platform-demo-promote has Contents: write and is installed on platform-demo-helm." ;;
    *) echo "See the answer above." ;;
  esac
}

[[ "$APP" =~ $APP_RE ]] || die "app '$APP' is not a chart name"
[[ "$VERSION" =~ $SEMVER ]] || die "version '$VERSION' is not X.Y.Z"
[[ "$DIGEST" =~ $DIGEST_RE ]] || die "the digest must be sha256:<64 hex> (got '$DIGEST')"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || stop "no commit to tag" "The commit of platform-demo-helm that holds version $VERSION is unknown ('$SHA')."
name="$APP-v$VERSION"

# the commit must hold this version for this digest: never tag something else
rc=0
at_sha=$(values_retry stage "$SHA") || rc=$?
[ "$rc" -eq 0 ] || stop "cannot read stage" "envs/stage/values/$APP.yaml could not be read at $SHA. Re-run the failed jobs of this run."
if [ "$(img_digest <<< "$at_sha")" != "$DIGEST" ] || [ "$(img_version <<< "$at_sha")" != "$VERSION" ]; then
  stop "the commit does not hold this version" "envs/stage/values/$APP.yaml at ${SHA:0:7} does not have version $VERSION for $DIGEST, so $name is not created."
fi

rc=0
out=$(gh api "repos/$HELM/git/ref/tags/$name" 2>&1) || rc=$?
tag_exists=false
if [ "$rc" -eq 0 ]; then
  tag_exists=true
elif ! grep -q 'HTTP 404' <<< "$out"; then
  stop "cannot look for the tag" "Asking GitHub for the tag $name failed: $out"
fi

made=""
if $tag_exists; then
  rc=0
  at_tag=$(values_retry stage "$name") || rc=$?
  [ "$rc" -eq 0 ] || stop "cannot read the existing tag" "The tag $name exists, but envs/stage/values/$APP.yaml at that tag could not be read."
  if [ "$(img_digest <<< "$at_tag")" != "$DIGEST" ]; then
    stop "the tag exists for another image" "The tag $name already exists in platform-demo-helm and its stage file holds another digest than this build's ($DIGEST). A tag is never moved. Nothing is promoted to prod from this run; look at the tag."
  fi
  made="the tag $name already existed for this digest (not touched)"
else
  rc=0
  out=$(gh api -X POST "repos/$HELM/git/refs" -f "ref=refs/tags/$name" -f "sha=$SHA" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    # a second run may have made it a moment ago: fine when it is the same image
    if grep -qi 'already exists' <<< "$out" && values_retry stage "$name" | img_digest | grep -qx "$DIGEST"; then
      made="the tag $name was made a moment ago by another run (not touched)"
    else
      echo "$out"
      stop "creating the tag $name failed" "GitHub refused to create the tag $name on ${SHA:0:7}: $out $(explain tag "$out")"
    fi
  else
    made="the tag $name was created on ${SHA:0:7}"
  fi
fi

rc=0
out=$(gh api "repos/$HELM/releases/tags/$name" 2>&1) || rc=$?
if [ "$rc" -eq 0 ]; then
  rel="the release $name already existed"
elif grep -q 'HTTP 404' <<< "$out"; then
  notes="Stage release \`$VERSION\` of \`$APP\`.

- image digest: \`$DIGEST\` (tag \`$TAG\`)
- source commit: $GITHUB_SERVER_URL/$SOURCE_REPO/commit/$SOURCE_SHA
- helm commit: $HELM_URL/commit/$SHA
- pipeline run: $RUN_URL

Made by the Publish run of the application repository after the promotion to stage was merged. The application repository is not tagged."
  rc=0
  out=$(gh api -X POST "repos/$HELM/releases" -f "tag_name=$name" -f "name=$APP $VERSION" -f "body=$notes" -f make_latest=false 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$out"
    stop "creating the release $name failed" "GitHub refused to create the release for the tag $name: $out $(explain release "$out")"
  fi
  rel="the release $name was created"
else
  stop "cannot look for the release" "Asking GitHub for the release $name failed: $out"
fi

{
  echo "### Released \`$name\`"
  echo
  echo "- $made"
  echo "- $rel: [$name]($HELM_URL/releases/tag/$name)"
  echo "- image \`$DIGEST\`, helm commit \`${SHA:0:7}\`"
  echo
  echo "Next: **Approve prod deployment** (Review deployments, tick \`prod\`), if this build is still what stage runs."
} | summary
"""

DEV_GUARD_SCRIPT = CHAIN_PRE + CHAIN_LIB + r"""
# The run is no longer serialised end to end (it may wait days for a click), so a slower, OLDER run can reach this point after a newer
# one already put its image into dev, and a re-run of an old run can too. Opening the pull request then would move dev BACK to the older
# image. dev's image.tag is sha-<first 7 characters of the commit that built it>: when that commit is a descendant of this one, this
# build is superseded and no dev pull request is opened. Anything unclear (another tag format, GitHub errors) lets the run go on: a
# newer pull request that is still open is already protected by gitops-promote.yaml itself.
summary() { cat >> "$GITHUB_STEP_SUMMARY"; }
[[ "$APP" =~ $APP_RE ]] || die "app '$APP' is not a chart name"
[[ "$DIGEST" =~ $DIGEST_RE ]] || die "the digest must be sha256:<64 hex> (got '$DIGEST')"
[[ "$TARGET_ENV" =~ ^[a-z][a-z0-9-]*$ ]] || die "env '$TARGET_ENV' is not an environment name"
stale=false
rc=0
file=$(values_retry "$TARGET_ENV") || rc=$?
if [ "$rc" -eq 0 ]; then
  dev_tag=$(img_get tag <<< "$file")
  dev_digest=$(img_digest <<< "$file")
  if [ "$dev_digest" != "$DIGEST" ] && [[ "$dev_tag" =~ ^sha-([0-9a-f]{7})$ ]]; then
    rc=0
    status=$(gh api "repos/$GITHUB_REPOSITORY/compare/${BASH_REMATCH[1]}...$GITHUB_SHA" --jq .status) || rc=$?
    if [ "$rc" -eq 0 ] && [ "$status" = behind ]; then
      stale=true
      {
        echo "### Dev change skipped: superseded by a newer build"
        echo
        echo "\`$TARGET_ENV\` already runs \`$dev_tag\` (\`${dev_digest:0:19}\`), built from a newer commit than this one (\`${GITHUB_SHA:0:7}\`). Opening a pull request for this older image would move \`$TARGET_ENV\` back."
        echo
        echo "This is not a failure. Nothing more happens for this build (no stage or prod approval); the run of the newer build carries on."
      } | summary
    fi
  fi
else
  echo "::warning::could not read the $TARGET_ENV values file; going on without the newer-build check"
fi
echo "stale=$stale" >> "$GITHUB_OUTPUT"
"""


RELAY_SCRIPT = r"""
{
  echo "digest=$DIGEST"
  echo "tag=$TAG"
  echo "version=$VERSION"
  echo "current=$CURRENT"
} >> "$GITHUB_OUTPUT"
"""


def chain_env(**kv):
    return "    env:\n" + "".join(f"      {k}: {v}\n" for k, v in kv.items()).rstrip("\n")


def wait_job(key, env_name, *, needs, digest, tag, pr, expect, cond=None):
    """`Wait for <env>`: polls the helm pull request of this build until it is merged (see WAIT_SCRIPT)."""
    out = f"steps.wait.outputs"
    outputs = ("    outputs:\n" + "".join(f"      {o}: ${{{{ {out}.{o} }}}}\n" for o in ("landed", "current", "sha", "version", "digest", "tag")).rstrip("\n"))
    env = chain_env(GH_TOKEN="${{ github.token }}", APP="${{ inputs.app }}", TARGET_ENV=env_name, DIGEST=digest, TAG=tag,
                    PR_NUMBER=pr, EXPECT_VERSION=expect, WAIT_MINUTES='"25"')
    steps = ["      - name: Wait for the helm pull request to merge\n        id: wait\n        run: |\n" + script(WAIT_SCRIPT)]
    return render_job(key, name_of(key), steps, timeout=35, perms="    permissions: {}", needs=needs, cond=cond, outputs=outputs, env=env)


def approve_job(key, env_name, *, needs, cond):
    """`Approve <env> deployment`: the click (GitHub environment of the app repository), then the checks that must hold AFTER the click."""
    outputs = ("    outputs:\n" + "".join(f"      {o}: ${{{{ steps.decide.outputs.{o} }}}}\n" for o in ("go", "version", "digest", "tag")).rstrip("\n"))
    prev = {"stage": "wait-dev", "prod": "release"}[env_name]
    env = chain_env(GH_TOKEN="${{ github.token }}", GH_REPO="${{ github.repository }}", RUN_ID="${{ github.run_id }}", APP="${{ inputs.app }}",
                    ENVIRONMENT=env_name, TARGET_ENV=env_name, DIGEST=f"${{{{ needs.{prev}.outputs.digest }}}}", TAG=f"${{{{ needs.{prev}.outputs.tag }}}}")
    steps = ["      - name: Check that the environment was approved\n        id: approval\n        run: |\n" + script(APPROVAL_SCRIPT),
             "      - name: Check that this build is still the one to promote, and choose the version\n        id: decide\n        env:\n"
             "          BY: ${{ steps.approval.outputs.by }}\n        run: |\n" + script(DECIDE_SCRIPT)]
    extra = [f"    environment: {env_name} # the owner is the required reviewer: Review deployments on the run page",
             "    concurrency:\n"
             f"      group: promote-{env_name}-${{{{ inputs.app }}}} # a newer build's waiting approval REPLACES an older one (the older run ends as cancelled)\n"
             "      cancel-in-progress: true"]
    return render_job(key, name_of(key), steps, timeout=10, perms="    permissions:\n      actions: read # who approved the environment (the run's review history)",
                      needs=needs, cond=cond, extra=extra, outputs=outputs, env=env)


def promote_job(key, env_name, approve_key):
    a = f"needs.{approve_key}.outputs"
    return f"""\
  {key}:
    name: "{name_of(key)}"
    needs: [{approve_key}]
    if: ${{{{ {a}.go == 'true' }}}}
    # the same shared workflow as `Open dev PR` (unchanged); the digest is THIS run's, never "the newest"
    permissions: {{}}
    uses: {GITOPS_PIN}
    with:
      app: ${{{{ inputs.app }}}}
      env: {env_name}
      image-tag: ${{{{ {a}.tag }}}}
      image-digest: ${{{{ {a}.digest }}}}
      source-repository: ${{{{ github.repository }}}}
      source-sha: ${{{{ github.sha }}}}
      version: ${{{{ {a}.version }}}}
      auto-merge: ${{{{ inputs.auto-merge }}}}
    secrets:
      PROMOTE_APP_ID: ${{{{ secrets.PROMOTE_APP_ID }}}}
      PROMOTE_APP_PRIVATE_KEY: ${{{{ secrets.PROMOTE_APP_PRIVATE_KEY }}}}
"""


def release_job():
    w = "needs.wait-stage.outputs"
    token = ("      # Short-lived, limited to ONE repository and ONE permission (the same action and pin as gitops-promote.yaml).\n"
             "      - name: Create a GitHub App token (platform-demo-helm only)\n"
             "        id: app-token\n"
             f"        uses: {wfgen.pin('actions/create-github-app-token')}\n"
             "        with:\n"
             "          app-id: ${{ secrets.PROMOTE_APP_ID }}\n"
             "          private-key: ${{ secrets.PROMOTE_APP_PRIVATE_KEY }}\n"
             "          owner: platform-boutique\n"
             "          repositories: platform-demo-helm\n"
             "          permission-contents: write")
    env = dict(GH_TOKEN="${{ steps.app-token.outputs.token }}", APP="${{ inputs.app }}", VERSION=f"${{{{ {w}.version }}}}",
               DIGEST=f"${{{{ {w}.digest }}}}", TAG=f"${{{{ {w}.tag }}}}", SHA=f"${{{{ {w}.sha }}}}",
               SOURCE_REPO="${{ github.repository }}", SOURCE_SHA="${{ github.sha }}",
               RUN_URL="${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}")
    step_env = "        env:\n" + "".join(f"          {k}: {v}\n" for k, v in env.items())
    # The first step cannot fail: it hands on what the prod approval needs, so a failed release (continue-on-error) leaves the outputs intact.
    relay = ("      - name: Pass on the build for the prod approval\n        id: relay\n        env:\n"
             f"          DIGEST: ${{{{ {w}.digest }}}}\n          TAG: ${{{{ {w}.tag }}}}\n          VERSION: ${{{{ {w}.version }}}}\n          CURRENT: ${{{{ {w}.current }}}}\n"
             "        run: |\n" + script(RELAY_SCRIPT))
    steps = [relay, token, "      - name: Create the tag and the release\n        id: release\n" + step_env + "        run: |\n" + script(RELEASE_SCRIPT)]
    outputs = ("    outputs:\n" + "".join(f"      {o}: ${{{{ steps.relay.outputs.{o} }}}}\n" for o in ("digest", "tag", "version", "current")).rstrip("\n"))
    return render_job("release", name_of("release"), steps, timeout=10, perms="    permissions: {}", needs=["wait-stage"],
                      cond=f"${{{{ {w}.landed == 'true' }}}}", outputs=outputs,
                      # best effort, like the prune: a refused tag or release (cause not proven yet) never blocks the prod approval.
                      # With continue-on-error the job's result is `success`, so the approval job below still runs; the failure shows as a red
                      # step, a warning annotation and the summary
                      extra=["    continue-on-error: true"])


def chain_jobs():
    return [
        wait_job("wait-dev", "${{ inputs.env }}", needs=["publish", "deploy"], digest="${{ needs.publish.outputs.digest }}",
                 tag="${{ needs.publish.outputs.tag }}", pr="${{ needs.deploy.outputs.pr-number }}", expect='""',
                 cond="${{ inputs.env == 'dev' }}"),
        approve_job("approve-stage", "stage", needs=["wait-dev"], cond="${{ needs.wait-dev.outputs.current == 'true' }}"),
        promote_job("stage", "stage", "approve-stage"),
        wait_job("wait-stage", "stage", needs=["approve-stage", "stage"], digest="${{ needs.approve-stage.outputs.digest }}",
                 tag="${{ needs.approve-stage.outputs.tag }}", pr="${{ needs.stage.outputs.pr-number }}",
                 expect="${{ needs.approve-stage.outputs.version }}"),
        release_job(),
        approve_job("approve-prod", "prod", needs=["release"], cond="${{ needs.release.outputs.current == 'true' }}"),
        promote_job("prod", "prod", "approve-prod"),
        wait_job("wait-prod", "prod", needs=["approve-prod", "prod"], digest="${{ needs.approve-prod.outputs.digest }}",
                 tag="${{ needs.approve-prod.outputs.tag }}", pr="${{ needs.prod.outputs.pr-number }}",
                 expect="${{ needs.approve-prod.outputs.version }}"),
    ]


# ───────────────────────── the file ─────────────────────────

HEADER = """\
name: Service publish

# GENERATED by tools/gen-service-publish.py - do not edit; change the source workflow or the generator and run the generator.
#
# The merge-to-main pipeline of every app repository, ONE flat workflow (no nested publish boxes in the Actions graph):
#
#   Detect changes -> Find or build image -> Scan image -> Publish image -+-> Create SBOM
#                                                                         +-> Clean up staging images
#                                                                         +-> Open dev PR -> Wait for dev -> Approve stage deployment
#                                                                               -> Promote to stage -> Wait for stage -> Release stage version
#                                                                               -> Approve prod deployment -> Promote to prod -> Wait for prod
#
#   Detect changes        a docs/metadata-only commit publishes nothing (a manual run with `before` diffs before..HEAD like a push)
#   Find or build image   the image of this commit's source tree: ghcr.io/<repo>:tree-<git tree hash>. FOUND (the pull request built
#                         it, main did not move) = reused, no build. NOT FOUND (the tree differs, or the image was pruned) = built now,
#                         the registry build cache is written, pushed under the same tag so a re-run finds it
#   Scan image            Trivy on that exact digest, again (new CVEs may have appeared since the pull request)
#   Publish image         copy that digest to Docker Hub as sha-<7 chars of the commit> ONLY (no latest, no semver; a digest that
#                         differs from the scanned one fails the run), cosign sign (keyless), verify; refuses commits not reachable from main
#   Create SBOM           SBOM attestation, signed, verified; next to the helm pull request, not before it; a failure fails the run
#   Open dev PR           the pull request in the helm repository that moves env `dev` to this digest (gitops-promote.yaml, unchanged)
#                         (skipped when dev already runs a newer commit: step `dev-guard` of Publish image)
#   Clean up staging images   best effort (the job may fail without failing the run): keep the newest `prune-keep` (10) `sha-*` and
#                         `prune-keep-tree` (20) `tree-*` versions of the ghcr.io package, never the buildcache package, never an
#                         image-index child of a kept image
#
# Each job writes its own short section of the run summary; `Publish image` carries the answer to "what was published": the Docker Hub
# tag, the commit, the digest, the signature and where the dev change goes. In the caller's run the checks are named `publish / <name>`.
# Nothing is tagged in the app repository and there is no `latest`. The version X.Y.Z is chosen after the stage click (next patch after the
# highest <app>-vX.Y.Z tag of platform-demo-helm, none: 1.0.0, like the helm repository's own promote.sh) and recorded with the digest.
#
#   Wait for dev            polls the dev pull request until it is merged; green with "superseded by a newer build" when a newer build
#                           took the branch over or dev already moved on, red when the pull request is closed unmerged or not merged in 25 minutes
#   Approve stage deployment   environment `stage`: the click ("Review deployments" on this run's page). AFTER the click: the approval is
#                           verified, dev must STILL hold this build's digest (else superseded: nothing is promoted, green), the version is chosen
#   Promote to stage        gitops-promote.yaml (the same pinned file as Open dev PR), env stage, THIS run's digest, auto-merge
#   Wait for stage          polls the stage pull request until merged (same rules as Wait for dev)
#   Release stage version   the tag and release <app>-vX.Y.Z in platform-demo-helm, with the GitHub App token; idempotent, a tag is never moved;
#                           best effort (continue-on-error): a refusal is a warning and never blocks the prod approval
#   Approve prod deployment, Promote to prod, Wait for prod   the same three for prod, with stage's version
#
# Concurrency: there is NO workflow-level group (the caller has none either): a run waits days for a click, and a group on the whole run
# would queue every later merge behind it. Job groups instead, per app: `Find or build image` (publish-build-<app>, no cancel: a second
# build waits, a waiting one is replaced by a newer one) and `Clean up staging images` (publish-prune-<app>, same); the two approvals
# promote-stage-<app> / promote-prod-<app> with cancel-in-progress: a NEWER build's waiting approval REPLACES an older waiting one, which
# ends as cancelled (not failed). The dev, stage and prod pull requests are serialised by gitops-promote.yaml's own group per app and
# environment. A slower older run never moves dev back: the step `dev-guard` of Publish image skips Open dev PR when dev already runs a
# newer commit. Details, two quick merges, a merge while an older run waits: docs/one-pipeline.md.
#
# Call it with:  uses: platform-boutique/platform-demo-workflows/.github/workflows/service-publish.yaml@<sha> # <tag>
# The calling job must grant: contents: read, packages: write, id-token: write, actions: read (who approved stage and prod).
# The app repository needs the GitHub environments `stage` and `prod` (required reviewer = the owner, deployment branches: main only).

on:
  workflow_call:
    inputs:
      app:
        description: Helm chart / application name = repository name without the `app-` prefix, e.g. checkout-service
        type: string
        required: true
      image-name:
        description: Docker Hub repository name under the account (default = app)
        type: string
        default: ""
      context:
        type: string
        default: .
      dockerfile:
        type: string
        default: Dockerfile
      trivyignores:
        description: Path to a .trivyignore file in the caller repo (optional)
        type: string
        default: ""
      before:
        description: >-
          Commit the manual run starts from (workflow_dispatch only; a push uses github.event.before): when set, a run whose
          before..HEAD changed only documentation publishes nothing. Empty on a manual run = publish.
        type: string
        default: ""
      env:
        description: Environment of the helm repository the new digest is proposed for
        type: string
        default: dev
      auto-merge:
        description: Let the helm pull request merge itself once the helm repository's checks pass
        type: boolean
        default: true
      version:
        description: Optional version label for gitops-promote (normally empty on dev)
        type: string
        default: ""
      prune:
        description: Prune old ghcr.io images after publishing (best effort)
        type: boolean
        default: true
      prune-keep:
        description: How many of the newest `sha-*` versions of the ghcr.io package the prune keeps
        type: number
        default: 10
      prune-keep-tree:
        description: How many of the newest `tree-*` versions it keeps (every pull-request push makes one, so more than `sha-*`)
        type: number
        default: 20
    secrets:
      DOCKERHUB_USERNAME:
        required: true
      DOCKERHUB_TOKEN:
        required: true
      PROMOTE_APP_ID:
        required: true
      PROMOTE_APP_PRIVATE_KEY:
        required: true
    outputs:
      published:
        description: "'true' when an image was published, 'false' for a documentation-only commit (or a ref other than main)"
        value: ${{ jobs.publish.outputs.digest != '' }}
      digest:
        description: Image digest (sha256:...)
        value: ${{ jobs.publish.outputs.digest }}
      tag:
        description: The sha-<7 chars> tag
        value: ${{ jobs.publish.outputs.tag }}
      pr-url:
        description: URL of the helm pull request
        value: ${{ jobs.deploy.outputs.pr-url }}

# Least privilege by default; every job below declares what it needs.
permissions: {}

jobs:
"""


def render():
    jobs = [changes_job(), image_job(), scan_job(), publish_job(), sbom_job(), deploy_job(), prune_job()] + chain_jobs()
    text = HEADER + "\n".join(jobs)
    assert not re.search(r"[ \t]+$", text, re.M), f"{OUT}: trailing whitespace"
    declared = set(re.findall(r"^      ([a-z0-9-]+):\s*$", text.split("\npermissions:")[0].split("    inputs:\n")[1].split("    secrets:")[0], re.M))
    used = set(re.findall(r"\binputs\.([a-z0-9-]+)", text.split("\njobs:\n")[1]))
    assert used <= declared, f"{OUT}: uses undeclared inputs {sorted(used - declared)}"
    return text.rstrip("\n") + "\n"


def main():
    text = render()
    with open(os.path.join(ROOT, ".github", "workflows", OUT), "w") as f:
        f.write(text)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
