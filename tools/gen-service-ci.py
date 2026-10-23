#!/usr/bin/env python3
"""Generate the per-language pull-request workflows service-ci-{go,node,python,java,dotnet,none}.yaml.

usage: gen-service-ci.py [repo-root]      (writes <repo-root>/.github/workflows/service-ci-*.yaml)

Why generated: the Actions graph of a caller shows every job of the called workflow, skipped ones too. One workflow for all languages
therefore drew four hollow language boxes and nested boxes inside boxes. These files are FLAT (no nested `uses:` of other workflows)
and contain only what the repository really runs:

    Detect changes | Secret scan | Workflow lint      no needs: start at t=0, together
    Lint | Build | Tests                              need only `Detect changes` (skipped for a docs-only change), run in
    Dependency scan | Code scan | Image build and scan   parallel with the other two (the image is pushed as
                                                         ghcr.io/<repo>:tree-<hash> by runs that may; see wfgen.py)
    gate                                              needs EVERY job, always runs

Job names: sentence case, what the job does, no group prefixes; the caller's job id is the prefix GitHub shows (`ci / Build`), and the
required check stays `ci / gate`. The gate keys on JOB IDS (the keys of `needs`), never on names; it only shows the names.
Run summaries (GITHUB_STEP_SUMMARY, one section per job on the run page): `Detect changes` writes what changed (changed-paths.yaml),
`Image build and scan` the image (tools/wfgen.py), `gate` the verdict, the results and what the Merge button will do.

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
  * the image job is assembled from the steps in tools/wfgen.py, which service-publish.yaml uses too (tree lookup, build, digest record,
    Trivy scan: written once); only the gate script lives in this file.
Every transformation asserts that it matched, so a refactoring of a source workflow fails here instead of silently changing a file.
tools/check-consistency.py fails when a generated file differs from this output.
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(HERE)
WFDIR = os.path.join(ROOT, ".github", "workflows")

sys.path.insert(0, HERE)
import wfgen  # noqa: E402
from wfgen import Job, read_lines, sub, with_if  # noqa: E402

wfgen.set_root(ROOT)

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
# Job names: sentence case, what the job does, no group prefixes (the caller's job id is the prefix GitHub shows: `ci / Build`).
# Language detail belongs in the step names. The same names in all six files (tools/check-consistency.py pins them).
NAMES = {
    "changes": "Detect changes", "secret-scan": "Secret scan", "workflow-lint": "Workflow lint",
    "lint": "Lint", "build": "Build", "test": "Tests",
    "sca": "Dependency scan", "sast": "Code scan", "image": "Image build and scan", "gate": "gate",
}
CHECKS = {l: ["lint", "build", "test", "sca", "sast", "image"] for l in LANGS if l != "none"}
CHECKS["none"] = ["image"]


# ───────────────────────── reading the stand-alone workflows (tools/wfgen.py) ─────────────────────────

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

def render_job(key, steps, **kw):
    return wfgen.render_job(key, NAMES[key], steps, **kw)


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


# The image job: build once per source tree, push it for runs that may, scan that exact digest. The steps are shared with
# service-publish.yaml (tools/wfgen.py), which finds the image this job pushed and ships it instead of building again.
def image_steps():
    return [
        wfgen.checkout_step(),
        wfgen.mode_step(),
        wfgen.buildx_step(),
        wfgen.login_step("Log in to ghcr.io (to read the build cache, and to push the image)",
                         cont="${{ steps.mode.outputs.push != 'true' }}",
                         comment="a run that cannot push only needs the cache, and a cache that cannot be read only makes the build slower: "
                                 "its failed login must not fail the pull request"),
        wfgen.cache_step(),
        wfgen.find_step("${{ steps.mode.outputs.push }}"),
        wfgen.build_step(push="expr", cache_write="when-pushing"),
        wfgen.record_step("${{ steps.mode.outputs.push }}"),
        wfgen.trivy_step("${{ steps.image.outputs.ref }}",
                         "same version, threshold and ignore file as the scan in service-publish.yaml (which scans the same digest again)"),
        wfgen.summary_step(scan=True),
    ]


def image_job():
    perms = ("    permissions:\n      contents: read\n"
             "      packages: write # pushes ghcr.io/<repo>:tree-<hash> for a run that may (same repository, not Dependabot); a fork's token is read-only")
    return render_job("image", image_steps(), timeout=30, perms=perms, needs=STAGE2_NEEDS,
                      cond="${{ needs.changes.outputs.code == 'true' && inputs.image }}")


# The gate. Same contract as before: the only required check. Strict about what must have succeeded, lenient about nothing else.
GATE_SCRIPT = r"""
# Jobs this workflow has, in the order of the graph: the three guards, then the checks (generated per language), and their names.
guards=" __GUARDS__ "
checks=" __CHECKS__ "
declare -A names=(__NAMES__)

