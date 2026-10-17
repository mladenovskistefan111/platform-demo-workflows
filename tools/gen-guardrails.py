#!/usr/bin/env python3
"""Write CODEOWNERS, dependabot.yml and .pre-commit-config.yaml. usage: gen_guardrails.py <repo-dir> <eco[,eco...]>
eco: gomod npm pip gradle nuget docker docker-compose (github-actions is always added)"""
import os, sys

repo, ecos = sys.argv[1], sys.argv[2].split(",")
gh = os.path.join(repo, ".github"); os.makedirs(gh, exist_ok=True)

open(os.path.join(gh, "CODEOWNERS"), "w").write("* @mladenovskistefan111\n")

def block(eco, directory=None, directories=None):
    d = f"    directory: {directory}\n" if directory else "    directories:\n" + "".join(f'      - "{x}"\n' for x in directories)
    return f"""  - package-ecosystem: {eco}
{d}    schedule:
      interval: weekly
    cooldown:
      default-days: 7 # let a new release settle before it is proposed
    open-pull-requests-limit: 5
    commit-message:
      prefix: "chore(deps)"
    groups:
      minor-and-patch:
        update-types: [minor, patch]
"""

out = "version: 2\nupdates:\n"
for e in ecos:
    if e == "nuget":
        out += block("nuget", directories=["/src", "/tests"])
    elif e:
        out += block(e, directory="/")
out += block("github-actions", directory="/")
open(os.path.join(gh, "dependabot.yml"), "w").write(out)

open(os.path.join(repo, ".pre-commit-config.yaml"), "w").write("""# Local secret blocking. One-time setup per clone:  pipx install pre-commit && pre-commit install
# - every commit: gitleaks scans the staged changes, plus private-key / large-file checks
# - every push:   gitleaks scans the commits being pushed before anything leaves the machine
# This is a fast local guard (git push --no-verify skips it); GitHub push protection and the
# CI secret scan are the enforcing layers.
default_install_hook_types: [pre-commit, pre-push]

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: 3e8a8703264a2f4a69428a0aa4dcb512790b2c8c # frozen: v6.0.0
    hooks:
      - id: detect-private-key
        stages: [pre-commit]
      - id: check-added-large-files
        stages: [pre-commit]

  - repo: https://github.com/gitleaks/gitleaks
    rev: 83d9cd684c87d95d656c1458ef04895a7f1cbd8e # frozen: v8.30.1
    hooks:
      - id: gitleaks
        stages: [pre-commit]

  - repo: local
    hooks:
      - id: gitleaks-push
        name: gitleaks (commits being pushed)
        language: golang
        additional_dependencies: [github.com/zricethezav/gitleaks/v8@v8.30.1]
        # pre-commit exports the pushed range; without it (e.g. a brand-new branch) the whole history is scanned
        entry: bash -c 'range=""; if [ -n "${PRE_COMMIT_FROM_REF:-}" ] && [ -n "${PRE_COMMIT_TO_REF:-}" ]; then range="${PRE_COMMIT_FROM_REF}..${PRE_COMMIT_TO_REF}"; fi; gitleaks git --redact --verbose ${range:+--log-opts="$range"}' --
        pass_filenames: false
        always_run: true
        stages: [pre-push]
""")
print("guardrails ->", repo, ecos)
