#!/usr/bin/env python3
"""GitHub validates the permissions of every job in a chain of reusable workflows at startup (also jobs that are skipped by `if:`),
and a nested job may not ask for more than the job that calls it. A violation shows up only as `startup_failure` with no message.
This check walks the service-ci-<language>.yaml files (flat: no nested workflow, so only their own jobs count) and
service-publish.yaml (flat too; its only nested workflow is gitops-promote.yaml) of the working tree (every
`uses: .../workflows/<name>.yaml@<sha>` of this repository is resolved to the local file) starting from the permissions that
tools/gen-callers.py grants in the generated callers, for all 11 app repositories.
The grant of the `ci` and `publish` jobs must also be EXACTLY what the called file's jobs need (least privilege): ci = contents read +
packages write (the image job pushes the tree image), plus checks: write for dotnet and nobody else; publish = contents read +
packages write + id-token write.
The `merge` job of ci.yaml (it lives in the caller, so it is not nested) must grant exactly contents, pull-requests and actions
write, run after `ci`, use the environment `merge`, be limited to same-repository pull requests, and never be a job of a called
workflow (so it can never become part of `gate`); publish.yaml must accept the `before` dispatch input and pass it on.
usage: check-permissions.py   (run from the repository root; needs PyYAML)"""
import re, subprocess, sys, tempfile, pathlib
import yaml

ORDER = {"none": 0, "read": 1, "write": 2}
WF = pathlib.Path(".github/workflows")


def load(p):
    doc = yaml.safe_load(p.read_text())
    if True in doc:  # YAML 1.1 reads the key `on` as a boolean
        doc["on"] = doc.pop(True)
    return doc


def grants(d):
    return {k: v for k, v in (d or {}).items() if v != "none"}


def walk(wf, ceiling, path, problems):
    top = wf.get("permissions")
    for jid, j in wf["jobs"].items():
        eff = grants(j["permissions"]) if "permissions" in j else grants(top) if top is not None else {"(default token)": "write"}
        for k, v in eff.items():
            if ORDER.get(v, 2) > ORDER[ceiling.get(k, "none")]:
                problems.append(f"{path}/{jid}: asks {k}: {v}, the caller grants {k}: {ceiling.get(k, 'none')}")
        use = j.get("uses")
        if use:
            m = re.match(r".*/platform-demo-workflows/\.github/workflows/([a-z-]+\.yaml)@", use)
            if m:
                walk(load(WF / m.group(1)), eff, f"{path}/{jid}>{m.group(1)}", problems)


def needed(wf):
    """The highest permission every job of the workflow asks for, per scope (nested workflows included)."""
    top, out = wf.get("permissions"), {}
    for j in wf["jobs"].values():
        eff = grants(j["permissions"]) if "permissions" in j else grants(top) if top is not None else {}
        for k, v in eff.items():
            if ORDER[v] > ORDER[out.get(k, "none")]:
                out[k] = v
        m = re.match(r".*/platform-demo-workflows/\.github/workflows/([a-z-]+\.yaml)@", j.get("uses", ""))
        if m:
            for k, v in needed(load(WF / m.group(1))).items():
                if ORDER[v] > ORDER[out.get(k, "none")]:
                    out[k] = v
    return out


MERGE_PERMS = {"contents": "write", "pull-requests": "write", "actions": "write"}
SERVICES = ("checkout-service", "shipping-service", "frontend", "product-catalog-service", "currency-service", "payment-service",
            "email-service", "recommendation-service", "ad-service", "cart-service", "product-catalog-db")
problems = []
with tempfile.TemporaryDirectory() as tmp:
    for svc in SERVICES:
        subprocess.run([sys.executable, "tools/gen-callers.py", tmp, svc, "0" * 40, "v0.0.0"], check=True, capture_output=True)
        for name in ("ci.yaml", "publish.yaml"):
            f = pathlib.Path(tmp) / ".github/workflows" / name
            if not f.exists():
                continue  # product-catalog-db has no publish.yaml
            caller = load(f)
            first = "ci" if name == "ci.yaml" else "publish"
            job = caller["jobs"].get(first)
            if job is None:
                problems.append(f"{svc}:{name} has no job {first}")
                continue
            target = re.match(r".*/platform-demo-workflows/\.github/workflows/([a-z-]+\.yaml)@", job["uses"]).group(1)
            if name == "ci.yaml" and not re.fullmatch(r"service-ci-(go|node|python|java|dotnet|none)\.yaml", target):
                problems.append(f"{svc}:{name} calls {target}, expected service-ci-<language>.yaml")
            if name == "publish.yaml" and target != "service-publish.yaml":
                problems.append(f"{svc}:{name} calls {target}, expected service-publish.yaml")
            walk(load(WF / target), grants(job.get("permissions")), f"{svc}:{name}>{target}", problems)
            if grants(job.get("permissions")) != needed(load(WF / target)):
                problems.append(f"{svc}:{name} grants {grants(job.get('permissions'))}, {target} needs exactly {needed(load(WF / target))}")
            if name == "ci.yaml":
                if sorted(caller["jobs"]) != ["ci", "merge"]:
                    problems.append(f"{svc}:{name} has jobs {sorted(caller['jobs'])}, expected ci and merge")
                    continue
                m = caller["jobs"]["merge"]
                if grants(m.get("permissions")) != MERGE_PERMS:
                    problems.append(f"{svc}:{name} merge grants {grants(m.get('permissions'))}, expected exactly {MERGE_PERMS}")
                if m.get("needs") != "ci" or m.get("environment") != "merge" or "uses" in m:
                    problems.append(f"{svc}:{name} merge must be a job with steps that needs ci and uses the environment merge")
                cond = str(m.get("if", ""))
                for part in ("github.event_name == 'pull_request'", "github.event.pull_request.head.repo.full_name == github.repository"):
                    if part not in cond:
                        problems.append(f"{svc}:{name} merge: `if` lacks {part!r}")
                if "timeout-minutes" not in m:
                    problems.append(f"{svc}:{name} merge has no timeout-minutes")
                if any("uses" in st for st in m["steps"]):
                    problems.append(f"{svc}:{name} merge uses an action; it must only use gh (nothing to pin, nothing to trust)")
                starts_publish = any("gh workflow run publish.yaml" in st.get("run", "") for st in m["steps"])
                if starts_publish != (svc != "product-catalog-db"):
                    problems.append(f"{svc}:{name} merge {'starts' if starts_publish else 'does not start'} Publish, but this repository {'has' if svc != 'product-catalog-db' else 'has no'} publish.yaml")
            else:
                dispatch = ((caller["on"].get("workflow_dispatch") or {}).get("inputs") or {})
                if "before" not in dispatch or job["with"].get("before") != "${{ inputs.before }}":
                    problems.append(f"{svc}:{name} must accept the dispatch input before and pass it to service-publish")
                if "before" not in load(WF / target)["on"]["workflow_call"]["inputs"]:
                    problems.append(f"{target} has no input before")
for lang in ("go", "node", "python", "java", "dotnet", "none"):
    if "merge" in load(WF / f"service-ci-{lang}.yaml")["jobs"]:
        problems.append(f"service-ci-{lang}.yaml has a merge job; it belongs in the caller, after `gate`, and must not be part of it")
if problems:
    print("\n".join(sorted(set(problems))))
    sys.exit(1)
print("nested permissions ok")
