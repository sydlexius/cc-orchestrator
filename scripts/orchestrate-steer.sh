#!/usr/bin/env bash
# orchestrate-steer.sh - WARN-level PreToolUse steering (advisory), SEPARATE from the hard-deny
# floor (orchestrate-guard.sh). Exit 0 ALWAYS - it NEVER blocks; it only emits a one-line steer to
# stderr when a rule matches, so Claude sees the nudge but the action still proceeds. Keeping it a
# distinct script preserves the floor's integrity (the guard stays pure hard-deny) and lets the
# steering be disabled (`configure --no-steer`) without touching deny logic.
#
# Rules (#95, #159, #226, #231, #284):
#   (1) MID-RUN CANONICAL EDIT (marker-gated): an Edit/Write whose target resolves to a canonical
#       file while THIS session's orchestrate marker is fresh -> WARN: log feedback to the mailbox,
#       do not edit mid-run. CANONICAL = SKILL.md, templates/*, orchestrate-guard.sh,
#       orchestrate-steer.sh, PLUS (#284) the Option-A-DEPLOYED helpers (HELPER_NAMES), the rest of
#       the floor fileset (orchestrate-authorize-merge.sh) and commands/*.md - their omission left a
#       mid-run safe-push.sh edit SILENT, which is the exact miss (#283) that motivated this rule. Enforces [[orchestrate-no-mid-run-canonical-edits]].
#       ACCEPTED FP (do not "fix" by weakening the rule): a TEAMMATE legitimately implementing an
#       assigned change to one of these files in its OWN worktree may ALSO see this WARN, because
#       tmux panes of one session share $TMUX and therefore see the same marker. Advisory-only, so the
#       cost is a nudge on legitimate work, never a block; #284 widened the file set, which widens this
#       FP too. (An earlier version of this header asserted a teammate has "a different $TMUX key, so
#       no marker" - that is NOT established and is probably false; the honest statement is here.)
#       ACCEPTED FP (2): the `*/commands/*.md` glob matches ANY repo's commands/ dir, so a
#       marker-active lead editing a TARGET repo's own commands/*.md draws a spurious nudge. Advisory;
#       tightening it to known basenames would miss a newly-added command - accepted.
#       Gated OFF
#       for a `Read` tool call (a Read carries a file_path too) so wiring the hook for Read never
#       turns reading a canonical file into a spurious "do not edit" nag.
#   (2) RAW GH-API MUTATION -> WRAPPER: a Bash command invoking `gh api` with a MUTATION flag
#       (-X/--method, -f/-F/--field/--raw-field/--input) directly on the command line and NOT via a
#       gh-* wrapper script -> WARN: use the gh-* wrapper. Marker-independent (steer every session).
#   (3) RAW GH PR comment/create -> CANONICAL PATH: `gh pr comment`/`gh pr create` on the command
#       line -> WARN toward reply-comment.sh/gh-comment.sh / /prep-pr. Marker-independent (#159).
#   (4) REDUNDANT RE-READ -> WARN (#226): a 2nd+ `Read` of a path already read THIS session with an
#       unchanged mtime+size -> WARN: the content is already in context, skip the Read. Stateful
#       (per-session, keyed on the stdin session_id), marker-independent, advisory only. The valid
#       exception (post-compaction re-read) is why this is a WARN and never a deny.
#   (5) FOREGROUND-AGENT CONTAINMENT (marker-gated, #231): an `Agent` spawned with an EXPLICIT
#       run_in_background:false -> WARN: name it AND omit the flag (both halves), because a foreground Agent
#       BLOCKS the lead console for its entire run. Type-EXACT (absent != false; see
#       is_foreground_agent) and marker-gated. THREE accepted limitations are documented at the rule.
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

