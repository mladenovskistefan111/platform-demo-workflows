#!/usr/bin/env python3
"""Generate the per-language pull-request workflows service-ci-{go,node,python,java,dotnet,none}.yaml.

usage: gen-service-ci.py [repo-root]      (writes <repo-root>/.github/workflows/service-ci-*.yaml)

Why generated: the Actions graph of a caller shows every job of the called workflow, skipped ones too. One workflow for all languages
therefore drew four hollow language boxes and nested boxes inside boxes. These files are FLAT (no nested `uses:` of other workflows)
and contain only what the repository really runs:

    Guard: Changes | Guard: Secrets | Guard: Workflows      no needs: start at t=0, together
    Check: Lint | Check: Build | Check: Tests               need only `Guard: Changes` (skipped for a docs-only change), run in
    Security: SCA | Security: SAST | Image: Build + CVE scan  parallel with the other two guards
    gate                                                     needs EVERY job, always runs

The left column means "starts at t=0", not "must pass first": a flaky gitleaks download or one lint complaint does not hide the
test, SAST and Trivy results (one round trip instead of two). The gate fails if any of them failed.

Source of truth (nothing is retyped here, so a fix cannot be forgotten):
  * the job bodies come from the stand-alone workflows, extracted as text: go-ci / node-ci / python-ci / java-gradle-ci / dotnet-ci
    (lint-format, build-check, unit-tests, sca, sast), changed-paths (detect), secret-scan, workflow-lint;
  * the optional smoke test and E2E job of go-ci / node-ci are folded into the Build and Tests jobs as steps with an `if:` on the
    input (one build and one toolchain setup instead of two; no job that shows up as "skipped");
  * tool versions (golangci-lint, gosec, semgrep, ...) are inputs of the stand-alone workflows; here they are written in as the
    stand-alone defaults, so they are bumped in one place;
  * input names, types and defaults are copied from the stand-alone workflow;
  * only the image job and the gate script live in this file (service-ci.yaml, their previous home, no longer exists).
Every transformation asserts that it matched, so a refactoring of a source workflow fails here instead of silently changing a file.
tools/check-consistency.py fails when a generated file differs from this output.
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(HERE)
WFDIR = os.path.join(ROOT, ".github", "workflows")

LANGS = ["go", "node", "python", "java", "dotnet", "none"]
SOURCE = {"go": "go-ci.yaml", "node": "node-ci.yaml", "python": "python-ci.yaml", "java": "java-gradle-ci.yaml", "dotnet": "dotnet-ci.yaml"}
TITLE = {"go": "Go", "node": "Node", "python": "Python", "java": "Java", "dotnet": ".NET", "none": "no language pipeline"}

# inputs a file declares besides the common ones (names, types, defaults are copied from the stand-alone workflow)
DECLARED = {
    "go": ["build-path", "test-packages", "e2e-packages", "smoke-port", "smoke-env"],
    "node": ["node-version-file", "semgrep-config", "smoke-port", "smoke-command", "smoke-health-args", "smoke-script"],
    "python": ["python-version-file", "source-dir", "build-check-file", "tests-path", "bandit-config"],
    "java": ["java-version", "java-distribution", "semgrep-config"],
    "dotnet": ["dotnet-version-file", "solution", "project", "test-project", "test-filter", "semgrep-config"],
    "none": [],
}
# tool versions: inputs of the stand-alone workflow, a fixed value here (the stand-alone default)
TOOL_VERSIONS = ["golangci-lint-version", "govulncheck-version", "gosec-version", "semgrep-version", "ruff-version",
                 "pip-audit-version", "bandit-version"]
# the job was an optional job of the stand-alone workflow; the wording of its enabling input changes with it
DESC_FIX = {
    ("e2e-packages", "empty skips the job"): "empty skips the E2E step",
    ("smoke-port", "empty skips the job"): "empty skips the smoke test",
}

COMMON_INPUTS = """\
      image:
        description: Build the image and scan it (when code changed). false for a repository without an image pipeline.
        type: boolean
        default: true
      context:
        description: Docker build context
        type: string
        default: .
      dockerfile:
        description: Dockerfile name inside the context
        type: string
        default: Dockerfile
      trivyignores:
        description: Path to a .trivyignore file in the caller repo (optional)
        type: string
        default: ""
