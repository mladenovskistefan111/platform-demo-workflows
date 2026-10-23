#!/usr/bin/env bash
# Read-only drift check of the repository settings that tools/protect-repo.sh and tools/transfer-repo.sh apply.
#
# usage: tools/check-repo-settings.sh [-o ORG] [-x REPO]... [REPO...]
#   -o ORG    organisation (default: $OWNER or platform-boutique)
#   -x REPO   leave this repository out (repeatable; also: SETTINGS_EXCLUDE="repo1 repo2")
#   REPO...   check only these repositories (default: every non-archived repository of the organisation)
#
# Per repository it checks:
#   secret scanning, push protection                     (repo security_and_analysis)
#   branch protection on the default branch: pull request required, required status checks present, enforce_admins, linear history
#   squash-only merges (merge commits and rebase merges off), allow_auto_merge
#   an active tag ruleset that blocks deleting and moving refs/tags/v*   (what makes release tags immutable)
# It prints a table (ok / NO / ?) and exits 0 = no drift, 1 = drift, 2 = nothing is wrong but something could not be read ("?").
# It never writes: only GET calls.
#
# Needs a token that can read repository ADMINISTRATION settings, otherwise security_and_analysis is hidden and branch protection answers 403:
#   - the logged-in `gh` (classic PAT with `repo`; `admin:org` is not needed to read), or
#   - in a workflow: a secret holding a fine-grained PAT / GitHub App token on the organisation with "Administration: read" and
#     "Metadata: read" for the repositories (the workflow's own GITHUB_TOKEN cannot read these settings and cannot see other repositories'),
#     exported as GH_TOKEN. In GitHub Actions it also writes the table to the job summary and emits ::error:: lines for drift.
set -uo pipefail

org=${OWNER:-platform-boutique}
exclude=" ${SETTINGS_EXCLUDE:-} "
while getopts "o:x:h" opt; do
  case $opt in
    o) org=$OPTARG ;;
    x) exclude="$exclude$OPTARG " ;;
    *) sed -n '2,/^set -uo/p' "$0" | sed '$d;s/^# \{0,1\}//'; exit 64 ;;
  esac
done
shift $((OPTIND - 1))

for tool in gh jq; do command -v "$tool" > /dev/null || { echo "$tool is required" >&2; exit 64; }; done

if [ $# -gt 0 ]; then
  repos=("$@")
else
  mapfile -t repos < <(gh api --paginate "orgs/${org}/repos?per_page=100&type=all" -q '.[] | select(.archived|not) | .name' | sort) || true
  [ ${#repos[@]} -gt 0 ] || { echo "no repositories listed for ${org} (is the token allowed to see the organisation?)" >&2; exit 2; }
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# yn <jq filter> <file>: "ok" when the filter is true, "NO" when false, "?" when the value is not there
yn() { jq -r "(${1}) as \$v | if \$v == true then \"ok\" elif \$v == false then \"NO\" else \"?\" end" "$2" 2> /dev/null || echo "?"; }

header=$(printf '%-40s %-6s %-6s %-6s %-6s %-6s %-6s %-6s %-6s %-6s' repository scan push pr checks admins linear squash auto tags)
rows=()
drift=0
unknown=0
drift_lines=()

for r in "${repos[@]}"; do
  case "$exclude" in *" $r "*) continue ;; esac

  if ! gh api "repos/${org}/${r}" > "$tmp/repo.json" 2> "$tmp/err"; then
    rows+=("$(printf '%-40s could not read the repository: %s' "$r" "$(head -c 80 "$tmp/err")")")
    unknown=$((unknown + 1)); continue
  fi
  branch=$(jq -r .default_branch "$tmp/repo.json")

  scan=$(yn '.security_and_analysis.secret_scanning.status == "enabled"' "$tmp/repo.json")
  push=$(yn '.security_and_analysis.secret_scanning_push_protection.status == "enabled"' "$tmp/repo.json")
  squash=$(yn '.allow_squash_merge == true and .allow_merge_commit == false and .allow_rebase_merge == false' "$tmp/repo.json")
  auto=$(yn '.allow_auto_merge' "$tmp/repo.json")

  pr="?"; checks="?"; admins="?"; linear="?"
  if gh api "repos/${org}/${r}/branches/${branch}/protection" > "$tmp/prot.json" 2> "$tmp/err"; then
    pr=$(yn '.required_pull_request_reviews != null' "$tmp/prot.json")
    checks=$(yn '((.required_status_checks.contexts // []) + ((.required_status_checks.checks // []) | map(.context))) | length > 0' "$tmp/prot.json")
    admins=$(yn '.enforce_admins.enabled' "$tmp/prot.json")
    linear=$(yn '.required_linear_history.enabled' "$tmp/prot.json")
  elif grep -qi 'not protected\|404' "$tmp/err" "$tmp/prot.json" 2> /dev/null; then
    pr=NO; checks=NO; admins=NO; linear=NO
  fi

  tags="?"
  if gh api "repos/${org}/${r}/rulesets" > "$tmp/rs.json" 2> "$tmp/err"; then
    tags=NO
    for id in $(jq -r '.[] | select(.target == "tag" and .enforcement == "active") | .id' "$tmp/rs.json"); do
      if gh api "repos/${org}/${r}/rulesets/${id}" > "$tmp/rule.json" 2> /dev/null &&
         [ "$(jq -r '([.rules[].type]) as $t
                     | ([.conditions.ref_name.include[]? | select(. == "refs/tags/v*" or . == "~ALL")] | length > 0)
                       and ($t | index("deletion") != null) and ($t | index("update") != null)' "$tmp/rule.json")" = true ]; then
        tags=ok; break
      fi
    done
  fi

  rows+=("$(printf '%-40s %-6s %-6s %-6s %-6s %-6s %-6s %-6s %-6s %-6s' "$r" "$scan" "$push" "$pr" "$checks" "$admins" "$linear" "$squash" "$auto" "$tags")")
  bad=()
  names=(secret-scanning push-protection pr-required required-checks enforce-admins linear-history squash-only auto-merge tag-ruleset)
  vals=("$scan" "$push" "$pr" "$checks" "$admins" "$linear" "$squash" "$auto" "$tags")
  for i in "${!vals[@]}"; do
    case "${vals[$i]}" in
      NO) bad+=("${names[$i]}") ;;
      '?') unknown=$((unknown + 1)) ;;
    esac
  done
  if [ ${#bad[@]} -gt 0 ]; then
    drift=$((drift + 1))
    drift_lines+=("${r}: ${bad[*]}")
  fi
done

echo "$header"
printf '%s\n' "${rows[@]}"
echo
if [ ${#drift_lines[@]} -gt 0 ]; then
  echo "Drift in ${drift} repositor$([ "$drift" = 1 ] && echo y || echo ies):"
  printf '  %s\n' "${drift_lines[@]}"
fi
[ "$unknown" -eq 0 ] || echo "${unknown} value(s) shown as ? could not be read (token without administration read access?)"
[ "$drift" -gt 0 ] || [ "$unknown" -gt 0 ] || echo "No drift: ${#repos[@]} repositories checked."

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Repository settings"
    echo '```'
    echo "$header"
    printf '%s\n' "${rows[@]}"
    echo '```'
    [ ${#drift_lines[@]} -eq 0 ] || { echo; printf -- '- %s\n' "${drift_lines[@]}"; }
  } >> "$GITHUB_STEP_SUMMARY"
fi
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  for l in "${drift_lines[@]}"; do echo "::error::settings drift: $l"; done
fi

[ "$drift" -eq 0 ] || exit 1
[ "$unknown" -eq 0 ] || exit 2
exit 0