# --- self-test: feed the marker-INDEPENDENT command rules and assert each emits a WARN at exit 0.
# Used by setup/doctor to catch a silently broken steer. Prints PASS/FAIL.
if [ "${1:-}" = "--self-test" ]; then
  st_fail=""
  # (2) raw gh-api mutation must WARN at exit 0.
  st_out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"gh api -X PATCH repos/o/r/issues/1"}}' \
    | "$0" 2>&1); st_rc=$?
  { [ "$st_rc" -eq 0 ] && printf '%s' "$st_out" | grep -q 'STEER'; } \
    || st_fail="gh-api rule (rc=$st_rc out=$st_out)"
  # (3a) raw gh pr comment mutation must WARN at exit 0.
  if [ -z "$st_fail" ]; then
    st_out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"gh pr comment 5 -b hi"}}' \
      | "$0" 2>&1); st_rc=$?
    { [ "$st_rc" -eq 0 ] && printf '%s' "$st_out" | grep -q 'STEER'; } \
      || st_fail="gh-pr rule (comment) (rc=$st_rc out=$st_out)"
  fi
  # (3b) raw gh pr create mutation must WARN at exit 0 (so the PASS message's "create" claim is real).
  if [ -z "$st_fail" ]; then
    st_out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"gh pr create --fill"}}' \
      | "$0" 2>&1); st_rc=$?
    { [ "$st_rc" -eq 0 ] && printf '%s' "$st_out" | grep -q 'STEER'; } \
      || st_fail="gh-pr rule (create) (rc=$st_rc out=$st_out)"
  fi
  # (4) read-dedup: a 2nd Read of an unchanged path (same session) must WARN at exit 0; the 1st is
  # silent. Uses an isolated temp state dir + file so the self-test never touches real read state.
  if [ -z "$st_fail" ]; then
    st_tmp=$(mktemp -d 2>/dev/null) || st_tmp=""
    if [ -n "$st_tmp" ]; then
      st_f="$st_tmp/f"; : > "$st_f"
      st_payload='{"tool_name":"Read","session_id":"selftest","tool_input":{"file_path":"'"$st_f"'"}}'
      # 1st read MUST be silent (asserted, not discarded - else a "1st read warns" regression would
      # slip through and the PASS message would be misleading).
      st_out1=$(printf '%s' "$st_payload" | ORCHESTRATE_READ_STATE_DIR="$st_tmp/state" "$0" 2>&1); st_rc1=$?
      { [ "$st_rc1" -eq 0 ] && ! printf '%s' "$st_out1" | grep -q 'STEER'; } \
        || st_fail="read-dedup rule 1st-read-not-silent (rc=$st_rc1 out=$st_out1)"
      # 2nd read of the unchanged path MUST warn at exit 0.
      if [ -z "$st_fail" ]; then
        st_out=$(printf '%s' "$st_payload" | ORCHESTRATE_READ_STATE_DIR="$st_tmp/state" "$0" 2>&1); st_rc=$?
        { [ "$st_rc" -eq 0 ] && printf '%s' "$st_out" | grep -q 'STEER'; } \
          || st_fail="read-dedup rule (rc=$st_rc out=$st_out)"
      fi
      rm -rf "$st_tmp" 2>/dev/null
    else
      # mktemp failed: do NOT let the PASS line falsely claim the read-dedup sub-check ran.
      st_fail="read-dedup rule (mktemp -d failed; sub-check could not run)"
    fi
  fi
  if [ -z "$st_fail" ]; then
    echo "orchestrate-steer self-test PASS (raw gh-api + raw gh pr comment/create mutations + read-dedup warned, exit 0)"
    exit 0
  fi
  echo "orchestrate-steer self-test FAIL: expected a STEER warn at exit 0, got $st_fail" >&2
  exit 1
fi

# --- read the payload: stdin JSON first, then $TOOL_INPUT env, else fail OPEN (exit 0, no warn) ---
tool_input_json=""
stdin_json=""
if [ ! -t 0 ]; then
  stdin_json=$(cat 2>/dev/null)