"""

GUARDS = ["changes", "secret-scan", "workflow-lint"]
NAMES = {
    "changes": "Guard: Changes", "secret-scan": "Guard: Secrets", "workflow-lint": "Guard: Workflows",
    "lint": "Check: Lint", "build": "Check: Build", "test": "Check: Tests",
    "sca": "Security: SCA", "sast": "Security: SAST", "image": "Image: Build + CVE scan", "gate": "gate",
}
CHECKS = {l: ["lint", "build", "test", "sca", "sast", "image"] for l in LANGS if l != "none"}
CHECKS["none"] = ["image"]


# ───────────────────────── reading the stand-alone workflows ─────────────────────────

def read_lines(fname):
    with open(os.path.join(WFDIR, fname)) as f:
        return f.read().split("\n")


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


def sub(text, old, new, src, count=1):
    assert text.count(old) == count, f"{src}: expected {count}x {old!r}, found {text.count(old)}x"
    return text.replace(old, new)


def with_if(step, cond, src):
    """Put `if: cond` right under the `- name:` line of a step."""
    first, rest = step.split("\n", 1)
    assert first.startswith("      - name: "), f"{src}: step has no name: {first}"
    return f"{first}\n        if: {cond}\n{rest}"


def tool_defaults(lang):
    """{input name: default} of the tool-version inputs of a stand-alone workflow."""
    out = {}
    if lang not in SOURCE:
        return out
    for name, block in input_blocks(SOURCE[lang]).items():
        if name in TOOL_VERSIONS:
            out[name] = re.search(r"^        default: (.*)$", block, re.M).group(1).strip()
    return out


def input_blocks(fname):
    lines = read_lines(fname)
    i = lines.index("    inputs:") + 1
    blocks, key = {}, None
    while i < len(lines) and (lines[i].startswith("      ") or not lines[i].strip()):
        m = re.match(r"^      ([a-z0-9-]+):\s*$", lines[i])
        if m:
            key = m.group(1)
            blocks[key] = []
        if key and lines[i].strip():
            blocks[key].append(lines[i])
        i += 1
    return {k: "\n".join(v) + "\n" for k, v in blocks.items()}


# ───────────────────────── rendering ─────────────────────────

def render_job(key, steps, *, timeout, perms, needs=None, cond=None, outputs=None):
    out = [f"  {key}:", f'    name: "{NAMES[key]}"']
    if needs:
        out.append(f"    needs: [{', '.join(needs)}]")
    if cond:
        out.append(f"    if: {cond}")
    out += ["    runs-on: ubuntu-latest", f"    timeout-minutes: {timeout}", perms]
    if outputs:
        out.append(outputs)
    out.append("    steps:")
    out.append("\n\n".join(steps))
    return "\n".join(out) + "\n"


STAGE2_NEEDS = ["changes"]  # only the change detection: a failed guard must not hide the results of the checks
STAGE2_IF = "${{ needs.changes.outputs.code == 'true' }}"


def guard_jobs():
    """The three guards: inline copies of changed-paths.detect, secret-scan.secret-scan and workflow-lint.actionlint."""
    jobs = []
    for key, fname, src in (("changes", "changed-paths.yaml", "detect"), ("secret-scan", "secret-scan.yaml", "secret-scan"),
                            ("workflow-lint", "workflow-lint.yaml", "actionlint")):
        j = Job(fname, src)
        jobs.append(render_job(key, j.steps, timeout=j.timeout(), perms=j.raw("permissions"),
                               outputs=j.raw("outputs") if "outputs" in j.fields else None))
    return jobs


def lang_jobs(lang):
    """Lint, Build, Tests, SCA, SAST of one language, from its stand-alone workflow."""
    fname = SOURCE[lang]
    tools = tool_defaults(lang)
    lint, build, tests = Job(fname, "lint-format"), Job(fname, "build-check"), Job(fname, "unit-tests")
    sca, sast = Job(fname, "sca"), Job(fname, "sast")
    b_steps, t_steps = list(build.steps), list(tests.steps)
    b_timeout, t_timeout = build.timeout(), tests.timeout()

    def fold(job_key, keep_from, names, cond, into, timeout):
        """Append the steps of an optional job (after its checkout/setup steps) to another job's steps."""
        j = Job(fname, job_key)
        extra = j.steps[keep_from:]
        assert len(extra) == len(names) and all(n in s for s, n in zip(extra, names)), f"{j.src}: steps changed: {[s.split(chr(10))[0] for s in j.steps]}"
        into.extend(with_if(s, cond, j.src) for s in extra)
        return timeout + j.timeout()

    if lang == "go":
        # the smoke test needs the binary: build it once (-o) when the smoke test is on, instead of building twice in two jobs
        s = b_steps[2]
        assert "- name: Build\n" in s, build.src
        s = sub(s, "          BUILD_PATH: ${{ inputs.build-path }}\n", "          BUILD_PATH: ${{ inputs.build-path }}\n          SMOKE_PORT: ${{ inputs.smoke-port }}\n", build.src)
        s = sub(s, '          go build "${targets[@]}"',
                '          if [ -n "$SMOKE_PORT" ]; then\n'
                '            go build -o "$RUNNER_TEMP/server" "${targets[@]}" # kept for the smoke test below\n'
                '          else\n'
                '            go build "${targets[@]}"\n'
                '          fi', build.src)
        b_steps[2] = s
        assert Job(fname, "smoke-test").steps[:2] == build.steps[:2], "go-ci: smoke-test no longer starts like build-check"
        # smoke steps keep the default `success()`: they run the binary the Build step just made, so after a failed build they
        # would only add a second, misleading failure. The E2E step does not depend on the unit tests: `!cancelled()` shows its
        # result even when a unit test failed.
        b_timeout = fold("smoke-test", 3, ["Install grpcurl", "Start server and check gRPC health"], "inputs.smoke-port != ''", b_steps, b_timeout)
        t_timeout = fold("e2e-tests", 2, ["Run E2E tests"], "${{ !cancelled() && inputs.e2e-packages != '' }}", t_steps, t_timeout)
    elif lang == "node":
        # same toolchain and `npm run build` as the Build job already did: only the grpcurl and start/health steps are added
        assert Job(fname, "smoke-test").steps[:3] == build.steps[:3], "node-ci: smoke-test no longer starts with the steps of build-check"
        b_timeout = fold("smoke-test", 4, ["Install grpcurl", "Start server and check gRPC health"], "inputs.smoke-port != ''", b_steps, b_timeout)

    jobs = {"lint": lint, "build": build, "test": tests, "sca": sca, "sast": sast}
    steps = {"lint": lint.steps, "build": b_steps, "test": t_steps, "sca": sca.steps, "sast": sast.steps}
    timeouts = {"lint": lint.timeout(), "build": b_timeout, "test": t_timeout, "sca": sca.timeout(), "sast": sast.timeout()}
    out = []
    for key in ("lint", "build", "test", "sca", "sast"):
        text = steps[key]
        for name, default in tools.items():  # tool versions: the stand-alone default, written in
            text = [s.replace("${{ inputs." + name + " }}", default) for s in text]
        out.append(render_job(key, text, timeout=timeouts[key], perms=jobs[key].raw("permissions"),
                              needs=STAGE2_NEEDS, cond=STAGE2_IF))
    return out


