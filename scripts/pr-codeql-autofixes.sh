#!/usr/bin/env bash
# pr-codeql-autofixes.sh -- surface GHAS code-scanning (CodeQL) alerts on a PR
# and any Copilot Autofix suggestions, so they get examined before dismiss/merge.
#
# WHY THIS EXISTS: pr-unreplied-comments.sh / pr-read-comments.sh read the
# review/issue *comments* API. Code-scanning alerts and their Copilot Autofix
# live in a SEPARATE API surface (repos/.../code-scanning/alerts[/{n}/autofix]),
# so the comment scripts structurally cannot see them. A CodeQL alert (and its
# suggested fix) otherwise sails right past the review-surfacing flow. This
# closes that blind spot.
#
# Usage: pr-codeql-autofixes.sh <pr_number> [repo] [--generate]
#   <pr_number>  required
#   [repo]       owner/name; auto-detected via gh if omitted or ""
#   --generate   also POST to request Copilot Autofix generation for each open
#                alert that has none yet (best-effort; GitHub only generates for
#                alerts open on the default ref, so this may 422 for PR-only ones)
#
# Exit codes: 0 = ran (with or without alerts); 2 = setup error.
#
# Notes:
# - Reads code-scanning, so the gh token needs the `security_events` scope.
#   If absent, the alerts query fails and the script says so (does not crash).
# - The autofix REST endpoint reliably returns suggestions for alerts on the
#   default branch. For PR-introduced alerts the suggestion is sometimes only in
#   the Files-changed UI; when the API has none, the script points at the alert
#   URL so the human can view/commit it there.

set -uo pipefail

pr=""; repo=""; generate=0
for a in "$@"; do
  case "$a" in
    --generate) generate=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) if [ -z "$pr" ]; then pr="$a"; elif [ -z "$repo" ]; then repo="$a"; fi ;;
  esac
done

[ -z "$pr" ] && { echo "usage: pr-codeql-autofixes.sh <pr_number> [repo] [--generate]" >&2; exit 2; }
if [ -z "$repo" ]; then
  repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)
fi
[ -z "$repo" ] && { echo "setup error: could not resolve repo (pass it as arg 2)" >&2; exit 2; }

ref="refs/pull/${pr}/merge"

# Numbers of open code-scanning alerts on the PR merge ref. --paginate walks
# every page so a PR with >100 open alerts is fully enumerated (the per_page=100
# cap alone would otherwise silently truncate the list).
nums=$(gh api --paginate "repos/$repo/code-scanning/alerts?ref=${ref}&state=open&per_page=100" \
        --jq '.[].number' 2>/dev/null)
api_rc=$?

if [ $api_rc -ne 0 ]; then
  echo "pr-codeql-autofixes: could not read code-scanning alerts for $repo #$pr."
  echo "  (code scanning may be disabled, or the gh token lacks the"
  echo "   'security_events' scope: gh auth refresh -s security_events)"
  exit 0
fi

if [ -z "$nums" ]; then
  echo "pr-codeql-autofixes: 0 open code-scanning alerts on $ref -- clean."
  exit 0
fi

n_count=$(printf '%s\n' "$nums" | grep -c .)
echo "pr-codeql-autofixes: $n_count open code-scanning alert(s) on $ref"
echo "  (these are NOT visible to the review-comment scripts -- examine before dismiss/merge)"
echo

for n in $nums; do
  # One fetch for the alert's display fields.
  line=$(gh api "repos/$repo/code-scanning/alerts/$n" --jq \
    '"#\(.number) [\(.rule.security_severity_level // .rule.severity)] \(.tool.name):\(.rule.id)\n  \(.most_recent_instance.location.path):\(.most_recent_instance.location.start_line)\n  \(.most_recent_instance.message.text | split("\n")[0])\n  \(.html_url)"' \
    2>/dev/null)
  echo "$line"

  # Optionally request generation first (best-effort; ignore failures).
  if [ "$generate" -eq 1 ]; then
    gh api -X POST "repos/$repo/code-scanning/alerts/$n/autofix" >/dev/null 2>&1 || true
  fi

  # Try to retrieve a Copilot Autofix suggestion.
  af_status=$(gh api "repos/$repo/code-scanning/alerts/$n/autofix" --jq '.status' 2>/dev/null)
  if [ "$af_status" = "success" ]; then
    af_desc=$(gh api "repos/$repo/code-scanning/alerts/$n/autofix" --jq '.description // ""' 2>/dev/null)
    echo "  AUTOFIX (Copilot): available -- ${af_desc:-<no description>}"
    echo "    review/commit it on the alert page above, or apply the equivalent edit yourself"
  elif [ "$af_status" = "pending" ]; then
    echo "  AUTOFIX (Copilot): generating -- re-run shortly, or open the alert page"
  else
    echo "  AUTOFIX (Copilot): none via API -- check the alert page (Files-changed may show one)"
    [ "$generate" -eq 0 ] && echo "    (re-run with --generate to request one)"
  fi
  echo
done

echo "Triage each: real -> fix (or apply the autofix); false positive -> dismiss via"
echo "  gh api -X PATCH repos/$repo/code-scanning/alerts/{n} -f state=dismissed \\"
echo "    -f dismissed_reason='false positive' -f dismissed_comment='...'"
