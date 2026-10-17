#!/usr/bin/env bash
# usage: contexts.sh repo pr  -> JSON array of required check names (passing, non-skipped, excluding noise)
gh pr view "$2" -R "mladenovskistefan111/$1" --json statusCheckRollup -q '[.statusCheckRollup[] | select(.__typename=="CheckRun" and .conclusion=="SUCCESS") | .name] | unique' \
 | python3 -c "import sys,json; n=json.load(sys.stdin); print(json.dumps(sorted(x for x in n if x not in ('Unit Test Results','dependabot.yml') and not x.lower().startswith('dependabot'))))"
