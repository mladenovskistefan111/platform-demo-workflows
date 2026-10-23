#!/usr/bin/env python3
"""Consistency checks for this repository (needs PyYAML). usage: tools/check-consistency.py [repo-dir]

1. generated workflows (node/python/java/dotnet-ci, secret-scan, the secret-scan job inside go-ci) are what
   tools/gen-lang-workflows.py writes today;
2. every workflow has `permissions: {}` at the top, every job has a `permissions` block, and every job that runs steps has
   `timeout-minutes` (a job that only calls a reusable workflow cannot have one: the called workflow's jobs do);
3. service-ci.yaml: each input it passes to a language workflow exists there with the same type and default (so the default is
   not silently changed by the pass-through), and each language workflow has the `secret-scan` input.
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

# 3. service-ci pass-through
LANG_WF = {"go": "go-ci.yaml", "node": "node-ci.yaml", "python": "python-ci.yaml", "java": "java-gradle-ci.yaml", "dotnet": "dotnet-ci.yaml"}
for lang, fname in LANG_WF.items():
    if "secret-scan" not in load(fname)["on"]["workflow_call"]["inputs"]:
        problems.append(f"{fname}: input secret-scan is missing")
if os.path.exists(os.path.join(wfdir, "service-ci.yaml")):
    sci = load("service-ci.yaml")
    sci_inputs = sci["on"]["workflow_call"]["inputs"]
    for lang, fname in LANG_WF.items():
        job = sci["jobs"].get(lang)
        if not job:
            problems.append(f"service-ci.yaml: no job for language {lang}")
            continue
        child = load(fname)["on"]["workflow_call"]["inputs"]
        for k, v in (job.get("with") or {}).items():
            if k not in child:
                problems.append(f"service-ci.yaml: job {lang} passes {k}, which {fname} does not have")
                continue
            if k == "secret-scan":
                continue
            expr = str(v).strip()
            src = f"inputs.{k}"
            if src not in expr:
                problems.append(f"service-ci.yaml: job {lang} passes {k} but not from inputs.{k}")
                continue
            if k not in sci_inputs:
                problems.append(f"service-ci.yaml: input {k} (passed to {lang}) is not declared")
                continue
            cd, sd = child[k].get("default"), sci_inputs[k].get("default")
            if child[k].get("type") != sci_inputs[k].get("type"):
                problems.append(f"service-ci.yaml: input {k} has another type than in {fname}")
            if "||" in expr:  # `inputs.x || 'child default'`: the fallback must be the child's default
                if str(cd) not in expr:
                    problems.append(f"service-ci.yaml: fallback for {k} in job {lang} is not {fname}'s default {cd!r}")
            elif cd != sd:
                problems.append(f"service-ci.yaml: input {k} defaults to {sd!r}, {fname} says {cd!r}")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("consistent")
