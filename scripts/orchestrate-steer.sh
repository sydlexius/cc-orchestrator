#!/usr/bin/env bash
# orchestrate-steer.sh - WARN-level PreToolUse steering (advisory), SEPARATE from the hard-deny
# floor (orchestrate-guard.sh). Exit 0 ALWAYS - it NEVER blocks; it only emits a one-line steer to
# stderr when a rule matches, so Claude sees the nudge but the action still proceeds. Keeping it a
# distinct script preserves the floor's integrity (the guard stays pure hard-deny) and lets the
# steering be disabled (`configure --no-steer`) without touching deny logic.
#
# Two rules (#95):
#   (1) MID-RUN CANONICAL EDIT (marker-gated): an Edit/Write whose target resolves to a canonical
#       file (SKILL.md, templates/*, orchestrate-guard.sh, orchestrate-steer.sh) while THIS session's
#       orchestrate marker is fresh -> WARN: log feedback to the mailbox, do not edit mid-run.
#       Enforces [[orchestrate-no-mid-run-canonical-edits]]. Marker-gated so a teammate editing those
#       files in its OWN worktree during a sanctioned PR build (a different $TMUX key, so no marker)
#       is NOT warned - only the lead editing them mid-run in the marker-active session is.
#   (2) RAW GH-API MUTATION -> WRAPPER: a Bash command invoking `gh api` with a MUTATION flag
#       (-X/--method, -f/-F/--field/--raw-field/--input) directly on the command line and NOT via a
#       gh-* wrapper script -> WARN: use the gh-* wrapper. Marker-independent (steer every session).
#
# These COMPLEMENT the guard's denies; they NEVER duplicate or weaken them (all WARN, exit 0). The
# guard already DENIES push-to-main, bare force, --no-verify, gh --admin, and marker-gated merge;
# this script touches none of those paths. Fails SILENT-OPEN (exit 0, no warn) on any internal error
# - it is advisory only, so a broken steer must never block a tool call.
set -u

FLOOR_DIR="${ORCHESTRATE_FLOOR_DIR:-$HOME/.claude/orchestrate-floor.d}"
TTL_HOURS="${ORCHESTRATE_FLOOR_TTL_HOURS:-72}"
# Reject a non-positive-integer TTL (mirrors the guard) so a typo'd override cannot silently
# disarm the marker gate; fall back to the 72h default.
case "$TTL_HOURS" in ''|*[!0-9]*) TTL_HOURS=72 ;; esac
[ "$TTL_HOURS" -ge 1 ] 2>/dev/null || TTL_HOURS=72

emit_warn() {
  printf 'STEER: %s\n' "$1" >&2
  exit 0
}

# --- self-test: feed a raw `gh api` mutation payload (marker-INDEPENDENT rule) and assert a WARN
# is emitted at exit 0. Used by setup/doctor to catch a silently broken steer. Prints PASS/FAIL.
if [ "${1:-}" = "--self-test" ]; then
  st_out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"gh api -X PATCH repos/o/r/issues/1"}}' \
    | "$0" 2>&1)
  st_rc=$?
  if [ "$st_rc" -eq 0 ] && printf '%s' "$st_out" | grep -q 'STEER'; then
    echo "orchestrate-steer self-test PASS (raw gh-api mutation warned, exit 0)"
    exit 0
  fi
  echo "orchestrate-steer self-test FAIL: expected a STEER warn at exit 0, got rc=$st_rc out=$st_out" >&2
  exit 1
fi

# --- read the payload: stdin JSON first, then $TOOL_INPUT env, else fail OPEN (exit 0, no warn) ---
tool_input_json=""
stdin_json=""
if [ ! -t 0 ]; then
  stdin_json=$(cat 2>/dev/null)
fi
if [ -n "$stdin_json" ]; then
  tool_input_json=$(printf '%s' "$stdin_json" | jq -c '.tool_input // empty' 2>/dev/null)
fi
if [ -z "$tool_input_json" ] && [ -n "${TOOL_INPUT:-}" ]; then
  tool_input_json="$TOOL_INPUT"
