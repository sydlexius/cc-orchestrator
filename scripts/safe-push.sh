#!/usr/bin/env bash
#
# safe-push.sh -- run `git push` and verify the remote actually received it.
#
# This wrapper exists because the common pipe-output invocation pattern:
#
#   git push -u origin <branch> 2>&1 | tail -30
#
# silently masks real push failures: without `set -o pipefail` the pipeline
# returns `tail`'s exit code (always 0), so a transient SSH blip, remote ref
# rejection, hook abort, or network drop looks identical to a quiet success.
# The agent then moves on to open a PR against a branch that never reached
# the remote.
#
# What this wrapper does:
#   1. Resolves the local HEAD and the branch (or current symbolic-ref).
#   2. Runs `git push` with full output captured to a log AND mirrored to
#      stderr so the caller can stream it.
#   3. After push returns, queries `git ls-remote origin <branch>` and
#      verifies the remote SHA matches local HEAD.
#   4. Exits non-zero with a clear message if push's exit code OR the post-push
#      ref check disagrees.
#
# Repo-agnostic: log path is `<git-dir>/safe-push.log` (inside .git/, always
# writable, never committed because .git/ is excluded by definition).
#
# Usage:
#   bash safe-push.sh                  # push current branch -u origin
#   bash safe-push.sh <branch>         # push named branch -u origin
#   bash safe-push.sh <branch> --force-with-lease   # extra flags forwarded to git push
#   bash safe-push.sh <branch> --rewrite            # DECLARE a history rewrite (see below)
#
# ADDITIVE vs REWRITE (#148): before pushing, the wrapper classifies the push
# against a FRESH `git ls-remote` SHA (not the stale local tracking ref):
#   - first push (no remote ref) or fast-forward (remote is an ancestor of local)
#     = ADDITIVE -> pushed normally.
#   - remote AHEAD of local (local is an ancestor of remote) = REFUSED (exit 1):
#     integrate first, this is not a rewrite.
#   - otherwise = HISTORY REWRITE -> REFUSED (exit 1) UNLESS the caller passed the
#     intent flag --rewrite (alias --rebased). With intent, the push proceeds with
#     --force-with-lease auto-added (never a bare --force; the deterministic floor
#     bans that regardless) and a reminder that any cited SHA is now orphaned and a
#     prior bot review owes a fresh full review. The --rewrite/--rebased flag is
#     CONSUMED here, never forwarded to git push.
#
# NOTE: the branch name must be the FIRST argument; `-u origin` is added
# automatically and must NOT be passed by the caller. Invoking it as
# `safe-push.sh -u origin <branch>` is a misuse: the leading `-u` is rejected
# (exit 2) rather than silently consumed, which previously produced a confusing
# `fatal: refs/remotes/origin/HEAD cannot be resolved to branch` error (#35).
#
# Exit codes:
#   0 -- push succeeded AND the remote ref matches local HEAD
#   1 -- push exited non-zero, the remote ref does not match local HEAD, the push
#        was REFUSED as a silent rewrite (no --rewrite/--rebased), the remote is
#        ahead (diverged) and must be integrated first, or the remote tip is not
#        in local history (run `git fetch origin` first so it can be classified)
#   2 -- invalid invocation / not in a git repo / cannot resolve branch

set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

# Repo-agnostic log location. `git rev-parse --git-dir` resolves correctly
# for the main worktree (.git), linked worktrees (.git/worktrees/<name>),
# and submodules. Falls back gracefully if we're somehow not inside a repo.
git_dir=$(git rev-parse --git-dir 2>/dev/null || true)
if [ -z "$git_dir" ]; then
  echo "safe-push: not inside a git repository" >&2
  exit 2
fi
LOG="$git_dir/safe-push.log"
# Truncate and lock down permissions before any write so the transcript is
# private to the current user even on shared systems. .git/ inherits 0755
# from git defaults, so the file's own mode is what protects it.
: >"$LOG"
chmod 600 "$LOG"

branch="${1:-}"
shift_count=0
if [ -n "$branch" ]; then
  # A leading-dash FIRST positional is a footgun: the caller almost certainly
  # passed flags (e.g. `-u origin <branch>`) where a branch name was expected.
  # Reject it with a clear usage error rather than silently discarding it and
  # letting the unconsumed flags flow onto the `git push` line (#35). A missing
  # first positional is still valid (handled below via the current-branch
  # fallback); only a leading-dash first positional is rejected here. Legitimate
  # trailing flags in "$@" (e.g. `<branch> --force-with-lease`) are untouched.
  if [ "${branch#-}" != "$branch" ]; then
    echo "safe-push: first arg must be a branch name; -u origin is added automatically." >&2
    echo "           Usage: safe-push.sh <branch> [extra git-push flags]" >&2
    exit 2
  fi
  shift_count=1
fi

if [ -z "$branch" ]; then
  branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
  if [ -z "$branch" ]; then
    echo "safe-push: HEAD is detached and no branch argument given" >&2
    exit 2
  fi
fi

# Drop the consumed positional so the remaining "$@" can flow into git push
# as extra flags (--force-with-lease, --no-verify, etc.). Quoted so flags
# with spaces survive intact.
if [ "$shift_count" -gt 0 ]; then
  shift
fi

# Parse the remaining args (#148): pull the INTENT flags --rewrite/--rebased OUT
# (they are safe-push's own signal, NOT git-push flags) and forward everything
# else verbatim. Accumulating into an array keeps flags-with-spaces intact.
rewrite_intent=0
push_args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --rewrite|--rebased) rewrite_intent=1; shift ;;
    *) push_args+=("$1"); shift ;;
  esac
