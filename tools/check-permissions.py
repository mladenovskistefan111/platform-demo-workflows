#!/usr/bin/env python3
"""GitHub validates the permissions of every job in a chain of reusable workflows at startup (also jobs that are skipped by `if:`),
and a nested job may not ask for more than the job that calls it. A violation shows up only as `startup_failure` with no message.
This check walks service-ci.yaml and service-publish.yaml of the working tree (every `uses: .../workflows/<name>.yaml@<sha>` of this
repository is resolved to the local file) starting from the permissions that tools/gen-callers.py grants in the generated callers.
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


problems = []
with tempfile.TemporaryDirectory() as tmp:
    for svc in ("checkout-service", "currency-service", "email-service", "ad-service", "cart-service", "product-catalog-db"):
        subprocess.run([sys.executable, "tools/gen-callers.py", tmp, svc, "0" * 40, "v0.0.0"], check=True, capture_output=True)
        for name, target in (("ci.yaml", "service-ci.yaml"), ("publish.yaml", "service-publish.yaml")):
            f = pathlib.Path(tmp) / ".github/workflows" / name
            if not f.exists():
                continue  # product-catalog-db has no publish.yaml
            caller = load(f)
            job = next(iter(caller["jobs"].values()))
            walk(load(WF / target), grants(job.get("permissions")), f"{svc}:{name}>{target}", problems)
if problems:
    print("\n".join(sorted(set(problems))))
    sys.exit(1)
print("nested permissions ok")
