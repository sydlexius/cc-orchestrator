#!/usr/bin/env bash
# gh-codeql-autofix.sh <alert-number> [repo]  (issue #24, P3-F phase 2)
#
# Dedicated wrapper for ONE CodeQL mutation: request an autofix for a code-scanning alert.
# Construction guarantee: the endpoint is built from a VALIDATED NUMERIC alert id, so it can
# only ever POST `code-scanning/alerts/<n>/autofix` - it cannot be coerced into a merge-by-API
# or any other endpoint. No caller-supplied -X/--method is accepted (the verb is a fixed POST).
#
# Canonical source: cc-orchestrator repo root; deployed by symlink into ~/.claude/scripts/.
set -euo pipefail

alert="${1:-}"
repo_arg="${2:-}"

# Whole-string numeric check via a bash `case` glob (no external tool): rejects empty and
# any non-digit, INCLUDING an embedded newline (newline is not 0-9, so it falls in the
# [!0-9] class). This is grep-INDEPENDENT - a line-oriented `grep -Eq '^[0-9]+$'` would let
# $'7\n../../pulls/1/merge' pass on its first line, and BSD grep's `-z`/`^...$` also matches
# such a value, so grep cannot be trusted for this whole-string check.
case "$alert" in
  (''|*[!0-9]*)
    echo "gh-codeql-autofix: alert number must be numeric (got: ${alert})" >&2
    exit 2
    ;;
esac

repo="${repo_arg:-${GITHUB_REPOSITORY:-}}"
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "gh-codeql-autofix: no repo (pass [repo] arg, set GITHUB_REPOSITORY=owner/name, or run in a gh-resolvable repo)" >&2
  exit 2
fi
# Validate repo (owner/name) as a whole string before interpolating, via a bash `case` glob
# (no external tool), so a value like o/r/../../../pulls/1 cannot retarget the endpoint.
# Rejects: empty, any char outside the allowed set (newline/metachars included), more than
# one slash (*/*/*), a leading or trailing slash, and any '..' traversal segment.
case "$repo" in
  (''|*[!A-Za-z0-9._/-]*|*/*/*|/*|*/|*..*)
    echo "gh-codeql-autofix: repo must be owner/name ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+); got: '${repo}'" >&2
    exit 2
    ;;
esac

exec gh api -X POST "repos/${repo}/code-scanning/alerts/${alert}/autofix"