# The image build + CVE scan job. Moved here from service-ci.yaml (v3.2.x), behaviour unchanged except for its needs.
IMAGE_STEPS = """\
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069 # v4.4.1

      - name: Log in to ghcr.io (to read the build cache)
        # a cache that cannot be read only makes the build slower, so a failed login must not fail the pull request
        continue-on-error: true
        uses: docker/login-action@dbcb813823bdd20940b903addbd779551569679f # v4.6.0
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Name the registry build cache
        id: cache
        run: echo "ref=ghcr.io/${GITHUB_REPOSITORY,,}/buildcache" >> "$GITHUB_OUTPUT"

      - name: Build the image into the local Docker daemon
        uses: docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc # v7.4.0
        with:
          context: ${{ inputs.context }}
          file: ${{ inputs.context }}/${{ inputs.dockerfile }}
          push: false
          load: true
          tags: service-ci-scan:local
          # read-only: pull requests (forks included) never write the cache; the publish run on main does
          cache-from: type=registry,ref=${{ steps.cache.outputs.ref }}

      - name: Run Trivy vulnerability scanner
        uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0
        with:
          # same version, threshold and ignore file as the scan in docker-build.yaml
          version: v0.76.1
          image-ref: service-ci-scan:local
          format: table
          severity: CRITICAL,HIGH
          exit-code: 1
          trivyignores: ${{ inputs.trivyignores }}"""


