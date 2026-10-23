#!/usr/bin/env python3
"""Unit tests of the shell logic in the generated workflows. Every script is taken out of the YAML exactly as committed and run with
bash (`bash -e`, like a GitHub `run:` step) against stubs of docker, gh, curl and sleep, and real git repositories. Needs bash, git,
jq, python3 and PyYAML.  usage: tools/test-shell.py [repo-dir]      exit 1 and one line per failed scenario

  * tree lookup / reuse / build decision, who may push (service-ci-* image job, service-publish image job)
  * the digest record, the copy to Docker Hub (a different digest is a hard failure, a published tag is never moved)
  * change detection with `before` (service-publish: push, workflow_dispatch with and without before)
  * the main-reachability check
  * prune selection (sha-* and tree-* families, protected tags, image-index children) with fake package JSON
  * service-merge.yaml, the merge button (approval check, branch-moved check, merge, `before`, starting Publish) with a stubbed gh, and
    the short caller job that calls it
  * the run summaries: what changed (file list capped, hostile file names), the image section, the published section, scan, SBOM, prune
  * the release tag button (version check, existing tag, annotated tag on the commit)
"""
import hashlib, json, os, shutil, stat, subprocess, sys, tempfile, textwrap
from datetime import datetime, timedelta, timezone
import yaml

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
WF = os.path.join(ROOT, ".github", "workflows")
failures, count = [], 0


def load(name):
    with open(os.path.join(WF, name)) as f:
        doc = yaml.safe_load(f)
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def step(job, key, value="id"):
    hits = [s for s in job["steps"] if s.get(key) == value]
    assert len(hits) == 1, f"expected one step with {key}={value!r}, found {len(hits)}"
    return hits[0]


