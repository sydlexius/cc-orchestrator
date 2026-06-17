#!/bin/bash
# cleanup-worktree.sh <suffix> -- remove worktree and branches after PR merge
#
# Repo-agnostic. Run from inside any git worktree of the target repo.
#
# <suffix> identifies the worktree by the tail of its directory basename
# after the repo prefix. The repo prefix is auto-detected from the main
# worktree's basename, so a repo rooted at /path/to/myrepo cleans up
# worktrees named myrepo-<suffix> or myrepo-m<N>-<suffix>.
#
# Examples (in a repo whose main worktree is ~/Developer/stillwater):
#   cleanup-worktree.sh 1180                    -> removes stillwater-1180
#   cleanup-worktree.sh m36-639                 -> removes stillwater-m36-639
#   cleanup-worktree.sh fanart-dup              -> removes stillwater-fanart-dup
#   cleanup-worktree.sh m49.5-settings-handler  -> removes stillwater-m49.5-settings-handler
set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

if [ -z "${1:-}" ]; then
  cat >&2 <<'USAGE'
Usage: cleanup-worktree.sh <suffix>

  <suffix> is the portion of the worktree directory basename after the
  repo prefix. Allowed characters: [A-Za-z0-9_.-] (dots allowed for
  dotted milestone names like m49.5-foo).

Examples:
  cleanup-worktree.sh 1180
  cleanup-worktree.sh m36-639
  cleanup-worktree.sh fanart-dup
  cleanup-worktree.sh m49.5-settings-handler
USAGE
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "Error: jq is required but not installed." >&2
  exit 1
fi

if ! command -v gh &>/dev/null; then
  echo "Error: gh (GitHub CLI) is required but not installed." >&2
  exit 1
fi

