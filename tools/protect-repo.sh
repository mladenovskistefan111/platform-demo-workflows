#!/usr/bin/env bash
# usage: protect.sh repo '<json array of contexts>'
set -uo pipefail
r=$1; ctx=$2; O=${OWNER:-platform-boutique}   # GitHub owner of the repositories (the organisation)
api(){ gh api "$@" >/dev/null 2>"$tmp" || { echo "   ! $* :: $(head -c 200 "$tmp")"; return 1; }; }
tmp=$(mktemp)
# repo settings: squash only, auto-delete branches
api -X PATCH repos/$O/$r -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false -F delete_branch_on_merge=true -F allow_auto_merge=true -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY && echo "   settings ok"
# security: secret scanning + push protection (server-side, cannot be skipped with --no-verify), Dependabot alerts + security updates
api -X PATCH repos/$O/$r --input - <<'JSON' && echo "   secret scanning + push protection ok"
{"security_and_analysis":{"secret_scanning":{"status":"enabled"},"secret_scanning_push_protection":{"status":"enabled"},"secret_scanning_non_provider_patterns":{"status":"enabled"}}}
JSON
api -X PUT repos/$O/$r/vulnerability-alerts && echo "   dependabot alerts ok"
api -X PUT repos/$O/$r/automated-security-fixes && echo "   dependabot security updates ok"
# release tags are immutable: no delete, no move
existing=$(gh api repos/$O/$r/rulesets -q '.[]|select(.name=="protect-release-tags")|.id' 2>/dev/null)
if [ -z "$existing" ]; then
  api -X POST repos/$O/$r/rulesets --input - <<'JSON' && echo "   tag ruleset ok"
{"name":"protect-release-tags","target":"tag","enforcement":"active","conditions":{"ref_name":{"include":["refs/tags/v*"],"exclude":[]}},"rules":[{"type":"deletion"},{"type":"update"}]}
JSON
else echo "   tag ruleset already present"; fi
# branch protection on main
python3 - "$ctx" <<'PY' > "$tmp.body"
import sys, json
print(json.dumps({
  "required_status_checks": {"strict": False, "contexts": json.loads(sys.argv[1])},
  "enforce_admins": True,
  "required_pull_request_reviews": {"required_approving_review_count": 0, "dismiss_stale_reviews": True, "require_code_owner_reviews": False},
  "restrictions": None,
  "required_linear_history": True,
  "allow_force_pushes": False,
  "allow_deletions": False,
  "required_conversation_resolution": True,
}))
PY
api -X PUT repos/$O/$r/branches/main/protection --input "$tmp.body" && echo "   branch protection ok"
rm -f "$tmp" "$tmp.body"