def sha256(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


class Env:
    """A scratch directory with stubs on PATH and the files GitHub gives a step."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="test-shell-")
        self.bin = os.path.join(self.dir, "bin")
        self.tmp = os.path.join(self.dir, "runner-temp")
        for d in (self.bin, self.tmp, os.path.join(self.dir, "reg")):
            os.makedirs(d)
        self.out = os.path.join(self.dir, "output")
        self.summary = os.path.join(self.dir, "summary")
        self.log = os.path.join(self.dir, "calls.log")
        for f in (self.out, self.summary, self.log):
            open(f, "w").close()
        self.stub("sleep", "#!/bin/sh\nexit 0\n")

    def stub(self, name, text):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write(text)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def run(self, run_text, env=None, cwd=None):
        script = os.path.join(self.dir, "step.sh")
        with open(script, "w") as f:
            f.write(run_text)
        e = {"PATH": self.bin + ":" + os.environ["PATH"], "HOME": self.dir, "GITHUB_OUTPUT": self.out, "GITHUB_STEP_SUMMARY": self.summary,
             "RUNNER_TEMP": self.tmp, "STUB_DIR": self.dir, "STUB_LOG": self.log, "GIT_CONFIG_GLOBAL": os.path.join(self.dir, "gitconfig"),
             "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
        e.update(env or {})
        p = subprocess.run(["bash", "-e", script], env=e, cwd=cwd, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def outputs(self):
        out = {}
        for line in open(self.out).read().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out

    def calls(self):
        return open(self.log).read().splitlines()

    def done(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def check(name, ok, why=""):
    global count
    count += 1
    if not ok:
        failures.append(f"{name}: {why}")


def git(cwd, *args, env=None):
    e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
             GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    e.update(env or {})
    p = subprocess.run(["git", *args], cwd=cwd, env=e, capture_output=True, text=True)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"
    return p.stdout.strip()


def commit(cwd, files, msg):
    for path, content in files.items():
        full = os.path.join(cwd, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "--allow-empty", "-m", msg)
    return git(cwd, "rev-parse", "HEAD")


DOCKER_STUB = r"""#!/usr/bin/env bash
# fake docker: a registry is a directory of files (reg/<ref with / : @ replaced by _>), an error message can be put in reg/<ref>.err
echo "docker $*" >> "$STUB_LOG"
key() { echo "$1" | tr '/:@' '___'; }
if [ "$1 $2 $3 $4" = "buildx imagetools inspect --raw" ]; then
  k=$STUB_DIR/reg/$(key "$5")
  if [ -f "$k" ]; then cat "$k"; exit 0; fi
  if [ -f "$k.err" ]; then cat "$k.err" >&2; exit 1; fi
  echo "ERROR: $5: not found" >&2; exit 1
fi
if [ "$1 $2 $3" = "buildx imagetools create" ]; then
  shift 3; tag=""; src=""
  while [ $# -gt 0 ]; do case "$1" in --tag) tag=$2; shift 2 ;; *) src=$1; shift ;; esac; done
  if [ -f "$STUB_DIR/create-noop" ]; then exit 0; fi
  if [ -f "$STUB_DIR/create-as" ]; then cp "$STUB_DIR/create-as" "$STUB_DIR/reg/$(key "$tag")"; else cp "$STUB_DIR/reg/$(key "$src")" "$STUB_DIR/reg/$(key "$tag")"; fi
  exit 0
fi
echo "unexpected docker call: $*" >&2; exit 99
"""


def reg_put(env, ref, content):
    with open(os.path.join(env.dir, "reg", ref.replace("/", "_").replace(":", "_").replace("@", "_")), "wb") as f:
        f.write(content)


def reg_err(env, ref, message):
    with open(os.path.join(env.dir, "reg", ref.replace("/", "_").replace(":", "_").replace("@", "_") + ".err"), "w") as f:
        f.write(message)


def parse_summary(env):
    return open(env.summary).read()


pub = load("service-publish.yaml")
ci_files = {l: load(f"service-ci-{l}.yaml") for l in ("go", "node", "python", "java", "dotnet", "none")}

# ───────────────────────── who may push: the `mode` step ─────────────────────────
for lang, wf in ci_files.items():
    mode = step(wf["jobs"]["image"], "id", "mode")["run"]
    for name, ev, head, actor, want in (
            ("same-repository pull request", "pull_request", "o/r", "alice", "true"),
            ("fork pull request", "pull_request", "fork/r", "alice", "false"),
            ("Dependabot pull request (read-only token)", "pull_request", "o/r", "dependabot[bot]", "false"),
            ("manual run", "workflow_dispatch", "", "alice", "true")):
        e = Env()
        rc, out = e.run(mode, {"EVENT": ev, "HEAD_REPO": head, "ACTOR": actor, "GITHUB_REPOSITORY": "o/r"})
        check(f"mode[{lang}]: {name}", rc == 0 and e.outputs().get("push") == want, f"rc={rc} outputs={e.outputs()} {out}")
        e.done()

# ───────────────────────── tree lookup: Find ─────────────────────────
FIND = {"service-ci-go": step(ci_files["go"]["jobs"]["image"], "id", "find")["run"], "service-publish": step(pub["jobs"]["image"], "id", "find")["run"]}
check("find: the pull-request and the publish workflow run the same script", FIND["service-ci-go"] == FIND["service-publish"])
for where, find in FIND.items():
    repo = tempfile.mkdtemp(prefix="test-repo-")
    git(repo, "init", "-q", "-b", "main")
    c1 = commit(repo, {"app.go": "package main\n"}, "one")
    tree1 = git(repo, "rev-parse", "HEAD^{tree}")
    base = {"GITHUB_REPOSITORY": "Plat-Form/App-Thing"}
    tag_ref = f"ghcr.io/plat-form/app-thing:tree-{tree1}"

    e = Env()
    e.stub("docker", DOCKER_STUB)
    manifest = b'{"schemaVersion":2,"manifests":[]}'
    reg_put(e, tag_ref, manifest)
    rc, out = e.run(find, {**base, "CAN_PUSH": "true"}, cwd=repo)
    o = e.outputs()
    check(f"find[{where}]: existing tree image is reused", rc == 0 and o.get("found") == "true" and o.get("digest") == sha256(manifest)
          and o.get("tree") == tree1 and o.get("tag-ref") == tag_ref, f"rc={rc} {o} {out}")
    check(f"find[{where}]: the repository name is lower-cased in the tag", "ghcr.io/plat-form/app-thing:tree-" in o.get("tag-ref", ""))
    e.done()

    e = Env()
    e.stub("docker", DOCKER_STUB)
    rc, out = e.run(find, {**base, "CAN_PUSH": "true"}, cwd=repo)
    o = e.outputs()
    check(f"find[{where}]: no image for the tree -> build (found=false), one lookup", rc == 0 and o.get("found") == "false" and o.get("digest") == ""
          and len([c for c in e.calls() if "inspect" in c]) == 1, f"rc={rc} {o} {e.calls()} {out}")
    e.done()

    # the squash commit on main: other message, other parent, same content -> same tree -> same image
    c2 = commit(repo, {}, "empty commit with another message")
    e = Env()
    e.stub("docker", DOCKER_STUB)
    reg_put(e, tag_ref, manifest)
    rc, out = e.run(find, {**base, "CAN_PUSH": "true"}, cwd=repo)
    check(f"find[{where}]: another commit with the same content finds the same image", rc == 0 and e.outputs().get("found") == "true" and c1 != c2,
          f"rc={rc} {e.outputs()} {out}")
    e.done()

    commit(repo, {"app.go": "package main // changed\n"}, "changed content")
    e = Env()
    e.stub("docker", DOCKER_STUB)
    reg_put(e, tag_ref, manifest)
    rc, out = e.run(find, {**base, "CAN_PUSH": "true"}, cwd=repo)
    check(f"find[{where}]: changed content = another tree = no image", rc == 0 and e.outputs().get("found") == "false"
          and e.outputs().get("tree") != tree1, f"rc={rc} {e.outputs()} {out}")
    e.done()

    e = Env()
    e.stub("docker", DOCKER_STUB)
    t = git(repo, "rev-parse", "HEAD^{tree}")
    reg_err(e, f"ghcr.io/plat-form/app-thing:tree-{t}", "ERROR: failed to do request: Get https://ghcr.io/v2/: net/http: TLS handshake timeout\n")
    rc, out = e.run(find, {**base, "CAN_PUSH": "true"}, cwd=repo)
    check(f"find[{where}]: a lookup error is retried 3 times, warned about, and the image is built", rc == 0 and e.outputs().get("found") == "false"
          and len([c for c in e.calls() if "inspect" in c]) == 3 and "::warning::" in out and "so the image is built" in out, f"rc={rc} {e.calls()} {out}")
    e.done()

    e = Env()
    e.stub("docker", DOCKER_STUB)
    rc, out = e.run(find, {**base, "CAN_PUSH": "false"}, cwd=repo)
    check(f"find[{where}]: a run that cannot push does not look anything up", rc == 0 and e.outputs().get("found") == "false" and e.calls() == [],
          f"rc={rc} {e.calls()} {out}")
    e.done()
    shutil.rmtree(repo, ignore_errors=True)

# ───────────────────────── Record the image ─────────────────────────
REC = step(ci_files["go"]["jobs"]["image"], "id", "image")["run"]
check("record: the pull-request and the publish workflow run the same script", REC == step(pub["jobs"]["image"], "id", "image")["run"])
D1, D2 = "sha256:" + "a" * 64, "sha256:" + "b" * 64
for name, env_, rc_want, want in (
        ("reused", {"FOUND": "true", "FOUND_DIGEST": D1, "PUSH": "true", "BUILT_DIGEST": ""}, 0, {"how": "reused", "digest": D1, "ref": f"ghcr.io/o/r@{D1}"}),
        ("built and pushed", {"FOUND": "false", "FOUND_DIGEST": "", "PUSH": "true", "BUILT_DIGEST": D2}, 0, {"how": "built", "digest": D2, "ref": f"ghcr.io/o/r@{D2}"}),
        ("built locally (fork)", {"FOUND": "false", "FOUND_DIGEST": "", "PUSH": "false", "BUILT_DIGEST": ""}, 0, {"how": "local", "ref": "service-ci-scan:local"}),
        ("pushed but no digest", {"FOUND": "false", "FOUND_DIGEST": "", "PUSH": "true", "BUILT_DIGEST": ""}, 1, {}),
        ("a digest that is not sha256:<64 hex>", {"FOUND": "true", "FOUND_DIGEST": "sha256:short", "PUSH": "true", "BUILT_DIGEST": ""}, 1, {})):
    e = Env()
    rc, out = e.run(REC, {"GITHUB_REPOSITORY": "O/R", "TREE": "t" * 40, **env_})
    o = e.outputs()
    check(f"record: {name}", (rc == 0) == (rc_want == 0) and all(o.get(k) == v for k, v in want.items()), f"rc={rc} {o} {out}")
    check(f"record: {name}: the record step writes outputs only (the summary is the last step)", parse_summary(e) == "")
    e.done()

# ───────────────────────── Copy to Docker Hub ─────────────────────────
COPY = step(pub["jobs"]["publish"], "id", "promote")["run"]
SRC_BYTES = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[{"digest":"sha256:' + b"c" * 64 + b'"}]}'
SRC_DIGEST = sha256(SRC_BYTES)
SRC = f"ghcr.io/plat-form/app-thing@{SRC_DIGEST}"
COMMIT = "1234567890abcdef1234567890abcdef12345678"
HUB = "someuser/thing"
HUB_TAG = f"{HUB}:sha-1234567"


def copy_env():
    e = Env()
    e.stub("docker", DOCKER_STUB)
    reg_put(e, SRC, SRC_BYTES)
    return e


cenv = {"GITHUB_SHA": COMMIT, "SOURCE": SRC, "SOURCE_DIGEST": SRC_DIGEST, "HUB_REPO": HUB}
e = copy_env()
rc, out = e.run(COPY, cenv)
o = e.outputs()
check("copy: the scanned digest is copied as sha-<7> only", rc == 0 and o.get("tag") == "sha-1234567" and o.get("digest") == SRC_DIGEST
      and o.get("ref") == f"{HUB}@{SRC_DIGEST}" and len([c for c in e.calls() if "imagetools create" in c]) == 1
      and all(f"--tag {HUB_TAG} " in c for c in e.calls() if "create" in c) and sum(c.count("--tag") for c in e.calls()) == 1, f"rc={rc} {o} {e.calls()} {out}")
e.done()

e = copy_env()
reg_put(e, HUB_TAG, SRC_BYTES)
rc, out = e.run(COPY, cenv)
check("copy: a re-run (the tag already has this digest) works", rc == 0 and e.outputs().get("digest") == SRC_DIGEST and "a re-run" in out, f"rc={rc} {out}")
e.done()

e = copy_env()
reg_put(e, HUB_TAG, b'{"some":"other image"}')
rc, out = e.run(COPY, cenv)
check("copy: a published tag with ANOTHER digest is never moved (hard fail, nothing copied)", rc != 0 and "never moved" in out
      and not any("create" in c for c in e.calls()) and e.outputs() == {}, f"rc={rc} {e.calls()} {out}")
e.done()

e = copy_env()
open(os.path.join(e.dir, "create-as"), "wb").write(b'{"docker hub changed the manifest"}')
rc, out = e.run(COPY, cenv)
check("copy: a Docker Hub digest that differs from the scanned one is a HARD failure, no outputs", rc != 0 and "::error::" in out
      and "refusing to sign" in out and e.outputs() == {}, f"rc={rc} {e.outputs()} {out}")
e.done()

e = copy_env()
reg_err(e, HUB_TAG, "ERROR: pull access denied, repository does not exist or may require authorization\n")
rc, out = e.run(COPY, cenv)
# the first read says "denied" (the repository does not exist yet); after the copy the tag exists, so the stub finds the file first
check("copy: a Docker Hub repository that does not exist yet is created by the first publish", rc == 0 and e.outputs().get("digest") == SRC_DIGEST, f"rc={rc} {out}")
e.done()

e = copy_env()
reg_err(e, HUB_TAG, "ERROR: 500 Internal Server Error\n")
open(os.path.join(e.dir, "create-noop"), "w").close()
rc, out = e.run(COPY, cenv)
check("copy: a Docker Hub that cannot be read after the copy fails (no signing of the unknown)", rc != 0 and "could not read" in out
      and len([c for c in e.calls() if "inspect" in c]) >= 6, f"rc={rc} {out}")
e.done()

e = copy_env()
rc, out = e.run(COPY, {**cenv, "SOURCE_DIGEST": "sha256:xyz"})
check("copy: a malformed scanned digest fails before anything is copied", rc != 0 and not any("create" in c for c in e.calls()), f"rc={rc} {out}")
e.done()

# ───────────────────────── Guard: Changes (before) ─────────────────────────
CHG = step(pub["jobs"]["changes"], "id", "diff")["run"]
repo = tempfile.mkdtemp(prefix="test-repo-")
git(repo, "init", "-q", "-b", "main")
A = commit(repo, {"main.go": "package main\n", "README.md": "# x\n"}, "A code")
B = commit(repo, {"README.md": "# x\nmore\n"}, "B docs")
C = commit(repo, {"docs/guide.md": "guide\n", "catalog-info.yaml": "a: b\n"}, "C docs")
D = commit(repo, {"main.go": "package main // v2\n"}, "D code")
F = commit(repo, {".github/workflows/ci.yaml": "name: x\n"}, "F workflow file is code")


def chg(event, before, head, want, name, expect_fail=False, contains=""):
    git(repo, "checkout", "-q", head)
    e = Env()
    rc, out = e.run(CHG, {"EVENT_NAME": event, "BEFORE_SHA": before}, cwd=repo)
    got = e.outputs().get("code")
    ok = (rc != 0) if expect_fail else (rc == 0 and got == want)
    check(f"changes: {name}", ok and contains in out + parse_summary(e), f"rc={rc} code={got} {out} {parse_summary(e)}")
    e.done()


chg("push", A, C, "false", "push, two docs commits since before -> no publish")
chg("push", A, D, "true", "push, a code commit since before -> publish")
chg("push", "0" * 40, C, "false", "push of a new branch (zeros): compares with the parent")
chg("push", "", D, "true", "push without before: compares with the parent (code)")
chg("workflow_dispatch", A, C, "false", "manual run with before, docs only since -> no publish (docs-only merge via the merge button)")
chg("workflow_dispatch", B, D, "true", "manual run with before, code since -> publish")
chg("workflow_dispatch", A, F, "true", "manual run with before: a workflow file counts as code")
chg("workflow_dispatch", "", C, "true", "manual run without before -> publish", contains="manual run without before")
chg("workflow_dispatch", "f" * 40, C, "true", "manual run with a before that does not exist -> assume code", contains="not available")
chg("workflow_dispatch", "not-a-sha", C, "", "manual run with a malformed before fails", expect_fail=True, contains="40-character")
chg("workflow_dispatch", "abc; touch pwned", C, "", "a before with shell characters fails and runs nothing", expect_fail=True)
check("changes: the malformed before ran nothing", not os.path.exists(os.path.join(repo, "pwned")))
chg("schedule", "", C, "true", "any other event: everything runs")
shutil.rmtree(repo, ignore_errors=True)

# ───────────────────────── Publishing is only allowed from commits on main ─────────────────────────
REACH = step(pub["jobs"]["publish"], "name", "Publishing is only allowed from commits on main")["run"]
origin = tempfile.mkdtemp(prefix="test-origin-")
git(origin, "init", "-q", "--bare", "-b", "main")
work = tempfile.mkdtemp(prefix="test-work-")
git(work, "init", "-q", "-b", "main")
m1 = commit(work, {"a": "1"}, "m1")
m2 = commit(work, {"a": "2"}, "m2")
git(work, "checkout", "-q", "-b", "side", m1)
s1 = commit(work, {"b": "1"}, "side only")
git(work, "remote", "add", "origin", origin)
git(work, "push", "-q", "origin", "main", "side")
for name, sha, ok in (("the tip of main", m2, True), ("an older commit of main (a re-run)", m1, True), ("a commit that is not on main", s1, False)):
    git(work, "checkout", "-q", sha)
    e = Env()
    rc, out = e.run(REACH, {"GITHUB_SHA": sha}, cwd=work)
    check(f"reachability: {name}", (rc == 0) == ok and (ok or "not reachable from main" in out), f"rc={rc} {out}")
    e.done()
shutil.rmtree(origin, ignore_errors=True)
shutil.rmtree(work, ignore_errors=True)

# ───────────────────────── Cleanup: prune ─────────────────────────
PRUNE = step(pub["jobs"]["prune"], "name", "Select and delete")["run"]
GH_PRUNE_STUB = r"""#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
if [ "$1" = api ] && [ "$2" = --paginate ]; then cat "$STUB_DIR/versions.json"; exit 0; fi
if [ "$1" = api ] && [ "$2" = -X ] && [ "$3" = DELETE ]; then
  id=${4##*/}
  if grep -qx "$id" "$STUB_DIR/delete-fails" 2>/dev/null; then echo "HTTP 500" >&2; exit 1; fi
  exit 0
fi
echo "unexpected gh call: $*" >&2; exit 99
"""
CURL_STUB = r"""#!/usr/bin/env bash
echo "curl $*" >> "$STUB_LOG"
url=${@: -1}
d=${url##*/}
f=$STUB_DIR/manifest-$(echo "$d" | tr ':' '_').json
cat > /dev/null
if [ -f "$f" ]; then cat "$f"; exit 0; fi
echo "404" >&2; exit 22
"""
now = datetime.now(timezone.utc)


def ver(i, tags, age_days, digest=None):
    return {"id": i, "name": digest or "sha256:" + format(i, "x").rjust(64, "0"),
            "created_at": (now - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "metadata": {"container": {"tags": tags}}}


def child(i):
    return "sha256:" + "e" + format(i, "x").rjust(63, "0")


def prune_run(versions, manifests, env_extra=None, delete_fails=()):
    e = Env()
    e.stub("gh", GH_PRUNE_STUB)
    e.stub("curl", CURL_STUB)
    json.dump(versions, open(os.path.join(e.dir, "versions.json"), "w"))
    open(os.path.join(e.dir, "delete-fails"), "w").write("\n".join(str(x) for x in delete_fails))
    for d, doc in manifests.items():
        json.dump(doc, open(os.path.join(e.dir, f"manifest-{d.replace(':', '_')}.json"), "w"))
    env_ = {"GH_TOKEN": "tok", "OWNER": "Plat-Form", "PACKAGE": "app-thing", "KEEP": "2", "KEEP_TREE": "2", "UNTAGGED_DAYS": "7", "DRY_RUN": "false"}
    env_.update(env_extra or {})
    rc, out = e.run(PRUNE, env_)
    deleted = sorted(int(c.rsplit("/", 1)[1]) for c in e.calls() if c.startswith("gh api -X DELETE"))
    return e, rc, out, deleted


# ids 1-5 sha-* (5 newest), 11-15 tree-* (15 newest), 20 latest, 30/31 children of kept images, 32 old untagged orphan, 33 recent untagged
idx = lambda kids: {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [{"digest": k, "mediaType": "application/vnd.oci.image.manifest.v1+json"} for k in kids]}
versions = ([ver(i, [f"sha-{i:07d}"], 100 - i) for i in range(1, 6)] + [ver(i, [f"tree-{i:040d}"], 50 - i) for i in range(11, 16)]
            + [ver(20, ["latest", "v1.2.3"], 200), ver(30, [], 40, child(30)), ver(31, [], 40, child(31)), ver(32, [], 40, child(32)), ver(33, [], 1, child(33))])
manifests = {v["name"]: idx([child(30)]) for v in versions if v["id"] in (5, 4)}
manifests.update({v["name"]: idx([child(31)]) for v in versions if v["id"] in (15, 14)})
manifests.update({v["name"]: idx([]) for v in versions if v["id"] == 20})
e, rc, out, deleted = prune_run(versions, manifests)
check("prune: keeps the newest 2 sha-* AND the newest 2 tree-*, deletes the older of both families and unreferenced old untagged",
      rc == 0 and deleted == [1, 2, 3, 11, 12, 13, 32], f"rc={rc} deleted={deleted}\n{out}")
check("prune: never deletes a latest/vX.Y.Z tag, a referenced index child, or a recent untagged version",
      not ({20, 30, 31, 33} & set(deleted)), f"deleted={deleted}")
e.done()

# a version that carries a tree- tag of a kept family and an old sha- tag stays
versions2 = [ver(1, ["sha-0000001"], 90), ver(2, ["sha-0000002"], 80), ver(3, ["sha-0000003", "tree-" + "3" * 40], 70), ver(4, ["sha-0000004"], 60),
             ver(5, ["tree-" + "5" * 40], 50), ver(6, ["tree-" + "6" * 40], 40)]
m2_ = {v["name"]: idx([]) for v in versions2}
e, rc, out, deleted = prune_run(versions2, m2_)
check("prune: a version with a sha- tag that is too old but a tree- tag that is among the newest is kept", rc == 0 and deleted == [1, 2], f"rc={rc} deleted={deleted}\n{out}")
e.done()

more = {**manifests, **{v["name"]: idx([]) for v in versions if v["id"] in (2, 3, 13)}}
e, rc, out, deleted = prune_run(versions, more, {"KEEP": "4", "KEEP_TREE": "3"})
check("prune: separate N per family (4 sha-*, 3 tree-*)", rc == 0 and deleted == [1, 11, 12, 32], f"rc={rc} deleted={deleted}\n{out}")
e.done()
e, rc, out, deleted = prune_run(versions, manifests, {"KEEP_TREE": "0"})
check("prune: keep-tree below 1 is refused", rc != 0 and deleted == [] and "at least 1" in out, f"rc={rc} deleted={deleted}\n{out}")
e.done()

e, rc, out, deleted = prune_run(versions, manifests, {"DRY_RUN": "true"})
check("prune: dry run deletes nothing and says what it would", rc == 0 and deleted == [] and "WOULD DELETE" in out, f"rc={rc} deleted={deleted}\n{out}")
e.done()

e, rc, out, deleted = prune_run(versions, manifests, {"PACKAGE": "app-thing/buildcache"})
check("prune: the buildcache package can never be addressed", rc != 0 and deleted == [] and "not a plain container package name" in out
      and not any(c.startswith("gh") for c in e.calls()), f"rc={rc} deleted={deleted}\n{out}")
e.done()

e, rc, out, deleted = prune_run(versions, {})   # the manifest of a kept image cannot be read
check("prune: refuses to delete when a kept image's manifest cannot be read (index children unknown)", rc != 0 and deleted == [] and "nothing was deleted" in out,
      f"rc={rc} deleted={deleted}\n{out}")
e.done()

e, rc, out, deleted = prune_run([ver(1, ["latest"], 5), ver(2, [], 30, child(2))], {})
check("prune: no sha-*/tree-* version at all -> nothing deleted", deleted == [], f"deleted={deleted}\n{out}")
e.done()

e, rc, out, deleted = prune_run(versions, manifests, delete_fails=[1])
check("prune: a failed delete does not stop the others (and exits non-zero, which the continue-on-error job turns into a notice)",
      rc != 0 and deleted == [1, 2, 3, 11, 12, 13, 32], f"rc={rc} deleted={deleted}\n{out}")
e.done()
check("prune: the job is best effort and runs next to the helm pull request", pub["jobs"]["prune"]["continue-on-error"] is True
      and pub["jobs"]["deploy"]["needs"] == ["publish"] and pub["jobs"]["prune"]["needs"] == ["publish"])

# ───────────────────────── the merge button (service-merge.yaml) ─────────────────────────
GH_MERGE_STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
d = os.environ["STUB_DIR"]
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("gh " + " ".join(args) + "\n")
st = json.load(open(d + "/state.json"))

def save():
    json.dump(st, open(d + "/state.json", "w"))

def out(obj, args):
    text = json.dumps(obj)
    for flag in ("--jq", "-q"):
        if flag in args:
            p = subprocess.run(["jq", "-r", args[args.index(flag) + 1]], input=text, capture_output=True, text=True)
            sys.stdout.write(p.stdout)
            sys.exit(p.returncode)
    print(text)

if args[:2] == ["api", f"repos/{st['repo']}/actions/runs/{st['run']}/approvals"]:
    out(st["approvals"], args)
elif args[:2] == ["pr", "view"]:
    pr = dict(st["pr"])
    pr["mergeCommit"] = {"oid": st["merge_commit"]} if pr["state"] == "MERGED" and st.get("merge_commit") else None
    out(pr, args)
elif args[:2] == ["api", f"repos/{st['repo']}/branches/main"]:
    out({"commit": {"sha": st["main_sha"]}}, args)
elif args[0] == "api" and args[1].startswith(f"repos/{st['repo']}/commits/"):
    out({"parents": [{"sha": st["parent"]}]}, args)
elif args[:2] == ["pr", "merge"]:
    if st.get("merge_fails"):
        if st.get("merged_anyway"):
            st["pr"]["state"] = "MERGED"; save()
        print("merge refused", file=sys.stderr); sys.exit(1)
    st["pr"]["state"] = "MERGED"; save()
elif args[:2] == ["workflow", "run"]:
    st["dispatch_calls"] = st.get("dispatch_calls", 0) + 1; save()
    if st["dispatch_calls"] <= st.get("dispatch_fail_times", 0):
        print("HTTP 502", file=sys.stderr); sys.exit(1)
else:
    print("unexpected gh call: " + " ".join(args), file=sys.stderr); sys.exit(99)
'''
TESTED = "a" * 40
PARENT = "b" * 40
MERGE_SHA = "c" * 40


def merge_setup(wf, **over):
    e = Env()
    e.stub("gh", GH_MERGE_STUB)
    st = {"repo": "plat-form/app-thing", "run": "987", "main_sha": "d" * 40, "parent": PARENT, "merge_commit": MERGE_SHA,
          "approvals": [{"state": "approved", "user": {"login": "owner"}, "environments": [{"name": "merge"}]}],
          "pr": {"state": "OPEN", "headRefOid": TESTED, "baseRefName": "main", "title": "Fix the thing", "body": "Body text\nsecond line"}}
    st.update(over)
    json.dump(st, open(os.path.join(e.dir, "state.json"), "w"))
    env_ = {"GH_TOKEN": "tok", "GH_REPO": "plat-form/app-thing", "PR_NUMBER": "7", "TESTED_SHA": TESTED, "TESTED_BASE": "d" * 40, "RUN_ID": "987"}
    return e, env_


def pr_state(e):
    return json.load(open(os.path.join(e.dir, "state.json")))["pr"]["state"]


# the callers: a short job that calls service-merge.yaml (Publish is started everywhere except in the repository without an image pipeline)
for svc in ("checkout-service", "product-catalog-db"):
    tmpd = tempfile.mkdtemp(prefix="test-callers-")
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gen-callers.py"), tmpd, svc, "0" * 40, "v0.0.0"], check=True, capture_output=True)
    text = open(os.path.join(tmpd, ".github/workflows/ci.yaml")).read()
    cj = yaml.safe_load(text)["jobs"]["merge"]
    shutil.rmtree(tmpd, ignore_errors=True)
    check(f"merge caller[{svc}]: a short job that calls service-merge.yaml", cj["uses"].endswith("/service-merge.yaml@" + "0" * 40) and cj["needs"] == "ci"
          and "steps" not in cj and len(text.splitlines()) < 60, f"{cj} {len(text.splitlines())} lines")
    check(f"merge caller[{svc}]: Publish is started only where publish.yaml exists", (cj.get("with") or {}).get("start-publish", True) == (svc != "product-catalog-db"))

mw = load("service-merge.yaml")
for svc in ("service-merge",):
    ci = mw
    mj = mw["jobs"]["merge"]
    approve, merge = step(mj, "name", "Check that the environment was approved")["run"], step(mj, "id", "merge")["run"]
    publish = step(mj, "name", "Start Publish")["run"]
    e, env_ = merge_setup(ci)
    rc, out = e.run(approve, env_)
    check(f"merge[{svc}]: approved environment passes", rc == 0, f"rc={rc} {out}")
    e.done()
    for name, approvals in (("nobody approved (environment without a reviewer)", []), ("approval of another environment", [{"state": "approved", "environments": [{"name": "prod"}]}]),
                            ("a rejection", [{"state": "rejected", "environments": [{"name": "merge"}]}])):
        e, env_ = merge_setup(ci, approvals=approvals)
        rc, out = e.run(approve, env_)
        check(f"merge[{svc}]: {name} -> refuses", rc != 0 and "nobody approved" in out, f"rc={rc} {out}")
        e.done()

    e, env_ = merge_setup(ci)
    rc, out = e.run(merge, env_)
    o = e.outputs()
    merge_calls = [c for c in e.calls() if c.startswith("gh pr merge")]
    check(f"merge[{svc}]: squash-merges the tested commit with the PR title and body", rc == 0 and len(merge_calls) == 1
          and f"--squash --delete-branch --match-head-commit {TESTED} --subject Fix the thing (#7) --body Body text" in merge_calls[0], f"rc={rc} {merge_calls} {out}")
    check(f"merge[{svc}]: before = the parent of the merge commit (main before the merge)", o.get("before") == PARENT and o.get("merge") == MERGE_SHA, f"{o} {out}")
    if publish:
        e.out = e.out  # same output file
        rc2, out2 = e.run(publish, {**env_, "BEFORE": o.get("before", "")})
        wcalls = [c for c in e.calls() if c.startswith("gh workflow run")]
        check(f"merge[{svc}]: starts Publish on main with before", rc2 == 0 and wcalls == [f"gh workflow run publish.yaml --ref main -f before={PARENT}"], f"rc={rc2} {wcalls} {out2}")
    e.done()

    e, env_ = merge_setup(ci, pr={"state": "OPEN", "headRefOid": "9" * 40, "baseRefName": "main", "title": "t", "body": "b"})
    rc, out = e.run(merge, env_)
    check(f"merge[{svc}]: the branch moved after the tests -> nothing is merged", rc != 0 and "the branch moved" in out
          and not any(c.startswith("gh pr merge") for c in e.calls()), f"rc={rc} {out}")
    e.done()
    for st_ in ("MERGED", "CLOSED"):
        e, env_ = merge_setup(ci, pr={"state": st_, "headRefOid": TESTED, "baseRefName": "main", "title": "t", "body": "b"})
        rc, out = e.run(merge, env_)
        check(f"merge[{svc}]: a {st_} pull request is not merged again", rc != 0 and not any(c.startswith("gh pr merge") for c in e.calls()), f"rc={rc} {out}")
        e.done()
    e, env_ = merge_setup(ci, pr={"state": "OPEN", "headRefOid": TESTED, "baseRefName": "release", "title": "t", "body": "b"})
    rc, out = e.run(merge, env_)
    check(f"merge[{svc}]: only pull requests into main", rc != 0 and not any(c.startswith("gh pr merge") for c in e.calls()), f"rc={rc} {out}")
    e.done()
    e, env_ = merge_setup(ci, main_sha="e" * 40)
    rc, out = e.run(merge, env_)
    check(f"merge[{svc}]: main moved since the tests -> warns, still merges", rc == 0 and "::warning::main moved" in out, f"rc={rc} {out}")
    e.done()
    e, env_ = merge_setup(ci, merge_fails=True)
    rc, out = e.run(merge, env_)
    check(f"merge[{svc}]: a refused merge (branch protection) fails the job, nothing started", rc != 0 and "was refused" in out and pr_state(e) == "OPEN", f"rc={rc} {out}")
    e.done()
    e, env_ = merge_setup(ci, merge_fails=True, merged_anyway=True)
    rc, out = e.run(merge, env_)
    check(f"merge[{svc}]: gh fails AFTER merging (branch deletion) -> still continues with before", rc == 0 and e.outputs().get("before") == PARENT, f"rc={rc} {out}")
    e.done()
    e, env_ = merge_setup(ci, merge_commit="")
    rc, out = e.run(merge, env_)
    check(f"merge[{svc}]: merged but the merge commit cannot be read -> loud error with the manual way out", rc != 0 and "Start Publish by hand" in out, f"rc={rc} {out}")
    e.done()
    e, env_ = merge_setup(ci, pr={"state": "OPEN", "headRefOid": TESTED, "baseRefName": "main", "title": 'x"; $(touch pwned) `touch pwned2`', "body": "$(touch pwned3)"})
    work = tempfile.mkdtemp(prefix="test-inj-")
    rc, out = e.run(merge, env_, cwd=work)
    mc = [c for c in e.calls() if c.startswith("gh pr merge")]
    check(f"merge[{svc}]: a PR title with shell syntax is passed as text and runs nothing", rc == 0 and mc and "$(touch pwned)" in mc[0] and os.listdir(work) == [], f"rc={rc} {mc} {os.listdir(work)}")
    shutil.rmtree(work, ignore_errors=True)
    e.done()
    if publish:
        e, env_ = merge_setup(ci, dispatch_fail_times=1)
        rc, out = e.run(publish, {**env_, "BEFORE": PARENT})
        check(f"merge[{svc}]: a failed dispatch is retried", rc == 0 and len([c for c in e.calls() if "workflow run" in c]) == 2, f"rc={rc} {out}")
        e.done()
        e, env_ = merge_setup(ci, dispatch_fail_times=9)
        rc, out = e.run(publish, {**env_, "BEFORE": PARENT})
        check(f"merge[{svc}]: when Publish cannot be started the job is red and says how to start it", rc != 0 and "Start it by hand" in out and PARENT in out, f"rc={rc} {out}")
        e.done()

# ───────────────────────── release.yaml ─────────────────────────
rel = load("release.yaml")["jobs"]["tag"]
vcheck, vtag = step(rel, "name", "Check the version")["run"], step(rel, "name", "Create and push the annotated tag")["run"]
origin = tempfile.mkdtemp(prefix="test-origin-")
git(origin, "init", "-q", "--bare", "-b", "main")
work = tempfile.mkdtemp(prefix="test-work-")
git(work, "init", "-q", "-b", "main")
head = commit(work, {"a": "1"}, "m1")
git(work, "remote", "add", "origin", origin)
git(work, "push", "-q", "origin", "main")
git(work, "tag", "-a", "v3.3.0", "-m", "old")
git(work, "push", "-q", "origin", "v3.3.0")
for version, ok in (("v3.4.0", True), ("v10.0.12", True), ("3.4.0", False), ("v3.4", False), ("v03.4.0", False), ("v3.4.0-rc1", False), ("", False),
                    ("v3.3.0", False), ("v3.4.0; touch pwned", False)):
    e = Env()
    rc, out = e.run(vcheck, {"VERSION": version}, cwd=work)
    check(f"release: version {version!r} {'accepted' if ok else 'refused'}", (rc == 0) == ok, f"rc={rc} {out}")
    e.done()
check("release: the refused version ran nothing", not os.path.exists(os.path.join(work, "pwned")))
e = Env()
rc, out = e.run(vtag, {"VERSION": "v3.4.0", "GITHUB_SHA": head}, cwd=work)
kind = subprocess.run(["git", "-C", origin, "cat-file", "-t", "refs/tags/v3.4.0"], capture_output=True, text=True).stdout.strip()
target = subprocess.run(["git", "-C", origin, "rev-parse", "refs/tags/v3.4.0^{commit}"], capture_output=True, text=True).stdout.strip()
check("release: creates an ANNOTATED tag on the commit of main and prints the SHA to pin", rc == 0 and kind == "tag" and target == head
      and head in parse_summary(e) and f"@{head} # v3.4.0" in parse_summary(e), f"rc={rc} kind={kind} target={target} {out}")
e.done()
rel_wf = load("release.yaml")
check("release: runs only on main, needs only contents: write, no environment approval", rel["if"] == "${{ github.ref == 'refs/heads/main' }}"
      and rel["permissions"] == {"contents": "write"} and "environment" not in rel)
shutil.rmtree(origin, ignore_errors=True)
shutil.rmtree(work, ignore_errors=True)

# ───────────────────────── Run summaries ─────────────────────────
SERVER = "https://github.com"
MAX_SUMMARY = 1024 * 1024  # GitHub rejects a step summary over 1 MiB

# ---- Detect changes: "What changed" (pull request, from the flat service-ci workflow; and the publish flavour) ----
CI_CHG = step(ci_files["go"]["jobs"]["changes"], "id", "diff")["run"]


def pr_repo(base_files, pr_files, main_moved=True):
    """A repository with a real merge commit (HEAD = main + the pull request), like the checkout of a pull_request run."""
    repo = tempfile.mkdtemp(prefix="test-repo-")
    git(repo, "init", "-q", "-b", "main")
    commit(repo, base_files, "base")
    git(repo, "checkout", "-q", "-b", "pr")
    commit(repo, pr_files, "the pull request")
    git(repo, "checkout", "-q", "main")
    if main_moved:
        commit(repo, {"unrelated.txt": "x\n"}, "main moved")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    return repo


def what_changed(script, event, repo, before=""):
    e = Env()
    rc, out = e.run(script, {"EVENT_NAME": event, "BEFORE_SHA": before}, cwd=repo)
    summ = parse_summary(e)
    o = e.outputs()
    e.done()
    return rc, out, summ, o


repo = pr_repo({"main.go": "package main\n"}, {"README.md": "# x\n", "docs/a.md": "a\n"})
rc, out, summ, o = what_changed(CI_CHG, "pull_request", repo)
check("summary: docs-only pull request: verdict, count, list",
      rc == 0 and o.get("code") == "false" and summ.startswith("### What changed\n\n**Documentation only.** Tests, scans and the image build are skipped.\n")
      and "\n2 changed files.\n" in summ and "<details><summary>Changed files (2)</summary>" in summ and "- `README.md`" in summ and "- `docs/a.md`" in summ
      and "</details>" in summ and "Workflow files" not in summ, f"rc={rc} {o}\n{summ}")
shutil.rmtree(repo, ignore_errors=True)

repo = pr_repo({"main.go": "package main\n"}, {".github/workflows/ci.yaml": "name: x\n", "main.go": "package main // v2\n", "README.md": "r\n"})
rc, out, summ, o = what_changed(CI_CHG, "pull_request", repo)
check("summary: code pull request that touches a workflow file: names the first code file and warns about the workflow",
      rc == 0 and o.get("code") == "true" and "**Code changed.** `.github/workflows/ci.yaml` is not documentation or metadata, so everything runs." in summ
      and "\n3 changed files.\n" in summ and "**Workflow files are part of this change.** Approving the merge includes them: read them first." in summ,
      f"rc={rc} {o}\n{summ}")
rc, out, summ_push, o = what_changed(CI_CHG, "push", repo, before=git(repo, "rev-parse", "HEAD^1"))
check("summary: the workflow warning is for pull requests only (a push has no merge to approve)", rc == 0 and "Workflow files" not in summ_push, summ_push)
shutil.rmtree(repo, ignore_errors=True)

repo = pr_repo({"main.go": "package main\n"}, {"one.md": "1\n"})
rc, out, summ, o = what_changed(CI_CHG, "pull_request", repo)
check("summary: one file is '1 changed file.'", "\n1 changed file.\n" in summ and "Changed files (1)" in summ, summ)
shutil.rmtree(repo, ignore_errors=True)

# hostile file names: a backtick, a newline followed by a workflow command, HTML, a pipe, non-ASCII, a very long name
hostile = {"a`b`c.go": "1", "x\n::error::boom.go": "1", "<img src=x onerror=alert(1)>.md": "1", "t|able.md": "1", "caf\u00e9.go": "1", "n" * 230 + ".go": "1"}
repo = pr_repo({"main.go": "package main\n"}, hostile)
rc, out, summ, o = what_changed(CI_CHG, "pull_request", repo)
items = [l for l in summ.splitlines() if l.startswith("- ")]
check("summary: hostile file names stay inside their code span (one list line each, one pair of backticks, plain characters, 200 characters at most)",
      rc == 0 and len(items) == len(hostile) and all(l.startswith("- `") and l.endswith("`") and l.count("`") == 2 and len(l) <= 206 for l in items)
      and not any(l.startswith("boom") or l.startswith("::") for l in summ.splitlines()) and all(ord(c) < 128 for c in summ), f"rc={rc}\n{summ}")
check("summary: a file name cannot inject a workflow command into the log", not any(l.startswith("::") for l in out.splitlines()), out)
shutil.rmtree(repo, ignore_errors=True)

# a very large pull request: the list is cut at 50, the summary stays small
many = {f"src/f{i:04d}.go": "x" for i in range(400)}
repo = pr_repo({"main.go": "package main\n"}, many)
rc, out, summ, o = what_changed(CI_CHG, "pull_request", repo)
items = [l for l in summ.splitlines() if l.startswith("- `")]
check("summary: 400 changed files: 50 listed, '- and 350 more', well under 1 MiB",
      rc == 0 and len(items) == 50 and "- and 350 more" in summ and "\n400 changed files.\n" in summ and len(summ) < 10000 and len(summ) < MAX_SUMMARY, f"len={len(summ)}")
shutil.rmtree(repo, ignore_errors=True)

# the cases decided before any file is listed: a reason, no list
repo = pr_repo({"main.go": "package main\n"}, {"a.md": "1\n"})
rc, out, summ, o = what_changed(CI_CHG, "workflow_dispatch", repo)
check("summary: manual run: treated as code, with the reason", rc == 0 and o.get("code") == "true"
      and "**Treated as a code change.** Reason: manual or scheduled run: everything runs." in summ and "<details>" not in summ, summ)
shutil.rmtree(repo, ignore_errors=True)

# publish flavour: a docs-only push says that nothing is published
repo = tempfile.mkdtemp(prefix="test-repo-")
git(repo, "init", "-q", "-b", "main")
b0 = commit(repo, {"main.go": "package main\n"}, "code")
commit(repo, {"docs/guide.md": "g\n"}, "docs")
rc, out, summ, o = what_changed(CHG, "push", repo, before=b0)
check("summary: publish, docs-only push: 'Nothing is published.'", rc == 0 and o.get("code") == "false" and "**Documentation only.** Nothing is published." in summ
      and "Tests, scans" not in summ, summ)
rc, out, summ, o = what_changed(CHG, "workflow_dispatch", repo, before="")
check("summary: publish, manual run without before", "Reason: manual run without before: everything runs." in summ, summ)
shutil.rmtree(repo, ignore_errors=True)

# ---- the image section (pull-request image job and publish image job: the same step) ----
IMG_PR = step(ci_files["go"]["jobs"]["image"], "name", "Write the image summary")
IMG_PUB = step(pub["jobs"]["image"], "name", "Write the image summary")
check("summary: the image section is the same script in the pull-request and the publish workflow", IMG_PR["run"] == IMG_PUB["run"])
check("summary: the image section runs after a failure, not after a cancel", IMG_PR["if"] == IMG_PUB["if"] == "${{ !cancelled() }}")
T = "7" * 40
for name, env_, has, lacks in (
        ("reused", {"HOW": "reused", "TREE": T, "DIGEST": D1}, ["### Image", "**Reused.**", f"| Tag | `tree-{T}` |", f"| Digest | `{D1}` |"], ["Trivy", "No image"]),
        ("built", {"HOW": "built", "TREE": T, "DIGEST": D2, "SCAN": "success"}, ["**Built** and pushed", f"`{D2}`", "| Trivy scan | passed: no HIGH or CRITICAL vulnerabilities |"], ["failed"]),
        ("scan failed", {"HOW": "built", "TREE": T, "DIGEST": D2, "SCAN": "failure"}, ["| Trivy scan | **failed**: HIGH or CRITICAL vulnerabilities", 'step "Run Trivy vulnerability scanner"'], ["passed"]),
        ("scan did not run", {"HOW": "built", "TREE": T, "DIGEST": D2, "SCAN": "skipped"}, ["| Trivy scan | not run |"], []),
        ("local (fork): no tag, no digest", {"HOW": "local", "TREE": T, "DIGEST": "", "SCAN": "success"}, ["**Built locally, not pushed.**", "| Trivy scan | passed"], ["| Tag", "| Digest"]),
        ("the image step failed: nothing recorded", {"HOW": "", "TREE": T, "DIGEST": "", "SCAN": "skipped"}, ["**No image was produced.**"], ["| Tag", "| Digest"]),
        ("the lookup failed before the tree was known", {"HOW": "", "TREE": "", "DIGEST": ""}, ["**No image was produced.**"], ["| Tag"])):
    e = Env()
    rc, out = e.run(IMG_PR["run"], env_)
    summ = parse_summary(e)
    check(f"summary: image: {name}", rc == 0 and all(x in summ for x in has) and not any(x in summ for x in lacks), f"rc={rc} {out}\n{summ}")
    e.done()

# ---- the published section ----
SUM = step(pub["jobs"]["publish"], "name", "Write the run summary")["run"]
pub_env = {"GITHUB_SHA": COMMIT, "GITHUB_SERVER_URL": SERVER, "GITHUB_REPOSITORY": "o/app-thing", "TREE": T, "HOW": "reused", "DIGEST": D1, "TAG": "sha-1234567",
           "APP": "thing", "TARGET_ENV": "dev", "HELM_URL": SERVER + "/o/helm"}
e = Env()
rc, out = e.run(SUM, pub_env)
summ = parse_summary(e)
check("summary: published: tag, commit link, digest, reused tree, signature, where the dev change goes",
      rc == 0 and summ.startswith("### Published `sha-1234567`\n") and f"| Commit | [`1234567`]({SERVER}/o/app-thing/commit/{COMMIT}) |" in summ
      and "| Docker Hub tag | `sha-1234567` |" in summ and f"| Digest | `{D1}` |" in summ and f"reused: built by the pull request from source tree `{T}`" in summ
      and "| Signature | verified (keyless cosign" in summ and f"[the pull request in platform-demo-helm that moves `dev` of `thing` to this digest]({SERVER}/o/helm/pulls?q=is%3Apr+head%3Apromote%2Fthing%2Fdev)" in summ,
      f"rc={rc} {out}\n{summ}")
e.done()
e = Env()
rc, out = e.run(SUM, {**pub_env, "HOW": "built"})
check("summary: published: a build in this run says so", rc == 0 and "built in this run from source tree" in parse_summary(e), parse_summary(e))
e.done()
e = Env()
rc, out = e.run(SUM, {**pub_env, "APP": "x) [evil](http://e.example) (", "TARGET_ENV": "dev"})
summ = parse_summary(e)
check("summary: published: an odd app name is neither printed nor made into a link", rc == 0 and "evil" not in summ and "](http" not in summ.split("| Dev change |")[1]
      and "| Dev change | the pull request in platform-demo-helm that moves the environment to this digest |" in summ, summ)
e.done()
check("summary: published: no Docker Hub account name can be in it (the copy step is the only one that knows it)", "$HUB_REPO" not in SUM and "DOCKERHUB" not in SUM)

# ---- scan and SBOM ----
SCAN_SUM = step(pub["jobs"]["scan"], "name", "Write the run summary")["run"]
for outcome, want in (("success", "Trivy found no HIGH or CRITICAL vulnerabilities in `" + D1 + "`."), ("failure", "**Trivy failed** on `" + D1 + "`"), ("skipped", "Trivy did not run.")):
    e = Env()
    rc, out = e.run(SCAN_SUM, {"SCAN": outcome, "DIGEST": D1})
    check(f"summary: scan image: {outcome}", rc == 0 and want in parse_summary(e) and parse_summary(e).startswith("### Image scan\n"), parse_summary(e))
    e.done()
e = Env()
rc, out = e.run(step(pub["jobs"]["sbom"], "name", "Write the run summary")["run"], {"DIGEST": D1})
check("summary: SBOM", rc == 0 and f"An SPDX SBOM is attached to `{D1}` as a signed attestation, and the attestation was verified." in parse_summary(e), parse_summary(e))
e.done()

# ---- prune ----
e, rc, out, deleted = prune_run(versions, manifests)
summ = parse_summary(e)
check("summary: prune: counts, then the deleted versions folded away",
      rc == 0 and summ.startswith("### Clean up staging images\n\n`ghcr.io/plat-form/app-thing` had 15 versions; deleted 7, 8 remain.\n")
      and "<summary>Versions selected for deletion (7)</summary>" in summ and summ.rstrip().endswith("</details>") and summ.count("\n| ") == 7 + 1, f"rc={rc}\n{summ}")
e.done()
e, rc, out, deleted = prune_run(versions, manifests, {"DRY_RUN": "true"})
summ = parse_summary(e)
check("summary: prune: a dry run says what it would do", "(dry run)" in summ and "would delete 7, 8 would remain" in summ and "Versions that would be deleted (7)" in summ, summ)
e.done()
e, rc, out, deleted = prune_run(versions, manifests, delete_fails=[2])
summ = parse_summary(e)
check("summary: prune: failed deletions are counted, not reported as deleted", rc != 0 and "deleted 6, 9 remain; 1 deletion(s) failed, see the log." in summ, f"rc={rc}\n{summ}")
e.done()
odd = [ver(1, ["sha-0000001|<b>`x"], 90), ver(2, ["sha-0000002"], 80), ver(3, ["sha-0000003"], 70), ver(4, ["tree-" + "4" * 40], 60), ver(5, ["tree-" + "5" * 40], 50)]
e, rc, out, deleted = prune_run(odd, {v["name"]: idx([]) for v in odd}, {"KEEP": "2", "KEEP_TREE": "2"})
summ = parse_summary(e)
check("summary: prune: registry text in the table is reduced to plain characters", rc == 0 and deleted == [1] and "sha-0000001?" in summ and "<b>" not in summ and "`x" not in summ, f"rc={rc} {deleted}\n{summ}")
e.done()

if failures:
    print("\n\n".join(failures))
    print(f"\n{len(failures)} of {count} shell scenarios failed")
    sys.exit(1)
print(f"shell: {count} scenarios ok")
