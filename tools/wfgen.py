"""Helpers shared by tools/gen-service-ci.py and tools/gen-service-publish.py (an import module, not a script).

  * reading the stand-alone workflows as text and cutting jobs / steps out of them (nothing is retyped, so a fix in the original
    cannot be forgotten in a generated file; every transformation asserts that it matched);
  * the image steps that the pull-request workflows and the publish workflow have in common: the lookup of an existing
    `tree-<hash>` image, the build, the digest record and the Trivy scan. They are written ONCE, here, and rendered into all seven
    files; tools/check-consistency.py fails when a generated file differs from this text.
"""
import os, re

WFDIR = None  # set by set_root()


def set_root(root):
    global WFDIR
    WFDIR = os.path.join(root, ".github", "workflows")


def read_text(fname):
    with open(os.path.join(WFDIR, fname)) as f:
        return f.read()


def read_lines(fname):
    return read_text(fname).split("\n")


def job_blocks(fname):
    """{job key: its lines} of a workflow file, without the comment lines that lead into the next job."""
    lines = read_lines(fname)
    start = lines.index("jobs:") + 1
    blocks, key = {}, None
    for line in lines[start:]:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            key = m.group(1)
            blocks[key] = []
        elif key is not None:
            blocks[key].append(line)
    for k, b in blocks.items():
        while b and (not b[-1].strip() or b[-1].startswith("  #")):
            b.pop()
    return blocks


class Job:
    """One extracted job: its indent-4 fields as raw text, and its steps as separate blocks."""

    def __init__(self, fname, key):
        self.src = f"{fname}:{key}"
        self.fields, cur = {}, None
        for line in job_blocks(fname)[key]:
            m = re.match(r"^    ([a-z-]+):(.*)$", line)
            if m:
                cur = m.group(1)
                self.fields[cur] = [line]
            elif cur:
                self.fields[cur].append(line)
        raw = self.fields.pop("steps")[1:]
        self.steps, step = [], None
        for line in raw:
            if line.startswith("      - "):
                step = [line]
                self.steps.append(step)
            elif step is not None:
                step.append(line)
        self.steps = ["\n".join(s).rstrip("\n") for s in self.steps]

    def scalar(self, name):
        return self.fields[name][0].split(":", 1)[1].strip()

    def timeout(self):
        return int(self.scalar("timeout-minutes"))

    def raw(self, name):
        return "\n".join(self.fields[name])

    def step(self, name):
        """The step with this `- name:`; exactly one must exist."""
        hits = [s for s in self.steps if s.split("\n")[0] == f"      - name: {name}"]
        assert len(hits) == 1, f"{self.src}: expected one step named {name!r}, found {len(hits)}: {[s.split(chr(10))[0] for s in self.steps]}"
        return hits[0]


def sub(text, old, new, src, count=1):
    assert text.count(old) == count, f"{src}: expected {count}x {old!r}, found {text.count(old)}x"
    return text.replace(old, new)


def with_if(step, cond, src):
    """Put `if: cond` right under the `- name:` line of a step."""
    first, rest = step.split("\n", 1)
    assert first.startswith("      - name: "), f"{src}: step has no name: {first}"
    return f"{first}\n        if: {cond}\n{rest}"


def render_job(key, name, steps, *, timeout, perms, needs=None, cond=None, outputs=None, extra=(), env=None):
    """One job as text. `extra` = more indent-4 lines (continue-on-error, ...) placed after `if`."""
    out = [f"  {key}:", f'    name: "{name}"']
    if needs:
        out.append(f"    needs: [{', '.join(needs)}]")
    if cond:
        out.append(f"    if: {cond}")
    out += list(extra)
    out += ["    runs-on: ubuntu-latest", f"    timeout-minutes: {timeout}", perms]
    if outputs:
        out.append(outputs)
    if env:
        out.append(env)
    out.append("    steps:")
    out.append("\n\n".join(steps))
    return "\n".join(out) + "\n"


