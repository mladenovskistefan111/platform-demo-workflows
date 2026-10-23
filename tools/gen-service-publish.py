#!/usr/bin/env python3
"""Generate service-publish.yaml: the merge-to-main pipeline of an app repository, ONE flat workflow.

usage: gen-service-publish.py [repo-root]      (writes <repo-root>/.github/workflows/service-publish.yaml)

    Guard: Changes -> Image: Find or build -> Image: Scan -> Publish: Copy, sign, verify -+-> Publish: SBOM
                                                                                          +-> Deploy: Open dev PR
                                                                                          +-> Cleanup: Prune staging

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
NAMES = {"changes": "Guard: Changes", "image": "Image: Find or build", "scan": "Image: Scan", "publish": "Publish: Copy, sign, verify",
         "sbom": "Publish: SBOM", "deploy": "Deploy: Open dev PR", "prune": "Cleanup: Prune staging"}


def name_of(key):
    return NAMES[key]


# ───────────────────────── Guard: Changes ─────────────────────────

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
    return render_job("changes", name_of("changes"), [j.steps[0], compare], timeout=j.timeout(), perms=j.raw("permissions"),
                      outputs=j.raw("outputs"))


# ───────────────────────── Image: Find or build, Image: Scan ─────────────────────────

def image_job():
    steps = [
        wfgen.checkout_step(),
        wfgen.buildx_step(),
        wfgen.login_step("Log in to ghcr.io"),
        wfgen.cache_step(),
        wfgen.find_step("true"),
        wfgen.build_step(push="true", cache_write=True),
        wfgen.record_step("true"),
    ]
    outputs = ("    outputs:\n"
               "      tree: ${{ steps.find.outputs.tree }}\n"
               "      digest: ${{ steps.image.outputs.digest }}\n"
               "      ref: ${{ steps.image.outputs.ref }}\n"
               "      how: ${{ steps.image.outputs.how }} # reused | built")
    perms = "    permissions:\n      contents: read\n      packages: write # pushes ghcr.io/<repo>:tree-<hash> when it has to build"
    return render_job("image", name_of("image"), steps, timeout=45, perms=perms, needs=["changes"], outputs=outputs,
                      # only commits on main are published (the publish job checks again from the git history)
                      cond="${{ needs.changes.outputs.code == 'true' && github.ref == 'refs/heads/main' }}")


def scan_job():
    first, rest = wfgen.checkout_step().split("\n", 1)
    steps = [
        # only the ignore file is needed from the checkout
        f"{first}\n        if: inputs.trivyignores != ''\n{rest}",
        wfgen.login_step("Log in to ghcr.io"),
        wfgen.trivy_step("${{ needs.image.outputs.ref }}",
                         "same version, threshold and ignore file as the image job of service-ci-<language>; new CVEs may have appeared since the pull request"),
    ]
    perms = "    permissions:\n      contents: read\n      packages: read"
    return render_job("scan", name_of("scan"), steps, timeout=15, perms=perms, needs=["image"])


# ───────────────────────── Publish: Copy, sign, verify ─────────────────────────

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
{
  echo "### Published"
  echo
  echo "| | |"
  echo "|---|---|"
  echo "| commit | \`$GITHUB_SHA\` |"
  echo "| source tree | \`$TREE\` |"
  case "$HOW" in
    reused) echo "| image | **reused**: the build of this tree from the pull request, not built again |" ;;
    *) echo "| image | **built** here (no image of this tree existed) |" ;;
  esac
  echo "| digest | \`$DIGEST\` |"
  echo "| Docker Hub tag | \`$TAG\` |"
  echo "| signature | verified (keyless, identity: this workflow, certificate names commit \`${GITHUB_SHA:0:7}\`) |"
  echo "| helm pull request | [open promotions of \`$APP\` to \`$TARGET_ENV\`]($HELM_URL/pulls?q=is%3Apr+head%3Apromote%2F$APP%2F$TARGET_ENV); the exact link is in the \"Deploy: Open dev PR\" summary |"
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
    summary = ("      - name: Summary\n"
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
    ]
    outputs = ("    outputs:\n"
               "      digest: ${{ steps.promote.outputs.digest }}\n"
               "      tag: ${{ steps.promote.outputs.tag }}\n"
               "      # no image reference here: it contains the Docker Hub account name, a secret, and GitHub drops outputs that contain secrets")
    perms = ("    permissions:\n      id-token: write # keyless cosign signing\n      contents: read\n"
             "      packages: read")
    return render_job("publish", name_of("publish"), steps, timeout=d.timeout(), perms=perms, needs=["image", "scan"], outputs=outputs)


# ───────────────────────── Publish: SBOM ─────────────────────────

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
    steps = [d.step("Log in to Docker Hub"), ref, d.step("Install Cosign")] + moved
    perms = "    permissions:\n      id-token: write # keyless cosign attestation\n      contents: read"
    return render_job("sbom", name_of("sbom"), steps, timeout=15, perms=perms, needs=["publish"])


# ───────────────────────── Deploy: Open dev PR ─────────────────────────

def deploy_job():
    return f"""\
  deploy:
    name: "{name_of('deploy')}"
    needs: [publish]
    # the one nested workflow: gitops-promote.yaml is shared with the helm repository and unchanged (works with the GitHub App token, not this one)
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


