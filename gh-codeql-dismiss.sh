#!/usr/bin/env bash
# gh-codeql-dismiss.sh <alert-number> [reason] [comment]  (issue #24, P3-F)
#
# Dedicated wrapper for the ONE legitimate CodeQL mutation: dismiss a code-scanning alert.
# Construction guarantee: the endpoint is built from a VALIDATED NUMERIC alert id, so it can
# only ever PATCH `code-scanning/alerts/<n>` - it cannot be coerced into a merge-by-API or
# any other mutation. `reason` is validated against GitHub's fixed enum.
#
# Canonical source: cc-orchestrator repo root; deployed by symlink into ~/.claude/scripts/.
set -euo pipefail

alert="${1:-}"
if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
  echo "usage: gh-codeql-dismiss.sh <alert-number> [reason] [comment]" >&2
  exit 2
fi
# GitHub's dismissed_reason enum includes "won<apostrophe>t fix" (a literal apostrophe);
# build it via a variable so no bare apostrophe sits in an ambiguous parser position.
apos="'"
wont_fix="won${apos}t fix"
reason="${2:-$wont_fix}"
comment="${3:-Dismissed via gh-codeql-dismiss.sh}"

# Whole-string numeric check via a bash `case` glob (no external tool): rejects empty and
# any non-digit, INCLUDING an embedded newline (newline is not 0-9, so it falls in the
# [!0-9] class). This is grep-INDEPENDENT - a line-oriented `grep -Eq '^[0-9]+$'` would let
# $'7\n../../pulls/1/merge' pass on its first line, and BSD grep's `-z`/`^...$` also matches
# such a value, so grep cannot be trusted for this whole-string check.
case "$alert" in
  (''|*[!0-9]*)
    echo "gh-codeql-dismiss: alert number must be numeric (got: ${alert})" >&2
    exit 2
    ;;
esac

repo="${GITHUB_REPOSITORY:-}"
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "gh-codeql-dismiss: no repo (set GITHUB_REPOSITORY=owner/name or run in a gh-resolvable repo)" >&2
  exit 2
fi
# Validate repo (owner/name) as a whole string before interpolating, via a bash `case` glob
# (no external tool), so a value like o/r/../../../pulls/1 cannot retarget the endpoint.
# Rejects: empty, any char outside the allowed set (newline/metachars included), more than
# one slash (*/*/*), a leading or trailing slash, and any '..' traversal segment.
case "$repo" in
  (''|*[!A-Za-z0-9._/-]*|*/*/*|/*|*/|*..*)
    echo "gh-codeql-dismiss: repo must be owner/name ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+); got: '${repo}'" >&2
    exit 2
    ;;
esac

case "$reason" in
  "false positive"|"$wont_fix"|"used in tests") ;;
  *)
    echo "gh-codeql-dismiss: reason must be one of: false positive | ${wont_fix} | used in tests (got: ${reason})" >&2
    exit 2
    ;;
esac

exec gh api -X PATCH "repos/${repo}/code-scanning/alerts/${alert}" \
  -f state=dismissed -f "dismissed_reason=${reason}" -f "dismissed_comment=${comment}"
