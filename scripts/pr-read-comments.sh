#!/bin/bash
# pr-read-comments.sh -- Read full bodies of PR review comments
#
# Usage:
#   pr-read-comments.sh [--reviews] [--issue] <pr> [owner/repo] [comment-id...]
#
# The optional [owner/repo] positional targets a repo other than the cwd's.
# An argument containing '/' is interpreted as a repo slug; otherwise the
# positionals after <pr> are treated as numeric comment IDs (so the legacy
# `pr-read-comments.sh <pr> <comment-id...>` form keeps working).
#
# Modes (combinable):
#   (default)  Inline review comments (diff comments)
#   --reviews  Review-body comments ONLY (summary comments on reviews)
#
# DEFAULT (no mode flag): ALL THREE surfaces - inline + review-body + issue-level - i.e. exactly
# what pr-unreplied-comments.sh COUNTS for the gate. A reader that shows less than the gate counts
# lets an agent conclude "no findings" and then be blocked by one it never saw (#289).
#   --issue    Issue-level PR comments (general conversation)
#
# With no IDs: prints all unreplied bot comments of the selected type(s).
# With IDs:    prints only those specific comment IDs (inline only).
#
# Addressed-state filtering: only the default (inline) mode filters out comments
# that the current user has already replied to. The --reviews and --issue modes
# surface ALL matching bot comments with no addressed-state filter (review-body
# and issue comments have no reply-threading to key an "addressed" check off
# of), so consumers use judgment to decide which still need action.
#
# Intended as a complement to pr-unreplied-comments.sh -- use that to
# get the list of IDs, then this to read the full bodies of specific ones.
set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

if ! command -v gh &>/dev/null; then
  echo "Error: gh (GitHub CLI) is required but not installed."
  exit 1
fi

mode_reviews=false
mode_issue=false
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --reviews) mode_reviews=true; shift ;;
    --issue)   mode_issue=true; shift ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

if [ "${#}" -lt 1 ]; then
  echo "Usage: $0 [--reviews] [--issue] <pr> [owner/repo] [comment-id...]"
  exit 1
fi

