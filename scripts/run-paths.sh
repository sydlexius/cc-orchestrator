#!/usr/bin/env bash
# run-paths.sh -- THE single producer of a worktree's run-artifact directory.
#
# SOURCE this file; do not execute it. It exports:
#
#   CC_RUN_ROOT          ${XDG_CACHE_HOME:-$HOME/.cache}/<repo-prefix>-run
#   CC_RUN_DIR           $CC_RUN_ROOT/<worktree-basename>-<sha12-of-recorded-path>
#   GOLANGCI_LINT_CACHE  $CC_RUN_DIR/golangci   (only when CC_RUN_DIR is non-empty)
#
# WHY THIS FILE EXISTS (#303). A SHARED consumer (cleanup-worktree.sh, which runs in
# every worktree of every repo) previously depended on a PER-REPO producer
# (<repo>/scripts/lib/run-paths.sh), with the path contract living only in a comment.
# The two sides disagreed: the producer created `<prefix>-run/<basename>-<sha12>`, the
# consumer removed `<prefix>-run/<basename>`. The removal never matched, and 170+ stale
# run dirs (922M) accumulated. When a value is derived in two places it WILL diverge;
# the fix is ONE producer, sourced by both sides, so drift becomes unrepresentable
# rather than merely detectable.
#
# TWO MODES:
#
#   CC_RUN_WORKTREE set (the CONSUMER / query mode)
#     The caller has already parsed a worktree path out of `git worktree list
#     --porcelain` and hands it over. That string is hashed VERBATIM: no porcelain
#     lookup, no realpath, no on-disk existence check. This is load-bearing, not an
#     optimization -- cleanup-worktree.sh must compute the SAME CC_RUN_DIR for a
#     worktree that has already been removed or pruned, and any lookup against the live
#     worktree list would yield nothing exactly then. A live-dir invocation and an
#     already-removed-dir invocation for the same recorded string are IDENTICAL.
#
#   CC_RUN_WORKTREE unset (the PRODUCER / dev mode)
#     Resolve the current worktree by matching $PWD against `git worktree list
#     --porcelain` and returning the RECORDED string. Deliberately NOT `git rev-parse
#     --show-toplevel`, which realpath-resolves: on macOS a worktree under /tmp comes
#     back as /private/tmp and hashes to a different identifier than the one git
#     recorded, reintroducing the very divergence this file exists to eliminate.
#     Resolving IS used for comparison (to find the right record); only the recorded
#     string is ever hashed.
#
# SOURCING CONTRACT (a sourced file runs in the CALLER'S shell):
#   - Never leaks `set -e` / `set -u` / `set -o pipefail` into the caller.
#   - Never aborts the sourcing shell. Every failure path degrades to an EMPTY
#     CC_RUN_DIR, which every consumer's `[ -d "$CC_RUN_DIR" ]` guard skips.
#   - Uses only namespaced locals (`_ccrp_*`), unset on the way out.
#
# DIRECTORY CREATION: created (0700) by default, because the dev-mode caller is about
# to write artifacts into it. Set CC_RUN_NO_MKDIR=1 to compute the path WITHOUT
# creating it -- what cleanup-worktree.sh does, since creating a directory only to
# remove it two lines later would be absurd (and would resurrect the dir for a
# worktree that no longer exists).
#
# DOWNSTREAM SHIM: a repo that had its own `scripts/lib/run-paths.sh` keeps a 2-line
# shim so nothing in its dev-restart/Makefile flow changes:
#     . "$HOME/.claude/scripts/run-paths.sh"
#     SW_RUN_DIR="$CC_RUN_DIR"; SW_RUN_ROOT="$CC_RUN_ROOT"; export SW_RUN_DIR SW_RUN_ROOT

CC_RUN_ROOT=""
CC_RUN_DIR=""

# _ccrp_sha12 <string> -- 12-char sha256 prefix of the string, or empty on any failure.
# Reads the string from stdin-free argv and never trails a newline into the hash.
_ccrp_sha12() {
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 2>/dev/null | awk '{print substr($1,1,12)}'
  elif command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum 2>/dev/null | awk '{print substr($1,1,12)}'
  fi
}