def script(text, indent=10):
    """A shell script as the body of a `run: |` block."""
    return "\n".join((" " * indent + l) if l else "" for l in text.strip("\n").split("\n"))


# ───────────────────────── pins: taken from the workflows that already use the action ─────────────────────────

def pin(action):
    """`<action>@<sha> # <version>` as docker-build.yaml / docker-promote.yaml / changed-paths.yaml / gitops-promote.yaml use it today."""
    for fname in ("docker-build.yaml", "docker-promote.yaml", "changed-paths.yaml", "gitops-promote.yaml"):
        m = re.search(r"uses: (" + re.escape(action) + r"@[0-9a-f]{40} # \S+)", read_text(fname))
        if m:
            return m.group(1)
    raise AssertionError(f"no pin for {action} in docker-build/docker-promote/changed-paths/gitops-promote")


# ───────────────────────── the image steps both pipelines share ─────────────────────────

MODE_SCRIPT = r"""
# A run pushes the image to ghcr.io only when its token may: a pull request from a fork has a read-only token, and so has one that
# Dependabot opened. Those runs build into the local Docker daemon and scan that (nothing is pushed, nothing is reused).
push=true
if [ "$EVENT" = pull_request ]; then
  if [ "$HEAD_REPO" != "$GITHUB_REPOSITORY" ] || [ "$ACTOR" = 'dependabot[bot]' ]; then
    push=false
  fi
fi
echo "push=$push" >> "$GITHUB_OUTPUT"
echo "this run pushes the image to ghcr.io: $push"
"""

FIND_SCRIPT = r"""
set -euo pipefail
# The image is named after the content it was built from: the git tree of the checked-out commit. A pull request's merge commit
# and the squash commit that lands on main have the SAME tree when main did not move, so the image the pull request built is the
# image that gets published; a different tree (main moved, a rebase with other changes) finds nothing and builds.
tree=$(git rev-parse 'HEAD^{tree}')
if ! [[ "$tree" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::unexpected tree id '$tree'"
  exit 1
fi
tag_ref="ghcr.io/${GITHUB_REPOSITORY,,}:tree-$tree"
found=false
digest=""
note=""
if [ "$CAN_PUSH" = true ]; then
  manifest="$RUNNER_TEMP/tree-manifest.json"
  err="$RUNNER_TEMP/tree-lookup.err"
  for attempt in 1 2 3; do
    if docker buildx imagetools inspect --raw "$tag_ref" > "$manifest" 2> "$err" && [ -s "$manifest" ]; then
      found=true
      digest="sha256:$(sha256sum "$manifest" | cut -d' ' -f1)"
      break
    fi
    if grep -qiE 'not found|manifest_unknown|name_unknown|404' "$err"; then
      note="no image for this tree yet"
      break
    fi
    # Not a clean "not found" (network, registry hiccup). Building is always safe: what is built is what gets scanned and shipped.
    note="the lookup failed ($(head -c 200 "$err" | tr '\n' ' ')), so the image is built"
    echo "::warning::looking up $tag_ref failed (attempt $attempt of 3): $(head -c 200 "$err" | tr '\n' ' ')"
    sleep $((attempt * 3))
  done
else
  note="this run cannot push, so nothing can be reused"
fi
{
  echo "tree=$tree"
  echo "tag-ref=$tag_ref"
  echo "found=$found"
  echo "digest=$digest"
} >> "$GITHUB_OUTPUT"
if [ "$found" = true ]; then
  echo "tree $tree: image exists ($digest); no build"
else
  echo "tree $tree: $note"
fi
"""

RECORD_SCRIPT = r"""
set -euo pipefail
if [ "$FOUND" = true ]; then
  how=reused
  digest=$FOUND_DIGEST
elif [ "$PUSH" = true ]; then
  how=built
  digest=$BUILT_DIGEST
else
  how=local
  digest=""
fi
if [ "$how" = local ]; then
  ref=service-ci-scan:local
else
  if ! [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "::error::no valid image digest (got '$digest')"
    exit 1
  fi
  ref="ghcr.io/${GITHUB_REPOSITORY,,}@$digest"
fi
{
  echo "ref=$ref"
  echo "digest=$digest"
  echo "how=$how"
} >> "$GITHUB_OUTPUT"
"""

