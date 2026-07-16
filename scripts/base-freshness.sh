#!/usr/bin/env bash
# base-freshness.sh <base> [head]   (issue #282)
#
# Report whether <head> is CURRENT with origin/<base>. GIT ONLY: it never calls `gh`, never
# mutates a remote, and never touches the working tree or index (its one network op is a
# read-only `git fetch`, which may update the local object DB + remote-tracking refs, nothing
# else).
#
# THE BASE IS ALWAYS CALLER-SUPPLIED. This script NEVER infers or hard-codes `main` - that is
# precisely what makes a backport / release / non-main base correct BY CONSTRUCTION: a backport
# branch is measured against its OWN declared base, so being behind `main` is never mistaken for
# staleness.
#
# NON-INTERACTIVE BY CONSTRUCTION: GIT_TERMINAL_PROMPT=0 (no HTTP credential prompt) plus SSH
# BatchMode (no SSH password / host-key prompt; a caller's own GIT_SSH_COMMAND is PRESERVED and
# `-o BatchMode=yes` appended LAST, so it OVERRIDES any caller BatchMode=no - the effective setting
# is always yes). An unreachable or auth-required origin therefore FAILS FAST instead of hanging a
# push path. (Same pattern as finding_channel.py.)
#
# Exactly ONE labeled `freshness:` line is printed on every path:
#   freshness: fresh   - <head> contains every commit on origin/<base> (0 behind).
#   freshness: behind  - <head> is N>0 commits behind origin/<base>; the line carries N and an
#                        ADDITIVE refresh pointer (merge / gh pr update-branch, never rebase).
#   freshness: unknown - could not be determined (unresolvable ref, fetch failure, shallow clone,
#                        not a git repo). The check DEGRADES; it never guesses.
#
# Usage:
#   base-freshness.sh <base> [head]
#
# Arguments:
#   base   The base branch NAME (e.g. main, release/1.2). Required. Compared as origin/<base>.
#   head   The ref to test (default: HEAD, i.e. the current branch). May be a remote-tracking
#          ref such as origin/<branch> when checking a branch you do not have checked out.
#
# Exit codes:
#   0  FRESH (0 behind) OR UNKNOWN. Best-effort degradation NEVER blocks a caller: an
#      undeterminable answer is reported, not enforced.
#   1  BEHIND - definitively resolved: <head> is N>0 commits behind origin/<base>.
#   2  Usage / malformed invocation (missing base, unknown flag, extra argument).
set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

# --- Parse arguments (strict: a malformed invocation is the ONLY exit-2 path) ---
case "${1:-}" in
  -*) echo "base-freshness: unknown flag '$1'" >&2
      echo "usage: base-freshness.sh <base> [head]" >&2
      exit 2 ;;
esac
base="${1:-}"
if [ -z "$base" ]; then
  echo "base-freshness: missing <base> (it is ALWAYS caller-supplied; this script never guesses 'main')" >&2
  echo "usage: base-freshness.sh <base> [head]" >&2
  exit 2
fi
# Validate the caller-supplied base as a real branch name BEFORE any fetch. This keeps the helper
# READ-ONLY: a refspec-shaped value (e.g. 'main:refs/heads/other') would otherwise reach `git fetch`
# and UPDATE local refs. One check covers both the fetch and the later rev-list use of $base.
if ! git check-ref-format "refs/heads/$base" >/dev/null 2>&1; then
  echo "base-freshness: invalid base branch name '$base' (must be a plain branch name, not a refspec)" >&2
  echo "usage: base-freshness.sh <base> [head]" >&2
  exit 2
fi
head_ref="${2:-HEAD}"
if [ "$#" -gt 2 ]; then
  echo "base-freshness: unexpected extra argument '$3'" >&2
  echo "usage: base-freshness.sh <base> [head]" >&2
  exit 2
fi

# --- Non-interactive git (fail fast, never hang) ---
export GIT_TERMINAL_PROMPT=0
# GIT_TERMINAL_PROMPT=0 stops HTTP credential prompts but NOT an SSH password / host-key prompt -
# that needs BatchMode. Preserve a caller's custom GIT_SSH_COMMAND (identity/port/wrapper) but ALWAYS
# append `-o BatchMode=yes` LAST: ssh honors the LAST `-o` for a given option, so this overrides any
# earlier BatchMode=no the caller set and guarantees the EFFECTIVE setting is non-interactive.
if [ -n "${GIT_SSH_COMMAND:-}" ]; then
  GIT_SSH_COMMAND="$GIT_SSH_COMMAND -o BatchMode=yes"
else
  GIT_SSH_COMMAND="ssh -o BatchMode=yes"
fi
export GIT_SSH_COMMAND

unknown() {
  echo "freshness: unknown - $1"
  exit 0
}

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  unknown "not a git repository; freshness not determined"
fi

# A shallow clone cannot see the base history, so any count it produces is a lie. Say so.
if [ "$(git rev-parse --is-shallow-repository 2>/dev/null || echo unknown)" = "true" ]; then
  unknown "shallow clone - origin/$base history is not present locally (run 'git fetch --unshallow'); freshness not determined"
fi

# Best-effort fetch of the caller's base. A FAILURE IS TOLERATED (never fatal) but it DOWNGRADES the
# answer to unknown rather than measuring against a possibly-stale remote-tracking ref: reporting a
# confident "fresh" off stale data is the one wrong answer this check must never give.
if ! git fetch origin "$base" --quiet >/dev/null 2>&1; then
  unknown "could not fetch origin/$base (offline, auth-required, or no such base); freshness not determined"
fi

if ! git rev-parse --verify --quiet "origin/$base^{commit}" >/dev/null 2>&1; then
  unknown "cannot resolve origin/$base; freshness not determined"
fi
if ! git rev-parse --verify --quiet "$head_ref^{commit}" >/dev/null 2>&1; then
  unknown "cannot resolve head '$head_ref'; freshness not determined"
fi

# Commits on origin/<base> that <head> does NOT contain = how far behind <head> is.
behind="$(git rev-list --count "$head_ref..origin/$base" 2>/dev/null || true)"
case "$behind" in
  ''|*[!0-9]*) unknown "could not count commits between '$head_ref' and origin/$base; freshness not determined" ;;
esac

if [ "$behind" -eq 0 ]; then
  echo "freshness: fresh - '$head_ref' is up to date with origin/$base (0 behind)"
  exit 0
fi

echo "freshness: behind - '$head_ref' is $behind commit(s) behind origin/$base; refresh it before merge with an ADDITIVE update that PRESERVES the reviewed commit SHAs: merge the base into the branch in its own worktree ('git merge origin/$base'), or for an OPEN PR use the server-side 'gh pr update-branch <n>' (DEFAULT merge-commit mode; a history-rewrite would orphan the fix SHAs cited in review replies)"
exit 1
