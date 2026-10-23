#!/usr/bin/env python3
"""Consistency checks for this repository (needs PyYAML). usage: tools/check-consistency.py [repo-dir]

1. generated workflows are what their generators write today: node/python/java/dotnet-ci, secret-scan and the secret-scan job
   inside go-ci (tools/gen-lang-workflows.py), service-ci-<language> (tools/gen-service-ci.py);
2. every workflow has `permissions: {}` at the top, every job has a `permissions` block, and every job that runs steps has
   `timeout-minutes` (a job that only calls a reusable workflow cannot have one: the called workflow's jobs do);
3. service-ci-<language> (the flat pull-request workflows):
   - no job calls another workflow (`uses:` at job level), no `language` input;
   - the inputs a file declares have the same type, default and `required` as in the stand-alone language workflow, and nothing
     is lost: every input of the stand-alone workflow is declared, except tool versions (fixed values) and `secret-scan`;
   - the common inputs (image, context, dockerfile, trivyignores) are identical in all six files;
   - the inline guards equal their reusable originals (changed-paths, secret-scan, workflow-lint): same steps, runner, timeout,
     permissions and outputs, so a fix in the original cannot be forgotten here;
   - the same job ids and names in every file; the guards have no `needs`, every other job needs only `changes`, and `gate`
     needs EVERY other job (a job missing from gate.needs would be a check nobody waits for);
   - every action pin equals the pin used by the other workflows of this repository, Trivy has the version docker-build uses;
4. each stand-alone language workflow has the `secret-scan` input.
Exit code 1 and one line per problem when something is off.
"""
import os, shutil, subprocess, sys, tempfile
import yaml

root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
wfdir = os.path.join(root, ".github", "workflows")
problems = []


def load(name):
    with open(os.path.join(wfdir, name)) as f:
        wf = yaml.safe_load(f)
    if True in wf:  # YAML 1.1 reads the key `on` as a boolean
        wf["on"] = wf.pop(True)
    return wf


# 1. generated files are current
with tempfile.TemporaryDirectory() as tmp:
    os.makedirs(os.path.join(tmp, ".github", "workflows"))
    shutil.copy(os.path.join(wfdir, "go-ci.yaml"), os.path.join(tmp, ".github", "workflows", "go-ci.yaml"))
    subprocess.run([sys.executable, os.path.join(root, "tools", "gen-lang-workflows.py"), tmp], check=True, stdout=subprocess.DEVNULL)
    for name in sorted(os.listdir(os.path.join(tmp, ".github", "workflows"))):
        fresh = open(os.path.join(tmp, ".github", "workflows", name)).read()
        have = open(os.path.join(wfdir, name)).read()
        if fresh != have:
            problems.append(f"{name} is not what tools/gen-lang-workflows.py generates: run it and commit the result")

# 1b. service-ci-<language> are what tools/gen-service-ci.py writes (it reads the stand-alone workflows from a copy of this tree)
with tempfile.TemporaryDirectory() as tmp:
    shutil.copytree(wfdir, os.path.join(tmp, ".github", "workflows"))
    subprocess.run([sys.executable, os.path.join(root, "tools", "gen-service-ci.py"), tmp], check=True, stdout=subprocess.DEVNULL)
    for lang in ("go", "node", "python", "java", "dotnet", "none"):
        name = f"service-ci-{lang}.yaml"
        fresh = open(os.path.join(tmp, ".github", "workflows", name)).read()
        path = os.path.join(wfdir, name)
        if not os.path.exists(path) or open(path).read() != fresh:
            problems.append(f"{name} is not what tools/gen-service-ci.py generates: run it and commit the result")

# 2. permissions and timeouts
for name in sorted(os.listdir(wfdir)):
    if not name.endswith((".yaml", ".yml")):
        continue
    wf = load(name)
    if wf.get("permissions") != {}:
        problems.append(f"{name}: top-level `permissions: {{}}` is missing")
    for key, j in (wf.get("jobs") or {}).items():
        if "permissions" not in j:
            problems.append(f"{name}: job {key} has no permissions block")
        if "uses" in j:
            if "timeout-minutes" in j:
                problems.append(f"{name}: job {key} calls a reusable workflow and cannot have timeout-minutes")
        elif "timeout-minutes" not in j:
            problems.append(f"{name}: job {key} has no timeout-minutes")

# 3. the flat service-ci-<language> workflows
LANG_WF = {"go": "go-ci.yaml", "node": "node-ci.yaml", "python": "python-ci.yaml", "java": "java-gradle-ci.yaml", "dotnet": "dotnet-ci.yaml"}
TOOL_INPUTS = {"golangci-lint-version", "govulncheck-version", "gosec-version", "semgrep-version", "ruff-version", "pip-audit-version", "bandit-version"}
COMMON_INPUTS = ["image", "context", "dockerfile", "trivyignores"]
GUARDS = {"changes": ("changed-paths.yaml", "detect"), "secret-scan": ("secret-scan.yaml", "secret-scan"),
          "workflow-lint": ("workflow-lint.yaml", "actionlint")}