# The "Image" section of the run summary: written by a last step that also runs after a failure, so the reader sees what the image
# step did and what the scan said (Trivy fails the step on a HIGH or CRITICAL finding; the findings themselves are in its log).
SUMMARY_SCRIPT = r"""
set -euo pipefail
{
  echo "### Image"
  echo
  case "$HOW" in
    reused) echo "**Reused.** An image of this source tree already exists, so nothing was built." ;;
    built) echo "**Built** and pushed to the staging registry (ghcr.io)." ;;
    local) echo "**Built locally, not pushed.** This run cannot push (a fork or Dependabot pull request), so nothing is reused or published from it." ;;
    *) echo "**No image was produced.** See the failed step in the log." ;;
  esac
  echo
  echo "| | |"
  echo "|---|---|"
  if [ -n "$TREE" ] && { [ "$HOW" = reused ] || [ "$HOW" = built ]; }; then
    echo "| Tag | \`tree-$TREE\` |"
  fi
  [ -z "$DIGEST" ] || echo "| Digest | \`$DIGEST\` |"
  if [ -n "${SCAN:-}" ]; then
    case "$SCAN" in
      success) echo "| Trivy scan | passed: no HIGH or CRITICAL vulnerabilities |" ;;
      failure) echo "| Trivy scan | **failed**: HIGH or CRITICAL vulnerabilities, or the scan could not run. The table is in the log of the step \"Run Trivy vulnerability scanner\" |" ;;
      *) echo "| Trivy scan | not run |" ;;
    esac
  fi
} >> "$GITHUB_STEP_SUMMARY"
"""


def checkout_step(extra=""):
    return f"      - uses: {pin('actions/checkout')}\n        with:\n          persist-credentials: false" + extra


def buildx_step():
    return f"      - name: Set up Docker Buildx\n        uses: {pin('docker/setup-buildx-action')}"


def mode_step():
    return ("      - name: Decide whether this run pushes the image\n"
            "        id: mode\n"
            "        env:\n"
            "          EVENT: ${{ github.event_name }}\n"
            "          HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}\n"
            "          ACTOR: ${{ github.actor }}\n"
            "        run: |\n" + script(MODE_SCRIPT))


def login_step(title, cont=None, comment=None):
    """Log in to ghcr.io. `cont` = a `continue-on-error` expression (a cache that cannot be read only makes the build slower)."""
    out = f"      - name: {title}\n"
    if comment:
        out += f"        # {comment}\n"
    if cont:
        out += f"        continue-on-error: {cont}\n"
    return (out + f"        uses: {pin('docker/login-action')}\n        with:\n          registry: ghcr.io\n"
            "          username: ${{ github.actor }}\n          password: ${{ secrets.GITHUB_TOKEN }}")


def cache_step():
    return ("      - name: Name the registry build cache\n        id: cache\n"
            '        run: echo "ref=ghcr.io/${GITHUB_REPOSITORY,,}/buildcache" >> "$GITHUB_OUTPUT"')


def find_step(can_push):
    return ("      - name: Find an image built from this tree\n"
            "        id: find\n"
            "        env:\n"
            f"          CAN_PUSH: {can_push}\n"
            "        run: |\n" + script(FIND_SCRIPT))