def image_job():
    perms = ("    permissions:\n      contents: read\n"
             "      packages: read # reads the registry build cache (ghcr.io/<repo>/buildcache); writing it is service-publish's job")
    return render_job("image", [IMAGE_STEPS], timeout=30, perms=perms, needs=STAGE2_NEEDS,
                      cond="${{ needs.changes.outputs.code == 'true' && inputs.image }}")


# The gate. Same contract as before: the only required check. Strict about what must have succeeded, lenient about nothing else.
GATE_SCRIPT = r"""
# Jobs this workflow has: the three guards and the checks (generated per language). NEEDS holds the result of every one of them.
guards=" __GUARDS__ "
checks=" __CHECKS__ "

case "$IMAGE" in
  true | false) ;;
  *) echo "::error::input image is '$IMAGE', expected true or false"; exit 1 ;;
esac

mapfile -t rows < <(jq -r 'to_entries[] | [.key, .value.result] | @tsv' <<< "$NEEDS")
for job in $guards $checks; do
  jq -e --arg job "$job" 'has($job)' <<< "$NEEDS" > /dev/null || { echo "::error::job $job is missing from needs"; exit 1; }
done

# 1. The cause first: a job that failed or was cancelled. Everything downstream of a failed guard is skipped by GitHub, and
#    that skip is a consequence, not a finding, so it is not reported as one.
failed=0
unexplained=0
{
  echo "### Gate (code changed: ${CODE:-unknown}, image: $IMAGE)"
} >> "$GITHUB_STEP_SUMMARY"
for row in "${rows[@]}"; do
  IFS=$'\t' read -r job result <<< "$row"
  case "$result" in
    failure | cancelled)
      failed=1
      echo "::error::$job $result"
      echo "- **$job $result**" >> "$GITHUB_STEP_SUMMARY"
      ;;
  esac
done

# 2. What each job should have done. Guards always succeed. Checks succeed when code changed (the image check only if image is on);
#    otherwise they are skipped: docs-only change, or image off. Any other result is unexplained.
declare -A want
for job in $guards; do want[$job]=success; done
for job in $checks; do
  if [ "$CODE" = "true" ] && { [ "$job" != "image" ] || [ "$IMAGE" = "true" ]; }; then want[$job]=success; else want[$job]=skipped; fi
done
if [ "$failed" = 0 ] && [ "$CODE" != "true" ] && [ "$CODE" != "false" ]; then
  echo "::error::change detection gave '$CODE', expected true or false"
  failed=1
fi

{
  echo
  echo "| job | result | expected |"
  echo "|---|---|---|"
} >> "$GITHUB_STEP_SUMMARY"
for row in "${rows[@]}"; do
  IFS=$'\t' read -r job result <<< "$row"
  expected=${want[$job]:-}
  if [ -z "$expected" ]; then expected="(unknown job)"; fi
  printf '%-14s %-10s (expected %s)\n' "$job" "$result" "$expected"
  echo "| $job | $result | $expected |" >> "$GITHUB_STEP_SUMMARY"
  # Only when nothing failed: otherwise the failure above is the cause and skipped jobs downstream of it are expected.
  if [ "$failed" = 0 ] && [ "$result" != "$expected" ]; then
    echo "::error::$job is '$result', expected '$expected'"
    unexplained=1
  fi
done
exit $((failed + unexplained))
"""