fi
# tool_name + session_id live at the stdin TOP LEVEL (not inside tool_input), so they are available
# only via the real PreToolUse stdin payload - the $TOOL_INPUT env fallback carries neither, which is
# fine: the read-dedup rule (which needs both) simply cannot fire on that channel (fail-open).
tool_name=""
session_id=""
if [ -n "$stdin_json" ]; then
  tool_input_json=$(printf '%s' "$stdin_json" | jq -c '.tool_input // empty' 2>/dev/null)
  tool_name=$(printf '%s' "$stdin_json" | jq -r '.tool_name // empty' 2>/dev/null)
  session_id=$(printf '%s' "$stdin_json" | jq -r '.session_id // empty' 2>/dev/null)
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
  # (#284) The Option-A-DEPLOYED helpers and the slash commands are canonical-source files by the
  # SAME argument as the guard: the repo is the source, they are deployed to a stable path, and a
  # mid-run edit of the working copy silently mutates canonical source while racing the in-flight PR.
  # Reproduced before this fix: a marker-active edit of safe-push.sh was SILENT, so the ONE mechanism
  # whose purpose is to say "log feedback, do not edit mid-run" missed the exact file that motivated
  # the rule (a lead edited safe-push.sh mid-run instead of filing the idea; #283).
  # Matched under a `scripts/` or `commands/` PARENT, so a same-named file elsewhere is not swallowed.
  #
  # Checked against BOTH the raw path AND the readlink-resolved path: a LEGACY claude-kit SYMLINK
  # (repo `scripts/safe-push.sh` -> `~/kit/safe-push.sh`) resolves AWAY from any `scripts/` parent, so
  # a resolved-only test goes silent on exactly the symlink layout readlink -f exists to handle. The
  # guard/steer entries above dodge this by matching on BASENAME; these are parent-anchored, so they
  # need both candidates.
  #
  # The helper list is LOCKSTEPPED to orchestrate-setup.py's HELPER_NAMES (the Option-A deployed set)
  # by a test-orchestrate-steer.py regression case. Do NOT hand-extend one without the other: this
  # list initially drifted 4 helpers behind that set, leaving a mid-run `issue-watch.sh` edit silent -
  # the very bug (#283/#284) this rule closes, for a different file.
  # The set is a SUPERSET of orchestrate-setup.py's HELPER_NAMES (the 15 Option-A-deployed helpers),
  # and deliberately so - be precise about which, because an earlier comment here over-claimed
  # "exactly HELPER_NAMES" and was wrong:
  #   - every HELPER_NAMES entry (a lockstep test pins this; adding a helper without adding it here
  #     is a test failure, which is how the first cut of this list drifted 4 helpers behind),
  #   - the deployed hook/CLI scripts (guard + steer match by BASENAME above; context-meter + setup),
  #   - orchestrate-authorize-merge.sh - on the FLOOR-FILESET ground, NOT the deployed ground. It is
  #     NOT in HELPER_NAMES and has no stable-path copy (a round-6 review caught an earlier version of
  #     this comment falsely calling it "deployed"). It is in because it ARMS the merge-auth token the
  #     deterministic FLOOR trusts to allow a merge - the repo's own floor fileset (the adversarial-prep
  #     charter) is guard + steer + authorize-merge - which makes it the most security-relevant non-guard
  #     script here. Backstopping pr-read-comments.sh while leaving THIS out was backwards.
  #   - the WHOLE `gh-*.sh` / `pr-*.sh` wrapper families, not just the deployed ones. Intentional: they
  #     are canonical plugin source by the same argument, and a future `pr-foo.sh` is covered on day 1,
  #   - `commands/*.md`.
  # THE MEMBERSHIP RULE, stated honestly. A file is canonical if a mid-run edit of it would mutate
  # CANONICAL PLUGIN SOURCE the live session depends on. That is THREE categories, not one - be exact,
  # because two earlier versions of this comment were WRONG (one claimed "exactly HELPER_NAMES", the
  # next claimed "the DEPLOYED set", and neither described the actual set):
  #   (i)   the Option-A-DEPLOYED set: HELPER_NAMES (15) + the deployed hook/CLI scripts;
  #   (ii)  the FLOOR fileset: guard + steer + authorize-merge (authorize-merge is NOT deployed - it is
  #         in on floor grounds alone, because it arms the merge-auth token the floor trusts);
  #   (iii) the remaining canonical PLUGIN SOURCE surfaces the session loads: the WHOLE `gh-*.sh` /
  #         `pr-*.sh` wrapper families (not just the deployed ones) and `commands/*.md`.
  # NOT canonical: an ordinary repo script that is in NONE of the three (an existing harness case pins
  # orchestrate-resources.py that way, and it is right).
  # First arm = the named HELPER_NAMES entries not already covered by the pr-*/gh-* globs, plus the
  # deployed hook/CLI scripts (guard + steer match by basename above). Second arm = the pr-*/gh-*
  # families (pr-watch, pr-unreplied-comments, pr-read-comments, pr-codeql-autofixes, gh-react, ...).
  # NOTE: no inline comments inside the pattern list - a `#` inside a continued case pattern is a
  # bash SYNTAX ERROR (shellcheck SC1009), which shellcheck caught here.
  local cand
  for cand in "$p" "$resolved"; do
    case "$cand" in
      */scripts/reply-comment.sh|*/scripts/resolve-threads.sh|*/scripts/cleanup-worktree.sh|\
      */scripts/patch-coverage.sh|*/scripts/safe-push.sh|*/scripts/gate-runner.py|\
      */scripts/pre-push-hook.sh|*/scripts/prefs-coverage.py|*/scripts/issue-watch.sh|\
      */scripts/ship-gate-preflight.sh|*/scripts/orchestrate-context-meter.sh|\
      */scripts/orchestrate-setup.py|*/scripts/orchestrate-authorize-merge.sh)
        return 0 ;;
      */scripts/pr-*.sh|*/scripts/gh-*.sh) return 0 ;;
      */commands/*.md) return 0 ;;
    esac
  done
  return 1
}

# (#231) An EXPLICIT foreground Agent spawn. Keyed on the exact shape, never on falsiness.
#
# THE TRAP (from the #221 spike's 45 captured live payloads): `run_in_background` is ABSENT, not
# `false`, when the caller omits it - and an Agent DEFAULTS TO BACKGROUND. Of those 45 spawns, 13
# omitted the field entirely while running backgrounded and legal. So a naive `if not
# run_in_background` check would warn on 28 of 45 spawns and be WRONG on 13 of them. Demand the
# EXACT shape (a literal `false`); anything else - absent, true, malformed, non-Agent - is SILENT.
# This is the floor-matcher lesson applied to an advisory rule: deny-on-doubt becomes silent-on-doubt.
is_foreground_agent() {
  # TYPE-EXACT by construction: `== false` matches ONLY a JSON boolean false. An absent key is null
  # (not false), the string "false" is not false, 0 is not false, and a non-object input makes jq -e
  # exit nonzero. So absent / true / "false" / 0 / malformed / missing-jq all fall to SILENT, and only
  # the one sanctioned shape warns.
  #
  # NOTE the trap this avoids: `// empty` is UNUSABLE here (jq's alternative operator treats a literal
  # `false` as empty and would erase the very value we test for), and a shell falsy check would be
  # WORSE - `run_in_background` is ABSENT, not false, when omitted, and an Agent DEFAULTS TO BACKGROUND,
  # so 13 of the 45 live spawns the #221 spike captured were legal background agents with no field at
  # all. A falsy check would have warned on all of them.
  printf '%s' "$1" | jq -e '.run_in_background == false' >/dev/null 2>&1
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

# A raw `gh pr` SUBCOMMAND that has a real canonical alternative, run directly on the command line
# (#159). This is canonical-STEERING, NOT creep prevention: every high-traffic gh pr subcommand is
# already allow-listed (so it never prompts), and a hook cannot intercept the "always allow" click -
# so warning the allow-listed set buys nothing. We nudge ONLY the two subcommands with a canonical
# target: `comment` (-> reply-comment.sh / gh-comment.sh) and `create` (-> /prep-pr). DELIBERATELY
# EXCLUDES `merge` (floor-denied in a marker session, AND the sanctioned prompt-free path in solo -
# a nag there is wrong), plus `edit`/`ready`/`close`/`review` and all reads (allow-listed lifecycle
# or no canonical redirect). A wrapper-ALONE invocation (gh-comment.sh, reply-comment.sh) is already
# silent: the bare-`gh` check needs `gh` + space/EOL and the char after `gh` in those names is `-`;
# likewise `comment`/`create` inside a wrapper name is not space-delimited. ACCEPTED false-positive
# (mirrors is_raw_gh_api_mutation's F30 class): a gh pr READ compounded with a standalone
# `comment`/`create` word in an arg trips the whole-line grep - harmless (advisory WARN, exit 0).
is_raw_gh_pr_mutation() {
  local c="$1"
  printf '%s' "$c" | grep -Eq '(^|[^[:alnum:]_-])gh([[:space:]]|$)' || return 1
  printf '%s' "$c" | grep -Eq '(^|[[:space:]])pr([[:space:]]|$)' || return 1
  printf '%s' "$c" | grep -Eq '(^|[[:space:]])(comment|create)([[:space:]]|$)' && return 0
  return 1
}

# #312: EVERY key this session could have armed under, first-precedence first. Mirrors the guard's
# _session_keys() exactly. See the DERIVATION REGISTRY in orchestrate-guard.sh: SIX live copies of
# this derivation exist and must move together.
_sanitize_key() {
  printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9' '_'
}

_session_keys() {
  local key found=0
  if [ -n "${TMUX:-}" ]; then
    key=$(_sanitize_key "$TMUX") || return 1
    if [ -n "$key" ]; then printf '%s\n' "$key"; found=1; fi
  fi
  if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
    key=$(_sanitize_key "$CLAUDE_CODE_SESSION_ID") || return 1
    if [ -n "$key" ]; then printf 'ccsid_%s\n' "$key"; found=1; fi
  fi
  [ "$found" -eq 1 ]
}

# THIS session's marker present AND fresh. Mirrors the guard's marker_active so the two sides never
# drift (GNU stat then BSD). #312: the session key is the sanitized $TMUX when set, AND/OR
# `ccsid_` + the sanitized $CLAUDE_CODE_SESSION_ID - tmux is NOT required for a gated session, so
# checking only $TMUX made every steer rule silently NO-OP for the whole non-tmux mode (rules 1 and 5
# are marker-gated). Like the guard, match ANY candidate key: an arm-side/check-side scheme
# disagreement must not silently drop the nudge. Only a session with NEITHER identifier is unkeyed.
marker_active() {
  local key marker mtime now age_h
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    marker="$FLOOR_DIR/$key"
    [ -f "$marker" ] || continue
    mtime=$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker" 2>/dev/null) || continue
    now=$(date +%s) || return 1
    age_h=$(( (now - mtime) / 3600 ))
    if [ "$age_h" -lt "$TTL_HOURS" ]; then
      return 0
    fi
  done <<EOF
$(_session_keys)
EOF
  return 1
}

# A redundant re-Read: a 2nd+ Read of a path already read THIS session whose mtime+size are unchanged
# (so the content is already in-context; the harness itself prints "file state is current"). Stateful,
# per-session, keyed on the stdin session_id - a different mechanism than the stateless command grep.
# Returns 0 (warn) ONLY on an unchanged repeat; records the fingerprint every time. Cheap by design:
# mtime+size, never a content hash (hashing every read file on the hot path would add per-call latency
# for no dedup gain - an mtime bump already means the content changed). Fails-open (silent) on any
# missing input, a stat failure (nonexistent/unreadable path), or a state-write failure.
READ_STATE_DIR="${ORCHESTRATE_READ_STATE_DIR:-${TMPDIR:-/tmp}/orchestrate-read-state}"
is_redundant_reread() {
  local p="$1" sid="$2" fp sess_key sess_dir key rec prior
  [ -n "$p" ] && [ -n "$sid" ] || return 1
  # Fingerprint = "mtime size"; a stat failure (missing/unreadable) means we cannot dedup -> silent.
  # ACCEPTED LIMITATION (F30-class, fail-SAFE): stat mtime is 1-second granular on both GNU (%Y) and
  # BSD (%m), so a file MODIFIED within the same wall-clock second as a prior read - then re-read -
  # keeps an unchanged fingerprint and draws a SPURIOUS advisory WARN. Harmless (a nudge, never a
  # deny, never data loss) and vanishingly rare (real edits/rebuilds land seconds later); sub-second
  # precision is not portable across GNU/BSD, so this is documented rather than chased.
  fp=$(stat -c '%Y %s' -- "$p" 2>/dev/null || stat -f '%m %z' -- "$p" 2>/dev/null) || return 1
  [ -n "$fp" ] || return 1
  # Per-session dir keyed on a sanitized session_id; per-path file keyed on a cksum of the path
  # (collision-tolerant: a rare clash only ever mutes/mis-fires an ADVISORY warn).
  sess_key=$(printf '%s' "$sid" | LC_ALL=C tr -c 'A-Za-z0-9' '_')
  sess_dir="$READ_STATE_DIR/$sess_key"
  # No in-hook prune (hostile-review #1): the state store carries NO recursive-delete path. Each
  # entry is a ~15-byte fingerprint file under a per-session dir in ${TMPDIR:-/tmp}, which the OS
  # reaps; active pruning of sibling dirs would be a destructive footgun (a mis-pointed
  # ORCHESTRATE_READ_STATE_DIR could delete unrelated files) that buys negligible hygiene for a
  # tiny, tmp-resident, self-limiting store. So we only ever create our own session dir, never
  # delete anything.
  # PREDICTABLE-TEMP-PATH HARDENING (CR): the default lives under the world-writable shared /tmp, so
  # create each level owner-only (-m 700) and REFUSE to write into a dir we do not own (-O) - defends
  # against a local attacker pre-creating or symlinking `orchestrate-read-state` to redirect the
  # fingerprint writes. `-m` is applied per level (not `-p -m`, which SC2174-flags as ignoring
  # intermediates); a custom deep ORCHESTRATE_READ_STATE_DIR with missing parents simply fails open
  # (no dedup) rather than creating loose-permissioned intermediates. Fail-open (return 1 -> silent).
  # `-m 700` only applies on CREATE; a PRE-EXISTING dir we own could still be group/other-writable
  # (created earlier under a permissive umask), which -O would not catch and which lets a group member
  # symlink/clobber inside. So after verifying ownership, ENFORCE 700 with chmod on every level (CR/
  # Codoki review-round: never operate in a group/other-writable state dir). All steps fail-open.
  mkdir -m 700 "$READ_STATE_DIR" 2>/dev/null
  [ -d "$READ_STATE_DIR" ] && [ -O "$READ_STATE_DIR" ] && chmod 700 "$READ_STATE_DIR" 2>/dev/null || return 1
  mkdir -m 700 "$sess_dir" 2>/dev/null
  [ -d "$sess_dir" ] && [ -O "$sess_dir" ] && chmod 700 "$sess_dir" 2>/dev/null || return 1
  key=$(printf '%s' "$p" | cksum | cut -d' ' -f1)
  rec="$sess_dir/$key"
  prior=""
  [ -f "$rec" ] && prior=$(cat -- "$rec" 2>/dev/null)
  # Record the current fingerprint for next time (idempotent; identical write on a repeat).
  printf '%s' "$fp" > "$rec" 2>/dev/null || return 1
  # Warn only when this exact fingerprint was already on record (a prior unchanged read this session).
  [ -n "$prior" ] && [ "$prior" = "$fp" ]
}

# --- dispatch (at most one rule fires; a tool call carries a file_path XOR a command) -------------
# (4) read-dedup WARN: only a `Read` tool call, marker-independent. Evaluated before the canonical-edit
# rule so a Read never falls through to it (and the canonical rule is itself gated off for Read below).
if [ "$tool_name" = "Read" ] && [ -n "$file_path" ] && is_redundant_reread "$file_path" "$session_id"; then
  emit_warn "Redundant re-Read: '$file_path' was already read this session and is unchanged (mtime/size) - its content is already in context; skip the Read (post-compaction re-read is the valid exception)."
fi

# (1) canonical-edit WARN: marker-gated. tool_name=='Read' is excluded so wiring the hook for Read
# does not turn a canonical-file READ into a spurious "do not edit mid-run" nag (an empty tool_name -
# the $TOOL_INPUT env channel - is NOT "Read", so the existing env-channel behavior is preserved).
if [ "$tool_name" != "Read" ] && [ -n "$file_path" ] && is_canonical_path "$file_path" && marker_active; then
  emit_warn "Canonical symlinked file - log skill/charter/guard feedback via orchestrate-feedback.sh add (~/.claude/orchestrate-feedback/) and triage via PR; do not edit mid-run."
fi

# (5) foreground-Agent containment WARN (#231): marker-gated, Agent-only. In an ORCHESTRATE session a
# foreground Agent BLOCKS the lead console end-to-end for its whole duration, freezing the lead's
# ability to drive the team. Only an EXPLICIT run_in_background=false trips it (is_foreground_agent is
# type-exact; absent means background). Advisory - it never blocks the spawn.
#
# It fires on a NAMED foreground agent too, deliberately: the override's rationale is ANTI-BLOCKING
# ("the anti-blocking requirement beats the naming-overhead concern"), and a named foreground agent
# blocks the console exactly as hard as an unnamed one (12 of the 45 spiked spawns were named AND
# foreground). The remedy must therefore be stated as BOTH halves - NAME IT **AND** OMIT THE FLAG:
#   - "name it" ALONE is causally FALSE: a name does NOT make an agent async, and `name` +
#     run_in_background:false still blocks the console (this is the named-foreground case above);
#   - "drop the flag" ALONE is UNSAFE on privileged work: it yields an UNNAMED BACKGROUNDED agent,
#     which the standing background-agent ban forbids and which STALLS SILENTLY on the first
#     permission prompt (it cannot answer one). Bare background is for a provably-0%-prompt
#     read-only pass and nothing else.
# Earlier drafts of this rule shipped each half alone; both were wrong, in opposite directions.
#
# THREE ACCEPTED LIMITATIONS, stated so nobody mistakes this for full enforcement:
#  (a) NUDGE, NOT A GUARANTEE. (#312 CLOSED the old "~15% blind" gap: marker_active() used to be
#      $TMUX-keyed, so it silently no-opped on the 7-of-45 live spawns the #221 spike captured
#      where $TMUX was ABSENT - exactly the in-process spawn case this rule most wants to catch.
#      The key now falls back to $CLAUDE_CODE_SESSION_ID, so those spawns ARE covered.) It remains
#      a nudge: an UNKEYED session (neither identifier) is still never gated, and this is advisory
#      either way.
#  (b) OVER-APPROXIMATES "team is live". The CLAUDE.md override forbids a foreground agent when the
#      lead has LIVE NAMED TEAMMATES, and re-sanctions the foreground one-shot when SOLO. A marker is
#      the closest proxy the hook can see, but it has a 72h TTL - so a lead who tore the team down and
#      is working solo in the same tmux pane can still be nagged for the SANCTIONED pattern. The
#      message therefore says so outright, so a correct spawn is not made to feel like a violation.
#  (c) NESTED SPAWNS. PreToolUse fires for a TEAMMATE's tool calls too, so a teammate spawning its own
#      foreground Agent also sees this WARN, where "blocks the LEAD console" is imprecise (it blocks
#      that teammate). Deliberately NOT special-cased: a nesting check would add a fragile inference
#      for an advisory nudge whose advice ("do not block yourself on a foreground agent") still holds.
if [ "$tool_name" = "Agent" ] && is_foreground_agent "$tool_input_json" && marker_active; then
  emit_warn "Foreground Agent in a marker-active session: run_in_background=false BLOCKS the lead console for the agent's ENTIRE run, freezing your ability to drive the team. REMEDY = give it a 'name' AND omit run_in_background:false (BOTH halves: a name alone does not make it async - named + foreground still blocks; dropping the flag alone leaves it UNNAMED and backgrounded, which cannot answer a permission prompt and will stall silently on privileged work). Collect it via its completion notification / SendMessage. Bare background (no name) is allowed ONLY for a provably-0%-prompt read-only pass. If NO teammates are live (solo work in a stale-marker pane), a foreground one-shot is sanctioned - ignore this."
fi

# (2) raw gh-api mutation WARN: marker-independent.
if [ -n "$cmd" ] && is_raw_gh_api_mutation "$cmd"; then
  emit_warn "Use the gh-* wrapper (gh-api-get.sh / gh-comment.sh / gh-codeql-dismiss.sh / gh-codeql-autofix.sh / gh-resolve-thread.sh / gh-delete-branch.sh) instead of raw gh api."
fi

# (3) raw gh pr comment/create -> canonical path WARN: marker-independent (#159; advisory only).
if [ -n "$cmd" ] && is_raw_gh_pr_mutation "$cmd"; then
  emit_warn "Canonical path for gh pr: 'gh pr comment' -> reply-comment.sh / gh-comment.sh; 'gh pr create' -> open the PR via /prep-pr (the required gate). Other gh pr subcommands (incl. merge) are intentionally not flagged."
fi

exit 0