done

local_sha=$(git rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)
if [ -z "$local_sha" ]; then
  echo "safe-push: local branch 'refs/heads/$branch' does not exist" >&2
  exit 2
fi

# --- Pre-push classification (#148): distinguish an ADDITIVE push (first push or
# fast-forward) from a HISTORY REWRITE before pushing, using a FRESH remote SHA
# from origin (never the stale local tracking ref). A rewrite is REFUSED unless
# the caller DECLARED intent (--rewrite/--rebased). The deterministic floor
# independently bans bare --force/-f and push-to-main regardless of this flag, so
# this only ADDS an additive-vs-rewrite signal the guard does not make; it never
# injects a bare --force and never weakens the floor.
pre_remote_line=$(git ls-remote origin "refs/heads/$branch" 2>/dev/null || true)
pre_remote_sha=${pre_remote_line%%$'\t'*}
if [ -z "$pre_remote_sha" ]; then
  push_kind="first-push"
elif ! git cat-file -e "${pre_remote_sha}^{commit}" 2>/dev/null; then
  # The remote tip is not in our local object DB (a stale local that has not
  # fetched, or a shallow clone). additive-vs-rewrite cannot be decided without
  # it, so DON'T guess "rewrite" (a confusing false refusal) - tell the caller
  # to fetch. Fails safe: refuses rather than force-pushing blind.
  echo "safe-push: origin/'$branch' is at $pre_remote_sha, which is not in your local history." >&2
  echo "          Run 'git fetch origin' so the push can be classified additive-vs-rewrite, then re-run." >&2
  exit 1
elif git merge-base --is-ancestor "$pre_remote_sha" "$local_sha" 2>/dev/null; then
  push_kind="fast-forward"
elif git merge-base --is-ancestor "$local_sha" "$pre_remote_sha" 2>/dev/null; then
  push_kind="diverged"
else
  push_kind="rewrite"
fi

case "$push_kind" in
  first-push|fast-forward) : ;;  # additive -- allowed, no force needed
  diverged)
    echo "safe-push: origin/'$branch' has commits your local branch lacks (remote is AHEAD)." >&2
    echo "          This is NOT a rewrite; integrate first (git fetch + rebase/merge), then re-push." >&2
    echo "          local:  $local_sha" >&2
    echo "          remote: $pre_remote_sha" >&2
    exit 1 ;;
  rewrite)
    if [ "$rewrite_intent" -ne 1 ]; then
      echo "safe-push: this push would REWRITE origin/'$branch' history (local is not a fast-forward of the remote)." >&2
      echo "          Refusing a silent rewrite. If it is intentional (rebase/amend/squash), re-run with --rewrite (or --rebased)." >&2
      echo "          local:  $local_sha" >&2
      echo "          remote: $pre_remote_sha" >&2
      exit 1
    fi
    # Intent declared: guarantee lease protection (append --force-with-lease if the
    # caller did not, NEVER a bare --force -- the floor bans that) and warn about
    # the consequences of rewriting a pushed branch.
    has_lease=0
    if [ "${#push_args[@]}" -gt 0 ]; then
      for a in "${push_args[@]}"; do
        case "$a" in --force-with-lease*) has_lease=1 ;; esac
      done
    fi
    if [ "$has_lease" -eq 0 ]; then
      push_args+=(--force-with-lease)
    fi
    echo "safe-push: REWRITING origin/'$branch' history (--rewrite declared; using --force-with-lease)." >&2
    echo "          Any previously-cited commit SHA is now ORPHANED; if a bot already reviewed this PR it" >&2
    echo "          owes a fresh full review (a force-push's incremental delta reads as empty)." >&2
    ;;
esac

# Capture full output to a log AND mirror to stderr. `tee` writes to both;
# `set -o pipefail` ensures git push's non-zero exit propagates through the
# pipeline rather than being hidden by tee's exit (which is the bug this
# wrapper exists to prevent).
echo "safe-push: pushing $branch ($local_sha) to origin" >&2
push_status=0
set -o pipefail
# Capture git push's real exit code. `if ! cmd; then` would set $? to the
# negated value (0) inside the then-block, masking the actual failure --
# the exact silent-failure mode this wrapper exists to prevent.
if git push -u origin "$branch" ${push_args[@]+"${push_args[@]}"} 2>&1 | tee "$LOG" >&2; then
  push_status=0
else
  push_status=$?
fi
set +o pipefail

# Independent verification: read the remote ref directly. ls-remote bypasses
# any local cache (no `git fetch` needed) and returns the authoritative SHA
# from origin. A "successful" push that somehow didn't update the ref (the
# silent-failure mode this wrapper guards against) will show here.
remote_line=$(git ls-remote origin "refs/heads/$branch" 2>/dev/null || true)
remote_sha=${remote_line%%$'\t'*}

if [ "$push_status" -ne 0 ]; then
  echo "safe-push: git push exited $push_status -- see $LOG" >&2
  exit 1
fi

if [ -z "$remote_sha" ]; then
  echo "safe-push: git push exited 0 but origin has no '$branch' ref" >&2
  echo "          local HEAD: $local_sha" >&2
  echo "          full log:   $LOG" >&2
  exit 1
fi

if [ "$remote_sha" != "$local_sha" ]; then
  echo "safe-push: git push exited 0 but origin/'$branch' does not match local HEAD" >&2
  echo "          local:  $local_sha" >&2
  echo "          remote: $remote_sha" >&2
  echo "          full log: $LOG" >&2
  exit 1
fi

echo "safe-push: verified origin/$branch -> $remote_sha" >&2
exit 0
