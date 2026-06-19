#!/usr/bin/env bash
# stale-branch-sweep.sh [--delete] [repo]   (issue #137, Phase 3)
#
# Periodic, defense-in-depth sweep for stale remote branches: heads that linger
# after their PR merged or closed but were never cleaned up (e.g. a UI/out-of-band
# merge where the lead never ran /post-merge-cleanup). It is a catch-all backstop
# for the per-merge cleanup path, NOT a replacement for it.
#
# DECISION phase is strictly READ-ONLY: it enumerates remote heads and cross-checks
# them against open vs merged/closed PRs to identify confirmed orphans. A remote
# head is a confirmed orphan ONLY when ALL hold:
#   - it is NOT the default branch, main, or master;
#   - it has NO open PR (a head with an open PR is never touched);
#   - it IS the head of a merged or closed PR (so we know its lifecycle ended).
# A head with no PR history at all (e.g. a manually pushed branch) is left alone -
# we only reap heads we can prove belonged to a finished PR.
#
# DELETION routes through the construction-guaranteed DELETE-only wrapper
# gh-delete-branch.sh (a sibling script); this script never hand-rolls a
# `gh api -X DELETE`. Deletion happens ONLY with --delete; the default is a safe
# dry-run that prints what WOULD be deleted and changes nothing.
#
# FAIL CLOSED: if the open-PR list cannot be read, the sweep aborts (exit 2)
# rather than risk deleting a branch whose open PR it failed to see.
#
# Usage:
#   stale-branch-sweep.sh [--delete] [repo]
#
# Options:
#   --delete   Actually delete confirmed orphan heads (via gh-delete-branch.sh).
#              Without it, the script runs in dry-run mode (default) and only
#              reports candidates.
#   -h|--help  Print this header as usage and exit 0.
#
# Arguments:
#   repo       owner/name slug (optional; resolved via `gh repo view` if omitted).
#
# Exit codes (mirroring the repo conventions):
#   0  Success - sweep completed (dry-run listed candidates, or delete mode
#      removed every confirmed orphan with no failures; also when none found).
#   1  One or more deletions FAILED in --delete mode.
#   2  Usage / setup error, OR a read failure that forces a fail-closed abort.
set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

# --- Parse arguments ---
delete_mode=false
repo_arg=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --delete) delete_mode=true; shift ;;
    --) shift; break ;;
    -*) echo "stale-branch-sweep: unknown flag '$1'" >&2
        echo "usage: stale-branch-sweep.sh [--delete] [repo]" >&2
        exit 2 ;;
    *) if [ -z "$repo_arg" ]; then repo_arg="$1"; shift
       else echo "stale-branch-sweep: unexpected extra argument '$1'" >&2; exit 2; fi ;;
  esac
done
# Allow a trailing repo after `--`.
if [ "$#" -gt 0 ] && [ -z "$repo_arg" ]; then
  repo_arg="$1"; shift
fi
if [ "$#" -gt 0 ]; then
  echo "stale-branch-sweep: unexpected extra argument '$1'" >&2
  exit 2
fi

# --- Resolve repo ---
repo="${repo_arg:-${GITHUB_REPOSITORY:-}}"
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "stale-branch-sweep: no repo (pass [repo] arg, set GITHUB_REPOSITORY=owner/name, or run in a gh-resolvable repo)" >&2
  exit 2
fi

# Locate the DELETE-only wrapper as a sibling of this script (works both in the
# repo and in the deployed ~/.claude/scripts/ layout).
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deleter="$script_dir/gh-delete-branch.sh"
if [ "$delete_mode" = true ] && [ ! -x "$deleter" ]; then
  echo "stale-branch-sweep: required wrapper not found/executable at $deleter" >&2
  exit 2
fi

# --- Read phase (READ-ONLY) ---
# Enumerate remote heads via git ls-remote (authoritative; independent of local
# fetch/prune state). Each line is "<sha>\trefs/heads/<branch>".
remote_heads="$(git ls-remote --heads origin 2>/dev/null \
  | sed -n 's#^[0-9a-f]\{40\}[[:space:]]\{1,\}refs/heads/##p' || true)"