def gate_job(lang):
    script = GATE_SCRIPT.replace("__GUARDS__", " ".join(GUARDS)).replace("__CHECKS__", " ".join(CHECKS[lang]))
    body = "\n".join(("          " + l) if l else "" for l in script.strip("\n").split("\n"))
    needs = GUARDS + CHECKS[lang]
    step = (
        "      - name: Every job passed, or was skipped for a reason we know\n"
        "        env:\n"
        "          NEEDS: ${{ toJSON(needs) }}\n"
        "          IMAGE: ${{ inputs.image }}\n"
        "          CODE: ${{ needs.changes.outputs.code }}\n"
        "        run: |\n" + body)
    return render_job("gate", [step], timeout=5, perms="    permissions: {}", needs=needs, cond="${{ always() }}")


def inputs_block(lang):
    text = ""
    if lang != "none":
        src = input_blocks(SOURCE[lang])
        missing = [n for n in DECLARED[lang] if n not in src]
        assert not missing, f"{SOURCE[lang]} has no input {missing}"
        for n in DECLARED[lang]:
            block = src[n]
            for (iname, old), new in DESC_FIX.items():
                if iname == n and old in block:
                    block = block.replace(old, new)
            text += block
    return COMMON_INPUTS + text


HEADER = """\
name: Service CI ({title})

# GENERATED by tools/gen-service-ci.py - do not edit; change the stand-alone workflow or the generator and run the generator.
#
# The pull-request pipeline of {who}, flat: every job is in this file and nothing is called from here, so the Actions graph shows
# exactly what runs, left to right, with short names (`ci / Check: Build`):
#
#   {s1}   (no needs: start at t=0)
#   {s2}
#            (need only `Guard: Changes`; a docs-only change skips them, the guards still run)
#   gate     (needs every job, always runs, names the job that failed first)
#
# The left column means "starts at t=0", not "must pass first": a failed guard does not hide the results of the checks.
# `ci / gate` is the ONLY required check.
{notes}#
# Call it with:  uses: platform-boutique/platform-demo-workflows/.github/workflows/{file}@<sha> # <tag>
# The calling job must grant: {grant}.

on:
  workflow_call:
    inputs:
{inputs}
# Least privilege by default; every job below declares what it needs.
permissions: {{}}

jobs:
"""


def render(lang):
    file = f"service-ci-{lang}.yaml"
    who = "a repository without a language pipeline" if lang == "none" else f"a {TITLE[lang]} app repository"
    s1 = " | ".join(NAMES[k] for k in GUARDS)
    s2 = " | ".join(NAMES[k] for k in CHECKS[lang])
    notes = ""
    if lang == "none":
        notes = ("# `image: false` (product-catalog-db) leaves only the guards and the gate; the image job is then skipped. With the image\n"
                 "# left on this suits a repository whose tests are their own jobs (the developer portal).\n")
    elif lang in ("go", "node"):
        notes = ("# The optional smoke test (input smoke-port) is a step of Check: Build and the E2E tests (input e2e-packages, go) a step of\n"
                 "# Check: Tests: no extra jobs, and the smoke test reuses the build.\n" if lang == "go" else
                 "# The optional smoke test (input smoke-port) is a step of Check: Build: no extra job, and it reuses the build.\n")
    grant = "contents: read, packages: read" + (" and checks: write (the test reporter)" if lang == "dotnet" else "")
    head = HEADER.format(title=TITLE[lang], who=who, s1=s1, s2=s2, notes=notes, file=file, grant=grant, inputs=inputs_block(lang))
    jobs = guard_jobs() + (lang_jobs(lang) if lang != "none" else []) + [image_job(), gate_job(lang)]
    text = head + "\n".join(jobs)
    assert not re.search(r"[ \t]+$", text, re.M), f"{file}: trailing whitespace"
    # every `inputs.<name>` used must be declared in this file
    declared = set(re.findall(r"^      ([a-z0-9-]+):\s*$", head.split("\npermissions:")[0].split("    inputs:\n")[1], re.M))
    used = set(re.findall(r"\binputs\.([a-z0-9-]+)", text.split("\njobs:\n")[1]))
    assert used <= declared, f"{file}: uses undeclared inputs {sorted(used - declared)}"
    return file, text.rstrip("\n") + "\n"


def render_all():
    return dict(render(l) for l in LANGS)


def main():
    for name, text in render_all().items():
        with open(os.path.join(WFDIR, name), "w") as f:
            f.write(text)
        print("wrote", name)


if __name__ == "__main__":
    main()
