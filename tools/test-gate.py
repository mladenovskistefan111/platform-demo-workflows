#!/usr/bin/env python3
"""Unit test of the `gate` job of every generated service-ci-*.yaml: the script is taken out of the YAML as written, run with bash
and fed the same environment GitHub gives it (NEEDS = toJSON(needs), IMAGE, CODE, and EVENT / HEAD_REPO / ACTOR for the line about the
Merge button). It checks the verdict (exit code and the exact job named), the run summary (verdict first, plain job names, the results,
what the Merge button will do) and that the gate keys on job ids, not names. Needs bash, jq and PyYAML.
usage: tools/test-gate.py [repo-dir]     exit 1 and one line per failed scenario"""
import json, os, subprocess, sys, tempfile
import yaml

root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
wfdir = os.path.join(root, ".github", "workflows")
GUARDS = ["changes", "secret-scan", "workflow-lint"]
NAME = {"changes": "Detect changes", "secret-scan": "Secret scan", "workflow-lint": "Workflow lint", "lint": "Lint", "build": "Build",
        "test": "Tests", "sca": "Dependency scan", "sast": "Code scan", "image": "Image build and scan"}
failures, count = [], 0


def gate_of(lang):
    with open(os.path.join(wfdir, f"service-ci-{lang}.yaml")) as f:
        wf = yaml.safe_load(f)
    job = wf["jobs"]["gate"]
    return job["needs"], job["steps"][0]["run"]


PR = {"event": "pull_request", "head": "o/r", "actor": "alice"}


def run(script, needs, image, code, event=PR):
    with tempfile.TemporaryDirectory() as tmp:
        summary = os.path.join(tmp, "summary.md")
        open(summary, "w").close()
        env = {"PATH": os.environ["PATH"], "NEEDS": json.dumps(needs), "IMAGE": image, "CODE": code, "GITHUB_STEP_SUMMARY": summary,
               "EVENT": event["event"], "HEAD_REPO": event["head"], "ACTOR": event["actor"], "GITHUB_REPOSITORY": "o/r"}
        p = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, capture_output=True, text=True)
        return p.returncode, p.stdout, open(summary).read()