if [ -z "$remote_heads" ]; then
  echo "stale-branch-sweep: no remote heads found (or could not reach origin) for $repo."
  exit 0
fi

# Default branch (never a deletion candidate). FAIL CLOSED: this read is as
# load-bearing as the open-PR read - if it fails, a repo whose default branch is
# not literally main/master (e.g. develop) would lose its protection, so we abort
# rather than risk deleting the default head.
if ! default_branch="$(gh repo view "$repo" --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null)"; then
  echo "stale-branch-sweep: could not read the default branch for $repo; aborting (fail closed)." >&2
  exit 2
fi

# Open PR heads. FAIL CLOSED: if this read fails we cannot prove a branch lacks an
# open PR, so we abort rather than risk deleting a live PR's head.
if ! open_heads="$(gh pr list --repo "$repo" --state open --limit 1000 --json headRefName --jq '.[].headRefName' 2>/dev/null)"; then
  echo "stale-branch-sweep: could not read open PRs for $repo; aborting (fail closed)." >&2
  exit 2
fi

# Merged + closed PR heads (lifecycle-ended). Each read is checked SEPARATELY and
# fails closed: a single grouped `$( merged; closed )` would only surface the LAST
# command's exit status, so a merged-read failure with a successful closed-read
# would slip through despite this guard. Without BOTH we cannot confirm a head's
# PR actually finished, so either failure aborts.
if ! merged_heads="$(gh pr list --repo "$repo" --state merged --limit 1000 --json headRefName --jq '.[].headRefName' 2>/dev/null)"; then
  echo "stale-branch-sweep: could not read merged PRs for $repo; aborting (fail closed)." >&2
  exit 2
fi
if ! closed_heads="$(gh pr list --repo "$repo" --state closed --limit 1000 --json headRefName --jq '.[].headRefName' 2>/dev/null)"; then
  echo "stale-branch-sweep: could not read closed PRs for $repo; aborting (fail closed)." >&2
  exit 2
fi
ended_heads="$(printf '%s\n%s' "$merged_heads" "$closed_heads")"

is_in_list() {
  # is_in_list <needle> <newline-list>; exact full-line match.
  local needle="$1" list="$2"
  [ -n "$list" ] && printf '%s\n' "$list" | grep -Fxq -- "$needle"
}

# --- Decision phase ---
candidates=()
while IFS= read -r branch; do
  [ -z "$branch" ] && continue
  case "$branch" in
    main|master) continue ;;
  esac
  if [ -n "$default_branch" ] && [ "$branch" = "$default_branch" ]; then
    continue
  fi
  if is_in_list "$branch" "$open_heads"; then
    continue   # has an open PR -> never touch
  fi
  if is_in_list "$branch" "$ended_heads"; then
    candidates+=("$branch")
  fi
done <<< "$remote_heads"

if [ "${#candidates[@]}" -eq 0 ]; then
  echo "stale-branch-sweep: no stale remote heads on $repo (nothing to do)."
  exit 0
fi

# --- Report / delete phase ---
if [ "$delete_mode" = false ]; then
  echo "stale-branch-sweep: DRY RUN -- ${#candidates[@]} stale remote head(s) on $repo would be deleted:"
  for branch in "${candidates[@]}"; do
    echo "  would delete: $branch"
  done
  echo "Re-run with --delete to remove them (deletion routes through gh-delete-branch.sh)."
  exit 0
fi

echo "stale-branch-sweep: deleting ${#candidates[@]} stale remote head(s) on $repo..."
fail=0
for branch in "${candidates[@]}"; do
  if "$deleter" "$branch" "$repo"; then
    echo "  deleted: $branch"
  else
    echo "  FAILED to delete: $branch" >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "stale-branch-sweep: one or more deletions failed." >&2
  exit 1
fi
echo "stale-branch-sweep: done."
exit 0
