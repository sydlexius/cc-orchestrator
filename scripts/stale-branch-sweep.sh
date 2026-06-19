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
#   - it IS the head of a merged or closed PR by BOTH name AND current commit SHA
#     (so we know THIS commit's lifecycle ended).
# A head with no PR history at all (e.g. a manually pushed branch) is left alone -
# we only reap heads we can prove belonged to a finished PR. A name that matches an
# ended PR head but now points at a DIFFERENT SHA (a deleted-then-recreated branch
# reusing the name) is also left alone - deleting it would destroy live new work.
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
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
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
# fetch/prune state). Each line is "<sha>\trefs/heads/<branch>". FAIL CLOSED: a
# network/auth failure must NOT be swallowed into "no stale heads" (which would
# exit 0 as if cleanup ran and found nothing) - capture the raw output, abort on
# failure, and only treat a genuinely-empty SUCCESSFUL read as "none".
raw_heads="$(git ls-remote --heads origin 2>/dev/null)" || {
  echo "stale-branch-sweep: could not read remote heads for $repo; aborting (fail closed)." >&2
  exit 2
}
# Reshape each "<40-hex-sha>\trefs/heads/<branch>" line into "<branch>\t<sha>" so
# the decision phase has each remote head's CURRENT sha (needed to tell a truly
# ended branch from a reused name that now points at new commits).
remote_heads="$(printf '%s\n' "$raw_heads" \
  | sed -nE 's#^([0-9a-f]{40})[[:space:]]+refs/heads/(.+)$#\2'"$(printf '\t')"'\1#p')"
if [ -z "$remote_heads" ]; then
  echo "stale-branch-sweep: no remote heads found for $repo."
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

# Merged + closed PR heads (lifecycle-ended), each as a "<name>\t<sha>" pair so the
# decision phase can require BOTH name AND commit identity to match. Each read is
# checked SEPARATELY and fails closed: a single grouped `$( merged; closed )` would
# only surface the LAST command's exit status, so a merged-read failure with a
# successful closed-read would slip through despite this guard. Without BOTH we
# cannot confirm a head's PR actually finished, so either failure aborts.
if ! merged_heads="$(gh pr list --repo "$repo" --state merged --limit 1000 --json headRefName,headRefOid --jq '.[] | "\(.headRefName)\t\(.headRefOid)"' 2>/dev/null)"; then
  echo "stale-branch-sweep: could not read merged PRs for $repo; aborting (fail closed)." >&2
  exit 2
fi
if ! closed_heads="$(gh pr list --repo "$repo" --state closed --limit 1000 --json headRefName,headRefOid --jq '.[] | "\(.headRefName)\t\(.headRefOid)"' 2>/dev/null)"; then
  echo "stale-branch-sweep: could not read closed PRs for $repo; aborting (fail closed)." >&2
  exit 2
fi
# Concatenate, dropping blank lines (an empty merged/closed set yields none).
ended_heads="$(printf '%s\n%s\n' "$merged_heads" "$closed_heads" | grep -v '^[[:space:]]*$' || true)"
# Names alone (for the "name matches an ended head but SHA differs" skip notice).
ended_names="$(printf '%s\n' "$ended_heads" | cut -f1)"

TAB="$(printf '\t')"

is_in_list() {
  # is_in_list <needle> <newline-list>; exact full-line match.
  local needle="$1" list="$2"
  [ -n "$list" ] && printf '%s\n' "$list" | grep -Fxq -- "$needle"
}

# --- Decision phase ---
# A remote head is a confirmed orphan ONLY if it has no open PR AND there exists an
# ended (merged/closed) PR head with the SAME name AND the SAME current SHA. A name
# match with a DIFFERENT SHA means the branch was deleted and recreated for new work
# (the name was reused) - deleting it would destroy live work, so it is skipped.
candidates=()
while IFS="$TAB" read -r branch sha; do
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
  if is_in_list "$branch$TAB$sha" "$ended_heads"; then
    candidates+=("$branch")
  elif is_in_list "$branch" "$ended_names"; then
    echo "stale-branch-sweep: skipping '$branch' - name matches an ended PR head but its current SHA differs (likely a reused branch with new commits)."
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