suffix="$1"
# Allow alphanumerics, dashes, underscores, and dots (for dotted milestone
# names like m49.5-foo). Dots are regex metacharacters in ERE, so they must
# be escaped before splicing into the pattern below -- see esc_suffix.
if ! [[ "$suffix" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Error: suffix must match ^[A-Za-z0-9_.-]+\$ (got: $suffix)" >&2
  exit 1
fi
esc_suffix="${suffix//./\\.}"

# Must be inside a git repo.
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: not inside a git work tree." >&2
  exit 1
fi

# Auto-detect the repo prefix from the main worktree's basename.
# `git worktree list --porcelain` emits the main worktree first.
main_worktree=$(git worktree list --porcelain \
  | awk '/^worktree / { print $2; exit }')
if [ -z "$main_worktree" ]; then
  echo "Error: could not determine main worktree." >&2
  exit 1
fi
prefix=$(basename "$main_worktree")

# Match basename ending in -<suffix>, optionally with a milestone insert
# (-m<N>-) between prefix and suffix.
pattern="${prefix}(-m[0-9.]+)?-${esc_suffix}$"

# Find worktree path and branch by matching the path's basename. Parse each
# porcelain record (separated by blank lines) so the match works whether the
# worktree is branch-backed or detached.
read -r worktree_path branch < <(
  git worktree list --porcelain \
    | awk -v p="$pattern" 'BEGIN { RS="" }
        {
          wt=""; br=""; base=""
          for (i=1; i<=NF; i++) {
            if ($i == "worktree")    { wt=$(i+1); base=wt; sub(/.*\//,"",base); i++ }
            else if ($i == "branch") { br=$(i+1); gsub("refs/heads/","",br);   i++ }
          }
          if (base ~ p) { print wt, br; exit }
        }'
)

if [ -z "$worktree_path" ]; then
  echo "No worktree found matching pattern: $pattern"
  echo "Current worktrees:"
  git worktree list
  exit 1
fi

echo "Worktree: $worktree_path"
echo "Branch:   $branch"
echo ""

# Remove worktree. If the directory was already deleted out-of-band, fall
# back to pruning stale admin metadata so the run stays idempotent.
echo "=== Removing worktree ==="
if [ -d "$worktree_path" ]; then
  git worktree remove "$worktree_path"
else
  echo "Worktree directory already gone; pruning admin metadata."
  git worktree prune -v
fi

# Resolve the repo's default branch so we never delete it. After a
# `gh pr merge --delete-branch` run from INSIDE a worktree, gh checks out the
# default branch in that worktree before deleting the feature branch, leaving
# the worktree sitting on the default branch. Without this guard the block
# below would then force-delete `main`/`master` (issue #1741). Two resolution
# methods, then a conservative fallback:
#   1. git symbolic-ref refs/remotes/origin/HEAD  (set by clone / `git remote set-head`)
#   2. gh repo view --json defaultBranchRef       (authoritative, needs network)
# If BOTH fail we enter "safe mode": the default branch is unknown, so we refuse
# the force-delete (`-D`) entirely and only attempt the merged-only
# `git branch -d`, which can never destroy unmerged work.
default_branch=""
if symref=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null); then
  default_branch="${symref#origin/}"
fi
if [ -z "$default_branch" ]; then
  default_branch=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || true)
fi

# Delete local + remote branch (guarded).
if [ -n "$branch" ]; then
  if [ -n "$default_branch" ] && [ "$branch" = "$default_branch" ]; then
    # Hard guard: the worktree is sitting on the default branch. This is the
    # `gh --delete-branch` aftermath -- the feature branch was already deleted
    # by gh, and `$branch` now resolves to the default branch. Deleting it here
    # would destroy the local default branch (issue #1741).
    echo "!!! WARNING: worktree was on the default branch ('$default_branch'); skipping branch deletion." >&2
    echo "!!! This is the expected result of running 'gh pr merge --delete-branch' from inside the" >&2
    echo "!!! worktree: gh checks out the default branch before deleting the feature branch, so the" >&2
    echo "!!! feature branch is already gone. Refusing to delete '$default_branch'." >&2
  else
    echo "=== Deleting local branch: $branch ==="
    if git show-ref --quiet "refs/heads/$branch"; then
      if [ -n "$default_branch" ]; then
        # Default branch resolved and $branch is confirmed NOT it, so the force
        # fallback is safe. `-d` (merged-only) first; `-D` covers the common
        # squash-merge case where the feature branch's commits are not ancestors
        # of the default branch and `-d` therefore refuses.
        git branch -d "$branch" || git branch -D "$branch"
      else
        # Safe mode: default branch unknown. Never force-delete. Try merged-only
        # and, if that refuses, leave the branch for the user to remove by hand.
        git branch -d "$branch" || \
          echo "warning: could not resolve the default branch; refusing 'git branch -D $branch'. Delete it manually if it is a merged feature branch." >&2
      fi
    else
      echo "Local branch already gone."
    fi

    # Delete remote branch
    echo "=== Deleting remote branch: $branch ==="
    repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)
    encoded=$(printf '%s' "$branch" | jq -sRr @uri)
    gh api "repos/$repo/git/refs/heads/$encoded" -X DELETE 2>/dev/null \
      && echo "Remote branch deleted." \
      || echo "Remote branch not found or already deleted."
  fi
fi

# Prune stale tracking refs
echo "=== Pruning stale refs ==="
git fetch --prune

# Remove the per-worktree run-artifact directory if one exists.
# Pairs with scripts/lib/run-paths.sh in repos that adopt the convention:
# every worktree's transient artifacts (coverage profiles, cookie jars, dev
# logs) live under ${XDG_CACHE_HOME:-$HOME/.cache}/<prefix>-run/<basename>.
# Removing it here keeps the cache from accumulating one stale subdirectory
# per worktree we ever created. Idempotent: silently skips if the repo does
# not use the convention, or if the dir was already cleaned by hand.
run_dir="${XDG_CACHE_HOME:-$HOME/.cache}/${prefix}-run/$(basename "$worktree_path")"
if [ -d "$run_dir" ]; then
  echo "=== Removing run dir: $run_dir ==="
  rm -rf "$run_dir"
fi

# Clear the golangci-lint cache. The cache is shared across worktrees and
# keyed by content + file path; entries referencing the worktree we just
# removed remain valid lookups for `--new-from-rev` runs in OTHER worktrees,
# which then report findings against the deleted path. Symptom: a benign
# diff in worktree B suddenly fails lint citing files in deleted worktree A
# that no longer exist. golangci-lint has no targeted prune, so the only
# reliable mitigation is a full clean here. Cost: each remaining worktree's
# next gate run pays a one-time cache warm-up (~30s). Silently skipped if
# the binary is not installed (cleanup should not require lint tooling).
if command -v golangci-lint >/dev/null 2>&1; then
  echo "=== Cleaning golangci-lint cache (prevents stale-path cross-worktree reports) ==="
  golangci-lint cache clean >/dev/null 2>&1 || true
fi

echo ""
echo "Done. Update your worktrees memory/notes to reflect the change."