# ───────────────────────── Cleanup: Prune staging ─────────────────────────

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
    step = sub(step, "{len(kept)} newest sha-* kept;", "{len(kept)} newest kept;", src)
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
                      extra=["    continue-on-error: true"], env=env)


# ───────────────────────── the file ─────────────────────────

HEADER = """\
name: Service publish

# GENERATED by tools/gen-service-publish.py - do not edit; change the source workflow or the generator and run the generator.
#
# The merge-to-main pipeline of every app repository, ONE flat workflow (no nested publish boxes in the Actions graph):
#
#   Guard: Changes -> Image: Find or build -> Image: Scan -> Publish: Copy, sign, verify -+-> Publish: SBOM
#                                                                                         +-> Deploy: Open dev PR
#                                                                                         +-> Cleanup: Prune staging
#
#   Guard: Changes              a docs/metadata-only commit publishes nothing (a manual run with `before` diffs before..HEAD like a push)
#   Image: Find or build        the image of this commit's source tree: ghcr.io/<repo>:tree-<git tree hash>. FOUND (the pull request built
#                               it, main did not move) = reused, no build. NOT FOUND (the tree differs, or the image was pruned) = built now,
#                               the registry build cache is written, pushed under the same tag so a re-run finds it
#   Image: Scan                 Trivy on that exact digest, again (new CVEs may have appeared since the pull request)
#   Publish: Copy, sign, verify copy that digest to Docker Hub as sha-<7 chars of the commit> ONLY (no latest, no semver; a digest that
#                               differs from the scanned one fails the run), cosign sign (keyless), verify; refuses commits not reachable from main
#   Publish: SBOM               SBOM attestation, signed, verified; next to the helm pull request, not before it; a failure fails the run
#   Deploy: Open dev PR         the pull request in the helm repository that moves env `dev` to this digest (gitops-promote.yaml, unchanged)
#   Cleanup: Prune staging      best effort (the job may fail without failing the run): keep the newest `prune-keep` (10) `sha-*` and `prune-keep-tree` (20) `tree-*` versions of
#                               the ghcr.io package, never the buildcache package, never an image-index child of a kept image
# Release tags and `latest` are not made here: a version is chosen when the digest is promoted to stage/prod in the helm repository.
#
# Concurrency (in the caller, group `publish-<ref>`, no cancel) runs one publish at a time. A run waiting behind a running one is
# REPLACED by a newer waiting one: of three quick merges the second never publishes by itself, the third publishes the tree of main
# which contains it (docs-only third merge after a code merge: see docs/build-once.md).
#
# Call it with:  uses: platform-boutique/platform-demo-workflows/.github/workflows/service-publish.yaml@<sha> # <tag>
# The calling job must grant: contents: read, packages: write, id-token: write.

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
    jobs = [changes_job(), image_job(), scan_job(), publish_job(), sbom_job(), deploy_job(), prune_job()]
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
