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
   - the same job ids and names in every file (plain names: sentence case, what the job does, no `Group:` prefix; the gate lists
     the same names for its summary and keys on job ids only, never on names); the guards have no `needs`, every other job needs only `changes`, and `gate`
     needs EVERY other job (a job missing from gate.needs would be a check nobody waits for);
   - every action pin equals the pin used by the other workflows of this repository, Trivy has the version docker-build uses;
   - the image job: job id `image`, permissions contents read + packages write, and the steps it shares with service-publish (the
     tree lookup, the build labels, the digest record, the Trivy scan) are the same text as there;
4. each stand-alone language workflow has the `secret-scan` input;
5. service-merge.yaml (the merge button called by every ci.yaml): one job in the environment `merge`, approval check first, no action;
   the registry build cache is written by publish and by pull-request runs that push; prune has separate N for `sha-*` and `tree-*`;
   service-publish.yaml (flat, generated): the job graph is Detect changes -> Find or build image -> Scan image -> Publish image ->
   (Create SBOM | Open dev PR | Clean up staging images); the run summaries are where the design puts them (the image summary is the last
   step of the three image jobs, the same text; publish, SBOM and scan write their own section); the only nested workflow is gitops-promote.yaml at the
   pinned commit that already exists (a sibling reference needs an existing SHA), the prune job is best effort
   (`continue-on-error`) and never waits for or blocks the helm pull request, the SBOM job is not on the way to it, no job output
   can carry the Docker Hub account name (GitHub drops outputs that contain a secret), every job has the permissions it needs, the
   unchanged steps equal docker-promote.yaml's, and release.yaml exists with a validated version input.
   Since v3.6 the graph goes on after `Open dev PR` as one straight line (Wait for dev -> Approve stage deployment -> Promote to stage -> Wait for
   stage -> Release stage version -> Approve prod deployment -> Promote to prod -> Wait for prod): the environments only on the two approval
   jobs (stage, prod), the job `if`s that skip the rest of the line, the concurrency groups (none at workflow level; image / prune per app
   without cancel; the approvals with cancel-in-progress), the supersede re-check after the click, the version logic, the same shared helpers in
   every chain script, the release's App token (same action and inputs as gitops-promote.yaml, contents: write only, never moves a tag),
   `Promote to stage|prod` pass this run's digest and the chosen version to the same pinned gitops-promote, and the newer-build check of dev.