# Text that does not come from this script (an unknown job id, a result) is shown with letters, digits and . _ - only.
safe() { printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9._-' '?' | cut -c1-60; }
label() { echo "${names[$1]:-$(safe "$1")}"; }
words() {
  case "$1" in
    success) echo passed ;;
    failure) echo failed ;;
    cancelled) echo cancelled ;;
    skipped) echo skipped ;;
    *) safe "$1" ;;
  esac
}

stop() { # stop <message for the log> <heading for the summary>: nothing can be judged
  echo "::error::$1"
  echo "### Blocked: $2" >> "$GITHUB_STEP_SUMMARY"
  exit 1
}

case "$IMAGE" in
  true | false) ;;
  *) stop "input image is '$IMAGE', expected true or false" "the input image is neither true nor false" ;;
esac

declare -A result
unknown=()
mapfile -t rows < <(jq -r 'to_entries[] | [.key, .value.result] | @tsv' <<< "$NEEDS")
for row in "${rows[@]}"; do
  IFS=$'\t' read -r job res <<< "$row"
  result[$job]=$res
  [ -n "${names[$job]+x}" ] || unknown+=("$job")
done
for job in $guards $checks; do
  [ -n "${result[$job]+x}" ] || stop "job $job is missing from needs" "$(label "$job") is missing from the results the gate received"
done

# 1. The cause first: a job that failed or was cancelled. Everything downstream of a failed guard is skipped by GitHub, and
#    that skip is a consequence, not a finding, so it is not reported as one.
failed=0
unexplained=0
bad_code=0
causes=()
for job in $guards $checks "${unknown[@]}"; do
  case "${result[$job]}" in
    failure | cancelled)
      failed=1
      echo "::error::$job ${result[$job]}"
      causes+=("$job")
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
  bad_code=1
fi

table=""
first_unexplained=""
for job in $guards $checks "${unknown[@]}"; do
  res=${result[$job]}
  expected=${want[$job]:-}
  shown_expected=${expected:-(unknown job)}
  printf '%-14s %-10s (expected %s)\n' "$job" "$res" "$shown_expected"
  if [ -z "$expected" ]; then
    cell="$(words "$res") (unknown job)"
  elif [ "$res" = "$expected" ]; then
    cell=$(words "$res")
    if [ "$res" = skipped ]; then
      if [ "$CODE" = "false" ]; then cell="skipped: documentation only"; else cell="skipped: no image pipeline"; fi
    fi
  elif [ "$failed" = 1 ]; then
    cell=$(words "$res")
  else
    cell="$(words "$res"), expected $(words "$expected")"
  fi
  table+="| $(label "$job") | $cell |"$'\n'
  # Only when nothing failed: otherwise the failure above is the cause and skipped jobs downstream of it are expected.
  if [ "$failed" = 0 ] && [ "$res" != "$expected" ]; then
    echo "::error::$job is '$res', expected '$shown_expected'"
    unexplained=1
    [ -n "$first_unexplained" ] || first_unexplained=$job
  fi
