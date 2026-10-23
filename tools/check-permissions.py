#!/usr/bin/env python3
"""GitHub validates the permissions of every job in a chain of reusable workflows at startup (also jobs that are skipped by `if:`),
and a nested job may not ask for more than the job that calls it. A violation shows up only as `startup_failure` with no message.
This check walks the service-ci-<language>.yaml files (flat: no nested workflow, so only their own jobs count) and
service-publish.yaml of the working tree (every `uses: .../workflows/<name>.yaml@<sha>` of this repository is resolved to the local
file) starting from the permissions that tools/gen-callers.py grants in the generated callers, for all 11 app repositories.
For the pull-request callers the grant must also be EXACTLY what the called file's jobs need (least privilege): contents + packages
read, plus checks: write for dotnet and nobody else.
usage: check-permissions.py   (run from the repository root; needs PyYAML)"""
import re, subprocess, sys, tempfile, pathlib
import yaml

ORDER = {"none": 0, "read": 1, "write": 2}
WF = pathlib.Path(".github/workflows")


def load(p):
    return yaml.safe_load(p.read_text())


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
            job = next(iter(caller["jobs"].values()))
            target = re.match(r".*/platform-demo-workflows/\.github/workflows/([a-z-]+\.yaml)@", job["uses"]).group(1)
            if name == "ci.yaml" and not re.fullmatch(r"service-ci-(go|node|python|java|dotnet|none)\.yaml", target):
                problems.append(f"{svc}:{name} calls {target}, expected service-ci-<language>.yaml")
            walk(load(WF / target), grants(job.get("permissions")), f"{svc}:{name}>{target}", problems)
            if name == "ci.yaml" and grants(job.get("permissions")) != needed(load(WF / target)):
                problems.append(f"{svc}:{name} grants {grants(job.get('permissions'))}, {target} needs exactly {needed(load(WF / target))}")
if problems:
    print("\n".join(sorted(set(problems))))
    sys.exit(1)
print("nested permissions ok")