Exit code 1 and one line per problem when something is off.
"""
import os, re, shutil, subprocess, sys, tempfile
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
# Job names: sentence case, what the job does, no group prefixes. The caller's job id (`ci`, `publish`) is the prefix GitHub shows.
JOB_NAMES = {"changes": "Detect changes", "secret-scan": "Secret scan", "workflow-lint": "Workflow lint", "lint": "Lint",
             "build": "Build", "test": "Tests", "sca": "Dependency scan", "sast": "Code scan",
             "image": "Image build and scan", "gate": "gate"}
PREFIXED = re.compile(r"^[A-Za-z]+: ")  # `Guard: Changes`, `Image: Scan`, ...: the old group prefixes

for fname in LANG_WF.values():
    if "secret-scan" not in load(fname)["on"]["workflow_call"]["inputs"]:
        problems.append(f"{fname}: input secret-scan is missing")

# pins used by the workflows that are not generated by gen-service-ci.py
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
    # the gate's summary shows the same names (generated from the same table) and keys on job ids
    gate_step = (gate.get("steps") or [{}])[0]
    shown = dict(re.findall(r'\[([a-z-]+)\]="([^"]*)"', gate_step.get("run", "").split("declare -A names=(", 1)[-1].split(")\n", 1)[0]))
    if shown != {k: v for k, v in JOB_NAMES.items() if k in jobs and k != "gate"}:
        problems.append(f"{fname}: the gate lists the names {shown}, which are not the names of the jobs")
    for var in ("EVENT", "HEAD_REPO", "ACTOR", "CODE", "IMAGE", "NEEDS"):
        if var not in gate_step.get("env", {}):
            problems.append(f"{fname}: the gate step has no env {var}")
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

for fname_ in ["service-publish.yaml", "service-merge.yaml"] + [f"service-ci-{l}.yaml" for l in LANG_WF] + ["service-ci-none.yaml"]:
    if os.path.exists(os.path.join(wfdir, fname_)):
        for jid_, j_ in load(fname_)["jobs"].items():
            if not j_.get("name"):
                problems.append(f"{fname_}: job {jid_} has no explicit `name:`")
            elif PREFIXED.match(j_["name"]):
                problems.append(f"{fname_}: job {jid_} is named {j_['name']!r}; job names have no `Group:` prefix")
if os.path.exists(os.path.join(wfdir, "service-merge.yaml")) and load("service-merge.yaml")["jobs"]["merge"].get("name") != "Approve and merge":
    problems.append("service-merge.yaml: the job must be named 'Approve and merge'")

# 5. service-publish.yaml
PUBLISH_NAMES = {"changes": "Detect changes", "image": "Find or build image", "scan": "Scan image", "publish": "Publish image",
                 "sbom": "Create SBOM", "deploy": "Open dev PR", "prune": "Clean up staging images",
                 "wait-dev": "Wait for dev", "approve-stage": "Approve stage deployment", "stage": "Promote to stage",
                 "wait-stage": "Wait for stage", "release": "Release stage version", "approve-prod": "Approve prod deployment",
                 "prod": "Promote to prod", "wait-prod": "Wait for prod"}
PUBLISH_NEEDS = {"changes": None, "image": ["changes"], "scan": ["image"], "publish": ["image", "scan"], "sbom": ["publish"],
                 "deploy": ["publish"], "prune": ["publish"],
                 # after `Open dev PR` a straight line: dev -> stage -> prod; sbom and prune stay parallel branches. The wait jobs also need
                 # the job that has the digest (a nested workflow call has no outputs of its own besides pr-url and pr-number)
                 "wait-dev": ["publish", "deploy"], "approve-stage": ["wait-dev"], "stage": ["approve-stage"],
                 "wait-stage": ["approve-stage", "stage"], "release": ["wait-stage"], "approve-prod": ["release"], "prod": ["approve-prod"],
                 "wait-prod": ["approve-prod", "prod"]}
PUBLISH_PERMS = {"changes": {"contents": "read"}, "image": {"contents": "read", "packages": "write"},
                 "scan": {"contents": "read", "packages": "read"}, "publish": {"id-token": "write", "contents": "read", "packages": "read"},
                 "sbom": {"id-token": "write", "contents": "read"}, "deploy": {}, "prune": {"contents": "read", "packages": "write"},
                 "wait-dev": {}, "approve-stage": {"actions": "read"}, "stage": {}, "wait-stage": {}, "release": {}, "approve-prod": {"actions": "read"},
                 "prod": {}, "wait-prod": {}}
NESTED_PROMOTE = {"deploy", "stage", "prod"}  # the jobs that call gitops-promote.yaml
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
    if jid in NESTED_PROMOTE:
        if j.get("uses", "").split(" ")[0] != GITOPS:
            problems.append(f"service-publish.yaml: {jid} must call gitops-promote.yaml at {GITOPS[-40:]}, it calls {j.get('uses')}")
    elif "uses" in j:
        problems.append(f"service-publish.yaml: job {jid} calls another workflow; only deploy, stage and prod (gitops-promote.yaml) may")
    if jid != "prune" and j.get("continue-on-error"):
        problems.append(f"service-publish.yaml: only the prune job is best effort, {jid} has continue-on-error")
if pj.get("prune", {}).get("continue-on-error") is not True:
    problems.append("service-publish.yaml: the prune job must have continue-on-error: true (a failed prune never fails a publish)")
if pj.get("prune", {}).get("if") != "${{ inputs.prune }}":
    problems.append("service-publish.yaml: prune must run only with `if: ${{ inputs.prune }}`")
if pj.get("image", {}).get("if") != "${{ needs.changes.outputs.code == 'true' && github.ref == 'refs/heads/main' }}":
    problems.append("service-publish.yaml: image must run only for code changes on main")
if set(pj.get("publish", {}).get("outputs", {})) != {"digest", "tag", "stale"}:
    problems.append("service-publish.yaml: the publish job outputs must be exactly digest, tag and stale (an output with the Docker Hub account name is dropped by GitHub)")
def step_by_id(job, sid):
    return next((s_ for s_ in job.get("steps", []) if s_.get("id") == sid), None)

# ---- a slower older run must not move dev back: `Open dev PR` is skipped when dev already runs a newer commit ----
guard_ = step_by_id(pj.get("publish", {}), "dev-guard")
if (pj.get("publish", {}).get("steps") or [{}])[-1] is not guard_ or guard_ is None:
    problems.append("service-publish.yaml: the last step of publish must be the dev-guard step")
elif "= behind" not in guard_["run"] or "compare/${BASH_REMATCH[1]}...$GITHUB_SHA" not in guard_["run"] or "stale=$stale" not in guard_["run"]:
    problems.append("service-publish.yaml: the dev-guard step must compare dev's sha-<7> tag with this commit and set stale when this commit is behind it")
if pj.get("deploy", {}).get("if") != "${{ needs.publish.outputs.stale != 'true' }}":
    problems.append("service-publish.yaml: deploy must be skipped when publish says stale")

# ---- the chain after `Open dev PR`: dev -> stage -> prod in the same run ----

CHAIN_IF = {"wait-dev": "${{ inputs.env == 'dev' }}", "approve-stage": "${{ needs.wait-dev.outputs.current == 'true' }}",
            "stage": "${{ needs.approve-stage.outputs.go == 'true' }}", "release": "${{ needs.wait-stage.outputs.landed == 'true' }}",
            "approve-prod": "${{ needs.release.outputs.current == 'true' }}", "prod": "${{ needs.approve-prod.outputs.go == 'true' }}",
            "wait-stage": None, "wait-prod": None}
for jid, want in CHAIN_IF.items():
    if pj.get(jid, {}).get("if") != want:
        problems.append(f"service-publish.yaml: job {jid} has `if: {pj.get(jid, {}).get('if')}`, expected {want}")
for jid in PUBLISH_NEEDS:
    if jid in CHAIN_IF and ("sbom" in (pj.get(jid, {}).get("needs") or []) or "prune" in (pj.get(jid, {}).get("needs") or [])):
        problems.append(f"service-publish.yaml: chain job {jid} waits for sbom or prune; they stay parallel branches")
# the environments: only the two approvals, and only stage / prod (never another name)
for jid, j in pj.items():
    want = {"approve-stage": "stage", "approve-prod": "prod"}.get(jid)
    if j.get("environment") != want:
        problems.append(f"service-publish.yaml: job {jid} has environment {j.get('environment')!r}, expected {want!r}")
# concurrency: no workflow-level group (a run may wait days for a click); job groups per app; approvals replace older waiting ones
if "concurrency" in pub:
    problems.append("service-publish.yaml: has a workflow-level concurrency group; a run waiting for a click would block later merges")
CONC = {"image": ("publish-build-${{ inputs.app }}", False), "prune": ("publish-prune-${{ inputs.app }}", False),
        "approve-stage": ("promote-stage-${{ inputs.app }}", True), "approve-prod": ("promote-prod-${{ inputs.app }}", True)}
for jid, j in pj.items():
    got = j.get("concurrency")
    want = CONC.get(jid)
    if want is None:
        if got is not None:
            problems.append(f"service-publish.yaml: job {jid} has a concurrency group; only {sorted(CONC)} may (gitops-promote.yaml serialises the pull requests itself)")
    elif got != {"group": want[0], "cancel-in-progress": want[1]}:
        problems.append(f"service-publish.yaml: job {jid} concurrency is {got}, expected group {want[0]!r} with cancel-in-progress {want[1]}")
# the approval jobs: same two scripts; approval verified; the supersede re-check is the first decision after the click
ap = {k: pj.get(k, {}) for k in ("approve-stage", "approve-prod")}
ap_steps = {k: [s_.get("id") for s_ in v.get("steps", [])] for k, v in ap.items()}
if any(v != ["approval", "decide"] for v in ap_steps.values()):
    problems.append(f"service-publish.yaml: the approval jobs must have the steps approval, decide (they have {ap_steps})")
else:
    for sid in ("approval", "decide"):
        if step_by_id(ap["approve-stage"], sid)["run"] != step_by_id(ap["approve-prod"], sid)["run"]:
            problems.append(f"service-publish.yaml: the {sid} step of both approval jobs must be the same script")
    decide = step_by_id(ap["approve-stage"], "decide")["run"]
    approval = step_by_id(ap["approve-stage"], "approval")["run"]
    if 'if [ "$prev_digest" != "$DIGEST" ]; then' not in decide or "go=false" not in decide or "superseded by a newer build" not in decide:
        problems.append("service-publish.yaml: the decide step lacks the supersede re-check (the previous environment must still hold this run's digest)")
    if not 0 <= decide.find('if [ "$prev_digest" != "$DIGEST" ]') < decide.find("next_version \"$APP\""):
        problems.append("service-publish.yaml: the supersede re-check must come before the version is chosen")
    if "/approvals" not in approval or '.state == "approved"' not in approval or "exit 1" not in approval:
        problems.append("service-publish.yaml: the approval step must refuse to go on without an approval of the environment (a missing environment has no protection)")
    for fragment in ("LC_ALL=C sort -V", "SEMVER=", "matching-refs/tags/$1-v", "echo 1.0.0"):
        if fragment not in decide:
            problems.append(f"service-publish.yaml: the version logic lacks {fragment!r} (helm's promote.sh: next patch after the highest <app>-vX.Y.Z tag, none: 1.0.0)")
    for k, v in ap.items():
        env_ = v.get("env", {})
        if env_.get("ENVIRONMENT") != env_.get("TARGET_ENV") or env_.get("ENVIRONMENT") != {"approve-stage": "stage", "approve-prod": "prod"}[k]:
            problems.append(f"service-publish.yaml: {k} asks the approval of the wrong environment")
# the nested promotions: this run's digest only, the computed version, the same secrets as Open dev PR
for jid, envname, ap_ in (("stage", "stage", "approve-stage"), ("prod", "prod", "approve-prod")):
    w = pj.get(jid, {}).get("with", {})
    a_ = f"${{{{ needs.{ap_}.outputs.%s }}}}"
    for key, want in (("env", envname), ("image-digest", a_ % "digest"), ("image-tag", a_ % "tag"), ("version", a_ % "version"),
                      ("source-sha", "${{ github.sha }}"), ("source-repository", "${{ github.repository }}"), ("auto-merge", "${{ inputs.auto-merge }}")):
        if w.get(key) != want:
            problems.append(f"service-publish.yaml: {jid} passes {key}={w.get(key)!r}, expected {want!r}")
    if pj.get(jid, {}).get("secrets") != pj.get("deploy", {}).get("secrets"):
        problems.append(f"service-publish.yaml: {jid} must pass the same secrets as deploy")
# the wait jobs: one script, polling with a limit below the job timeout, all outputs, helm read with the job's own (public) token
waits = {k: pj.get(k, {}) for k in ("wait-dev", "wait-stage", "wait-prod")}
if len({step_by_id(v, "wait")["run"] for v in waits.values() if step_by_id(v, "wait")}) != 1:
    problems.append("service-publish.yaml: the three wait jobs must run the same script")
for k, v in waits.items():
    if set(v.get("outputs", {})) != {"landed", "current", "sha", "version", "digest", "tag"}:
        problems.append(f"service-publish.yaml: {k} outputs are {sorted(v.get('outputs', {}))}")
    if v.get("env", {}).get("WAIT_MINUTES") != "25" or v.get("timeout-minutes", 0) < 30:
        problems.append(f"service-publish.yaml: {k} must wait 25 minutes and have a job timeout above 30")
    if v.get("env", {}).get("GH_TOKEN") != "${{ github.token }}":
        problems.append(f"service-publish.yaml: {k} reads the public helm repository with the job's own token")
# every chain script writes the run summary, on the failure paths too, and the version checks do not drift between the shared copies
LIB_START, LIB_END = "HELM=platform-boutique/platform-demo-helm", "  return 1\n}\n"
libs = {}
for jid, sid in (("wait-dev", "wait"), ("approve-stage", "decide"), ("release", "release")):
    run_ = (step_by_id(pj.get(jid, {}), sid) or {}).get("run", "")
    if "GITHUB_STEP_SUMMARY" not in run_ or "summary()" not in run_:
        problems.append(f"service-publish.yaml: the {sid} step of {jid} writes no run summary")
    if LIB_START in run_:
        libs[jid] = run_[run_.index(LIB_START):run_.index(LIB_END, run_.index("values_retry()")) + len(LIB_END)]
    else:
        problems.append(f"service-publish.yaml: the {sid} step of {jid} lacks the shared helm helpers")
if len(set(libs.values())) > 1:
    problems.append("service-publish.yaml: the shared helm helpers (values file parsing, values_file) differ between the chain scripts")
# the release: App token for platform-demo-helm only, the same action and inputs as gitops-promote.yaml, never moves or deletes anything
rel = pj.get("release", {})
tok = step_by_id(rel, "app-token") or {}
gp_tok = next((s_ for s_ in load("gitops-promote.yaml")["jobs"]["promote"]["steps"] if s_.get("id") == "app-token"), {})
if tok.get("uses") != gp_tok.get("uses"):
    problems.append("service-publish.yaml: release mints the App token with another action pin than gitops-promote.yaml")
for key in ("app-id", "private-key", "owner", "repositories"):
    if (tok.get("with") or {}).get(key) != (gp_tok.get("with") or {}).get(key):
        problems.append(f"service-publish.yaml: release's App token input {key} differs from gitops-promote.yaml")
if {k: v for k, v in (tok.get("with") or {}).items() if k.startswith("permission-")} != {"permission-contents": "write"}:
    problems.append("service-publish.yaml: release's App token must ask for contents: write and nothing else")
rel_run = (step_by_id(rel, "release") or {}).get("run", "")
if re.search(r"-X (DELETE|PATCH|PUT)|--force|git push|git tag", rel_run):
    problems.append("service-publish.yaml: the release script must only create (never move, update or delete a tag)")
if (step_by_id(rel, "release") or {}).get("env", {}).get("GH_TOKEN") != "${{ steps.app-token.outputs.token }}":
    problems.append("service-publish.yaml: the release must use the App token")
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
for lang, image in flat_images.items():
    for sid in ("find", "image", "mode", "cache"):
        a, b = step_by_id(image, sid), step_by_id(pj["image"], sid)
        if sid == "mode":
            continue  # only the pull-request workflows decide whether a run may push
        if a is None or b is None or a.get("run") != b.get("run"):
            problems.append(f"service-ci-{lang}.yaml: step {sid!r} of the image job differs from the one in service-publish.yaml")
    sa = [s_ for s_ in image["steps"] if s_.get("name") == "Write the image summary"]
    sb = [s_ for s_ in pj["image"]["steps"] if s_.get("name") == "Write the image summary"]
    if len(sa) != 1 or len(sb) != 1 or sa[0].get("run") != sb[0].get("run") or image["steps"][-1] is not sa[0] or pj["image"]["steps"][-1] is not sb[0]:
        problems.append(f"service-ci-{lang}.yaml: the last step of the image job must be 'Write the image summary', the same text as in service-publish.yaml")
    elif sa[0].get("if") != "${{ !cancelled() }}" or sb[0].get("if") != "${{ !cancelled() }}":
        problems.append(f"service-ci-{lang}.yaml: the image summary step must run with `if: ${{{{ !cancelled() }}}}` (it reports a failed scan too)")
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
# the other publish summaries: the publish job writes the answer, scan and SBOM write their own section
for jid_, sect in (("publish", "### Published"), ("sbom", "### SBOM"), ("scan", "### Image scan")):
    last = (pj.get(jid_, {}).get("steps") or [{}])[-1]
    if jid_ == "publish":  # the answer to "what was published" is followed by the newer-build check of dev
        last = (pj.get(jid_, {}).get("steps") or [{}, {}])[-2]
    if last.get("name") != "Write the run summary" or sect not in last.get("run", ""):
        problems.append(f"service-publish.yaml: the last step of job {jid_} must be 'Write the run summary' with the section {sect!r}")
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