done

# 3. The summary: the verdict first, then what changed, the results, and what the Merge button will do.
blocked=1
if [ "${#causes[@]}" -gt 0 ]; then
  verdict="Blocked: $(label "${causes[0]}") $(words "${result[${causes[0]}]}")"
  [ "${#causes[@]}" -eq 1 ] || verdict+=" (and $((${#causes[@]} - 1)) more)"
elif [ "$bad_code" = 1 ]; then
  verdict="Blocked: change detection gave no valid answer"
elif [ "$unexplained" = 1 ]; then
  verdict="Blocked: $(label "$first_unexplained") ended as $(words "${result[$first_unexplained]}"), which was not expected"
elif [ "$EVENT" = pull_request ]; then
  verdict="Ready to merge"
  blocked=0
else
  verdict="All checks passed"
  blocked=0
fi

{
  echo "### $verdict"
  echo
  case "$CODE" in
    true) echo "Change: **code**. All checks apply."; echo ;;
    false) echo "Change: **documentation only**. Tests, scans and the image build were skipped."; echo ;;
  esac
  echo "| Job | Result |"
  echo "|---|---|"
  printf '%s' "$table"
  if [ "$EVENT" = pull_request ]; then
    echo
    if [ "$blocked" = 1 ]; then
      echo "Merging is blocked: \`ci / gate\` is the required check. The \`merge\` job does not run after a failed run."
    elif [ "$HEAD_REPO" = "$GITHUB_REPOSITORY" ] && [ "$ACTOR" != 'dependabot[bot]' ]; then
      publish=""
      [ "$IMAGE" != true ] || publish=" and start Publish"
      echo "**Merge button:** approve the \`merge\` environment on this run page (Review deployments) to squash-merge this pull request${publish}."
      if [ "$IMAGE" = true ] && [ "$CODE" = false ]; then
        echo "Publish will find only documentation and publish nothing."
      fi
    else
      echo "The \`merge\` job does not run for fork or Dependabot pull requests: use the Merge button on the pull request."
      if [ "$IMAGE" = true ]; then echo "Publish starts from the push to \`main\`."; fi
    fi
  fi
} >> "$GITHUB_STEP_SUMMARY"
exit $((failed + unexplained))
"""


def gate_job(lang):
    names = " ".join(f'[{k}]="{NAMES[k]}"' for k in GUARDS + CHECKS[lang])
    script = (GATE_SCRIPT.replace("__GUARDS__", " ".join(GUARDS)).replace("__CHECKS__", " ".join(CHECKS[lang]))
              .replace("__NAMES__", names))
    body = "\n".join(("          " + l) if l else "" for l in script.strip("\n").split("\n"))
    needs = GUARDS + CHECKS[lang]
    step = (
        "      - name: Every job passed, or was skipped for a reason we know\n"
        "        env:\n"
        "          NEEDS: ${{ toJSON(needs) }}\n"
        "          IMAGE: ${{ inputs.image }}\n"
        "          CODE: ${{ needs.changes.outputs.code }}\n"
        "          EVENT: ${{ github.event_name }}\n"
        "          HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}\n"
        "          ACTOR: ${{ github.actor }}\n"
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
# exactly what runs, left to right, with plain names (`ci / Build`):
#
#   {s1}   (no needs: start at t=0)
#   {s2}
#            (need only `Detect changes`; a docs-only change skips them, the guards still run)
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
        notes = ("# The optional smoke test (input smoke-port) is a step of Build and the E2E tests (input e2e-packages, go) a step of\n"
                 "# Tests: no extra jobs, and the smoke test reuses the build.\n" if lang == "go" else
                 "# The optional smoke test (input smoke-port) is a step of Build: no extra job, and it reuses the build.\n")
    grant = "contents: read and packages: write (the image job pushes the tree image)" + (" and checks: write (the test reporter)" if lang == "dotnet" else "")
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
