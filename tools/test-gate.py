#!/usr/bin/env python3
"""Unit test of the `gate` job of every generated service-ci-*.yaml: the script is taken out of the YAML as written, run with bash
and fed the same environment GitHub gives it (NEEDS = toJSON(needs), IMAGE, CODE). Needs bash, jq and PyYAML.
usage: tools/test-gate.py [repo-dir]     exit 1 and one line per failed scenario"""
import json, os, subprocess, sys, tempfile
import yaml

root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
wfdir = os.path.join(root, ".github", "workflows")
GUARDS = ["changes", "secret-scan", "workflow-lint"]
failures, count = [], 0


def gate_of(lang):
    with open(os.path.join(wfdir, f"service-ci-{lang}.yaml")) as f:
        wf = yaml.safe_load(f)
    job = wf["jobs"]["gate"]
    return job["needs"], job["steps"][0]["run"]


def run(script, needs, image, code):
    with tempfile.TemporaryDirectory() as tmp:
        summary = os.path.join(tmp, "summary.md")
        open(summary, "w").close()
        env = {"PATH": os.environ["PATH"], "NEEDS": json.dumps(needs), "IMAGE": image, "CODE": code, "GITHUB_STEP_SUMMARY": summary}
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

    def check(name, expect_ok, needs_, image="true", code="true", contains=(), absent=()):
        global count
        count += 1
        rc, out, summ = run(script, needs_, image, code)
        text = out + summ
        problems = []
        if (rc == 0) != expect_ok:
            problems.append(f"exit code {rc}, expected {'0' if expect_ok else 'non-zero'}")
        problems += [f"missing {s!r}" for s in contains if s not in text]
        problems += [f"unexpected {s!r}" for s in absent if s in text]
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
    check("step summary has the table", True, n, contains=["| job | result | expected |", "| changes | success | success |"])

if failures:
    print("\n\n".join(failures))
    print(f"\n{len(failures)} of {count} gate scenarios failed")
    sys.exit(1)
print(f"gate: {count} scenarios over 6 workflows ok")