JOB_NAMES = {"changes": "Guard: Changes", "secret-scan": "Guard: Secrets", "workflow-lint": "Guard: Workflows", "lint": "Check: Lint",
             "build": "Check: Build", "test": "Check: Tests", "sca": "Security: SCA", "sast": "Security: SAST",
             "image": "Image: Build + CVE scan", "gate": "gate"}

for fname in LANG_WF.values():
    if "secret-scan" not in load(fname)["on"]["workflow_call"]["inputs"]:
        problems.append(f"{fname}: input secret-scan is missing")

# pins used by the workflows that are not generated by gen-service-ci.py
import re
pin = re.compile(r"uses: ([\w.-]+/[\w./-]+)@([0-9a-f]{40})")
pins = {}
for name in os.listdir(wfdir):
    if name.endswith(".yaml") and not name.startswith("service-ci-"):
        for action, sha in pin.findall(open(os.path.join(wfdir, name)).read()):
            if not action.startswith("platform-boutique/"):
                pins.setdefault(action, set()).add(sha)
docker_build = open(os.path.join(wfdir, "docker-build.yaml")).read()

flat = {}
flat_common = None
for lang in ["go", "node", "python", "java", "dotnet", "none"]:
    fname = f"service-ci-{lang}.yaml"
    if not os.path.exists(os.path.join(wfdir, fname)):
        problems.append(f"{fname} is missing")
        continue
    wf = flat[lang] = load(fname)
    ins = wf["on"]["workflow_call"]["inputs"]
    jobs = wf["jobs"]
    if "language" in ins:
        problems.append(f"{fname}: has a `language` input; the language is the file")
    for k, j in jobs.items():
        if "uses" in j:
            problems.append(f"{fname}: job {k} calls another workflow; the file must be flat")
    # inputs
    if lang != "none":
        std = load(LANG_WF[lang])["on"]["workflow_call"]["inputs"]
        for k, v in ins.items():
            if k in COMMON_INPUTS:
                continue
            if k not in std:
                problems.append(f"{fname}: input {k} does not exist in {LANG_WF[lang]}")
                continue
            for attr in ("type", "default", "required"):
                if v.get(attr) != std[k].get(attr):
                    problems.append(f"{fname}: input {k} {attr} is {v.get(attr)!r}, {LANG_WF[lang]} says {std[k].get(attr)!r}")
        for k in std:
            if k not in ins and k not in TOOL_INPUTS and k != "secret-scan":
                problems.append(f"{fname}: input {k} of {LANG_WF[lang]} is not declared")
    for k in COMMON_INPUTS:
        if k not in ins:
            problems.append(f"{fname}: common input {k} is missing")
    if flat_common is None:
        flat_common = {k: ins.get(k) for k in COMMON_INPUTS}
    elif {k: ins.get(k) for k in COMMON_INPUTS} != flat_common:
        problems.append(f"{fname}: common inputs differ from the other service-ci files")
    # guards equal their originals
    for jid, (orig_file, orig_job) in GUARDS.items():
        a, b = jobs.get(jid), load(orig_file)["jobs"][orig_job]
        if a is None:
            problems.append(f"{fname}: guard job {jid} is missing")
            continue
        for field in ("steps", "runs-on", "timeout-minutes", "permissions", "outputs"):
            if a.get(field) != b.get(field):
                problems.append(f"{fname}: job {jid} differs from {orig_file} ({orig_job}) in {field}")
    # graph and names
    expected = {"changes", "secret-scan", "workflow-lint", "image", "gate"} | (set() if lang == "none" else {"lint", "build", "test", "sca", "sast"})
    if set(jobs) != expected:
        problems.append(f"{fname}: jobs are {sorted(jobs)}, expected {sorted(expected)}")
    for jid, j in jobs.items():
        if j.get("name") != JOB_NAMES.get(jid):
            problems.append(f"{fname}: job {jid} is named {j.get('name')!r}, expected {JOB_NAMES.get(jid)!r}")
        if jid in GUARDS and "needs" in j:
            problems.append(f"{fname}: guard {jid} has needs; the guards start at t=0")
        elif jid not in GUARDS and jid != "gate" and j.get("needs") != ["changes"]:
            problems.append(f"{fname}: job {jid} needs {j.get('needs')}, expected [changes] only")
    gate = jobs.get("gate", {})
    if sorted(gate.get("needs", [])) != sorted(k for k in jobs if k != "gate"):
        problems.append(f"{fname}: gate.needs {sorted(gate.get('needs', []))} is not every other job {sorted(k for k in jobs if k != 'gate')}")
    if gate.get("if") != "${{ always() }}":
        problems.append(f"{fname}: gate must run with `if: ${{{{ always() }}}}`")
    # pins
    text = open(os.path.join(wfdir, fname)).read()
    for action, sha in pin.findall(text):
        if action in pins and sha not in pins[action]:
            problems.append(f"{fname}: {action}@{sha[:7]} is not the pin the other workflows use")
    m = re.search(r"version: (v[\d.]+)\s+image-ref: service-ci-scan:local", text)
    if not m or f"version: {m.group(1)}" not in docker_build:
        problems.append(f"{fname}: Trivy version of the image job is not the one docker-build.yaml uses")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("consistent")