for lang in ("go", "node", "python", "java", "dotnet", "none"):
    all_jobs, script = gate_of(lang)
    checks = [j for j in all_jobs if j not in GUARDS]
    assert set(GUARDS) <= set(all_jobs)

    def needs(code="true", img=True, **override):
        n = {}
        for j in all_jobs:
            if j in GUARDS:
                n[j] = {"result": "success", "outputs": {"code": code} if j == "changes" else {}}
            elif code == "true" and (j != "image" or img):
                n[j] = {"result": "success", "outputs": {}}
            else:
                n[j] = {"result": "skipped", "outputs": {}}
        for j, r in override.items():
            n[j.replace("_", "-")]["result"] = r
        return n

    def check(name, expect_ok, needs_, image="true", code="true", contains=(), absent=(), event=PR, summary_has=(), summary_lacks=(), starts=None):
        global count
        count += 1
        rc, out, summ = run(script, needs_, image, code, event)
        text = out + summ
        problems = []
        if (rc == 0) != expect_ok:
            problems.append(f"exit code {rc}, expected {'0' if expect_ok else 'non-zero'}")
        problems += [f"missing {s!r}" for s in contains if s not in text]
        problems += [f"unexpected {s!r}" for s in absent if s in text]
        problems += [f"summary lacks {s!r}" for s in summary_has if s not in summ]
        problems += [f"summary has {s!r}" for s in summary_lacks if s in summ]
        if starts is not None and not summ.startswith(starts):
            problems.append(f"summary starts with {summ.splitlines()[:1]}, expected {starts!r}")
        if problems:
            failures.append(f"{lang}: {name}: {'; '.join(problems)}\n{text}")

    victim = "sca" if "sca" in checks else "image"
    check("all green, code changed", True, needs())
    check("docs-only change: guards ran, checks skipped", True, needs(code="false"), code="false")
    check("docs-only change, image off", True, needs(code="false", img=False), image="false", code="false")
    check("code changed, image off: image skipped", True, needs(img=False), image="false")
    check("image off but the image job ran", False, {**needs(img=False), "image": {"result": "success", "outputs": {}}}, image="false", contains=["image is 'success', expected 'skipped'"])
    check("a check failed", False, needs(**{victim: "failure"}), contains=[f"{victim} failure"], absent=["expected 'success'"])
    check("a check was cancelled", False, needs(**{victim: "cancelled"}), contains=[f"{victim} cancelled"])
    check("secret scan failed (the checks still ran): the cause is named", False, needs(secret_scan="failure"),
          contains=["secret-scan failure"], absent=["expected 'success'"])
    check("secret scan failed, checks skipped: the skips are a consequence, not reported",
          False, {**needs(secret_scan="failure"), **{j: {"result": "skipped", "outputs": {}} for j in checks}},
          contains=["secret-scan failure"], absent=["expected 'success'", "expected 'skipped'"])
    check("workflow lint failed (checks skipped as a consequence)", False,
          {**needs(workflow_lint="failure"), **{j: {"result": "skipped", "outputs": {}} for j in checks}},
          contains=["workflow-lint failure"], absent=["expected 'success'"])
    check("change detection failed (no output)", False,
          {**needs(changes="failure"), **{j: {"result": "skipped", "outputs": {}} for j in checks}}, code="",
          contains=["changes failure"], absent=["expected 'success'", "expected true or false"])
    check("a guard was cancelled", False, needs(secret_scan="cancelled"), contains=["secret-scan cancelled"])
    check("a check is skipped with no explanation", False, needs(**{victim: "skipped"}), contains=[f"{victim} is 'skipped', expected 'success'"])
    check("docs-only change but a check ran", False, needs(code="false", **{victim: "success"}), code="false", contains=[f"{victim} is 'success', expected 'skipped'"])
    check("a guard was skipped", False, needs(secret_scan="skipped"), contains=["secret-scan is 'skipped', expected 'success'"])
    check("unknown value of the image input", False, needs(), image="maybe", contains=["input image is 'maybe'"])
    check("change detection gave a value that is neither true nor false", False, needs(), code="yes", contains=["gave 'yes'"])
    n = needs()
    del n["secret-scan"]
    check("a job is missing from needs", False, n, contains=["secret-scan is missing from needs"])
    check("a job nobody told the gate about", False, {**needs(), "lint-extra": {"result": "success", "outputs": {}}}, contains=["(unknown job)"])
    check("failed and a different job unexplained-skipped: only the failure is the cause", False,
          needs(**{victim: "failure", "workflow_lint": "skipped"}), contains=[f"{victim} failure"], absent=["workflow-lint is 'skipped'"])
    n = needs()
    check("step summary has the table", True, n, summary_has=["| Job | Result |", "| Detect changes | passed |", "| Secret scan | passed |"])

    # ---- the run summary: the verdict first, plain names, what the Merge button will do ----
    vname = NAME[victim]
    check("summary: green pull request", True, needs(), starts="### Ready to merge\n",
          summary_has=["Change: **code**. All checks apply.", f"| {vname} | passed |", "| Image build and scan | passed |" if "image" in all_jobs else "|",
                       "**Merge button:** approve the `merge` environment on this run page (Review deployments) to squash-merge this pull request and start Publish."],
          summary_lacks=["Blocked", "failed"])
    check("summary: the names are the plain names, no prefixes", True, needs(), summary_lacks=["Guard:", "Check:", "Security:", "Image:", "| changes |", "| sca |"])
    check("summary: a run that is not a pull request has no Merge line", True, needs(), event={"event": "workflow_dispatch", "head": "", "actor": "alice"},
          starts="### All checks passed\n", summary_lacks=["Merge button", "Merging is blocked", "merge"])
    check("summary: docs-only change", True, needs(code="false"), code="false", starts="### Ready to merge\n",
          summary_has=["Change: **documentation only**. Tests, scans and the image build were skipped.", f"| {vname} | skipped: documentation only |",
                       "| Detect changes | passed |", "Publish will find only documentation and publish nothing."])
    if "image" in all_jobs:
        check("summary: image off says so and does not promise Publish", True, needs(img=False), image="false",
              summary_has=["| Image build and scan | skipped: no image pipeline |", "squash-merge this pull request."], summary_lacks=["start Publish"])
    check("summary: a failed check is the verdict, the Merge line says it is blocked", False, needs(**{victim: "failure"}),
          starts=f"### Blocked: {vname} failed\n", summary_has=[f"| {vname} | failed |", "Merging is blocked: `ci / gate` is the required check."],
          summary_lacks=["Merge button", "Ready to merge"])
    check("summary: a cancelled check", False, needs(**{victim: "cancelled"}), starts=f"### Blocked: {vname} cancelled\n")
    check("summary: a failed guard is named, its skipped checks are only 'skipped'", False,
          {**needs(secret_scan="failure"), **{j: {"result": "skipped", "outputs": {}} for j in checks}},
          starts="### Blocked: Secret scan failed\n", summary_has=["| Secret scan | failed |"] + [f"| {NAME[j]} | skipped |" for j in checks],
          summary_lacks=["expected"])
    n2 = needs(secret_scan="failure", **{victim: "failure"})
    check("summary: two failures name the first in graph order and count the others", False, n2,
          starts="### Blocked: Secret scan failed (and 1 more)\n")
    check("summary: a check that is skipped without a reason", False, needs(**{victim: "skipped"}),
          starts=f"### Blocked: {vname} ended as skipped, which was not expected\n", summary_has=[f"| {vname} | skipped, expected passed |"])
    check("summary: change detection returned nothing valid", False, needs(), code="yes", starts="### Blocked: change detection gave no valid answer\n",
          summary_lacks=["yes"])
    check("summary: a pull request from a fork", True, needs(), event={"event": "pull_request", "head": "fork/r", "actor": "alice"},
          summary_has=["The `merge` job does not run for fork or Dependabot pull requests: use the Merge button on the pull request.", "Publish starts from the push to `main`."],
          summary_lacks=["approve the `merge` environment"])
    check("summary: a Dependabot pull request", True, needs(), event={"event": "pull_request", "head": "o/r", "actor": "dependabot[bot]"},
          summary_has=["does not run for fork or Dependabot pull requests"], summary_lacks=["approve the `merge` environment"])
    check("summary: a fork pull request that fails is blocked, not 'use the Merge button'", False, needs(**{victim: "failure"}),
          event={"event": "pull_request", "head": "fork/r", "actor": "alice"}, summary_has=["Merging is blocked"], summary_lacks=["use the Merge button"])
    check("summary: an invalid image input still leaves a summary", False, needs(), image="maybe", starts="### Blocked: the input image is neither true nor false")
    n3 = needs()
    del n3["secret-scan"]
    check("summary: a missing job is named", False, n3, starts="### Blocked: Secret scan is missing from the results the gate received")
    check("summary: text of an unknown job is reduced to plain characters", False,
          {**needs(), "ev`il<b>|x": {"result": "success\n<img src=x>", "outputs": {}}}, summary_lacks=["ev`il", "<b>", "<img", "|x"], contains=["unknown job"])

    # ---- the gate keys on job ids, never on names: the same results with other names in the YAML text change nothing ----
    renamed = script
    for old_name in NAME.values():
        renamed = renamed.replace(f'"{old_name}"', '"Something else"')
    count += 1
    rc1, _, _ = run(script, needs(**{victim: "failure"}), "true", "true")
    rc2, _, summ2 = run(renamed, needs(**{victim: "failure"}), "true", "true")
    rc3, _, _ = run(renamed, needs(), "true", "true")
    if not (rc1 != 0 and rc2 != 0 and rc3 == 0 and "Something else" in summ2):
        failures.append(f"{lang}: renaming the jobs changed the verdict (rc {rc1}, {rc2}, {rc3})")

if failures:
    print("\n\n".join(failures))
    print(f"\n{len(failures)} of {count} gate scenarios failed")
    sys.exit(1)
print(f"gate: {count} scenarios over 6 workflows ok")
