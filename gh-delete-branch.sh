#!/usr/bin/env bash
# gh-delete-branch.sh <branch-name> [repo]  (issue #24, P3-F phase 2)
#
# Dedicated wrapper for ONE git mutation: delete a branch ref (post-merge cleanup).
# Construction guarantee: the branch name is validated against a strict allow-list charset
# (^[A-Za-z0-9._/-]+$, rejecting path-traversal/metachars) and then URL-encoded, so the
# endpoint is the literal `git/refs/heads/<encoded-branch>` and cannot be coerced into a
# merge-by-API or any other endpoint. The verb is a fixed DELETE; no caller-supplied
# -X/--method is accepted.
#
# Canonical source: cc-orchestrator repo root; deployed by symlink into ~/.claude/scripts/.
set -euo pipefail

branch="${1:-}"
repo_arg="${2:-}"

if [ -z "$branch" ]; then
  echo "gh-delete-branch: branch name is required" >&2
  exit 2
fi

# Reject a multiline value outright (grep matches per-line, so a newline could let a
# benign first line pass while a second line smuggles other content).
case "$branch" in
  *$'\n'*)
    echo "gh-delete-branch: branch name must not contain a newline" >&2
    exit 2
    ;;
esac
# Reject a leading dash so the value can never be mistaken for a flag.
case "$branch" in
  -*)
    echo "gh-delete-branch: branch name must not start with '-' (got: ${branch})" >&2
    exit 2
    ;;
esac
# Reject anything outside a strict charset (no spaces, no shell/url metachars) via a bash
# `case` glob (no external tool). Slashes are allowed (branches like feat/x). The glob
# anchors the WHOLE string and rejects an embedded newline (out-of-charset) - belt-and-
# suspenders with the check above, and grep-INDEPENDENT (BSD grep's -z/^...$ cannot be
# trusted to anchor the whole string).
case "$branch" in
  (''|*[!A-Za-z0-9._/-]*)
    echo "gh-delete-branch: branch name must match ^[A-Za-z0-9._/-]+\$ (got: ${branch})" >&2
    exit 2
    ;;
esac
# Defense-in-depth: forbid a path-traversal segment even within the allowed charset.
case "/${branch}/" in
  */../*)
    echo "gh-delete-branch: branch name must not contain a '..' path segment (got: ${branch})" >&2
    exit 2
    ;;
esac

repo="${repo_arg:-${GITHUB_REPOSITORY:-}}"
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "gh-delete-branch: no repo (pass [repo] arg, set GITHUB_REPOSITORY=owner/name, or run in a gh-resolvable repo)" >&2
  exit 2
fi
# Validate repo (owner/name) as a whole string before interpolating, via a bash `case` glob
# (no external tool), so a value like o/r/../../../pulls/1/merge cannot retarget the endpoint.
# Rejects: empty, any char outside the allowed set (newline/metachars included), more than
# one slash (*/*/*), a leading or trailing slash, and any '..' traversal segment.
case "$repo" in
  (''|*[!A-Za-z0-9._/-]*|*/*/*|/*|*/|*..*)
    echo "gh-delete-branch: repo must be owner/name ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+); got: '${repo}'" >&2
    exit 2
    ;;
esac

# URL-encode the branch (keep the validated unreserved set + '/' as a path separator,
# since git/refs/heads/<ref> takes a slash-bearing ref literally).
encode_ref() {
  local s="$1" out="" i c
  for (( i = 0; i < ${#s}; i++ )); do
    c="${s:i:1}"
    case "$c" in
      [A-Za-z0-9._/-]) out+="$c" ;;
      *) out+="$(printf '%%%02X' "'$c")" ;;
    esac
  done
  printf '%s' "$out"
}

encoded="$(encode_ref "$branch")"

exec gh api -X DELETE "repos/${repo}/git/refs/heads/${encoded}"