fi
[ -z "$tool_input_json" ] && exit 0

file_path=$(printf '%s' "$tool_input_json" | jq -r '.file_path // empty' 2>/dev/null)
cmd=$(printf '%s' "$tool_input_json" | jq -r '.command // empty' 2>/dev/null)

# --- rule helpers ----------------------------------------------------------
# A canonical file: the skill playbook, any per-role template, or a floor/steer hook script. Resolve
# symlinks first (readlink -f) so both the repo path and a legacy ~/.claude/skills symlink match.
is_canonical_path() {
  local p resolved base
  p="$1"
  resolved=$(readlink -f -- "$p" 2>/dev/null || printf '%s' "$p")
  base=$(basename -- "$resolved")
  case "$base" in
    orchestrate-guard.sh|orchestrate-steer.sh) return 0 ;;
  esac
  case "$resolved" in
    */skills/orchestrate/SKILL.md) return 0 ;;
    */skills/orchestrate/templates/*) return 0 ;;
  esac
  return 1
}

# A raw `gh api` MUTATION on the command line, NOT routed through a gh-* wrapper. Requires the `gh`
# word, the `api` subcommand, and a mutation flag. A wrapper-ALONE invocation (e.g. `gh-comment.sh
# 5 hi`) is already silent because the bare-`gh` check requires `gh` followed by space/EOL, and the
# char after `gh` in `gh-comment.sh` is `-`, not a boundary - so no global gh-*.sh exemption is
# needed (and a blanket exemption is WRONG: in a compound `gh-comment.sh ... && gh api -X PATCH ...`
# a bare `gh api` mutation IS present and must still warn).
# Separator-tolerant (space/=/glued), mirroring the guard's is_merge_api flag matching.
is_raw_gh_api_mutation() {
  local c="$1"
  printf '%s' "$c" | grep -Eq '(^|[^[:alnum:]_-])gh([[:space:]]|$)' || return 1
  printf '%s' "$c" | grep -Eq '(^|[[:space:]])api([[:space:]]|$)' || return 1
  printf '%s' "$c" | grep -Eq '(--method[[:space:]=]|-X[[:space:]=]?[A-Za-z])' && return 0
  printf '%s' "$c" | grep -Eq '(^|[[:space:]])(--(field|input|raw-field)[[:space:]=]|-[fF][[:space:]=]?[^[:space:]])' && return 0
  return 1
}

# THIS session's marker present AND fresh. Verbatim mirror of the guard's marker_active so the two
# sides never drift (keyed by sanitized $TMUX; non-tmux/solo is never gated; GNU stat then BSD).
marker_active() {
  [ -n "${TMUX:-}" ] || return 1
  local key marker mtime now age_h
  key=$(printf '%s' "$TMUX" | LC_ALL=C tr -c 'A-Za-z0-9' '_')
  marker="$FLOOR_DIR/$key"
  [ -f "$marker" ] || return 1
  mtime=$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker" 2>/dev/null) || return 1
  now=$(date +%s) || return 1
  age_h=$(( (now - mtime) / 3600 ))
  [ "$age_h" -lt "$TTL_HOURS" ]
}

# --- dispatch (at most one rule fires; a tool call carries a file_path XOR a command) -------------
# (1) canonical-edit WARN: marker-gated.
if [ -n "$file_path" ] && is_canonical_path "$file_path" && marker_active; then
  emit_warn "Canonical symlinked file - log skill/charter/guard feedback via orchestrate-feedback.sh add (~/.claude/orchestrate-feedback/) and triage via PR; do not edit mid-run."
fi

# (2) raw gh-api mutation WARN: marker-independent.
if [ -n "$cmd" ] && is_raw_gh_api_mutation "$cmd"; then
  emit_warn "Use the gh-* wrapper (gh-api-get.sh / gh-comment.sh / gh-codeql-dismiss.sh / gh-codeql-autofix.sh / gh-resolve-thread.sh / gh-delete-branch.sh) instead of raw gh api."
fi

exit 0
