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
# The prefix is spliced into an ERE below; a repo basename can legally contain
# regex metacharacters (e.g. a dot), so escape every ERE metacharacter before
# use, exactly as esc_suffix does for the suffix.
esc_prefix=$(printf '%s' "$prefix" | sed -e 's/[][(){}.^$*+?|\\-]/\\&/g')

# Match basename ending in -<suffix>, optionally with a milestone insert
# (-m<N>-) between prefix and suffix. Anchored at both ends so a longer
# basename that merely contains the prefix cannot match the wrong worktree.
pattern="^${esc_prefix}(-m[0-9.]+)?-${esc_suffix}$"

# Find worktree path and branch by matching the path's basename. Parse each
# porcelain record (separated by blank lines) so the match works whether the
# worktree is branch-backed or detached.
# Tab-delimit the awk output (and split on tab) so a worktree path containing
# spaces is not truncated by the read.
# Capture the awk match into a variable FIRST, then conditionally read it. A
# direct `read < <(awk ...)` aborts under `set -e` when awk emits nothing (no
# matching worktree), short-circuiting the no-match diagnostic below.
worktree_path=""
branch=""
match=$(
  git worktree list --porcelain \
    | awk -v p="$pattern" 'BEGIN { RS="" }
        {
          wt=""; br=""; base=""
          for (i=1; i<=NF; i++) {
            if ($i == "worktree")    { wt=$(i+1); base=wt; sub(/.*\//,"",base); i++ }
            else if ($i == "branch") { br=$(i+1); gsub("refs/heads/","",br);   i++ }
          }
          if (base ~ p) { print wt "\t" br; exit }
        }'
)
if [ -n "$match" ]; then
  IFS=$'\t' read -r worktree_path branch <<<"$match"
fi

if [ -z "$worktree_path" ]; then
  echo "No worktree found matching pattern: $pattern"
  echo "Current worktrees:"
  git worktree list
  exit 1
fi

echo "Worktree: $worktree_path"
echo "Branch:   $branch"
echo ""

# --- Capture the run dir BEFORE removing the worktree (#303 amendment 2) ---
#
# run-paths.sh is THE single producer of this path (#303). Sourcing it -- rather than
# reconstructing the path here -- is the entire point: the previous local
# reconstruction (`<prefix>-run/<basename>`) omitted the producer's `-<sha12>` suffix,
# so the removal below never matched and 170+ stale run dirs (922M) accumulated. Any
# sha12 computed HERE would be a second derivation of the same value, i.e. the exact
# drift this fixes. CC_RUN_WORKTREE hands the producer the path string git recorded,
# which it hashes verbatim -- no lookup, no realpath -- so this yields the identical
# CC_RUN_DIR whether the worktree is still live or was pruned long ago.
#
# Ordering is load-bearing: capture BEFORE `git worktree remove`, remove, then rm -rf.
# CC_RUN_NO_MKDIR keeps the producer from creating the directory we are about to delete.
run_dir=""
rp_src=""
for cand in "$(dirname "$0")/run-paths.sh" "$HOME/.claude/scripts/run-paths.sh"; do
  if [ -r "$cand" ]; then rp_src="$cand"; break; fi
done
if [ -n "$rp_src" ]; then
  # Subshell-free source, but scoped: run-paths.sh is written not to leak shell options
  # and to degrade to an empty CC_RUN_DIR rather than abort a sourcing shell.
  # shellcheck source=scripts/run-paths.sh disable=SC1091
  CC_RUN_WORKTREE="$worktree_path" CC_RUN_NO_MKDIR=1 . "$rp_src" || true
  run_dir="${CC_RUN_DIR:-}"
else
  echo "warning: run-paths.sh not found next to $0 or at ~/.claude/scripts/; skipping run-dir cleanup." >&2
  echo "  Run 'orchestrate-setup.py configure --apply' to deploy it." >&2
fi

# Remove worktree. If the directory was already deleted out-of-band, fall back to pruning
# stale admin metadata so the run stays idempotent.
#
# Both calls are failure-TOLERANT for the same reason the network calls below are (#302):
# under `set -euo pipefail` an unguarded failure here aborts the script ABOVE all local
# cleanup, leaking the run dir exactly as the 422 did. `git worktree remove` refuses on a
# dirty worktree (an untracked build artifact or a stray .env is enough), which is common
# and has nothing to do with whether the run dir should be reclaimed. On failure the
# worktree survives and is reported, so the user can re-run or force it by hand; the lint
# cache is still cleaned, but the RUN DIR IS DELIBERATELY KEPT (see the local-cleanup block
# below -- a surviving worktree may have a gate running against that dir).
echo "=== Removing worktree ==="
wt_removed=1
if [ -d "$worktree_path" ]; then
  if ! git worktree remove "$worktree_path"; then
    wt_removed=0
    echo "warning: 'git worktree remove $worktree_path' FAILED; the worktree is still present." >&2
    echo "  Common cause: modified or untracked files in it. Inspect, then re-run, or force with:" >&2
    echo "    git worktree remove --force \"$worktree_path\"" >&2
    echo "  Continuing with the remaining cleanup; the run dir is KEPT (see below)." >&2
  fi
else
  echo "Worktree directory already gone; pruning admin metadata."
  git worktree prune -v || echo "warning: 'git worktree prune' failed; stale admin metadata remains." >&2
fi

# --- LOCAL CLEANUP, deliberately placed AHEAD of every network call (#302) ---
#
# This block used to sit at the very END, downstream of the remote-branch DELETE and
# `git fetch --prune`. Under `set -euo pipefail` either of those could exit the script,
# and on the MOST COMMON path -- a squash-merge with auto-delete-branch, where the
# DELETE returns 422 "Reference does not exist" -- it did, so the run dir and the lint
# cache were never touched. Fixing only the 422 would leave the same skip reachable via
# `git fetch --prune`. The structural fix is ordering: nothing local depends on the
# network, so nothing local runs after it.
# The run dir is reclaimed only once the worktree is actually GONE. A worktree whose
# removal failed is still live, and its run dir may hold an IN-FLIGHT gate's artifacts
# (the convention puts the gate's own lock dir under $CC_RUN_DIR) -- deleting it under a
# running gate is a concurrency wipe, a worse bug than the leak this fixes. The #302 case
# is unaffected: there the worktree removal SUCCEEDS and only a later network call blew up.
if [ "$wt_removed" -eq 0 ]; then
  # Name the path explicitly. Once the user follows the force-remove advice above, a re-run
  # of this script matches no worktree and exits before ever reaching this block, so the dir
  # would be orphaned with nothing left that knows its name.
  echo "=== Keeping run dir (worktree removal failed; it may still be in use) ==="
  if [ -n "$run_dir" ]; then
    echo "    $run_dir"
    echo "    After forcing the worktree removal, reclaim it with: rm -rf \"$run_dir\""
  fi
elif [ -n "$run_dir" ] && [ -d "$run_dir" ]; then
  echo "=== Removing run dir: $run_dir ==="
  rm -rf "$run_dir"
fi

# Clear the SHARED (default-location) golangci-lint cache. Once every Go worktree in the
# repo sources run-paths.sh -- which exports a PER-WORKTREE GOLANGCI_LINT_CACHE under
# $CC_RUN_DIR, removed above together with the run dir -- cross-worktree stale-path
# phantoms are impossible and this blunt global clean is unnecessary. It stays as a
# MID-TRANSITION fallback: worktrees created BEFORE adoption still share the default
# cache, and removing it outright would regress them. Opt out once the repo has fully
# adopted the per-worktree cache:  CC_SKIP_GLOBAL_LINT_CACHE_CLEAN=1
#
# `env -u GOLANGCI_LINT_CACHE` is LOAD-BEARING, not tidiness. Sourcing run-paths.sh above
# EXPORTED that variable into this very shell, and golangci-lint honors it over the
# default location -- so a plain `cache clean` here would clean the per-worktree cache
# (already destroyed one line up with the run dir) and NEVER touch the shared cache this
# block exists to clear, leaving the whole fallback inert while reading as correct.
# Cost when it runs: each remaining worktree's next gate pays a one-time warm-up (~30s).
# Silently skipped if the binary is not installed (cleanup must not require lint tooling).
if [ "${CC_SKIP_GLOBAL_LINT_CACHE_CLEAN:-}" = "1" ]; then
  echo "=== Skipping global golangci-lint cache clean (CC_SKIP_GLOBAL_LINT_CACHE_CLEAN=1) ==="
elif command -v golangci-lint >/dev/null 2>&1; then
  echo "=== Cleaning global golangci-lint cache (prevents stale-path cross-worktree reports) ==="
  env -u GOLANGCI_LINT_CACHE golangci-lint cache clean >/dev/null 2>&1 || true
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

    # Delete remote branch (guarded). Only delete when the default branch is
    # resolved AND differs from $branch. In safe mode (default unknown) we
    # refuse, so an unresolved default branch can never lead to deleting the
    # remote default branch (the same #1741 hazard the local guard covers).
    if [ -n "$default_branch" ] && [ "$branch" != "$default_branch" ]; then
      echo "=== Deleting remote branch: $branch ==="
      repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)
      encoded=$(printf '%s' "$branch" | jq -sRr @uri)
      # VERIFY THE OUTCOME; do not infer it from the status code (#337).
      #
      # This block used to classify the API response: success, else `grep -q '404'` =
      # already deleted, else fail. GitHub returns 404 OR 422 "Reference does not exist"
      # for an absent ref depending on the path taken, so keying on 404 alone was
      # incomplete BY CONSTRUCTION -- and a squash-merge with auto-delete-branch (the
      # most common path here) returns exactly the 422, hard-failing a cleanup whose
      # branch was deleted correctly.
      #
      # Widening the grep to '404|422' was rejected: 422 is generic Unprocessable
      # Entity, so the bare code would also swallow genuine failures, and matching the
      # message text pins on error PROSE, not an API contract. Instead test the
      # condition actually being asserted -- is the ref gone? -- which is immune to
      # every status-code variation and is already the house pattern (safe-push.sh
      # verifies the remote ref MOVED rather than trusting an exit code).
      #
      # stderr is still captured, but purely as DIAGNOSTICS attached to a real failure.
      del_err=$(mktemp)
      # Reclaim the temp file on ANY exit, including the failure path below and a signal.
      # The explicit rm -f calls stay (they keep the happy path tidy); the trap covers
      # what they cannot.
      trap 'rm -f "$del_err"' EXIT
      del_rc=0
      gh api "repos/$repo/git/refs/heads/$encoded" -X DELETE >/dev/null 2>"$del_err" || del_rc=$?
      # NOTE on `git ls-remote` exit statuses: 0 = ref present, 2 = no matching ref
      # (--exit-code), 128 = could NOT determine (unreachable host, auth failure, bad
      # remote). Anything non-zero is treated as ABSENT, so a 128 reports "already
      # deleted" when the truth is unknown. That is DELIBERATE, not an oversight: it only
      # happens when the network is already down, the cost is a leftover remote branch
      # (recoverable, and stale-branch-sweep.sh catches it later), and failing closed here
      # would resurrect the exact #302 pathology of a network condition aborting a cleanup
      # that has already done its local work. Do not "fix" this into a hard failure.
      # GIT_TERMINAL_PROMPT=0 + SSH BatchMode so an auth-required origin fails FAST rather
      # than hanging cleanup on a credential prompt (the house pattern from
      # base-freshness.sh); a caller's own GIT_SSH_COMMAND is preserved.
      if GIT_TERMINAL_PROMPT=0 \
         GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh} -o BatchMode=yes" \
         git ls-remote --exit-code origin "refs/heads/$branch" >/dev/null 2>&1; then
        # Ref STILL PRESENT: a genuine failure regardless of what the API reported.
        echo "Error: failed to delete remote branch '$branch' (ref still present on origin):" >&2
        # An `if`, not `[ -s ] && cat`: under `set -e` that AND-list returns 1 when the
        # file is EMPTY, which aborts at a point where the exit status is meant to be
        # chosen deliberately.
        if [ -s "$del_err" ]; then cat "$del_err" >&2; fi
        rm -f "$del_err"
        exit 1
      fi
      if [ "$del_rc" -eq 0 ]; then
        echo "Remote branch deleted."
      else
        # The DELETE reported an error but the ref is verifiably gone -- the
        # already-deleted case (404, or the 422 GitHub returns after auto-delete).
        # Report the API's complaint as an annotation, not a verdict.
        echo "Remote branch already deleted (verified absent on origin; DELETE reported an error)."
        if [ -s "$del_err" ]; then sed 's/^/  api: /' "$del_err"; fi
      fi
      rm -f "$del_err"
      trap - EXIT
    else
      echo "!!! WARNING: default branch unknown (safe mode); skipping remote branch deletion for '$branch'." >&2
      echo "!!! Refusing to delete a remote branch when the repo default cannot be resolved. Delete it manually if it is a merged feature branch." >&2
    fi
  fi
fi

# Prune stale tracking refs. LAST on purpose (#302): this is a network call under
# `set -e`, so anything downstream of it is skippable by a transient origin failure.
# All local cleanup already ran above; a failure here loses nothing but the prune, so
# it is tolerated and reported rather than aborting a run that already did its work.
echo "=== Pruning stale refs ==="
git fetch --prune || echo "warning: 'git fetch --prune' failed; stale tracking refs were not pruned." >&2

echo ""
echo "Done. Update your worktrees memory/notes to reflect the change."
