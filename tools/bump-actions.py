#!/usr/bin/env python3
"""Move every SHA-pinned `uses: owner/repo@<sha> # vX.Y.Z` in the given files to the newest release that is at least
--min-age days old (supply-chain cooldown), keeping the `# vX.Y.Z` comment truthful.

usage: tools/bump-actions.py [--min-age 7] [--hold owner/repo=v5 ...] [--dry-run] FILE...
  --hold  keep an action inside a major, e.g. --hold gradle/actions=v5 (v6 changed the licence of its caching component)

Needs the GitHub CLI (`gh`) logged in. Prints one line per action; rewrites files in place unless --dry-run.
Reusable workflows from this repository (platform-boutique/platform-demo-workflows, formerly mladenovskistefan111/...) are ignored: bump those with a release.
"""
import argparse, datetime, json, re, subprocess, sys

ap = argparse.ArgumentParser()
ap.add_argument("files", nargs="+")
ap.add_argument("--min-age", type=int, default=7)
ap.add_argument("--hold", action="append", default=[])
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()
hold = dict(h.split("=", 1) for h in args.hold)
cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=args.min_age)
pat = re.compile(r"((?:[\w.-]+)/(?:[\w.-]+))((?:/[\w./-]+)?)@([0-9a-f]{40})( # [^\n]*)")
cache = {}


def gh(*a):
    return subprocess.check_output(["gh", "api", *a], text=True)


def newest(repo):
    if repo in cache:
        return cache[repo]
    prefix = hold.get(repo)
    for r in json.loads(gh(f"repos/{repo}/releases?per_page=60")):
        if r["prerelease"] or r["draft"]:
            continue
        if prefix and not r["tag_name"].startswith(prefix):
            continue
        if datetime.datetime.strptime(r["published_at"][:10], "%Y-%m-%d") > cutoff:
            continue
        sha = gh(f"repos/{repo}/commits/{r['tag_name']}", "-q", ".sha").strip()
        cache[repo] = (r["tag_name"], sha, r["published_at"][:10])
        return cache[repo]
    cache[repo] = None
    return None


for path in args.files:
    text = open(path).read()
    changed = 0

    def sub(m):
        global changed
        repo = m.group(1)
        if repo in ("platform-boutique/platform-demo-workflows", "mladenovskistefan111/platform-demo-workflows"):
            return m.group(0)
        n = newest(repo)
        if not n:
            return m.group(0)
        new = f"{repo}{m.group(2)}@{n[1]} # {n[0]}"
        if new != m.group(0):
            changed += 1
        return new

    out = pat.sub(sub, text)
    if out != text:
        print(f"{path}: {changed} pin(s) moved")
        if not args.dry_run:
            open(path, "w").write(out)
for repo, n in sorted(cache.items()):
    print(f"  {repo:36s} -> {n[0] if n else 'no eligible release'}  {n[2] if n else ''}")
