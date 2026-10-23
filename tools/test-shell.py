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
  * the promotion chain of service-publish (v3.6): Wait for dev|stage|prod (merged, replaced by a newer build, closed, timeout, API errors),
    the approval check, the re-check after the click (superseded), the version logic (none -> 1.0.0, highest +1, look-alike tags, the version in
    the stage file, prod takes stage's), the release (idempotent, never moves a tag, 403 and ruleset answers told apart) and the check that dev
    does not already run a newer commit; all against a stub gh that plays platform-demo-helm
"""
import hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile, textwrap
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


# ───────────────────────── the promotion chain after `Open dev PR` (service-publish.yaml) ─────────────────────────
# One stub of gh for all of it: platform-demo-helm is a dictionary (values files per environment and ref, tags, pull requests, releases).
# A value that is a LIST is consumed call by call (the last one stays), so "open, open, merged" can be told.
GH_CHAIN_STUB = r"""#!/usr/bin/env python3
import json, os, re, subprocess, sys
d = os.environ["STUB_DIR"]
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("gh " + " ".join(args) + "\n")
st = json.load(open(d + "/chain.json"))

def save():
    json.dump(st, open(d + "/chain.json", "w"))

def seq(key, value):
    if not isinstance(value, list):
        return value
    pos = st.setdefault("pos", {})
    i = pos.get(key, 0)
    pos[key] = i + 1
    save()
    return value[min(i, len(value) - 1)]

def emit(obj):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    for flag in ("--jq", "-q"):
        if flag in args and not isinstance(obj, str):
            p = subprocess.run(["jq", "-r", args[args.index(flag) + 1]], input=text, capture_output=True, text=True)
            sys.stdout.write(p.stdout); sys.exit(p.returncode)
    print(text)
    sys.exit(0)

def err(msg, code=1):
    print(msg, file=sys.stderr); sys.exit(code)

def not_found():
    err("{\"message\":\"Not Found\"}\ngh: Not Found (HTTP 404)")

url = next((a for a in args if a.startswith("repos/")), "")
post = "-X" in args and args[args.index("-X") + 1] == "POST"
if "-X" in args and args[args.index("-X") + 1] in ("DELETE", "PATCH", "PUT"):
    err("this stub refuses to change or delete anything", 98)
H = "repos/" + st["helm"]
m = re.match(re.escape(H) + r"/contents/envs/([a-z]+)/values/([a-z0-9-]+)\.yaml\?ref=(.+)$", url)
if m:
    key = m.group(1) + "@" + m.group(3)
    fails = st.get("read_fail", {})
    if fails.get(key, 0) > 0:
        fails[key] -= 1; save(); err("gh: Bad Gateway (HTTP 502)")
    if key not in st["files"]:
        not_found()
    sys.stdout.write(seq("file:" + key, st["files"][key])); sys.exit(0)
m = re.match(re.escape(H) + r"/pulls/(\d+)$", url)
if m:
    if st.get("pr_fail"):
        st["pr_fail"] -= 1; save(); err("gh: Bad Gateway (HTTP 502)")
    emit(seq("pr", st["pr"]))
if url.startswith(H + "/git/matching-refs/tags/"):
    if st.get("tags_fail"):
        err("gh: Bad Gateway (HTTP 502)")
    prefix = "refs/tags/" + url.split("/git/matching-refs/tags/")[1]
    emit([{"ref": r} for r in st["tags"] if r.startswith(prefix)])
if url.startswith(H + "/commits?path="):
    emit([{"sha": st["path_commit"]}])
if url.startswith(H + "/git/ref/tags/"):
    name = url.split("/git/ref/tags/")[1]
    if name in st.get("tag_refs", []):
        emit({"ref": "refs/tags/" + name, "object": {"sha": "f" * 40, "type": "commit"}})
    not_found()
if url == H + "/git/refs" and post:
    if st.get("post_ref_fail"):
        err(st["post_ref_fail"])
    st.setdefault("tag_refs", []).append(next(a for a in args if a.startswith("ref=refs/tags/"))[len("ref=refs/tags/"):]); save()
    emit({"ref": "created"})
if url.startswith(H + "/releases/tags/"):
    name = url.split("/releases/tags/")[1]
    if name in st.get("releases", []):
        emit({"tag_name": name})
    not_found()
if url == H + "/releases" and post:
    if st.get("post_release_fail"):
        err(st["post_release_fail"])
    st.setdefault("releases", []).append(next(a for a in args if a.startswith("tag_name="))[len("tag_name="):]); save()
    emit({"html_url": "x"})
m = re.match(r"repos/([^/]+/[^/]+)/actions/runs/(\d+)/approvals$", url)
if m:
    emit(st["approvals"])
m = re.match(r"repos/([^/]+/[^/]+)/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)$", url)
if m:
    if st.get("compare") == "error":
        not_found()
    emit({"status": st.get("compare", "ahead")})
err("unexpected gh call: " + " ".join(args), 99)
"""
HELM_FULL = "platform-boutique/platform-demo-helm"
CD_OLD, CD_NEW, CD_NEWER = "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64
MSHA = "d" * 40


def vals(digest, version=None, tag="sha-1234567"):
    return f"replicas: 1\nimage:\n  tag: \"{tag}\"\n  digest: {digest}{'   # v' + version if version else ''}\nresources: {{}}\n"


def chain_env(files=None, **over):
    e = Env()
    e.stub("gh", GH_CHAIN_STUB)
    st = {"helm": HELM_FULL, "files": files or {}, "tags": [], "pr": {"merged": False, "state": "open", "merge_commit_sha": None},
          "path_commit": MSHA, "approvals": []}
    st.update(over)
    json.dump(st, open(os.path.join(e.dir, "chain.json"), "w"))
    return e


def calls_of(e, *words):
    return [c for c in e.calls() if all(w in c for w in words)]


def writes(e):
    return [c for c in e.calls() if re.search(r"-X (POST|DELETE|PATCH|PUT)", c)]


MERGED = {"merged": True, "state": "closed", "merge_commit_sha": MSHA}
WAIT = {k: step(pub["jobs"][k], "id", "wait")["run"] for k in ("wait-dev", "wait-stage", "wait-prod")}
check("chain: the three wait jobs run one script", len(set(WAIT.values())) == 1)
WAIT_RUN = WAIT["wait-dev"]


def wait_env(extra=None):
    base = {"GH_TOKEN": "t", "APP": "thing", "TARGET_ENV": "dev", "DIGEST": CD_NEW, "TAG": "sha-1234567", "PR_NUMBER": "7",
            "EXPECT_VERSION": "", "WAIT_MINUTES": "1", "POLL_SECONDS": "15"}
    base.update(extra or {})
    return base


# dev: open, open, merged with this build; main shows it afterwards
e = chain_env({"dev@main": vals(CD_NEW), f"dev@{MSHA}": vals(CD_NEW)},
              pr=[{"merged": False, "state": "open"}, {"merged": False, "state": "open"}, MERGED])
rc, out = e.run(WAIT_RUN, wait_env())
o, summ = e.outputs(), parse_summary(e)
check("wait dev: the pull request merges with this build -> landed and current", rc == 0 and o.get("landed") == "true" and o.get("current") == "true"
      and o.get("sha") == MSHA and o.get("digest") == CD_NEW and o.get("tag") == "sha-1234567"
      and summ.startswith("### Dev deployed\n") and f"[pull request #7](https://github.com/{HELM_FULL}/pull/7)" in summ and "Approve stage deployment" in summ
      and "ends as cancelled" in summ and not writes(e), f"rc={rc} {o}\n{summ}\n{out}")
e.done()
# dev: nothing was opened because main already has the digest
e = chain_env({"dev@main": vals(CD_NEW)})
rc, out = e.run(WAIT_RUN, wait_env({"PR_NUMBER": ""}))
check("wait dev: no pull request and main already has the digest -> landed at once, no pull request read",
      rc == 0 and e.outputs().get("landed") == "true" and "nothing had to change" in parse_summary(e) and not calls_of(e, "/pulls/"), f"rc={rc} {out}")
e.done()
# dev: no pull request, another digest (an older build did not replace a newer promotion)
e = chain_env({"dev@main": vals(CD_NEWER)})
rc, out = e.run(WAIT_RUN, wait_env({"PR_NUMBER": ""}))
summ = parse_summary(e)
check("wait dev: no pull request and another digest in dev -> superseded, green", rc == 0 and e.outputs().get("landed") == "false" and e.outputs().get("current") == "false"
      and summ.startswith("### Dev: superseded by a newer build\n") and "not a failure" in summ, f"rc={rc} {summ}")
e.done()
# dev: the pull request merged with the content of a newer build
e = chain_env({"dev@main": [vals(CD_OLD), vals(CD_NEWER)], f"dev@{MSHA}": vals(CD_NEWER)}, pr=MERGED)
rc, out = e.run(WAIT_RUN, wait_env())
check("wait dev: merged, but with a newer build's digest (the branch is shared) -> superseded, not 'landed'", rc == 0 and e.outputs().get("landed") == "false"
      and "superseded by a newer build" in parse_summary(e), f"rc={rc} {out}")
e.done()
# dev: merged with this build, then a newer build moved dev
e = chain_env({"dev@main": [vals(CD_OLD), vals(CD_NEWER)], f"dev@{MSHA}": vals(CD_NEW)}, pr=MERGED)
rc, out = e.run(WAIT_RUN, wait_env())
check("wait dev: merged with this build, dev has moved on since -> landed but not current (no approval is queued)", rc == 0 and e.outputs().get("landed") == "true"
      and e.outputs().get("current") == "false" and "merged, then replaced by a newer build" in parse_summary(e), f"rc={rc} {e.outputs()} {out}")
e.done()
# dev: closed without merging
e = chain_env({"dev@main": vals(CD_OLD)}, pr={"merged": False, "state": "closed"})
rc, out = e.run(WAIT_RUN, wait_env())
summ = parse_summary(e)
check("wait dev: closed unmerged -> the chain stops with a clear message", rc != 0 and summ.startswith("### Dev: the pull request was closed without merging\n")
      and "Nothing was changed in dev" in summ and "::error::" in out, f"rc={rc} {summ}")
e.done()
# dev: closed, but another pull request carried the same digest
e = chain_env({"dev@main": vals(CD_NEW)}, pr={"merged": False, "state": "closed"})
rc, out = e.run(WAIT_RUN, wait_env())
check("wait dev: closed unmerged, but dev has this build anyway -> landed", rc == 0 and e.outputs().get("landed") == "true", f"rc={rc} {out}")
e.done()
# dev: still open after the wait
e = chain_env({"dev@main": vals(CD_OLD)}, pr={"merged": False, "state": "open"})
rc, out = e.run(WAIT_RUN, wait_env())
summ = parse_summary(e)
check("wait dev: still open after the timeout -> fails, names the pull request and the way on", rc != 0 and "waited 1 minutes, not merged yet" in summ
      and f"https://github.com/{HELM_FULL}/pull/7" in summ and "Re-run failed jobs" in summ and len(calls_of(e, "/pulls/7")) == 4, f"rc={rc} {summ} {len(calls_of(e, '/pulls/7'))}")
e.done()
# dev: GitHub fails for a while
e = chain_env({"dev@main": vals(CD_NEW), f"dev@{MSHA}": vals(CD_NEW)}, read_fail={f"dev@{MSHA}": 2, "dev@main": 1}, pr_fail=2, pr=MERGED)
rc, out = e.run(WAIT_RUN, wait_env({"WAIT_MINUTES": "2"}))
check("wait dev: API errors (pull request, file at the merge commit, main) are waited out or retried, then the chain goes on",
      rc == 0 and e.outputs().get("landed") == "true" and e.outputs().get("current") == "true" and "could not read pull request" in out, f"rc={rc} {out}")
e.done()
e = chain_env({"dev@main": [vals(CD_OLD), vals(CD_NEW)]}, read_fail={"dev@main": 2})
rc, out = e.run(WAIT_RUN, wait_env({"WAIT_MINUTES": "2", "PR_NUMBER": ""}))
check("wait dev: no pull request, GitHub fails to answer for a while -> waits, then main shows another digest -> superseded", rc == 0 and e.outputs().get("landed") == "false", f"rc={rc} {out}")
e.done()
e = chain_env({})
rc, out = e.run(WAIT_RUN, wait_env({"PR_NUMBER": ""}))
check("wait dev: a missing values file stops the chain", rc != 0 and "does not exist in platform-demo-helm" in parse_summary(e), f"rc={rc} {out}")
e.done()
# stage: the version is read from the merged file and must be the expected one
e = chain_env({"stage@main": vals(CD_NEW, "1.0.1"), f"stage@{MSHA}": vals(CD_NEW, "1.0.1")}, pr=MERGED)
rc, out = e.run(WAIT_RUN, wait_env({"TARGET_ENV": "stage", "EXPECT_VERSION": "1.0.1"}))
check("wait stage: landed with the expected version", rc == 0 and e.outputs().get("version") == "1.0.1" and e.outputs().get("landed") == "true"
      and "### Stage deployed: version `1.0.1`" in parse_summary(e) and "thing-v1.0.1" in parse_summary(e), f"rc={rc} {out}\n{parse_summary(e)}")
e.done()
e = chain_env({"stage@main": vals(CD_OLD, "1.0.0"), f"stage@{MSHA}": vals(CD_NEW, "1.0.7")}, pr=MERGED)
rc, out = e.run(WAIT_RUN, wait_env({"TARGET_ENV": "stage", "EXPECT_VERSION": "1.0.1"}))
check("wait stage: another version than expected stops the chain", rc != 0 and "the version is not the expected one" in parse_summary(e), f"rc={rc} {out}")
e.done()
# stage: the pull request cannot merge any more because dev moved on (the helm promotion policy) -> superseded after the wait, green
e = chain_env({"stage@main": vals(CD_OLD, "1.0.0"), "dev@main": vals(CD_NEWER)}, pr={"merged": False, "state": "open"})
rc, out = e.run(WAIT_RUN, wait_env({"TARGET_ENV": "stage", "EXPECT_VERSION": "1.0.1"}))
summ = parse_summary(e)
check("wait stage: still open at the timeout, but dev holds a newer build -> superseded (green), the open pull request is explained",
      rc == 0 and e.outputs().get("landed") == "false" and "can no longer merge" in summ and "promotion policy" in summ and not writes(e), f"rc={rc} {summ}")
e.done()
e = chain_env({"stage@main": vals(CD_OLD, "1.0.0"), "dev@main": vals(CD_NEW)}, pr={"merged": False, "state": "open"})
rc, out = e.run(WAIT_RUN, wait_env({"TARGET_ENV": "stage", "EXPECT_VERSION": "1.0.1"}))
check("wait stage: still open at the timeout and dev still holds this build -> red, the checks are the reason", rc != 0 and "not merged yet" in parse_summary(e), f"rc={rc} {parse_summary(e)}")
e.done()
# prod ends the chain
e = chain_env({"prod@main": vals(CD_NEW, "1.0.1"), f"prod@{MSHA}": vals(CD_NEW, "1.0.1")}, pr=MERGED)
rc, out = e.run(WAIT_RUN, wait_env({"TARGET_ENV": "prod", "EXPECT_VERSION": "1.0.1"}))
check("wait prod: the last line says the chain is complete", rc == 0 and "### Prod deployed: version `1.0.1`" in parse_summary(e)
      and "The chain is complete: dev, stage and prod run this build." in parse_summary(e), f"rc={rc} {out}\n{parse_summary(e)}")
e.done()
# present without a pull request: the commit of the file is used
e = chain_env({"stage@main": vals(CD_NEW, "1.0.1"), f"stage@{MSHA}": vals(CD_NEW, "1.0.1")})
rc, out = e.run(WAIT_RUN, wait_env({"TARGET_ENV": "stage", "EXPECT_VERSION": "1.0.1", "PR_NUMBER": ""}))
check("wait stage: already in stage -> sha is the commit that last changed the file", rc == 0 and e.outputs().get("sha") == MSHA and e.outputs().get("version") == "1.0.1", f"rc={rc} {e.outputs()} {out}")
e.done()
# hostile or wrong inputs run nothing
for name, over in (("digest", {"DIGEST": "sha256:xyz"}), ("app", {"APP": "x; touch pwned"}), ("pull request number", {"PR_NUMBER": "7; touch pwned"}),
                   ("minutes", {"WAIT_MINUTES": "31"}), ("minutes 0", {"WAIT_MINUTES": "0"}), ("environment", {"TARGET_ENV": "uat"}), ("expected version", {"EXPECT_VERSION": "1.0"})):
    e = chain_env({"dev@main": vals(CD_OLD)})
    work = tempfile.mkdtemp(prefix="test-inj-")
    rc, out = e.run(WAIT_RUN, wait_env(over), cwd=work)
    check(f"wait: a wrong {name} is refused before anything runs", rc != 0 and os.listdir(work) == [] and not e.calls(), f"rc={rc} {out} {os.listdir(work)}")
    shutil.rmtree(work, ignore_errors=True)
    e.done()

# ---- the newer-build check of dev (step dev-guard of Publish image) ----
GUARD = step(pub["jobs"]["publish"], "id", "dev-guard")["run"]
SHA_ = "e" * 40


def guard_run(files, compare="ahead", extra=None, **over):
    e = chain_env(files, compare=compare, **over)
    env_ = {"GH_TOKEN": "t", "APP": "thing", "TARGET_ENV": "dev", "DIGEST": CD_NEW, "GITHUB_REPOSITORY": "o/app-thing", "GITHUB_SHA": SHA_}
    env_.update(extra or {})
    rc, out = e.run(GUARD, env_)
    return e, rc, out


e, rc, out = guard_run({"dev@main": vals(CD_NEWER, tag="sha-abcdef0")}, compare="behind")
summ = parse_summary(e)
check("dev guard: dev runs an image of a NEWER commit -> stale, green, the summary says why", rc == 0 and e.outputs().get("stale") == "true"
      and summ.startswith("### Dev change skipped: superseded by a newer build\n") and "not a failure" in summ and calls_of(e, "compare/abcdef0..." + SHA_) and not writes(e), f"rc={rc} {summ}")
e.done()
for label, files, cmp_, want in (("dev runs an older commit", {"dev@main": vals(CD_OLD, tag="sha-abcdef0")}, "ahead", "false"),
                                 ("dev already has this digest (a re-run)", {"dev@main": vals(CD_NEW, tag="sha-abcdef0")}, "behind", "false"),
                                 ("dev's tag is not sha-<7> (another format)", {"dev@main": vals(CD_NEWER, tag="v1.2.3")}, "behind", "false"),
                                 ("the commits cannot be compared", {"dev@main": vals(CD_NEWER, tag="sha-abcdef0")}, "error", "false"),
                                 ("diverged history is not treated as newer", {"dev@main": vals(CD_NEWER, tag="sha-abcdef0")}, "diverged", "false"),
                                 ("dev cannot be read", {}, "behind", "false")):
    e, rc, out = guard_run(files, compare=cmp_)
    check(f"dev guard: {label} -> not stale, the run goes on", rc == 0 and e.outputs().get("stale") == want and not writes(e), f"rc={rc} {e.outputs()} {out}")
    e.done()
for name, over in (("digest", {"DIGEST": "nope"}), ("app", {"APP": "x; touch pwned"}), ("env", {"TARGET_ENV": "dev; touch pwned"})):
    e, rc, out = guard_run({"dev@main": vals(CD_OLD)}, extra=over)
    check(f"dev guard: a wrong {name} is refused before anything runs", rc != 0 and not e.calls(), f"rc={rc} {out}")
    e.done()

# ---- the approval jobs: approval check, then re-check and version ----
APPROVAL = step(pub["jobs"]["approve-stage"], "id", "approval")["run"]
DECIDE = {k: step(pub["jobs"][k], "id", "decide")["run"] for k in ("approve-stage", "approve-prod")}
check("chain: both approval jobs run the same two scripts", step(pub["jobs"]["approve-prod"], "id", "approval")["run"] == APPROVAL and DECIDE["approve-stage"] == DECIDE["approve-prod"])
DECIDE_RUN = DECIDE["approve-stage"]
for name, appr, ok in (("approved", [{"state": "approved", "user": {"login": "owner"}, "environments": [{"name": "stage"}]}], True),
                       ("nobody approved (environment without a reviewer)", [], False),
                       ("approval of another environment", [{"state": "approved", "user": {"login": "o"}, "environments": [{"name": "merge"}]}], False),
                       ("a rejection", [{"state": "rejected", "user": {"login": "o"}, "environments": [{"name": "stage"}]}], False)):
    e = chain_env({}, approvals=appr)
    rc, out = e.run(APPROVAL, {"GH_TOKEN": "t", "GH_REPO": "o/app-thing", "RUN_ID": "5", "ENVIRONMENT": "stage"})
    check(f"approval stage: {name}", (rc == 0) == ok and (not ok or e.outputs().get("by") == "owner") and (ok or "no approval" in parse_summary(e)), f"rc={rc} {out}")
    e.done()


def decide_env(files, tags=(), env_name="stage", extra=None, **over):
    e = chain_env(files, tags=list(tags), **over)
    env_ = {"GH_TOKEN": "t", "APP": "thing", "DIGEST": CD_NEW, "TAG": "sha-1234567", "TARGET_ENV": env_name, "BY": "owner"}
    env_.update(extra or {})
    return e, env_


def version_for(tags, stage_text, label, want):
    e, env_ = decide_env({"dev@main": vals(CD_NEW), "stage@main": stage_text}, tags)
    rc, out = e.run(DECIDE_RUN, env_)
    got = e.outputs().get("version")
    check(f"version: {label}", rc == 0 and got == want and e.outputs().get("go") == "true", f"rc={rc} got={got} want={want} {out}")
    e.done()


def T(*v):
    return [f"refs/tags/thing-v{x}" for x in v]


version_for([], vals(CD_OLD), "no tag at all -> 1.0.0", "1.0.0")
version_for(T("1.0.0", "1.0.1"), vals(CD_OLD), "the next patch after the highest tag", "1.0.2")
version_for(T("1.0.2", "1.0.9", "1.0.10", "1.0.3"), vals(CD_OLD), "highest by number, not by text or order (1.0.10 > 1.0.9)", "1.0.11")
version_for(T("1.2.0", "1.0.9"), vals(CD_OLD), "a higher minor is the base", "1.2.1")
version_for(["refs/tags/thing-v9.9.9-rc1", "refs/tags/thing-v8.0.0+build", "refs/tags/thing-v01.0.0", "refs/tags/thing-v2.0", "refs/tags/thing-v2.0.0.1",
             "refs/tags/thing-vlatest", "refs/tags/thing-v3.0.0/x", "refs/tags/thing-v1.0.4"], vals(CD_OLD), "pre-releases and look-alike tags are ignored", "1.0.5")
version_for(T("1.0.2"), vals(CD_OLD, "1.0.7"), "a version in the stage file whose tag is not made yet counts", "1.0.8")
version_for(T("1.0.9"), vals(CD_OLD, "1.0.2"), "the stage file's lower version does not lower it", "1.0.10")
version_for(T("1.0.0"), vals(CD_NEW, "1.0.0"), "the digest is already in stage with a version: that version, no new one", "1.0.0")
version_for(T("1.0.3"), vals(CD_NEW, "1.0.0"), "... even when later tags exist", "1.0.0")
version_for(T("1.0.3"), vals(CD_NEW), "the digest is in stage without a version: a new one is chosen", "1.0.4")
e, env_ = decide_env({"dev@main": vals(CD_NEW), "stage@main": vals(CD_OLD)}, T("1.0.1"), tags_fail=True)
rc, out = e.run(DECIDE_RUN, env_)
check("version: tags that cannot be listed stop the chain (never a guessed 1.0.0)", rc != 0 and not e.outputs().get("go") and "cannot choose the version" in parse_summary(e), f"rc={rc} {out}")
e.done()
e, env_ = decide_env({"dev@main": vals(CD_NEW), "stage@main": vals(CD_OLD)}, T("1.0.1"))
rc, out = e.run(DECIDE_RUN, env_)
o, summ = e.outputs(), parse_summary(e)
check("decide stage: outputs for the promotion, summary with old -> new, the version and who approved", rc == 0 and o == {"go": "true", "digest": CD_NEW, "tag": "sha-1234567", "version": "1.0.2"}
      and summ.startswith("### Stage approved by owner\n") and f"`{CD_OLD}`" in summ and f"`{CD_NEW}`" in summ and "`1.0.2`" in summ and not writes(e), f"rc={rc} {o}\n{summ}")
e.done()
# the supersede re-check
e, env_ = decide_env({"dev@main": vals(CD_NEWER), "stage@main": vals(CD_OLD)}, T("1.0.1"))
rc, out = e.run(DECIDE_RUN, env_)
summ = parse_summary(e)
check("decide stage: dev moved to a newer build while the approval waited -> go=false, green, clear summary, no tags read",
      rc == 0 and e.outputs().get("go") == "false" and summ.startswith("### Stage not promoted: superseded by a newer build\n") and "not a failure" in summ
      and not calls_of(e, "matching-refs"), f"rc={rc} {e.outputs()} {summ}")
e.done()
e, env_ = decide_env({"dev@main": vals(CD_NEW)}, T())
rc, out = e.run(DECIDE_RUN, env_)
check("decide stage: a missing stage file stops the chain", rc != 0 and "A promotion only changes an existing values file" in parse_summary(e), f"rc={rc} {out}")
e.done()
# prod
e, env_ = decide_env({"stage@main": vals(CD_NEW, "1.0.3"), "prod@main": vals(CD_OLD, "1.0.2")}, T("1.0.9"), env_name="prod")
rc, out = e.run(DECIDE_RUN, env_)
check("decide prod: the version is stage's, whatever the tags say", rc == 0 and e.outputs().get("version") == "1.0.3" and e.outputs().get("go") == "true"
      and not calls_of(e, "matching-refs") and "### Prod approved by owner" in parse_summary(e), f"rc={rc} {e.outputs()} {out}")
e.done()
e, env_ = decide_env({"stage@main": vals(CD_NEW), "prod@main": vals(CD_OLD, "1.0.2")}, env_name="prod")
rc, out = e.run(DECIDE_RUN, env_)
check("decide prod: stage without a version for this digest stops the chain", rc != 0 and "stage has no version for this digest" in parse_summary(e), f"rc={rc} {out}")
e.done()
e, env_ = decide_env({"stage@main": vals(CD_NEWER, "1.0.4"), "prod@main": vals(CD_OLD, "1.0.2")}, env_name="prod")
rc, out = e.run(DECIDE_RUN, env_)
check("decide prod: stage moved to a newer build -> superseded, nothing promoted", rc == 0 and e.outputs().get("go") == "false"
      and "### Prod not promoted: superseded by a newer build" in parse_summary(e), f"rc={rc} {out}")
e.done()
for name, over in (("digest", {"DIGEST": "sha256:xyz"}), ("app", {"APP": "x; touch pwned"}), ("tag", {"TAG": "a b"}), ("environment", {"TARGET_ENV": "dev"})):
    e, env_ = decide_env({"dev@main": vals(CD_NEW), "stage@main": vals(CD_OLD)}, extra=over)
    work = tempfile.mkdtemp(prefix="test-inj-")
    rc, out = e.run(DECIDE_RUN, env_, cwd=work)
    check(f"decide: a wrong {name} is refused before anything runs", rc != 0 and os.listdir(work) == [] and not e.calls(), f"rc={rc} {out}")
    shutil.rmtree(work, ignore_errors=True)
    e.done()

# ---- the release ----
REL = step(pub["jobs"]["release"], "id", "release")["run"]


def rel_env(files=None, **over):
    base_files = {f"stage@{MSHA}": vals(CD_NEW, "1.0.1")}
    base_files.update(files or {})
    e = chain_env(base_files, **over)
    return e, {"GH_TOKEN": "app", "APP": "thing", "VERSION": "1.0.1", "DIGEST": CD_NEW, "TAG": "sha-1234567", "SHA": MSHA, "CURRENT": "true",
               "SOURCE_REPO": "o/app-thing", "SOURCE_SHA": "e" * 40, "RUN_URL": "https://github.com/o/app-thing/actions/runs/5", "GITHUB_SERVER_URL": "https://github.com"}


e, env_ = rel_env()
rc, out = e.run(REL, env_)
posts = open(e.log).read().split("gh ")  # the notes have several lines: look at the raw log, one chunk per call
posts = [c for c in posts if c.startswith("api -X POST")]
check("release: creates the tag on the helm commit, then the release (not 'latest'), notes with the digest", rc == 0 and len(posts) == 2
      and f"git/refs -f ref=refs/tags/thing-v1.0.1 -f sha={MSHA}" in posts[0] and "tag_name=thing-v1.0.1" in posts[1] and "make_latest=false" in posts[1]
      and CD_NEW in posts[1] and "o/app-thing/commit/" in posts[1] and "actions/runs/5" in posts[1]
      and "### Released `thing-v1.0.1`" in parse_summary(e) and len(writes(e)) == 2, f"rc={rc} {posts} {out}")
e.done()
e, env_ = rel_env(tag_refs=["thing-v1.0.1"], releases=["thing-v1.0.1"], files={"stage@thing-v1.0.1": vals(CD_NEW, "1.0.1")})
rc, out = e.run(REL, env_)
check("release: tag and release exist for this digest -> nothing is written, nothing is moved", rc == 0 and not writes(e) and "already existed" in parse_summary(e), f"rc={rc} {e.calls()} {out}")
e.done()
e, env_ = rel_env(tag_refs=["thing-v1.0.1"], files={"stage@thing-v1.0.1": vals(CD_NEW, "1.0.1")})
rc, out = e.run(REL, env_)
posts = writes(e)
check("release: tag exists, release missing -> only the release is added (the tag is not touched)", rc == 0 and len(posts) == 1 and "/releases " in posts[0], f"rc={rc} {posts} {out}")
e.done()
e, env_ = rel_env(tag_refs=["thing-v1.0.1"], files={"stage@thing-v1.0.1": vals(CD_NEWER, "1.0.1")})
rc, out = e.run(REL, env_)
check("release: the tag exists for ANOTHER image -> fails, never moved", rc != 0 and not writes(e) and "A tag is never moved" in parse_summary(e), f"rc={rc} {out}")
e.done()
e, env_ = rel_env({f"stage@{MSHA}": vals(CD_NEW, "1.0.9")})
rc, out = e.run(REL, env_)
check("release: the commit does not hold this version -> fails before writing anything", rc != 0 and not writes(e) and "does not have version 1.0.1" in parse_summary(e), f"rc={rc} {out}")
e.done()
e, env_ = rel_env(post_ref_fail="{\"message\":\"Resource not accessible by integration\"}\ngh: Resource not accessible by integration (HTTP 403)")
rc, out = e.run(REL, env_)
summ = parse_summary(e)
check("release: a 403 on the tag says which call failed and points at the App's permission", rc != 0 and "creating the tag thing-v1.0.1 failed" in summ
      and "Contents: write" in summ and len(writes(e)) == 1, f"rc={rc} {summ}")
e.done()
# best effort: the job is continue-on-error, its outputs come from a first step that cannot fail, and a refusal is a warning
relay = step(pub["jobs"]["release"], "id", "relay")["run"]
e = Env()
rc, out = e.run(relay, {"DIGEST": CD_NEW, "TAG": "sha-1234567", "VERSION": "1.0.1", "CURRENT": "true"})
check("release: the outputs for the prod approval are written by the first step (they survive a failed release)",
      rc == 0 and e.outputs() == {"digest": CD_NEW, "tag": "sha-1234567", "version": "1.0.1", "current": "true"}, f"rc={rc} {e.outputs()}")
e.done()
check("release: the job is continue-on-error and the relay step comes first", pub["jobs"]["release"].get("continue-on-error") is True
      and pub["jobs"]["release"]["steps"][0]["id"] == "relay" and pub["jobs"]["approve-prod"]["if"] == "${{ needs.release.outputs.current == 'true' }}")
e, env_ = rel_env(post_ref_fail="gh: Resource not accessible by integration (HTTP 403)")
rc, out = e.run(REL, env_)
check("release: a refusal is a warning annotation and the summary says the prod approval is not blocked", rc != 0 and "::warning title=Release not made::" in out
      and "::error::" not in out and "the prod approval is not blocked" in parse_summary(e) and "Re-run failed jobs" in parse_summary(e), f"rc={rc} {out}")
e.done()
e, env_ = rel_env(post_ref_fail="gh: Repository rule violations found\nCannot create ref due to creations being restricted. (HTTP 422)")
rc, out = e.run(REL, env_)
check("release: a ruleset refusal is told apart and names the bypass list", rc != 0 and "bypass list" in parse_summary(e), f"rc={rc} {parse_summary(e)}")
e.done()
e, env_ = rel_env(post_release_fail="gh: Resource not accessible by integration (HTTP 403)")
rc, out = e.run(REL, env_)
check("release: the tag is made, then the release fails -> red (a re-run only adds the release)", rc != 0 and "creating the release thing-v1.0.1 failed" in parse_summary(e), f"rc={rc} {parse_summary(e)}")
e.done()
e, env_ = rel_env(post_ref_fail="gh: Reference already exists (HTTP 422)", files={"stage@thing-v1.0.1": vals(CD_NEW, "1.0.1")})
rc, out = e.run(REL, env_)
check("release: a second run made the tag a moment ago (same image) -> goes on", rc == 0 and "made a moment ago" in parse_summary(e), f"rc={rc} {out}")
e.done()
for name, over in (("version", {"VERSION": "1.0"}), ("app", {"APP": "x; touch pwned"}), ("digest", {"DIGEST": "nope"})):
    e, env_ = rel_env()
    env_.update(over)
    work = tempfile.mkdtemp(prefix="test-inj-")
    rc, out = e.run(REL, env_, cwd=work)
    check(f"release: a wrong {name} is refused before anything runs", rc != 0 and os.listdir(work) == [] and not writes(e), f"rc={rc} {out}")
    shutil.rmtree(work, ignore_errors=True)
    e.done()
e, env_ = rel_env()
env_["SHA"] = "zz"
rc, out = e.run(REL, env_)
check("release: no commit to tag -> fails", rc != 0 and not writes(e), f"rc={rc} {out}")
e.done()

# ---- the values-file helpers are the helm repository's own parsing ----
lib_env = chain_env({})
lib = WAIT_RUN.split("title=${TARGET_ENV^}")[0]
probe = lib + r"""
printf 'image:\n  tag: "sha-abcdef0"\n  digest: "sha256:%s"   # v2.10.3\nother:\n  digest: sha256:%s\n' "$(printf 'a%.0s' {1..64})" "$(printf 'f%.0s' {1..64})" > "$RUNNER_TEMP/v.yaml"
echo "digest=$(img_digest < "$RUNNER_TEMP/v.yaml")"
echo "version=$(img_version < "$RUNNER_TEMP/v.yaml")"
echo "tag=$(img_get tag < "$RUNNER_TEMP/v.yaml")"
other=$(printf 'image:\n  digest: sha256:%s\n  # v1.2.3\n' "$(printf 'b%.0s' {1..64})" | img_version)
echo "other=${other:-none}"
"""
rc, out = lib_env.run(probe, {"APP": "thing", "GH_TOKEN": "x"})
check("helpers: image.digest, quotes, the version comment and the tag are read like promotion-policy.sh does (only the image: block)",
      rc == 0 and f"digest=sha256:{'a' * 64}\n" in out and "version=2.10.3" in out and "tag=sha-abcdef0" in out and out.rstrip().endswith("other=none"), f"rc={rc} {out}")
lib_env.done()

if failures:
    print("\n\n".join(failures))
    print(f"\n{len(failures)} of {count} shell scenarios failed")
    sys.exit(1)
print(f"shell: {count} scenarios ok")