pr="$1"
shift
# Optional [owner/repo] positional: an argument containing '/' is a repo slug,
# never a numeric comment ID (pattern-based disambiguation). This keeps the
# legacy `<pr> <comment-id...>` form working while accepting the documented
# `<pr> <owner/repo> [comment-id...]` form.
if [[ "${1:-}" == */* ]]; then
  repo="$1"
  shift
else
  repo=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null) || {
    echo "Error: could not determine repository. Run from inside a git repo with a GitHub remote."
    exit 1
  }
fi
me=$(gh api user --jq .login 2>/dev/null) || { echo "Error: could not determine current GitHub user (run 'gh auth status' or re-auth)" >&2; exit 1; }

BOT_LOGIN_FILTER='(
  .user.login == "coderabbitai[bot]" or
  .user.login == "Copilot" or
  .user.login == "copilot-pull-request-reviewer[bot]" or
  .user.login == "greptile-apps[bot]" or
  .user.login == "codoki-pr-intelligence[bot]" or
  .user.login == "github-advanced-security[bot]" or
  .user.login == "github-actions[bot]"
)'

FMT='"---\nID:   \(.id)\nFile: \(.path // "n/a"):\(.original_line // "?")\nBy:   \(.user.login)\n\n\(.body)\n"'

# Specific IDs: inline-only fast path
if [ "${#}" -gt 0 ]; then
  ids_json=$(printf '%s\n' "$@" | jq -R 'tonumber' | jq -s '.')
  gh api "repos/$repo/pulls/$pr/comments" --paginate | \
    jq --argjson ids "$ids_json" \
      "[.[] | select(.id as \$id | \$ids | any(. == \$id))] | sort_by(.original_line) | .[] | $FMT" -r
  exit 0
fi

# No IDs: print unreplied bot comments of selected type(s).
#
# THE READER MUST SHOW AT LEAST WHAT THE GATE COUNTS (#289; maintainer-flagged on PR #290).
# The DEFAULT used to be inline-ONLY, while pr-unreplied-comments.sh COUNTS inline + review-body
# + issue-level findings. So the two helpers disagreed in the dangerous direction: an agent ran
# the reader, saw nothing, concluded "no findings" - and was then BLOCKED by a review-body
# finding it was never shown. That is the #251 CR-nitpick miss, structurally guaranteed. (SKILL.md
# even DOCUMENTED the default as reading "comment + review BODIES", which it did not.)
#
# The default now covers every surface the gate gates on. An explicit flag still NARROWS to one
# surface, so `--issue` / `--reviews` keep their exact meaning for a caller that wants only that.
if [ "$mode_reviews" = false ] && [ "$mode_issue" = false ]; then
  mode_inline=true
  mode_reviews=true
  mode_issue=true
else
  mode_inline=false
fi

found=0

# Inline review comments
if [ "$mode_inline" = true ]; then
  all=$(gh api "repos/$repo/pulls/$pr/comments" --paginate)
  bot_ids=$(echo "$all" | jq "[.[] | select($BOT_LOGIN_FILTER and .in_reply_to_id == null) | .id]")
  replied=$(echo "$all" | jq --arg me "$me" \
    '[.[] | select(.user.login == $me and .in_reply_to_id != null) | .in_reply_to_id]')
  unreplied=$(jq -n --argjson b "$bot_ids" --argjson r "$replied" \
    '[$b[] | . as $id | if ($r | any(. == $id)) then empty else $id end]')
  count=$(echo "$unreplied" | jq 'length')
  if [ "$count" -gt 0 ]; then
    echo "=== Inline review comments ($count) ==="
    echo "$all" | jq --argjson ids "$unreplied" \
      "[.[] | select(.id as \$id | \$ids | any(. == \$id))] | sort_by(.original_line) | .[] | $FMT" -r
    found=$((found + count))
  fi
fi

# Review-body comments
if [ "$mode_reviews" = true ]; then
  reviews=$(gh api "repos/$repo/pulls/$pr/reviews" --paginate | jq "[.[] | select(
    .body != \"\" and .body != null and
    $BOT_LOGIN_FILTER and
    (.body | test(\"Outside diff range|Potential issue|Refactor suggestion|Actionable comments posted|Nitpick|CAUTION|Duplicate comments|<img alt=.P[0-9].|greptile\"; \"i\")) and
    (.body | test(\"^## Pull request overview\"; \"\") | not)
  ) | {id, path: \"(review body)\", original_line: null, user, body}]")
  count=$(echo "$reviews" | jq 'length')
  if [ "$count" -gt 0 ]; then
    echo "=== Review-body comments ($count) ==="
    echo "$reviews" | jq --arg fmt "$FMT" '.[] | "---\nID:   \(.id)\nFile: (review body)\nBy:   \(.user.login)\n\n\(.body)\n"' -r
    found=$((found + count))
  fi
fi

# Issue-level comments
if [ "$mode_issue" = true ]; then
  issue=$(gh api "repos/$repo/issues/$pr/comments" --paginate | jq --arg me "$me" "[.[] | select(
    $BOT_LOGIN_FILTER and
    (.body | test(\"auto-generated\"; \"i\") | not) and
    (.body | test(\"^\\\\s*\$\") | not)
  ) | {id, path: \"(issue comment)\", original_line: null, user, body}]")
  count=$(echo "$issue" | jq 'length')
  if [ "$count" -gt 0 ]; then
    echo "=== Issue-level comments ($count) ==="
    echo "$issue" | jq '.[] | "---\nID:   \(.id)\nFile: (issue comment)\nBy:   \(.user.login)\n\n\(.body)\n"' -r
    found=$((found + count))
  fi
fi

if [ "$found" -eq 0 ]; then
  echo "No unreplied bot comments on PR #$pr."
fi
