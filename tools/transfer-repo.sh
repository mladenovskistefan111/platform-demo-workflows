#!/usr/bin/env bash
# usage: transfer-repo.sh <repo> [new-owner]    (env OWNER = current owner, default mladenovskistefan111)
# Transfers a repository to the organisation and re-applies what a transfer resets, then prints what survived.
# Learned on 2026-10-17 moving the 15 platform-demo repositories to platform-boutique:
#   survives:  branch protection, tag ruleset, topics, squash-only settings, Actions secrets (DOCKERHUB_*), tags, open PRs, the old URL redirects
#   resets:    secret scanning + push protection (re-enabled here), Dependabot's stored "ignore this major version" choices
#              (encode ignores in .github/dependabot.yml instead), local clones keep working via the redirect but should be repointed
#   needs:     a token with `repo` + `admin:org`; the organisation must allow the GitHub CLI (Third-party access)
set -uo pipefail
r=${1:?usage: transfer-repo.sh <repo> [new-owner]}; N=${2:-platform-boutique}; O=${OWNER:-mladenovskistefan111}
before=$(gh secret list -R "$O/$r" 2>/dev/null | awk '{print $1}' | sort | tr '\n' ',')
gh api -X POST "repos/$O/$r/transfer" -f new_owner="$N" -q .full_name >/dev/null 2>&1 || { echo "$r: TRANSFER FAILED"; exit 1; }
for _ in $(seq 1 20); do gh api "repos/$N/$r" -q .full_name >/dev/null 2>&1 && break; sleep 3; done
patch='{"security_and_analysis":{"secret_scanning":{"status":"enabled"},"secret_scanning_push_protection":{"status":"enabled"},"secret_scanning_non_provider_patterns":{"status":"enabled"}}}'
for _ in 1 2 3 4 5 6; do   # the settings need a moment to settle after a transfer: apply, check, repeat
  gh api -X PATCH "repos/$N/$r" --input - >/dev/null 2>&1 <<<"$patch"
  sleep 6
  [ "$(gh api "repos/$N/$r" -q '"\(.security_and_analysis.secret_scanning.status)/\(.security_and_analysis.secret_scanning_push_protection.status)"')" = "enabled/enabled" ] && break
done
gh api -X PUT "repos/$N/$r/vulnerability-alerts" >/dev/null 2>&1
gh api -X PUT "repos/$N/$r/automated-security-fixes" >/dev/null 2>&1
after=$(gh secret list -R "$N/$r" 2>/dev/null | awk '{print $1}' | sort | tr '\n' ',')
echo "$r: moved | $(gh api "repos/$N/$r" -q '"scan=\(.security_and_analysis.secret_scanning.status) push=\(.security_and_analysis.secret_scanning_push_protection.status)"') | $(gh api "repos/$N/$r/branches/main/protection" -q '"admins=\(.enforce_admins.enabled) checks=\(.required_status_checks.contexts|length)"' 2>&1) | secrets before=[$before] after=[$after]"
echo "next: git remote set-url origin https://github.com/$N/$r.git (local clone); update workflow callers/Argo repoURL/signature regexps if they name the old owner"
