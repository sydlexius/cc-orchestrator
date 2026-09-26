#!/usr/bin/env bash
# orchestrate-steer.sh - WARN-level PreToolUse steering (advisory), SEPARATE from the hard-deny
# floor (orchestrate-guard.sh). Exit 0 ALWAYS - it NEVER blocks; it only emits a one-line steer to
# stderr when a rule matches, so Claude sees the nudge but the action still proceeds. Keeping it a
# distinct script preserves the floor's integrity (the guard stays pure hard-deny) and lets the
# steering be disabled (`configure --no-steer`) without touching deny logic.
#
# Rules (#95, #159, #226, #231, #284, #432):
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
#   (2) RAW GH-API MUTATION -> WRAPPER: a shell clause (split quote-aware in EVERY code frame, see
#       _steer_scan) invoking `gh api` NOT via a gh-* wrapper, with a REST mutation flag (-X/--method,
#       -f/-F/--field/--raw-field/--input; silent when every explicit method is a literal read verb
#       GET/HEAD/OPTIONS, #413) or, for `gh api graphql`, a
#       query DOCUMENT on the line that is a `mutation` operation, or an explicit -X/--method
#       PATCH|PUT|DELETE (which GraphQL never takes, so that is a mis-aimed REST mutation). A GraphQL
#       READ is silent; a --jq filter never counts; with no document on the line and no such verb it
#       is silent-on-doubt (-F query=@file, --input, -f query="$Q") -> WARN: use the gh-* wrapper.
#       Marker-independent (steer every session).
#   (3) RAW GH PR comment/create -> CANONICAL PATH: a `gh [flags] pr [flags] create|comment|new` word
#       sequence anywhere in one clause of the command's CODE - the top level, $(...), backticks, a
#       `sh|bash|... [opts] -c` / eval script, or a heredoc fed to a shell - so sudo/env/timeout/xargs
#       shapes warn; quoted prose, comments, arithmetic and heredoc bodies not fed to a shell are
#       masked -> WARN toward reply-comment.sh/gh-comment.sh / /prep-pr. A gh pr READ never warns on
#       its own subcommand; the only way a read-only command warns is unquoted prose elsewhere on it
#       (`echo next: gh pr create`), an ACCEPTED false positive (see _steer_scan). Quote it to silence.
#       Marker-independent (#159).
#   (6) PIPED SAFE-PUSH -> RUN IT BARE (#432): a `safe-push.sh` call at command position (bare, any
#       path, behind VAR=val / sudo / env / if ..., or `bash safe-push.sh`) in a clause ENDED by a
#       lone `|` -> WARN. Without pipefail a pipeline returns the LAST command's exit code, so
#       `safe-push.sh b 2>&1 | tail -5` reports a refused push as 0 (observed 3x downstream). Uses
#       the same frame-scoped clause split as rules 2/3, so it fires inside `bash -c '...'` and
#       `$(...)`, while `||`, a `#` comment and quoted prose stay silent. Marker-independent.
#   (7) EXPENSIVE GATE PROFILE (#343, OPT-IN): a clause setting a var the repo DECLARES in
#       `.gates.toml` (`[steer] expensive_profile_env`) to a value other than empty/0 - `VAR=1 cmd`,
#       `env VAR=1 cmd`, or an earlier `export VAR=1` - on a gate (gate-runner.py, pre-push-hook.sh)
#       or upload (safe-push.sh, git push) at command position -> WARN: fast compile step first. An
#       upload at a HEAD holding a passing /prep-pr receipt is named as a DOUBLE SPEND. Same clause
#       split as rules 2/3/6. No declaration -> silent. RESIDUALS (silent or accepted): the var
#       reaching a gate only through `bash -c` from an outer prefix; a `cd` inside the command (the
#       repo is taken from the payload cwd). A value is OFF when it is empty or `0`, bare or as the
#       whole of one quoted word (`VAR="0"`, `VAR=""`, `VAR=$'0'`); a MIXED word (`VAR=0""`) or an
#       expansion (`VAR="$V"`) counts as set. ACCEPTED FALSE POSITIVES (warn, the var never reaches
#       the gate): `export -n VAR=1; <gate>` (un-exports) and `export VAR=1 | <gate>` (the export
#       runs in a pipeline subshell). An export/unset inside a CODE frame (`bash -c '...'`,
#       `$(...)`, backticks, a heredoc fed to a shell) is SCOPED to that frame, as bash scopes
#       it to that process; `eval` shares the shell, so its export does reach later clauses.
#       Needs python3 >= 3.11 (tomllib, as gate-runner.py does); without it the declaration is
#       unreadable and the rule is silent.
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
  # (6) a piped safe-push must WARN at exit 0 (#432).
  if [ -z "$st_fail" ]; then
    # the bare shape, a QUOTED path (prep-pr Step 7's own invocation, #432 I1), and a wrapper (M1)
    for st_cmd in "safe-push.sh b 2>&1 | tail -5" \
        "bash '\${CLAUDE_PLUGIN_ROOT}/scripts/safe-push.sh' b 2>&1 | tail -5" \
        "bash \"\$HOME/.claude/scripts/safe-push.sh\" b | tail" "timeout 60 safe-push.sh b | tail"; do
      st_payload=$(jq -cn --arg c "$st_cmd" '{tool_name:"Bash",tool_input:{command:$c}}' 2>/dev/null) \
        || { st_fail="piped safe-push rule (jq unavailable)"; break; }
      st_out=$(printf '%s' "$st_payload" | "$0" 2>&1); st_rc=$?
      { [ "$st_rc" -eq 0 ] && printf '%s' "$st_out" | grep -q 'STEER'; } \
        || { st_fail="piped safe-push rule '$st_cmd' (rc=$st_rc out=$st_out)"; break; }
    done
  fi
  # (2r)/(3r) the two READ shapes that used to false-positive must stay SILENT at exit 0: a GraphQL
  # read, and a gh pr read compounded with a standalone `create` word.
  # (#413) an explicit READ method on a REST call is silent too, even with -f query parameters.
  for st_cmd in "gh api graphql -f query='{viewer{login}}'" "gh pr view 943 && echo create" \
      "gh api -X GET search/issues -f q=x" "gh api --method HEAD repos/o/r" \
      "safe-push.sh b || echo x" "safe-push.sh b # prep-pr-ok" 'echo "safe-push.sh b | tail"' \
      "git commit -m 'use safe-push.sh | tail'"; do
    [ -n "$st_fail" ] && break
    st_payload=$(jq -cn --arg c "$st_cmd" '{tool_name:"Bash",tool_input:{command:$c}}' 2>/dev/null) \
      || { st_fail="read-silence (jq unavailable)"; break; }
    st_out=$(printf '%s' "$st_payload" | "$0" 2>&1); st_rc=$?
    { [ "$st_rc" -eq 0 ] && ! printf '%s' "$st_out" | grep -q 'STEER'; } \
      || st_fail="read-silence '$st_cmd' (rc=$st_rc out=$st_out)"
  done
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
  # (7) expensive gate profile (#343): a fixture repo declaring SW_X warns on SW_X=1 + a gate and is
  # silent on SW_X=0; the same command in an UNdeclared repo is silent (opt-in). Rule 7 reads the
  # declaration with tomllib (python3 >= 3.11, the same floor as gate-runner.py): without it the rule
  # is silent everywhere by design, so the warn case is SKIPPED (noted), never FAILED.
  st_x7=""
  if [ -z "$st_fail" ] && ! python3 -c 'import tomllib' >/dev/null 2>&1; then
    st_x7=" (rule 7 SKIPPED: python3 tomllib unavailable, needs >= 3.11)"
  elif [ -z "$st_fail" ]; then
    st_tmp=$(mktemp -d 2>/dev/null) || st_tmp=""
    if [ -n "$st_tmp" ] && mkdir -p "$st_tmp/r/.git" "$st_tmp/u/.git" 2>/dev/null \
        && printf '[steer]\nexpensive_profile_env = ["SW_X"]\n' > "$st_tmp/r/.gates.toml"; then
      for st_case in "r:1:SW_X=1 python3 gate-runner.py" "r:0:SW_X=0 python3 gate-runner.py" \
          "u:0:SW_X=1 python3 gate-runner.py" "r:0:SW_X=\"0\" python3 gate-runner.py"; do
        st_dir=${st_case%%:*}; st_want=${st_case#*:}; st_cmd=${st_want#*:}; st_want=${st_want%%:*}
        st_payload=$(jq -cn --arg c "$st_cmd" --arg w "$st_tmp/$st_dir" \
          '{tool_name:"Bash",cwd:$w,tool_input:{command:$c}}' 2>/dev/null) \
          || { st_fail="expensive-profile rule (jq unavailable)"; break; }
        st_out=$(printf '%s' "$st_payload" | "$0" 2>&1); st_rc=$?
        st_got=0; printf '%s' "$st_out" | grep -q 'STEER' && st_got=1
        { [ "$st_rc" -eq 0 ] && [ "$st_got" = "$st_want" ]; } \
          || { st_fail="expensive-profile rule '$st_case' (rc=$st_rc out=$st_out)"; break; }
      done
    else
      st_fail="expensive-profile rule (fixture setup failed; sub-check could not run)"
    fi
    [ -n "$st_tmp" ] && rm -rf "$st_tmp" 2>/dev/null
  fi
  if [ -z "$st_fail" ]; then
    echo "orchestrate-steer self-test PASS (raw gh-api + raw gh pr comment/create mutations + piped safe-push + read-dedup + declared expensive gate profile warned, graphql read + gh pr read + REST GET + unpiped safe-push + profile off/undeclared silent, exit 0)$st_x7"
    exit 0
  fi
  echo "orchestrate-steer self-test FAIL: expected a STEER warn at exit 0, got $st_fail" >&2
  exit 1
fi

# --- read the payload: stdin JSON first, then $TOOL_INPUT env, else fail OPEN (exit 0, no warn) ---
# tool_name + session_id live at the stdin TOP LEVEL (not inside tool_input), so they are available
# only via the real PreToolUse stdin payload - the $TOOL_INPUT env fallback carries neither, which is
# fine: the read-dedup rule (which needs both) simply cannot fire on that channel (fail-open).
#
# ONE jq FORK (#414). This used to fork jq five times (plus cat) on EVERY call, ~14ms of ~20ms.
# One program now reads stdin, falls back to $TOOL_INPUT (read via jq's own `env`), and emits every
# field NUL-TERMINATED, followed by an END sentinel. NUL is the one byte that cannot collide: a bash
# variable cannot hold it, so each value has its NULs removed in jq first - exactly what the old
# `$(jq -r ...)` did to them, only without bash's "ignored null byte" warning. Each field keeps the
# old per-field semantics: `// empty` (absent/null/false -> ""), a per-field error (non-object
# input) -> "" without costing the other fields, trailing newlines stripped as `$(...)` stripped
# them. Two deliberate differences: a NON-STRING container value renders as compact JSON where `jq -r`
# pretty-printed it (whitespace only; no rule can match either form), and a multi-document stdin
# now yields only its FIRST document (Claude Code sends exactly one). base64 per field was measured
# and rejected: bash has no builtin decoder, so it trades the jq forks for base64 forks.
# fg is rule (5)'s `.run_in_background == false`, computed here instead of in a sixth fork: TYPE-EXACT
# (see is_foreground_agent) - absent / true / "false" / 0 / non-object all yield "".
# A missing jq, empty stdin, or malformed JSON yields no END sentinel (or empty fields) -> exit 0.
# shellcheck disable=SC2016  # a jq program: its $vars are jq's, never the shell's
_EXTRACT='
def s: (if type == "string" then . else tojson end) | split("\u0000") | join("")
  | if endswith("\n") then (explode) as $e
      | .[:(first(range(($e | length) - 1; -1; -1) | select($e[.] != 10)) // -1) + 1]
    else . end;
def f(p): (first(try (p | select(. != null and . != false)) catch empty) | s) // "";
(try input catch null) as $p
| [try ($p | .tool_input | select(. != null and . != false)) catch empty] as $a
| (if ($a | length) > 0 then {ok: true, v: $a[0]}
   elif ((env.TOOL_INPUT // "") | length) > 0 then {ok: true, v: (try (env.TOOL_INPUT | fromjson) catch null)}
   else {ok: false, v: null} end) as $t
| ($p | f(.tool_name)), "\u0000", ($p | f(.session_id)), "\u0000",
  (if $t.ok then "1" else "" end), "\u0000",
  ($t.v | f(.file_path)), "\u0000", ($t.v | f(.command)), "\u0000",
  (if (try ($t.v.run_in_background == false) catch false) then "1" else "" end), "\u0000",
  ($p | f(.cwd)), "\u0000",
  "END", "\u0000"'
# Never let jq wait on a terminal (the old code skipped reading a TTY stdin for the same reason).
if [ -t 0 ]; then exec </dev/null; fi
tool_name="" session_id="" has_input="" file_path="" cmd="" fg_agent="" hook_cwd="" x_end=""
{ IFS= read -r -d '' tool_name; IFS= read -r -d '' session_id; IFS= read -r -d '' has_input
  IFS= read -r -d '' file_path; IFS= read -r -d '' cmd; IFS= read -r -d '' fg_agent
  IFS= read -r -d '' hook_cwd; IFS= read -r -d '' x_end; } < <(jq -nj "$_EXTRACT" 2>/dev/null)
[ "$x_end" = END ] && [ -n "$has_input" ] || exit 0

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
      */scripts/orchestrate-setup.py|*/scripts/orchestrate-authorize-merge.sh|\
      */scripts/run-paths.sh|*/scripts/base-freshness.sh|*/scripts/cr-quota-watch.sh|*/scripts/elmer-enqueue.sh|*/scripts/elmer-triage.sh|*/scripts/elmer-tick.sh|\
      */scripts/orchestrate-status.sh|*/scripts/orchestrate-feedback.sh)
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
  # (not false), the string "false" is not false, 0 is not false, and a non-object input is caught
  # to false. So absent / true / "false" / 0 / malformed / missing-jq all fall to SILENT, and only
  # the one sanctioned shape warns. The test itself runs in the ONE payload jq (_EXTRACT, #414),
  # which hands its verdict here as $1 = "1" or "".
  #
  # NOTE the trap this avoids: `// empty` is UNUSABLE here (jq's alternative operator treats a literal
  # `false` as empty and would erase the very value we test for), and a shell falsy check would be
  # WORSE - `run_in_background` is ABSENT, not false, when omitted, and an Agent DEFAULTS TO BACKGROUND,
  # so 13 of the 45 live spawns the #221 spike captured were legal background agents with no field at
  # all. A falsy check would have warned on all of them.
  [ "${1:-}" = 1 ]
}

# --- shared command scanner for rules (2) and (3) -------------------------------
# HISTORY. The base (pre-0.97.2) grepped the WHOLE command line for independent words, so a gh READ
# plus a stray word (`gh pr view 943 && echo create`) or any GraphQL read (`gh api graphql -f
# query='{...}'`) drew a nudge. The first per-clause rewrite fixed those but required `gh` at the
# clause's command position (so `URL=$(gh pr create)`, `sudo gh pr create`, `bash -c 'gh pr create'`
# went SILENT), split clauses quote-blind, and forked 2-4 greps PER CLAUSE. The second rewrite (one awk
# pass over a byte-aligned masked copy) fixed those, but judged every nested-code shape against the
# OUTER clause (a `|` inside `$(...)` cut the outer call off its -f flag; a `;` inside `bash -c '...'`
# never split), read a heredoc fed to a shell as prose, and had a quadratic tail. This is round 3.
#
# THE SCAN (_steer_scan, ONE awk process, linear time). It walks the command once with a FRAME STACK.
# Every CODE frame owns its own clause buffer and is judged on its own clauses:
#   U  the top level;           P  $(...), <(...), >(...);          B  `...`;
#   S/D/E  the script argument of `sh|bash|zsh|dash|ksh [opts] -c` or `eval` ('...', "..." or $'...');
#   H  a heredoc BODY fed to a shell as code: `bash|sh|... [opts] <<[-]DELIM` (quoted or unquoted
#      delimiter, bare or -s), or `cat <<DELIM | bash`.
# PROSE frames contribute ONE placeholder word (`Q`) to the enclosing clause and nothing else: '...',
# "...", $'...' (unless one of the code cases above), a `#` comment, $((...))/((...)) arithmetic (so a
# `<<` shift is never a heredoc), a heredoc body NOT fed to a shell, and - inside a code quote - a
# nested quote (`bash -c 'echo "gh pr create"'` is prose inside the script). A nested code frame
# contributes the placeholder `X` to its parent (so `repos/$(...)/comments` stays one word), and its
# own separators never cut the parent's clause. A quoted token therefore stays ONE non-space word, so a
# quoted flag value (`-H 'X-A: 1'`, `--repo 'o/r'`) still reads as a single flag value.
# CLAUSE boundaries, in EVERY code frame (including inside a -c/eval script and a shell-fed heredoc):
# `&&`, `||`, `;`, `|`, an unescaped newline, or a LONE `&` (not part of `&&`, `>&`, `<&`, `&>`).
# Backslash-newline continuations are joined (they are removed, as bash does). Each quote that is the
# GraphQL query value (`query=` right before it, or a quote that begins `query=`) and a heredoc body in
# a `graphql` clause is ALSO copied to that clause's DOCUMENT buffer, which is what the mutation test
# reads (so a `--jq` filter mentioning `mutation` never counts).
# RULE 2 (raw gh api mutation) is judged PER CLAUSE: the clause must contain the `gh` and `api` words; if
# `api` [flag groups] `graphql` is the endpoint, it warns only when the query DOCUMENT carries a
# `mutation` operation (at the document's start, at the start of a line, or after the `}` closing a
# preceding fragment), when an unquoted `query=mutation...` is on the clause, or on -X PATCH/PUT/DELETE;
# otherwise (REST) it warns on an explicit -X/--method or any -f/-F/--field/--raw-field/--input, UNLESS
# every explicit method on the clause is a literal read verb (-X GET/HEAD/OPTIONS, --method GET, #413):
# there -f/-F are query parameters, so `gh api -X GET search/issues -f q=x` is silent. Any other
# explicit method alongside it (-X GET ... -X POST, -XPOST) or a non-literal one (-X "$M") still warns.
# RULE 3 (raw gh pr create/comment) is a WORD SEQUENCE anywhere in one clause:
# `gh` [flag groups] `pr` [flag groups] `create|comment|new` (`new` is create's alias), where `gh` is a
# standalone word (a path prefix like /opt/homebrew/bin/gh counts; gh-comment.sh does not). A read
# differs in the SUBCOMMAND word, so `gh pr view 5 && echo create` stays silent while `sudo gh pr
# create`, `URL=$(gh pr create)`, `xargs gh pr comment` and `bash -c 'gh pr create'` warn. An unescaped
# newline ends a command, so `gh pr` NEWLINE `create` is two commands and silent.
#
# _FLAGS mirrors orchestrate-guard.sh's flag-group regex (inside `is_pr_merge`): zero or more
# `-flag [value]` groups, a value being a token that does not itself start with `-`.
_FLAGS='([[:space:]]+-[^[:space:]]+([[:space:]]+[^-[:space:]][^[:space:]]*)?)*'

# PREFILTER (#perf): a no-fork bash test that a command COULD match rule 2 or 3. It must be a true
# SUPERSET of what _steer_scan can flag. Rule 3 needs a `gh` word, a `pr` word and create|comment|new;
# rule 2 needs `gh`, `api` and one of the mutation flags (a GraphQL mutation always carries its query
# via -f/-F/--field/--raw-field, whose spellings all contain `-f`/`-F`). Each word test ends on any
# non-word byte, NOT on whitespace: the scanner's own GH also accepts end-of-string, and heredoc()
# rewrites a `<<WORD` to a space, so `gh api<<D graphql -f query=...` reads as `gh api graphql ...`
# to the scanner while the raw bytes have `<` after `api`. Demanding whitespace there filtered out
# commands the scanner DOES flag, inverting the containment. A backslash-newline can split
# ANY of those words (`gh\<NL> pr`, `cre\<NL>ate`), and joining in bash (`${c//\\$'\n'/}`) is
# super-linear on a long command, so a command carrying one skips the prefilter and goes straight to
# the (linear) scanner, which joins it. Everything else costs what it did on base: no awk, no grep.
_steer_prefilter() {
  local c="$1" re_gh re_pr re_sub re_api re_flag
  [[ $c == *\\$'\n'* ]] && return 0
  # rule 6 (#432): a safe-push.sh word and a `|` byte anywhere (a superset of a piped call).
  [[ $c == *safe-push.sh* && $c == *'|'* ]] && return 0
  re_gh='(^|[^[:alnum:]_-])gh([^[:alnum:]_-]|$)'
  re_pr='(^|[^[:alnum:]_-])pr([^[:alnum:]_-]|$)'
  re_sub='(create|comment|new)'
  re_api='(^|[^[:alnum:]_-])api([^[:alnum:]_-]|$)'
  re_flag='(-[XfF]|--method|--input)'
  [[ $c =~ $re_gh ]] || return 1
  [[ $c =~ $re_pr && $c =~ $re_sub ]] && return 0
  [[ $c =~ $re_api && $c =~ $re_flag ]] && return 0
  return 1
}

# Print `api` (rule 2 fires), `pr` (rule 3 fires), `push` (rule 6 fires) or nothing. LC_ALL=C so the walk is bytewise.
# LINEAR by construction: the input is split to a char array once; every buffer is appended in 256-byte
# chunks and joined pairwise only when a clause is judged; no substr() of the whole command is ever
# taken in the loop (BWK awk's substr is O(length of the source string), which is what made the
# previous scanner quadratic: 1MB of `'a'` words took 58s there, ~1s on base).
# KNOWN RESIDUALS (advisory; all are either rare or undecidable without running bash):
#   - UNQUOTED PROSE: `echo next: gh pr create` warns. bash cannot tell an echo argument from a
#     command word without knowing what the words are used for, and silencing `echo`/`printf` clauses
#     would also silence `echo gh pr create | bash`, a real invocation that base and round 2 warned on.
#     A warn-only nudge on a rare shape is the right side to err on. Quote the prose to silence it.
#   - A QUOTED string piped to a shell (`echo 'gh pr create' | bash`) and a quote passed to a shell
#     other than sh/bash/zsh/dash/ksh -c or eval (e.g. `ssh host 'gh pr create'`) are prose: silent.
#   - A GraphQL document not on the command line (-F query=@file, --input FILE, `--input -` with a
#     heredoc JSON body, -f query="$Q") cannot be classified and is SILENT-ON-DOUBT.
#   - A `query=` field whose VALUE is a search string beginning `mutation <word>` warns.
#   - Inside a $'...' -c script, a `\'` is taken as an escape, not as a nested prose quote.
#   - `case x in a) ...` inside $(...) closes the substitution early (as round 2 did).
_steer_scan() {
  printf '%s' "$1" | LC_ALL=C awk -v FL="$_FLAGS" -v EXV="${_EXV:-}" '
    # ---- chunked buffers: append O(1) amortized, joined pairwise (O(n log n)) only when judged ----
    function bapp(k, c) {
      sb[k] = sb[k] c
      if (++sl[k] >= 256) {
        ch[k, ++nch[k]] = sb[k]
        if (!gq[k] && index(lc[k] sb[k], "graphql")) gq[k] = 1
        lc[k] = sb[k]; sb[k] = ""; sl[k] = 0
      }
    }
    function bget(k,   i, m, w) {
      m = nch[k]; if (m == 0) return sb[k]
      for (i = 1; i <= m; i++) W[i] = ch[k, i]
      W[++m] = sb[k]
      while (m > 1) { w = 0; for (i = 1; i <= m; i += 2) W[++w] = (i < m ? W[i] W[i + 1] : W[i]); m = w }
      return W[1]
    }
    function bclr(k) { sb[k] = ""; sl[k] = 0; nch[k] = 0; lc[k] = ""; gq[k] = 0 }
    function tail(k,   s, l) { s = lc[k] sb[k]; l = length(s); return (l > 64 ? substr(s, l - 63) : s) }
    # ---- judging one clause of code frame k ----
    function judge(k, sep,   s, dc, t) {
      s = bget(k)
      # RULE 6 (#432): a safe-push.sh call at command position whose clause is ended by a
      # PIPE (`|`, never `||` - cut() records only a lone `|` as "|"). Judged before the gh
      # early return because the clause carries no gh word.
      if (sep == "|" && !FSP && index(s, "safe-push") && s ~ SP) FSP = 1
      if (EXN && FEX != "xpush") ex7(s)
      if (!index(s, "gh") || s !~ GH) return
      if (index(s, "api") && s ~ API) {
        if (index(s, "graphql") && s ~ GQL) {
          dc = bget("d" k)
          if (dc ~ MUTD || s ~ MUTU || s ~ GQLM) { print "api"; exit }
        } else if (s ~ RM || s ~ RF) {
          # #413: an explicit READ method (GET/HEAD/OPTIONS) makes -f/-F query parameters, not a
          # body. Silent only when EVERY explicit method on the clause is a literal read verb:
          # strip each read-method occurrence, and any method flag left over (-X POST, -XPOST,
          # -X "$M" -> -X Q) still warns. The loop re-matches from scratch, so adjacent
          # occurrences are all stripped (a single gsub would consume the shared space).
          if (s !~ GETM) { print "api"; exit }
          t = s
          while (match(t, GETM)) t = substr(t, 1, RSTART - 1) " " substr(t, RSTART + RLENGTH)
          if (t ~ RM) { print "api"; exit }
        }
      }
      if (!FPR && index(s, "pr") && s ~ PR) FPR = 1
    }
    function cut(sep) { judge(d, sep); bclr(d); bclr("d" d); lastcut[d] = sep }
    # ---- the frame stack ----
    function push(t, code, st,   p, nm, iscq, s, pfxnw, pfxw, pfxi, pfxnm) {
      p = d; d++
      ft[d] = t; fc[d] = code; fp[d] = 0; dk[d] = 0; dq[d] = 0; pb[d] = ""; fst[d] = st
      csq[d] = csq[p]; qd[d] = qd[p]
      if (t == "P" || t == "B" || t == "A") qd[d] = 0
      if (code && (t == "S" || t == "E")) csq[d] = d
      if (code && t == "D") qd[d] = d
      if (code) { bclr(d); bclr("d" d); lastcut[d] = "" }
      # RULE 7: a CODE frame (bash -c script, $(...), backticks, a heredoc fed to a shell) is its
      # own PROCESS, so an export/unset inside it never reaches the enclosing shell. Snapshot the
      # declared vars here and restore them in pop(). An `eval` script runs in the SAME shell, so
      # its frame is not scoped (fx7 = 0). CQEVAL is set by codeq() and consumed here only.
      iscq = (code && (t == "S" || t == "D" || t == "E"))
      fx7[d] = (EXN && code && !(CQEVAL && iscq))
      if (fx7[d]) {
        for (nm in EXS) { XS[d, nm] = (nm in EXP) ? EXP[nm] : -1 }
        # (#478) A -c SCRIPT FRAME (never eval, which already shares the shell) is a NEW PROCESS
        # bash execs: a PREFIX ASSIGNMENT on the command that opens it (e.g. SW_X=1 bash -c SCRIPT)
        # flows into that child process environment even though it is never export-ed in the
        # parent shell, and the snapshot above alone leaves the child blind to it (EXP still reads
        # whatever the OUTER shell had). The restore below already reverts EXP on pop(), matching
        # bash (the assignment never touches the invoking shell own copy of the var).
        # (E1, #478 fix round 1) ONLY a CONTIGUOUS assignment/wrapper run from the very START of
        # bget(p) - matched by the anchored PFX7 - counts: a NAME=value word is a prefix assignment
        # ONLY when nothing but more assignments/transparent-wrapper keywords sits between it and
        # the clause start. A whole-buffer word scan (the prior version of this fix) instead
        # treated ANY NAME=value-shaped word ANYWHERE in the clause as a prefix assignment, so
        # `find . -name SW_GATE_FULL=1 -exec bash -c SCRIPT` seeded from an argument to `-name`,
        # nowhere near being a prefix assignment on the `bash -c` it happens to precede. PFX7 stops
        # at the first token that is neither: `-name`/`-exec`/`find`/`printf` all fail it, so the
        # match consumes zero real assignments and nothing is seeded.
        if (iscq && !CQEVAL) {
          s = bget(p)
          if (match(s, PFX7)) {
            pfxnw = split(substr(s, 1, RLENGTH), pfxw, /[[:space:]]+/)
            for (pfxi = 1; pfxi <= pfxnw; pfxi++)
              if (match(pfxw[pfxi], /^[A-Za-z_][A-Za-z0-9_]*=/)) {
                pfxnm = substr(pfxw[pfxi], 1, RLENGTH - 1)
                if (pfxnm in EXS) EXP[pfxnm] = x7on(substr(pfxw[pfxi], RLENGTH + 1))
              }
          }
        }
      }
      CQEVAL = 0
    }
    function pop(   nm) {
      if (fc[d]) judge(d, "")
      if (fx7[d]) for (nm in EXS) {
        if (XS[d, nm] == -1) delete EXP[nm]; else EXP[nm] = XS[d, nm]
        delete XS[d, nm]
      }
      if (ft[d] == "H") { HD = hprev[d]; HE = (HD ? he[HD] : -1) }
      if (dk[d] && !dq[d]) bapp("d" dk[d], "\n")
      d--
    }
    function closeto(k) { if (k < 2) k = 2; while (d >= k) pop() }
    # a quote in a code frame: CODE when it is the script of `<shell> [opts] -c` or `eval`
    function codeq(   tl, k, w) {
      # cheap necessary condition on the raw bytes first (no string building on the common path): the
      # quote follows whitespace, and the word before it is `eval` or a `-...c...` option cluster.
      # (#478) BOTH scans are bounded by fst[d], the CURRENT frame own start: without that bound
      # they walk past the frame boundary into the raw a[] bytes of the ENCLOSING frame (the open
      # paren of a $( or the opening double-quote of a bash -c script), neither of which is
      # whitespace - so eval as the very first word of a NESTED frame collected the enclosing
      # opener plus "eval" as one token and failed the w == "eval" test: an eval opening a code
      # frame from inside $(...) or a double-quoted bash -c script was read as prose. fst[d] is
      # the frame own first CONTENT byte (set by the st argument to push()), so stopping there
      # never eats the opener.
      k = j - 1
      while (k >= fst[d] && (a[k] == " " || a[k] == "\t" || a[k] == "\n" || a[k] == "\\")) k--
      if (k == j - 1 || k < fst[d]) return 0
      w = ""
      while (k >= fst[d] && k > j - 24 && a[k] !~ /[[:space:]]/) { w = a[k] w; k-- }
      if (w != "eval" && w !~ /^-[A-Za-z]*c[A-Za-z]*$/) return 0
      CQEVAL = (w == "eval")   # rule 7: an eval script shares the shell (read by push)
      tl = tail(d)
      if (!index(tl, "sh") && !index(tl, "eval")) return 0
      return (tl ~ CODEQ || tl ~ /(^|[^[:alnum:]_.-])eval[[:space:]]+$/)
    }
    function cq(t, st) { bapp(d, "Q"); push(t, 1, st) }
    # a PROSE quote: placeholder word for the clause; its text feeds the document buffer only when it
    # is the query= value (confirmed now, or probed from its own first 6 bytes)
    function pq(t, st,   p, tl) {
      p = d; bapp(p, "Q"); push(t, 0, st)
      # the query= value? (raw adjacency: `query=` is unquoted code directly before the quote)
      tl = (j > 6 && a[j - 1] == "=" && a[j - 6] == "q") ? a[j-6] a[j-5] a[j-4] a[j-3] a[j-2] "=" : ""
      dk[d] = p; dq[d] = (tl == "query=" ? 0 : 1)
    }
    # (#432 I1) a prose quote collapses to ONE `Q`, so a QUOTED script path
    # (bash + a single-quoted ${CLAUDE_PLUGIN_ROOT}/scripts/safe-push.sh + `b | tail`, the exact
    # prep-pr Step 7 shape) was invisible to rule 6. NOTE: no single-quote byte may appear in this
    # awk program, comments included - the whole program is one single-quoted shell word. When a prose quote closes, spq() checks its RAW content (bytes s0..e-1,
    # e = the closing byte) in O(1): it must END in `/safe-push.sh` or BE `safe-push.sh`. Only then is
    # `/safe-push.sh` appended after the `Q`, so the clause reads `bash Q/safe-push.sh b` and SP judges
    # command position as usual. Prose that merely MENTIONS it (`echo "safe-push.sh b | tail"`) does
    # not end in the name, and a quoted name in an argument slot (`echo "x/safe-push.sh" | tail`) is
    # not at command position, so both stay silent.
    # (#343) generalized: returns the name to append (/safe-push.sh or /gate-runner.py) or "", so a
    # quoted gate-runner path (prep-pr Step 3 shape) reaches rule 7 the same way.
    function spq(s0, e,   i, w, L, nm) {
      nm = (a[e - 1] == "h" ? "safe-push.sh" : (a[e - 1] == "y" ? "gate-runner.py" : ""))
      L = length(nm); if (L == 0 || e - s0 < L) return ""
      w = ""; for (i = e - L; i < e; i++) w = w a[i]
      if (w != nm) return ""
      return (e - s0 == L || a[e - L - 1] == "/") ? "/" nm : ""
    }
    # RULE 7 (#343): an EXPORT clause records each declared var as on/off (a later gate clause in
    # this command inherits it); a gate/upload clause at command position fires when a declared
    # var is on, its own VAR=val / env VAR=val prefix overriding the export. Off = empty or 0.
    # OFF = empty or 0, bare or as the whole of one quoted value (QOFF, see qoff).
    function x7on(v) { return (v != "" && v != "0" && v != QOFF) }
    function ex7(s,   m, w, i, nw, nm, v, kind) {
      # (#478 b2) an optional leading `{` (a brace-group opener, e.g. `{ export SW_X=1; }; <gate>`)
      # is allowed before export/unset: `{` is not a special byte to this scanner (it is appended
      # to the buffer like any ordinary character), so it stays on the clause text and previously
      # made the anchored `^[[:space:]]*export` tests fail outright, silently skipping the export.
      if (s ~ /^[[:space:]]*\{?[[:space:]]*(export|unset)[[:space:]]/) {
        nw = split(s, w, /[[:space:]]+/)
        for (i = 1; i <= nw; i++) {
          if (w[i] in EXS) { if (s ~ /^[[:space:]]*\{?[[:space:]]*unset/) EXP[w[i]] = 0 }
          else if (match(w[i], /^[A-Za-z_][A-Za-z0-9_]*=/)) {
            nm = substr(w[i], 1, RLENGTH - 1); v = substr(w[i], RLENGTH + 1)
            if (nm in EXS) EXP[nm] = (s ~ /^[[:space:]]*\{?[[:space:]]*export/ && x7on(v))
          }
        }
        return
      }
      if (match(s, EXG)) kind = "xgate"; else if (match(s, EXU)) kind = "xpush"; else return
      m = substr(s, RSTART, RLENGTH)
      split("", EFF); for (nm in EXP) EFF[nm] = EXP[nm]
      nw = split(m, w, /[[:space:]]+/)
      for (i = 1; i <= nw; i++) if (match(w[i], /^[A-Za-z_][A-Za-z0-9_]*=/)) {
        nm = substr(w[i], 1, RLENGTH - 1); v = substr(w[i], RLENGTH + 1)
        if (nm in EXS) EFF[nm] = x7on(v)
      }
      # an UPLOAD outranks a gate, so gate-then-push in one command still reaches the double-spend test
      for (nm in EFF) if (EFF[nm]) { if (FEX != "xpush") FEX = kind; return }
    }
    # (#413, CR on #450) a QUOTED literal read method (-X "GET", or GET in single quotes) is the
    # same literal to bash and gh, but collapsed to `Q` it read as a non-literal method and warned.
    # rmw() returns that word when the raw content (bytes s0..e-1) is EXACTLY GET/HEAD/OPTIONS,
    # else "". O(1): only a 3/4/7-byte span is ever built. The FAST path (sqprose) knows the close
    # before writing, so it writes the word INSTEAD of `Q`. The slow path wrote its `Q` at OPEN
    # time (it may already be flushed into a chunk, so it is never edited back out); on close it
    # appends RMS + the word, and GETM accepts an optional `Q` RMS before the verb. RMS is the \001
    # byte, which rmw never emits for anything else, so a quoted "$M" stays a bare `Q` and warns.
    function rmw(s0, e,   w, i) {
      if (e - s0 != 3 && e - s0 != 4 && e - s0 != 7) return ""
      w = ""; for (i = s0; i < e; i++) w = w a[i]
      return (w == "GET" || w == "HEAD" || w == "OPTIONS") ? w : ""
    }
    # (#343 R343-3) a prose quote that is an ASSIGNMENT VALUE (raw `=` right before the opening quote,
    # or before the `$` of a $-quote) whose content is EMPTY or exactly `0` appends OFFM after its `Q`,
    # so rule 7 reads SW_X="0" / SW_X="" / SW_X=single-quoted-empty as the OFF value bash sees. OFFM is
    # the \002 byte, never emitted for anything else; any other content (incl. "$V") stays a bare `Q`.
    function qoff(s0, e,   o) {
      if (e - s0 > 1 || (e - s0 == 1 && a[s0] != "0")) return 0
      o = s0 - 1; if (ft[d] == "E") o--
      return (o > 1 && a[o - 1] == "=")
    }
    function qpop(e,   sp, rw, of) {
      sp = spq(fst[d], e); rw = rmw(fst[d], e); of = qoff(fst[d], e); pop()
      if (of) bapp(d, OFFM)
      if (rw != "") bapp(d, RMS rw)
      if (sp != "") bapp(d, sp)
    }
    # FAST PATH for the common prose single quote: not inside a code "..." (whose `"` must still close
    # it), not a possible query= value (a `=` before it, or content starting with `q`): skip to the
    # closing quote without a frame. (Inside a single-quoted code script a quote never reaches here: the main loop
    # closes the script first.) Bounded by the enclosing shell-fed heredoc body; unbalanced -> slow path.
    function sqprose(   k, lim, w) {
      if (qd[d] || a[j - 1] == "=" || a[j + 1] == "q") { pq("S", j + 1); return }
      lim = (HD ? HE : n + 1)
      k = j + 1; while (k < lim && a[k] != SQ) k++
      if (k >= lim) { pq("S", j + 1); return }
      w = rmw(j + 1, k); bapp(d, (w != "" ? w : "Q")); w = spq(j + 1, k); if (w != "") bapp(d, w); j = k
    }
    function dapp(c) {
      if (dq[d]) {
        pb[d] = pb[d] c
        if (length(pb[d]) >= 6) { if (pb[d] == "query=") dq[d] = 0; else dk[d] = 0 }
        return
      }
      bapp("d" dk[d], c)
    }
    function ws() { return (j == 1 || j == fst[d] || a[j - 1] ~ /[[:space:];&|(]/) }
    # ---- heredocs ----
    function heredoc(   k, w, wl, strip, c2, tl, i, m) {
      tl = tail(d)
      k = j + 2; strip = 0; w = ""; wl = 0
      if (a[k] == "-") { strip = 1; k++ }
      while (a[k] == " " || a[k] == "\t") k++
      while (k <= n && a[k] !~ /[[:space:];&|<>()]/) {
        c2 = a[k]
        if (c2 != SQ && c2 != "\"" && c2 != "\\" && ++wl <= 256) w = w c2
        k++
      }
      if (w != "") {
        nhd++; hs[nhd] = strip; m = split(w, HT, ""); hl[nhd] = m
        for (i = 1; i <= m; i++) hdc[nhd, i] = HT[i]
        hc[nhd] = (index(tl, "sh") && tl ~ SHFEED)
        hg[nhd] = (gq[d] || index(lc[d] sb[d], "graphql") > 0)
      }
      return k - 1
    }
    # j is at a code-frame newline with pending heredocs: locate every body (linear, no substr), copy a
    # graphql prose body into the document, judge the clause, then skip prose bodies / enter code ones.
    function hbodies(   h, k, bs, de, p, i, L, ok, nq, code, pipe) {
      k = j + 1; nq = 0
      pipe = (lastcut[d] == "|" && nch[d] == 0 && sb[d] ~ SHALONE)
      for (h = 1; h <= nhd; h++) {
        bs = k; de = n + 1
        while (k <= n) {
          p = k
          if (hs[h]) while (a[p] == "\t") p++
          L = hl[h]; ok = 1
          for (i = 1; i <= L; i++) if (a[p + i - 1] != hdc[h, i]) { ok = 0; break }
          if (ok && (p + L > n || a[p + L] == "\n")) { de = k; k = p + L + 1; break }
          while (k <= n && a[k] != "\n") k++
          k++
        }
        if (k > n + 1) k = n + 1
        code = (hc[h] || pipe)
        nq++; Qs[d, nq] = bs; Qe[d, nq] = de; Qk[d, nq] = k; Qc[d, nq] = code
        if (!code && hg[h]) { for (i = bs; i < de; i++) bapp("d" d, a[i]); bapp("d" d, "\n") }
      }
      nhd = 0
      cut("")
      qn[d] = nq; qi[d] = 1
      runq()
    }
    function runq(   i, p) {
      p = d
      while (qi[p] <= qn[p]) {
        i = qi[p]++
        if (Qc[p, i]) {
          push("H", 1, Qs[p, i]); he[d] = Qe[p, i]; hk[d] = Qk[p, i]
          hprev[d] = HD; HD = d; HE = he[d]; j = Qs[p, i] - 1
          return
        }
        j = Qk[p, i] - 1
      }
    }
    { L[NR] = $0 }
    END {
      # join the lines pairwise (O(n log n); a running T = T "\n" $0 is quadratic) and split ONCE
      m = NR
      while (m > 1) { w = 0; for (i = 1; i <= m; i += 2) L[++w] = (i < m ? L[i] "\n" L[i + 1] : L[i]); m = w }
      n = (NR ? split(L[1], a, "") : 0)
      SQ = sprintf("%c", 39)
      # the only bytes a code frame / a prose "..." acts on; everything else is appended as-is
      m = split("\\ \" $ ( ) ` # < ; | &", tmp, " "); for (i = 1; i <= m; i++) SPC[tmp[i]] = 1
      SPC[SQ] = 1; SPC["\n"] = 1
      DSP["\\"] = 1; DSP["\""] = 1; DSP["$"] = 1; DSP["`"] = 1
      SHC = "(ba|z|da|k)?sh([[:space:]]+(-[A-Za-z]+|--[A-Za-z-]+|[-+]O[[:space:]]+[A-Za-z_]+))*"
      CODEQ = "(^|[^[:alnum:]_.-])" SHC "[[:space:]]+-[A-Za-z]*c[A-Za-z]*[[:space:]]+$"
      SHFEED = "(^|[^[:alnum:]_.-])" SHC "[[:space:]]*$"
      SHALONE = "^[[:space:]]*((sudo|command|exec)[[:space:]]+)?" SHC "[[:space:]]*$"
      GH = "(^|[^[:alnum:]_-])gh([[:space:]]|$)"
      API = "(^|[[:space:]])api([[:space:]]|$)"
      GQL = "(^|[[:space:]])api" FL "[[:space:]]+graphql([[:space:]]|$)"
      MTAIL = "mutation([[:space:]]*[({]|[[:space:]]+[A-Za-z_]|[[:space:]]*$)"
      MUTD = "((^|\n)[[:space:]]*|[}][[:space:]]*)" MTAIL
      MUTU = "query=" MTAIL
      GQLM = "(--method[[:space:]=]+|-X[[:space:]=]*)(PATCH|PUT|DELETE)"
      RM = "(--method[[:space:]=]|-X[[:space:]=]?[A-Za-z])"
      RMS = "\001"
      OFFM = "\002"; QOFF = "Q" OFFM
      GETM = "(^|[[:space:]])(--method[[:space:]=]+|-X[[:space:]=]*)(Q" RMS ")?(GET|HEAD|OPTIONS)([[:space:]]|$)"
      RF = "(^|[[:space:]])(--(field|input|raw-field)[[:space:]=]|-[fF][[:space:]=]?[^[:space:]])"
      # command position: optional ( / { openers, then any run of VAR=val assignments, shell
      # keywords, transparent wrappers (sudo/env/time/timeout/nice/nohup/...), an interpreter word
      # (`bash`, `/bin/bash`), and the -flags / numeric args those take (`sudo -E`, `bash -x`,
      # `timeout 60`, `nice -n 10`); then safe-push.sh itself, bare or behind any path (a quoted
      # path reads as Q/safe-push.sh, see spq). DOCUMENTED RESIDUALS (silent, accepted): a wrapper
      # option with a NON-numeric value (`sudo -u bob`, `timeout -s KILL 60`), `xargs safe-push.sh`,
      # and a brace group `{ safe-push.sh b; } | tail` (the pipe ends the `}` clause, not the call).
      SPW = "if|then|do|else|elif|while|until|!|time|command|exec|sudo|nohup|env|nice|ionice|stdbuf|timeout"
      SP = "^[[:space:]]*[({]*[[:space:]]*(([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*|" SPW "|([^[:space:]]*/)?(ba|z|da|k)?sh|([^[:space:]]*/)?env|-[^[:space:]]*|[0-9][0-9.]*[smhd]?)[[:space:]]+)*([^[:space:]]*/)?safe-push\\.sh([[:space:]]|$)"
      PR = "(^|[^[:alnum:]_.-])gh" FL "[[:space:]]+pr" FL "[[:space:]]+(create|comment|new)([^[:alnum:]_-]|$)"
      # rule 7 (#343): the SP command-position prefix plus an interpreter word (python3 gate-runner.py).
      # RECOGNIZED SET: GATE = gate-runner.py, pre-push-hook.sh; UPLOAD = safe-push.sh, git [flags] push.
      CP7 = "^[[:space:]]*[({]*[[:space:]]*(([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*|" SPW "|([^[:space:]]*/)?((ba|z|da|k)?sh|env|python[0-9.]*)|-[^[:space:]]*|[0-9][0-9.]*[smhd]?)[[:space:]]+)*"
      EXG = CP7 "([^[:space:]]*/)?(gate-runner\\.py|pre-push-hook\\.sh)([[:space:]]|$)"
      EXU = CP7 "([^[:space:]]*/)?(safe-push\\.sh|git" FL "[[:space:]]+push)([[:space:]]|$)"
      # (#478 E1) the push() prefix-assignment SEED (see push()) anchors on THIS, narrower than CP7:
      # a CONTIGUOUS run of NAME=value assignments and/or the SAME transparent wrapper KEYWORDS
      # (SPW; bare words only, no flags) from the very start of the clause. Deliberately EXCLUDES
      # CP7 own generic flag alternative (-[^[:space:]]*) and its shell/env/python interpreter
      # alternative: a bare hyphen-flag test is what let `find . -name SW_GATE_FULL=1 -exec bash -c`
      # match past `-name`/`-exec` as if they were transparent, seeding SW_GATE_FULL from an
      # argument to `find`/`printf` that has nothing to do with the `bash -c` it happens to precede.
      # Excluding flags means a real `sudo -E bash -c` prefix assignment goes unseeded too (a false
      # NEGATIVE, not a false positive) - the safe side for an advisory nudge, and accepted.
      PFX7 = "^[[:space:]]*[({]*[[:space:]]*(([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*|" SPW ")[[:space:]]+)*"
      EXN = split(EXV, tmp, " "); for (i = 1; i <= EXN; i++) EXS[tmp[i]] = 1
      d = 1; ft[1] = "U"; fc[1] = 1; fp[1] = 0; csq[1] = 0; qd[1] = 0; fst[1] = 1
      bclr(1); bclr("d1"); HD = 0; HE = -1; nhd = 0; FPR = 0; FSP = 0; FEX = ""
      for (j = 1; j <= n; j++) {
        if (HD && j >= HE) {
          # the innermost shell-fed heredoc body ended: close it (and anything left open inside it)
          k0 = hk[HD]; closeto(HD)
          if (j < k0) j = k0 - 1; else j--
          runq(); continue
        }
        c = a[j]; t = ft[d]
        # a single quote ALWAYS ends an enclosing -c/eval single-quoted script (bash has no escape in '')
        if (c == SQ && csq[d]) { closeto(csq[d]); continue }
        if (t == "K") {
          if (c != "\n") { if (c == "\"" && qd[d]) closeto(qd[d]); continue }
          pop(); t = ft[d]
        }
        if (!fc[d]) {
          if (t == "S") {
            if (c == SQ) qpop(j); else if (c == "\"" && qd[d]) closeto(qd[d]); else if (dk[d]) dapp(c)
            continue
          }
          if (t == "E") {
            if (c == "\\") { j++; if (dk[d]) dapp(a[j]) }
            else if (c == SQ) qpop(j); else if (c == "\"" && qd[d]) closeto(qd[d]); else if (dk[d]) dapp(c)
            continue
          }
          if (t == "X") {
            if (c == "\\" && a[j + 1] == "\"") { qe = j; j++; qpop(qe) }
            else if (c == "\\") { j++; if (dk[d]) dapp(a[j]) }
            else if (c == "\"") closeto(qd[d]); else if (dk[d]) dapp(c)
            continue
          }
          if (t == "D") {
            if (!(c in DSP)) { if (dk[d]) dapp(c); continue }
            if (c == "\\") { j++; if (dk[d]) dapp(a[j]) }
            else if (c == "\"") qpop(j)
            else if (c == "$" && a[j + 1] == "(") {
              if (a[j + 2] == "(") { push("A", 0, j + 3); j += 2 } else { push("P", 1, j + 2); j++ }
            }
            else if (c == "`") push("B", 1, j + 1)
            else if (dk[d]) dapp(c)
            continue
          }
          if (t == "A") {
            if (c == "(") fp[d]++
            else if (c == ")") { if (fp[d] > 0) fp[d]--; else { if (a[j + 1] == ")") j++; pop() } }
            continue
          }
        }
        # ---- a code frame: U, P, B, H, or a code quote S/D/E ----
        if (!(c in SPC)) { bapp(d, c); continue }
        if (c == "\\") {
          nx = a[j + 1]
          if (nx == "\n") { j++; continue }
          if (t == "D" && nx == "\"") { pq("X", j + 2); j++; continue }
          if (t == "E" && nx == "n") { j++; c = "\n" }
          else if (nx == SQ && csq[d] && ft[csq[d]] == "S") continue
          else { bapp(d, c); bapp(d, nx); j++; continue }
        }
        if (c == SQ) { if (codeq()) cq("S", j + 1); else sqprose(); continue }
        if (c == "\"") {
          if (t == "D") { pop(); continue }
          if (qd[d]) { closeto(qd[d]); continue }
          if (codeq()) cq("D", j + 1); else pq("D", j + 1)
          continue
        }
        # !csq[d]: inside a single-quoted code script bash has no ANSI-C quote -- it ends the string
        # at the next SQ, so that SQ is the script CLOSE, not the open of one. Without the guard this
        # branch beats the SQ close above whenever a $ sits immediately before the closing quote (a
        # trailing regex anchor is the common way that happens), eats that quote, and opens a frame
        # that never closes -- silencing every clause after it. Vector: SQ_DOLLAR_WARN.
        if (c == "$" && a[j + 1] == SQ && !csq[d]) { if (codeq()) cq("E", j + 2); else pq("E", j + 2); j++; continue }
        if (c == "$" && a[j + 1] == "(") {
          bapp(d, "X")
          if (a[j + 2] == "(") { push("A", 0, j + 3); j += 2 } else { push("P", 1, j + 2); j++ }
          continue
        }
        if (c == "(" && a[j + 1] == "(" && ws()) { bapp(d, "X"); push("A", 0, j + 2); j++; continue }
        if (c == "(" && j > 1 && (a[j - 1] == "<" || a[j - 1] == ">")) { bapp(d, "X"); push("P", 1, j + 1); continue }
        if (c == "`") { if (t == "B") pop(); else { bapp(d, "X"); push("B", 1, j + 1) } ; continue }
        if (t == "P" && c == "(") { fp[d]++; bapp(d, c); continue }
        if (t == "P" && c == ")") { if (fp[d] > 0) { fp[d]--; bapp(d, c) } else pop(); continue }
        if (c == "#" && ws()) { push("K", 0, j + 1); continue }
        if (c == "<" && a[j + 1] == "<" && a[j + 2] != "<" && !(j > 1 && a[j - 1] == "<")) {
          j = heredoc(); bapp(d, " "); continue
        }
        if (c == "\n") { if (nhd > 0) hbodies(); else cut(""); continue }
        if (c == ";") { cut(""); continue }
        if (c == "|") { if (a[j + 1] == "|") { j++; cut("") } else cut("|"); continue }
        if (c == "&") {
          if (a[j + 1] == "&") { j++; cut("") }
          else if (!(j > 1 && (a[j - 1] == ">" || a[j - 1] == "<")) && a[j + 1] != ">") cut("")
          else bapp(d, c)
          continue
        }
        bapp(d, c)
      }
      closeto(2); judge(1, "")
      if (FPR) print "pr"
      else if (FSP) print "push"
      else if (FEX != "") print FEX
    }'
}

# Which command rule (if any) fires: prints api | pr | push | xgate | xpush | nothing. Prefilter
# first (no fork on a miss); a rule-7 declaration ($_EXV) sends the command straight to the scan.
_command_rule() {
  [ -n "${_EXV:-}" ] || _steer_prefilter "$1" || return 0
  _steer_scan "$1"
}

# --- rule 7 (#343) helpers: EXPENSIVE GATE PROFILE -------------------------------------------
# OPT-IN: a repo declares its expensive-profile env var(s) in `.gates.toml` as
# `[steer] expensive_profile_env = ["SW_GATE_FULL"]`; no declaration -> rule silent everywhere.
# Fork-free prefilter: a `=` (an assignment shape) plus a gate/upload word. Only past it do we walk
# up from the payload cwd (fork-free `[ -e ]` tests) to the nearest dir holding `.git` and read
# its `.gates.toml` with ONE python3 tomllib fork (the grammar gate-runner.py itself requires, so a
# hand-rolled TOML subset cannot silently drop a valid declaration). Nothing is cached.
# NOT DONE: concurrent-gate lock detection. gate-runner.py takes no lock, and the lock a consumer's
# own gate takes has no declared location, so there is nothing cheap and deterministic to test.
_x7_prefilter() {
  local c="$1"
  [[ $c == *=* ]] || return 1
  [[ $c == *\\$'\n'* || $c == *gate-runner* || $c == *push* ]]
}

# Print the declared var names (space-separated, identifiers only) for the repo holding $1.
_x7_declared() {
  local d="$1"
  case "$d" in /*) ;; *) return 0 ;; esac
  while [ ! -e "$d/.git" ]; do
    [ "$d" = / ] && return 0
    d="${d%/*}"; [ -n "$d" ] || d=/
  done
  [ -f "$d/.gates.toml" ] || return 0
  # (R343-4) fork-free pre-check: a .gates.toml that never names the key (this repo, and every
  # gate-runner user that has not opted in) skips the python3 fork. `read -d ''` returns 1 at EOF;
  # that is its own statement, and the script runs `set -u` without -e, so the status is harmless.
  local _t=""
  IFS= read -r -d '' _t 2>/dev/null < "$d/.gates.toml"
  [[ $_t == *expensive_profile_env* ]] || return 0
  python3 -c '
import re, sys
try:
    import tomllib
    with open(sys.argv[1], "rb") as f:
        v = tomllib.load(f).get("steer", {}).get("expensive_profile_env", [])
    v = [v] if isinstance(v, str) else v
    print(" ".join(n for n in v if isinstance(n, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)))
except Exception:
    pass' "$d/.gates.toml" 2>/dev/null
}

# DOUBLE-SPEND: a passing /prep-pr receipt (gate-runner.py --receipt, at
# $(git rev-parse --git-dir)/prep-pr-receipt.json) whose commit_sha IS the current HEAD.
_x7_gated_at_head() {
  local out gd head sha
  out=$(git -C "$1" rev-parse --absolute-git-dir HEAD 2>/dev/null) || return 1
  gd=${out%%$'\n'*}; head=${out#*$'\n'}
  [ -f "$gd/prep-pr-receipt.json" ] || return 1
  sha=$(jq -r 'if .schema == "gate-receipt/v1" and .result == "pass" and .producer == "gate-runner"
    then .commit_sha else empty end' "$gd/prep-pr-receipt.json" 2>/dev/null) || return 1
  [ -n "$sha" ] && [ "$sha" = "$head" ]
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
  emit_warn "Redundant re-Read: '$file_path' is unchanged since you read it this session - skip it (a post-compaction re-read is the valid exception)."
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
if [ "$tool_name" = "Agent" ] && is_foreground_agent "$fg_agent" && marker_active; then
  # ONE LINE, and deliberately so (#406). This fires on EVERY foreground spawn in a marker
  # session and blocks nothing, so its body is re-read by someone who has already seen it.
  # The full argument -- why BOTH halves are required, the nested-spawn imprecision, the
  # solo-in-a-stale-marker-pane exception -- is in the comment block directly above, which is
  # where a reader who needs convincing will look. A nudge states the fix; the file states
  # the case.
  emit_warn "Foreground Agent blocks the lead console for its whole run: give it a 'name' AND omit run_in_background:false (both halves). Sanctioned if you are solo."
fi

# (2)/(3) command rules, marker-independent (#159; advisory only). ONE scan decides both; the
# prefilter inside _command_rule keeps a command with no gh api/pr shape fork-free.
if [ -n "$cmd" ]; then
  x7_dir="${hook_cwd:-$PWD}"; _EXV=""
  _x7_prefilter "$cmd" && _EXV=$(_x7_declared "$x7_dir")
  cmd_rule=$(_command_rule "$cmd" 2>/dev/null)
  # (2) raw gh-api mutation WARN.
  if [ "$cmd_rule" = "api" ]; then
    emit_warn "Use the gh-* wrapper (gh-api-get.sh / gh-comment.sh / gh-codeql-dismiss.sh / gh-codeql-autofix.sh / gh-resolve-thread.sh / gh-delete-branch.sh) instead of raw gh api."
  fi
  # (3) raw gh pr comment/create -> canonical path WARN.
  if [ "$cmd_rule" = "pr" ]; then
    emit_warn "Canonical path: 'gh pr comment' -> reply-comment.sh / gh-comment.sh; 'gh pr create' -> /prep-pr (the required gate)."
  fi
  # (6) piped safe-push WARN (#432).
  if [ "$cmd_rule" = "push" ]; then
    emit_warn "Never pipe safe-push.sh: without pipefail a pipe returns the LAST command's exit, so a refused push reads as 0. Run it bare; its exit code IS the verdict."
  fi
  # (7) expensive gate profile WARN (#343). An upload at a HEAD a gate already passed is named as
  # the double-spend it is: its pre-push hook re-runs that gate under the expensive profile.
  if [ "$cmd_rule" = "xpush" ] && _x7_gated_at_head "$x7_dir"; then
    emit_warn "Double spend: a gate already passed at this HEAD, so this expensive-profile push re-runs it as a second full pass. Push with the default profile."
  fi
  if [ "$cmd_rule" = "xgate" ] || [ "$cmd_rule" = "xpush" ]; then
    emit_warn "Expensive gate profile: run the repo's fast compile/vet step first, and decide splits before gating; the default profile catches most breaks far cheaper."
  fi
fi

exit 0