def build_step(*, push, cache_write):
    """The build. push = 'expr' (decided by the `mode` step) or 'true'. A run that cannot push loads the image into the local daemon instead.
    cache_write = True (the publish) or 'when-pushing' (pull requests: only the runs that push write the registry cache)."""
    if push == "true":
        pushing = "          push: true\n          tags: ${{ steps.find.outputs.tag-ref }}\n"
    else:
        pushing = ("          push: ${{ steps.mode.outputs.push == 'true' }}\n"
                   "          load: ${{ steps.mode.outputs.push != 'true' }}\n"
                   "          tags: ${{ steps.mode.outputs.push == 'true' && steps.find.outputs.tag-ref || 'service-ci-scan:local' }}\n")
    cache_to = "type=registry,ref=${{ steps.cache.outputs.ref }},mode=max,image-manifest=true,oci-mediatypes=true"
    if cache_write is True:
        cache = ("          cache-from: type=registry,ref=${{ steps.cache.outputs.ref }}\n"
                 f"          cache-to: {cache_to}\n")
    else:
        assert cache_write == "when-pushing", cache_write
        cache = ("          # a run that pushes the image (same repository, not Dependabot) also refreshes the cache; a fork's or Dependabot's run only reads it\n"
                 "          cache-from: type=registry,ref=${{ steps.cache.outputs.ref }}\n"
                 "          cache-to: ${{ steps.mode.outputs.push == 'true' && format('type=registry,ref={0},mode=max,image-manifest=true,oci-mediatypes=true', steps.cache.outputs.ref) || '' }}\n")
    return ("      - name: Build the image\n"
            "        id: build\n"
            "        if: steps.find.outputs.found != 'true'\n"
            f"        uses: {pin('docker/build-push-action')}\n"
            "        with:\n"
            "          context: ${{ inputs.context }}\n"
            "          file: ${{ inputs.context }}/${{ inputs.dockerfile }}\n" + pushing +
            "          # where it was built (the signature of the published image names the commit that was shipped)\n"
            "          labels: |\n"
            "            org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}\n"
            "            org.opencontainers.image.revision=${{ github.event.pull_request.head.sha || github.sha }}\n"
            "            io.platform-demo.tree=${{ steps.find.outputs.tree }}\n"
            "            io.platform-demo.build-run=${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}\n" + cache.rstrip("\n"))


def record_step(push):
    return ("      - name: Record the image to scan\n"
            "        id: image\n"
            "        env:\n"
            "          TREE: ${{ steps.find.outputs.tree }}\n"
            "          FOUND: ${{ steps.find.outputs.found }}\n"
            "          FOUND_DIGEST: ${{ steps.find.outputs.digest }}\n"
            f"          PUSH: {push}\n"
            "          BUILT_DIGEST: ${{ steps.build.outputs.digest }}\n"
            "        run: |\n" + script(RECORD_SCRIPT))


def summary_step(scan):
    """The last step of an image job. `scan` = True when the Trivy step (id `trivy`) is in the same job."""
    return ("      - name: Write the image summary\n"
            "        if: ${{ !cancelled() }}\n"
            "        env:\n"
            "          TREE: ${{ steps.find.outputs.tree }}\n"
            "          HOW: ${{ steps.image.outputs.how }}\n"
            "          DIGEST: ${{ steps.image.outputs.digest }}\n"
            + ("          SCAN: ${{ steps.trivy.outcome }}\n" if scan else "") +
            "        run: |\n" + script(SUMMARY_SCRIPT))


def trivy_version():
    """The Trivy release docker-build.yaml scans with: bumped in that one reviewed place."""
    m = re.search(r"version: (v\d+\.\d+\.\d+)\n\s+image-ref:", read_text("docker-build.yaml"))
    assert m, "docker-build.yaml: no `version: vX.Y.Z` before `image-ref:`"
    return m.group(1)


def trivy_step(image_ref, why):
    return ("      - name: Run Trivy vulnerability scanner\n"
            "        id: trivy\n"
            f"        uses: {pin('aquasecurity/trivy-action')}\n"
            "        with:\n"
            f"          # {why}\n"
            f"          version: {trivy_version()}\n"
            f"          image-ref: {image_ref}\n"
            "          format: table\n"
            "          severity: CRITICAL,HIGH\n"
            "          exit-code: 1\n"
            "          trivyignores: ${{ inputs.trivyignores }}")