# _ccrp_main_worktree -- the repo's MAIN worktree path (porcelain emits it first).
#
# The `|| true` is LOAD-BEARING under a caller running `set -o pipefail`. Without it a
# `git` that exits 128 (not a repo, git missing, git broken) propagates as the
# PIPELINE's status -- awk exits 0, but pipefail takes the leftmost failure -- and the
# file-scope command substitution that calls this would then trip the caller's errexit
# and ABORT the sourcing shell, breaking this file's central promise. (The sibling
# _ccrp_current_worktree does not need it: it exits explicitly via its own
# `[ -n "$_ccrp_recs" ] || return 0`, and its call site is `||`-guarded regardless.)
_ccrp_main_worktree() {
  git worktree list --porcelain 2>/dev/null | awk '/^worktree / { print $2; exit }' || true
}

# _ccrp_current_worktree -- the RECORDED path string of the worktree containing $PWD.
#
# Matching strategy, in order (first hit wins):
#   1. exact / path-prefix match of $PWD against a recorded path (no resolution at all)
#   2. the same comparison with both sides physically resolved (`cd -P` + pwd -P), which
#      handles the /tmp -> /private/tmp case WITHOUT ever hashing a resolved path -- the
#      RECORDED string of the matching record is what is returned.
# Longest match wins so a nested worktree is not swallowed by its parent's prefix.
_ccrp_current_worktree() {
  local _ccrp_here _ccrp_here_p _ccrp_rec _ccrp_rec_p _ccrp_best="" _ccrp_recs
  _ccrp_here="$PWD"
  _ccrp_recs="$(git worktree list --porcelain 2>/dev/null | awk '/^worktree / { print substr($0, 10) }')"
  [ -n "$_ccrp_recs" ] || return 0

  while IFS= read -r _ccrp_rec; do
    [ -n "$_ccrp_rec" ] || continue
    case "$_ccrp_here" in
      "$_ccrp_rec"|"$_ccrp_rec"/*)
        [ "${#_ccrp_rec}" -gt "${#_ccrp_best}" ] && _ccrp_best="$_ccrp_rec" ;;
    esac
  done <<EOF
$_ccrp_recs
EOF
  if [ -n "$_ccrp_best" ]; then printf '%s' "$_ccrp_best"; return 0; fi

  # Fall back to a physically-resolved comparison. Resolution is used ONLY to decide
  # which record matches; the recorded string is what gets returned (and hashed).
  _ccrp_here_p="$(cd -P "$_ccrp_here" 2>/dev/null && pwd -P)"
  [ -n "$_ccrp_here_p" ] || return 0
  while IFS= read -r _ccrp_rec; do
    [ -n "$_ccrp_rec" ] || continue
    _ccrp_rec_p="$(cd -P "$_ccrp_rec" 2>/dev/null && pwd -P)"
    [ -n "$_ccrp_rec_p" ] || continue
    case "$_ccrp_here_p" in
      "$_ccrp_rec_p"|"$_ccrp_rec_p"/*)
        [ "${#_ccrp_rec}" -gt "${#_ccrp_best}" ] && _ccrp_best="$_ccrp_rec" ;;
    esac
  done <<EOF
$_ccrp_recs
EOF
  printf '%s' "$_ccrp_best"
}

# ---------------------------------------------------------------------------
# Resolve the worktree path string whose identifier we are producing.
# ---------------------------------------------------------------------------
# Every command substitution below is `|| <empty>`-guarded. These run at FILE SCOPE in the
# CALLER'S shell, where an unguarded failing substitution DOES abort an errexit caller --
# verified directly: sourcing a file containing `x="$(false)"` under `set -euo pipefail`
# kills the caller (rc=1), while `x="$(false)" || x=""` survives with x empty.
#
# Honest note on testability: only some of these guards can be independently proven by the
# harness, because the underlying commands rarely fail on their own (`_ccrp_sha12`, for one,
# returns 0 even with both hashers absent -- its if/elif simply falls through). They are kept
# uniformly because the COST is a few characters and the FAILURE MODE is killing a caller's
# shell. Do not remove one on the grounds that no test turns red; test silence here reflects
# a command that did not fail, not a guard that does nothing.
_ccrp_wt="${CC_RUN_WORKTREE:-}"
if [ -z "$_ccrp_wt" ]; then
  _ccrp_wt="$(_ccrp_current_worktree)" || _ccrp_wt=""
fi

_ccrp_prefix=""
_ccrp_base=""
_ccrp_id=""
if [ -n "$_ccrp_wt" ]; then
  _ccrp_main="$(_ccrp_main_worktree)" || _ccrp_main=""
  # An `if`, not `[ -n ... ] && _ccrp_prefix=...`. NOT for the errexit reason one might
  # assume: bash exempts the left operand of `&&` from errexit, and a failing AND-list at
  # non-terminal position does NOT abort (verified: `set -euo pipefail; [ -n "" ] && x=1;
  # echo hi` prints hi). The reason is plainer -- an `if` cannot silently become fatal if
  # this block is ever moved to the end of the file, where the AND-list's status WOULD
  # become the sourced file's status.
  if [ -n "$_ccrp_main" ]; then
    _ccrp_prefix="$(basename "$_ccrp_main")" || _ccrp_prefix=""
  fi
  _ccrp_base="$(basename "$_ccrp_wt")" || _ccrp_base=""
  _ccrp_id="$(_ccrp_sha12 "$_ccrp_wt")" || _ccrp_id=""
fi

# Every component must be present and sane. A "/" basename (from a path of "/") would
# collapse the run dir to `$CC_RUN_ROOT//`, silently merging artifacts from unrelated
# invocations into one bucket -- degrade to empty instead.
# The cache root. `${XDG_CACHE_HOME:-}` / `${HOME:-}` -- NEVER a bare `$HOME`. This runs at
# FILE SCOPE in the CALLER'S shell, so under `set -u` an unset HOME (cron, systemd User=
# without PAM, a CI runner, `env -i`) is an unbound-variable error that kills the SOURCING
# SHELL outright. A consumer's `. run-paths.sh || true` does NOT rescue it: `set -u` aborts the
# whole shell, not just the `.` builtin (verified). That would abort cleanup-worktree.sh at its
# source line -- ABOVE the worktree removal, the run-dir reclaim and the lint-cache clean --
# a strictly worse version of the #302 pathology this file exists to eliminate.
_ccrp_cache="${XDG_CACHE_HOME:-}"
if [ -z "$_ccrp_cache" ] && [ -n "${HOME:-}" ]; then
  _ccrp_cache="$HOME/.cache"
fi

# Every component must be present and sane. A "/" basename (from a path of "/") would
# collapse the run dir to `$CC_RUN_ROOT//`, silently merging artifacts from unrelated
# invocations into one bucket -- degrade to empty instead.
if [ -n "$_ccrp_cache" ] \
   && [ -n "$_ccrp_prefix" ] && [ "$_ccrp_prefix" != "/" ] \
   && [ -n "$_ccrp_base" ] && [ "$_ccrp_base" != "/" ] && [ -n "$_ccrp_id" ]; then
  CC_RUN_ROOT="$_ccrp_cache/${_ccrp_prefix}-run"
  CC_RUN_DIR="$CC_RUN_ROOT/${_ccrp_base}-${_ccrp_id}"
elif [ -z "$_ccrp_cache" ]; then
  echo "run-paths.sh: neither XDG_CACHE_HOME nor HOME is set; CC_RUN_DIR is empty." >&2
else
  echo "run-paths.sh: could not derive a run dir (worktree='${_ccrp_wt}'); CC_RUN_DIR is empty." >&2
fi

if [ -n "$CC_RUN_DIR" ] && [ "${CC_RUN_NO_MKDIR:-}" != "1" ]; then
  # 0700 before any caller writes secrets into it (cookie jars, coverage profiles, log
  # dumps). mkdir -p inherits the caller's umask, typically 0755, which would leave
  # these artifacts readable by other local users on a permissive home directory.
  if mkdir -p "$CC_RUN_DIR" 2>/dev/null; then
    chmod 700 "$CC_RUN_DIR" 2>/dev/null || true
  else
    echo "run-paths.sh: could not create '$CC_RUN_DIR'; CC_RUN_DIR is empty." >&2
    CC_RUN_DIR=""
    CC_RUN_ROOT=""
  fi
fi

# Per-worktree golangci-lint cache. This is what makes cross-worktree lint phantoms
# IMPOSSIBLE rather than merely cleaned-up-after: the shared default cache is keyed by
# content + file path, so entries referencing a REMOVED worktree stay valid lookups for
# runs in OTHER worktrees, which then report findings against paths that no longer
# exist (observed live in stillwater: a clean branch failed its gate with 9 issues, all
# resolving into a worktree deleted in a prior session). A per-worktree cache cannot be
# consulted by a different worktree at all. GOCACHE is deliberately untouched -- it is
# content-addressed, shared safely, and expensive to rebuild.
if [ -n "$CC_RUN_DIR" ]; then
  GOLANGCI_LINT_CACHE="$CC_RUN_DIR/golangci"
  export GOLANGCI_LINT_CACHE
fi

export CC_RUN_ROOT CC_RUN_DIR
unset _ccrp_wt _ccrp_main _ccrp_prefix _ccrp_base _ccrp_id _ccrp_cache
unset -f _ccrp_sha12 _ccrp_main_worktree _ccrp_current_worktree
