#!/usr/bin/env python3
"""Consistency checks for this repository (needs PyYAML). usage: tools/check-consistency.py [repo-dir]

1. generated workflows are what their generators write today: node/python/java/dotnet-ci, secret-scan and the secret-scan job
   inside go-ci (tools/gen-lang-workflows.py), service-ci-<language> (tools/gen-service-ci.py), service-publish
   (tools/gen-service-publish.py);
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
   - the image job: job id `image`, permissions contents read + packages write, and the steps it shares with service-publish (the
     tree lookup, the build labels, the digest record, the Trivy scan) are the same text as there;
4. each stand-alone language workflow has the `secret-scan` input;
5. service-merge.yaml (the merge button called by every ci.yaml): one job in the environment `merge`, approval check first, no action;
   the registry build cache is written by publish and by pull-request runs that push; prune has separate N for `sha-*` and `tree-*`;
   service-publish.yaml (flat, generated): the job graph is Guard: Changes -> Image: Find or build -> Image: Scan -> Publish: Copy, sign,
   verify -> (Publish: SBOM | Deploy: Open dev PR | Cleanup: Prune staging); the only nested workflow is gitops-promote.yaml at the
   pinned commit that already exists (a sibling reference needs an existing SHA), the prune job is best effort
   (`continue-on-error`) and never waits for or blocks the helm pull request, the SBOM job is not on the way to it, no job output
   can carry the Docker Hub account name (GitHub drops outputs that contain a secret), every job has the permissions it needs, the
   unchanged steps equal docker-promote.yaml's, and release.yaml exists with a validated version input.
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

# 1c. service-publish.yaml is what tools/gen-service-publish.py writes
with tempfile.TemporaryDirectory() as tmp:
    shutil.copytree(wfdir, os.path.join(tmp, ".github", "workflows"))
    subprocess.run([sys.executable, os.path.join(root, "tools", "gen-service-publish.py"), tmp], check=True, stdout=subprocess.DEVNULL)
    fresh = open(os.path.join(tmp, ".github", "workflows", "service-publish.yaml")).read()
    path = os.path.join(wfdir, "service-publish.yaml")
    if not os.path.exists(path) or open(path).read() != fresh:
        problems.append("service-publish.yaml is not what tools/gen-service-publish.py generates: run it and commit the result")

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
flat_images = {}
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
    m = re.search(r"version: (v[\d.]+)\s+image-ref: \$\{\{ steps\.image\.outputs\.ref \}\}", text)
    if not m or f"version: {m.group(1)}" not in docker_build:
        problems.append(f"{fname}: Trivy version of the image job is not the one docker-build.yaml uses")
    # the image job
    image = jobs.get("image", {})
    if image.get("permissions") != {"contents": "read", "packages": "write"}:
        problems.append(f"{fname}: image job permissions are {image.get('permissions')}, expected contents read + packages write")
    flat_images[lang] = image

# 5. service-publish.yaml
PUBLISH_NAMES = {"changes": "Guard: Changes", "image": "Image: Find or build", "scan": "Image: Scan", "publish": "Publish: Copy, sign, verify",
                 "sbom": "Publish: SBOM", "deploy": "Deploy: Open dev PR", "prune": "Cleanup: Prune staging"}
PUBLISH_NEEDS = {"changes": None, "image": ["changes"], "scan": ["image"], "publish": ["image", "scan"], "sbom": ["publish"],
                 "deploy": ["publish"], "prune": ["publish"]}
PUBLISH_PERMS = {"changes": {"contents": "read"}, "image": {"contents": "read", "packages": "write"},
                 "scan": {"contents": "read", "packages": "read"}, "publish": {"id-token": "write", "contents": "read", "packages": "read"},
                 "sbom": {"id-token": "write", "contents": "read"}, "deploy": {}, "prune": {"contents": "read", "packages": "write"}}
GITOPS = "platform-boutique/platform-demo-workflows/.github/workflows/gitops-promote.yaml@341f641320553ad73969b637ff125466e6b2d99c"
pub_text = open(os.path.join(wfdir, "service-publish.yaml")).read()
pub = load("service-publish.yaml")
pj = pub["jobs"]
if set(pj) != set(PUBLISH_NAMES):
    problems.append(f"service-publish.yaml: jobs are {sorted(pj)}, expected {sorted(PUBLISH_NAMES)}")
for jid, j in pj.items():
    if j.get("name") != PUBLISH_NAMES.get(jid):
        problems.append(f"service-publish.yaml: job {jid} is named {j.get('name')!r}, expected {PUBLISH_NAMES.get(jid)!r}")
    if j.get("needs") != PUBLISH_NEEDS.get(jid):
        problems.append(f"service-publish.yaml: job {jid} needs {j.get('needs')}, expected {PUBLISH_NEEDS.get(jid)}")
    if (j.get("permissions") or {}) != PUBLISH_PERMS.get(jid):
        problems.append(f"service-publish.yaml: job {jid} permissions are {j.get('permissions')}, expected {PUBLISH_PERMS.get(jid)}")
    if jid == "deploy":
        if j.get("uses", "").split(" ")[0] != GITOPS:
            problems.append(f"service-publish.yaml: deploy must call gitops-promote.yaml at {GITOPS[-40:]}, it calls {j.get('uses')}")
    elif "uses" in j:
        problems.append(f"service-publish.yaml: job {jid} calls another workflow; only deploy (gitops-promote.yaml) may")
    if jid != "prune" and j.get("continue-on-error"):
        problems.append(f"service-publish.yaml: only the prune job is best effort, {jid} has continue-on-error")
if pj.get("prune", {}).get("continue-on-error") is not True:
    problems.append("service-publish.yaml: the prune job must have continue-on-error: true (a failed prune never fails a publish)")
if pj.get("prune", {}).get("if") != "${{ inputs.prune }}":
    problems.append("service-publish.yaml: prune must run only with `if: ${{ inputs.prune }}`")
if pj.get("image", {}).get("if") != "${{ needs.changes.outputs.code == 'true' && github.ref == 'refs/heads/main' }}":
    problems.append("service-publish.yaml: image must run only for code changes on main")
if set(pj.get("publish", {}).get("outputs", {})) != {"digest", "tag"}:
    problems.append("service-publish.yaml: the publish job outputs must be exactly digest and tag (an output with the Docker Hub account name is dropped by GitHub)")
for old in ("docker-build.yaml", "docker-promote.yaml", "ghcr-prune.yaml", "changed-paths.yaml"):
    if re.search(r"uses: .*" + re.escape(old), pub_text):
        problems.append(f"service-publish.yaml: calls {old}; everything except gitops-promote.yaml must be inline")
# the dependency of the helm pull request: nothing but publish may sit in front of it
for jid in ("sbom", "prune"):
    if jid in pj.get("deploy", {}).get("needs", []):
        problems.append(f"service-publish.yaml: deploy waits for {jid}")
# unchanged steps equal docker-promote's
promote_steps = {s_.get("name"): s_ for s_ in load("docker-promote.yaml")["jobs"]["promote"]["steps"]}
for jid, names in (("publish", ["Publishing is only allowed from commits on main", "Log in to ghcr.io", "Log in to Docker Hub", "Set up Docker Buildx",
                               "Install Cosign", "Sign image", "Verify the signature we just made"]),
                   ("sbom", ["Log in to Docker Hub", "Install Cosign"])):
    have = {s_.get("name"): s_ for s_ in pj.get(jid, {}).get("steps", [])}
    for n in names:
        if have.get(n) != promote_steps.get(n):
            problems.append(f"service-publish.yaml: step {n!r} of job {jid} differs from docker-promote.yaml")
# the sign / verify flags (cosign) are the ones docker-promote uses
for flag in ("cosign sign --yes --new-bundle-format=false --use-signing-config=false",
             "cosign verify --new-bundle-format=false", "cosign attest --yes --new-bundle-format=false --use-signing-config=false --type spdxjson",
             "cosign verify-attestation --new-bundle-format=false --type spdxjson"):
    if flag not in pub_text or flag not in open(os.path.join(wfdir, "docker-promote.yaml")).read():
        problems.append(f"service-publish.yaml: cosign call {flag!r} is not in both service-publish and docker-promote")
# Docker Hub tag: sha-<7 chars> only (one --tag, built from the commit; no latest, no semver)
copy_runs = [s_.get("run", "") for s_ in pj.get("publish", {}).get("steps", []) if "imagetools create" in s_.get("run", "")]
if len(copy_runs) != 1 or copy_runs[0].count("--tag") != 1 or 'tag="sha-${GITHUB_SHA::7}"' not in copy_runs[0]:
    problems.append("service-publish.yaml: the Docker Hub copy must make exactly one tag, sha-<first 7 chars of the commit>")
# steps shared with the pull-request image job are the same text
def step_by_id(job, sid):
    return next((s_ for s_ in job.get("steps", []) if s_.get("id") == sid), None)

for lang, image in flat_images.items():
    for sid in ("find", "image", "mode", "cache"):
        a, b = step_by_id(image, sid), step_by_id(pj["image"], sid)
        if sid == "mode":
            continue  # only the pull-request workflows decide whether a run may push
        if a is None or b is None or a.get("run") != b.get("run"):
            problems.append(f"service-ci-{lang}.yaml: step {sid!r} of the image job differs from the one in service-publish.yaml")
    a, b = step_by_id(image, "build"), step_by_id(pj["image"], "build")
    if a is None or b is None or a["with"]["labels"] != b["with"]["labels"] or a["uses"] != b["uses"]:
        problems.append(f"service-ci-{lang}.yaml: build step labels or pin differ from service-publish.yaml")
    ta = [s_ for s_ in image["steps"] if str(s_.get("uses", "")).startswith("aquasecurity/trivy-action")]
    tb = [s_ for s_ in pj["scan"]["steps"] if str(s_.get("uses", "")).startswith("aquasecurity/trivy-action")]
    if len(ta) != 1 or len(tb) != 1 or {k: v for k, v in ta[0]["with"].items() if k != "image-ref"} != {k: v for k, v in tb[0]["with"].items() if k != "image-ref"}:
        problems.append(f"service-ci-{lang}.yaml: the Trivy step differs from the one in service-publish.yaml (version, severity, ignore file)")
    if ta and ta[0]["with"]["image-ref"] != "${{ steps.image.outputs.ref }}":
        problems.append(f"service-ci-{lang}.yaml: Trivy must scan the digest recorded by the image step")
if tb and tb[0]["with"]["image-ref"] != "${{ needs.image.outputs.ref }}":
    problems.append("service-publish.yaml: Trivy must scan the digest of the image job")
# pins
for action, sha in pin.findall(pub_text):
    if action in pins and sha not in pins[action]:
        problems.append(f"service-publish.yaml: {action}@{sha[:7]} is not the pin the other workflows use")
# prune: separate N per tag family (every pull-request push makes a tree- version)
pins_ = pub["on"]["workflow_call"]["inputs"]
for name_, default_ in (("prune-keep", 10), ("prune-keep-tree", 20)):
    if pins_.get(name_, {}).get("default") != default_ or pins_[name_].get("type") != "number":
        problems.append(f"service-publish.yaml: input {name_} must be a number with default {default_}")
if "KEEP_TREE" not in pj.get("prune", {}).get("env", {}):
    problems.append("service-publish.yaml: the prune job does not pass KEEP_TREE")
# the registry cache: refreshed by the publish and by pull-request runs that push; forks and Dependabot only read it
for lang, image in flat_images.items():
    b_ = step_by_id(image, "build")
    cto = (b_ or {}).get("with", {}).get("cache-to", "")
    if "steps.mode.outputs.push == 'true'" not in cto or "type=registry" not in cto or "mode=max" not in cto:
        problems.append(f"service-ci-{lang}.yaml: cache-to of the image build must be set only for runs that push")
if "mode=max" not in step_by_id(pj["image"], "build")["with"].get("cache-to", ""):
    problems.append("service-publish.yaml: the build must write the registry cache")
# the merge button: one reusable workflow, one job, in the environment merge, only gh
if not os.path.exists(os.path.join(wfdir, "service-merge.yaml")):
    problems.append("service-merge.yaml is missing")
else:
    mw = load("service-merge.yaml")
    mi = mw["on"]["workflow_call"]["inputs"]
    if mi.get("start-publish", {}).get("type") != "boolean" or mi["start-publish"].get("default") is not True:
        problems.append("service-merge.yaml: input start-publish must be a boolean defaulting to true")
    mj = mw["jobs"]
    if list(mj) != ["merge"] or mj["merge"].get("environment") != "merge":
        problems.append("service-merge.yaml: exactly one job, merge, in the environment merge")
    else:
        order = [s_.get("name") for s_ in mj["merge"]["steps"]]
        if order != ["Check that the environment was approved", "Squash-merge the pull request that was tested", "Start Publish"]:
            problems.append(f"service-merge.yaml: steps are {order}; the approval check must come first, the start of Publish last")
        if mj["merge"]["permissions"] != {"contents": "write", "pull-requests": "write", "actions": "write"}:
            problems.append("service-merge.yaml: the job needs exactly contents, pull-requests and actions: write")
        if any("uses" in s_ for s_ in mj["merge"]["steps"]):
            problems.append("service-merge.yaml: uses an action; it must only use gh")
# the tag button
if not os.path.exists(os.path.join(wfdir, "release.yaml")):
    problems.append("release.yaml is missing")
else:
    rel = load("release.yaml")
    if "version" not in (rel["on"].get("workflow_dispatch") or {}).get("inputs", {}):
        problems.append("release.yaml: input version is missing")
    if rel["jobs"]["tag"]["permissions"] != {"contents": "write"}:
        problems.append("release.yaml: the tag job needs exactly contents: write")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("consistent")
