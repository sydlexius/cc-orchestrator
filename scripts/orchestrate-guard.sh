#!/usr/bin/env bash
# orchestrate-guard.sh - single PreToolUse Bash deny authority for the orchestrate floor.
# Exit 2 = block (stderr reason). Exit 0 = allow. Fails OPEN on any internal error.
#
# Two tiers:
#   Tier-1 = GENERAL bash-safety floor, MARKER-INDEPENDENT (fires every session, no
#            $TMUX/marker needed): push to main/master, bare --force/-f (non-lease),
#            `git ... --no-verify` (any git subcommand, skips git hooks), and
#            `gh ... --admin` (admin-bypass over branch protection / required reviews).
#            None of these is ever legitimate from Claude, so deny is always free.
#   Tier-2 = orchestrate-marker-gated MERGE: the `gh pr merge` CLI (#105) AND merge-by-API
#            (`gh api ... pulls/N/merge` mutating). Fires ONLY when THIS session's marker is
#            present and fresh - so a SOLO session (no marker) can merge (the maintainer's
#            /merge-pr), while a marker-active team session blocks a bot from merging.
#
# Spec: ~/.claude/skills/orchestrate/design/DESIGN-deterministic-floor.md
# NO `set -e`: a grep no-match returns 1 and is normal control flow here.
set -u

# P3-A: per-session marker is a SESSION-KEYED file under FLOOR_DIR (refcounting). #312: the
# key is $TMUX-derived when $TMUX is set, else derived from $CLAUDE_CODE_SESSION_ID - tmux is
# NOT required to run a gated session. No identifier at all => no key => never gated (see
# _session_keys / marker_active).
FLOOR_DIR="${ORCHESTRATE_FLOOR_DIR:-$HOME/.claude/orchestrate-floor.d}"
TTL_HOURS="${ORCHESTRATE_FLOOR_TTL_HOURS:-72}"
# Reject a non-positive-integer TTL (negative, decimal, "abc", 0). A bad TTL must NOT
# silently disarm the gate (TTL<=0 would make age_h<TTL always false -> never active);
# fall back to the 72h default so the security guarantee survives a typo'd override.
case "$TTL_HOURS" in ''|*[!0-9]*) TTL_HOURS=72 ;; esac
[ "$TTL_HOURS" -ge 1 ] 2>/dev/null || TTL_HOURS=72

# --- self-test: `orchestrate-guard.sh --self-test` feeds a known Tier-1 block
# payload and asserts exit 2; used by install/setup to catch a silently
# failing-open guard. Prints PASS/FAIL, exits 0 on pass, 1 on fail.
if [ "${1:-}" = "--self-test" ]; then
  rc=0
  printf '%s' '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}' \
    | "$0" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -ne 2 ]; then
    echo "orchestrate-guard self-test FAIL: expected exit 2, got $rc - guard is failing OPEN" >&2
    exit 1
  fi
  # (#186) a pure tag push is exempt from the prep-pr-ok advisory -> exit 0.
  trc=0
  printf '%s' '{"tool_name":"Bash","tool_input":{"command":"git push origin refs/tags/v0.0.0"}}' \
    | "$0" >/dev/null 2>&1 || trc=$?
  if [ "$trc" -ne 0 ]; then
    echo "orchestrate-guard self-test FAIL: tag push expected exit 0, got $trc (#186 carve-out broken)" >&2
    exit 1
  fi
  echo "orchestrate-guard self-test PASS (Tier-1 push-main blocked; tag push exempt from advisory)"
  exit 0
fi

# --- read the command: stdin JSON first, then $TOOL_INPUT env, else fail OPEN ---
cmd=""
stdin_json=""
if [ ! -t 0 ]; then
  stdin_json=$(cat 2>/dev/null)
fi
if [ -n "$stdin_json" ]; then
  cmd=$(printf '%s' "$stdin_json" | jq -r '.tool_input.command // empty' 2>/dev/null)
fi
if [ -z "$cmd" ] && [ -n "${TOOL_INPUT:-}" ]; then
  cmd=$(printf '%s' "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null)
fi
# Fail OPEN on empty read - never block on no signal.
[ -z "$cmd" ] && exit 0

# --- matchers (honest-path; whole-word, separator-aware) -------------------
# A real git push INVOCATION at COMMAND POSITION: `git` at clause start (after an optional
# env prefix, a bash/sh wrapper, and/or a path), its global options (-C <dir>, -c <kv>,
# --flag), then the `push` SUBCOMMAND. CLAUSE-START anchored (mirrors looks_like_safe_push)
# so a `git push` appearing as a quoted ARGUMENT (`pgrep/grep -f 'git push origin'`) or as
# prose inside a heredoc/echo body is NOT matched - the per-clause loop puts a real
# `cd x && git push` invocation at its own clause start, so it still blocks. Tolerates an env
# prefix (FOO=bar git push) and the lead's routine `git -C <worktree> push`. (-C/-c consume
# their following arg; other -flags do not.) FP2 (2026-06-07): the prior `(^|non-word)git...
# push` matched a git-push sequence ANYWHERE in a clause, which denied read-only inspectors
# and even the feedback-log entry documenting this very block (dogfood report). Known residual:
# a heredoc/echo body line that is ITSELF a clause-leading `git push ...` - vanishingly rare;
# string hook, honest-path.
# Command-position INTRODUCERS that still leave the NEXT word at command position, so a real
# push after them must keep being caught (the old `(^|non-word)git` matched these; the
# clause-start anchor must not regress them). Covers a subshell/group open `(`/`{`, a leading
# redirection, and the prefix builtins/keywords. A QUOTE is deliberately NOT an introducer:
# `'git push'` keeps git preceded by a quote, so the FP2 read-only-inspector / prose cases
# stay allowed. Shared by both matchers so they cannot drift. (Honest-path accepted limits:
# an env prefix BEFORE an introducer, or `eval` of a QUOTED push - evasion is out of scope.)
_INTRO='([({][[:space:]]*|[^[:space:]]*[<>][^[:space:]]*[[:space:]]+|(command|nohup|time|eval|exec|then|do|else)[[:space:]]+)*'
looks_like_git_push() {
  printf '%s' "$cmd" | grep -Eq '^[[:space:]]*'"$_INTRO"'([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*((bash|sh)[[:space:]]+)?([^[:space:]]*/)?git([[:space:]]+(-[Cc][[:space:]]+[^[:space:]]+|-[^[:space:]]+))*[[:space:]]+push([[:space:]]|$)'
}
# A real safe-push INVOCATION: the wrapper at a COMMAND position - clause start,
# after an optional env prefix (FOO=bar), a bash/sh wrapper, and/or a path
# (scripts/, ~/.claude/scripts/, ./). NOT the word "safe-push" inside a commit
# message or other prose (same prose-false-positive class that looks_like_git_push
# fixes for the push subcommand). Per-clause splitting puts a `cd x && safe-push
# ...` invocation at its own clause start, so this anchor still catches it.
looks_like_safe_push() {
  printf '%s' "$cmd" | grep -Eq '^[[:space:]]*'"$_INTRO"'([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*((bash|sh)[[:space:]]+)?([^[:space:]]*/)?safe-push(\.sh)?([[:space:]]|$)'
}
is_push() { looks_like_git_push || looks_like_safe_push; }

# (#186) A push clause that targets ONLY tag refs (a release tag push) is exempt
# from the prep-pr-ok ADVISORY (block 3) only - a tag push never goes through a PR,
# so /prep-pr (gate/review/squash) is conceptually N/A. This can NEVER weaken a
# Tier-1/Tier-2 deny: those are evaluated and exit BEFORE is_push_clause is recorded
# (so a tag push that ALSO trips main/force/--no-verify/--admin/merge is already
# hard-blocked upstream); this matcher only ever suppresses the advisory nudge.
# Recognized forms: the `--tags` flag, or a `refs/tags/<name>` destination (the
# trailing slash is required, so a branch like `refs/tags-backup` is NOT exempt).
# Accepted limitations (advisory-only, never a deny): a bare tag-NAME push
# (`git push origin v1.2.3`) is indistinguishable from a branch push by static
# matching and is NOT exempt (use the refs/tags/ form or the `# prep-pr-ok`
# override); a clause that MIXES a tag ref with a branch ref is exempted by the tag
# match and so skips the nudge for that branch - it only ever relaxes a NUDGE.
is_tag_only_push() {
  is_push || return 1
  # Strip a trailing `#...` shell comment so a tag token mentioned ONLY in a comment
  # (`git push origin feat # refs/tags/v1`) does not falsely exempt a real branch push.
  local tcmd
  tcmd=$(printf '%s' "$cmd" | sed 's/[[:space:]]#.*$//')
  printf '%s' "$tcmd" | grep -Eq '(^|[[:space:]])--tags([[:space:]]|$)' && return 0
  printf '%s' "$tcmd" | grep -Eq '(^|[[:space:]:])refs/tags/'
}

# main/master as a push DESTINATION: whole word, boundary = start/space/colon/quote.
# Quotes catch `git push origin 'main'` / "main" (the shell strips them, so it IS a
# push to main); the right-side colon catches a refspec (feat:main, HEAD:main). Slash
# is deliberately excluded so feature/main and refs/heads/main are NOT matched (the
# explicit-ref form is a non-obvious spelling, branch-protection backstop). A branch
# named maintenance/domain/main-ci is NOT matched (the boundary excludes substrings).
has_main_dest() {
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]:'\''"])(main|master)([[:space:]:'\''"]|$)'
}

# bare --force or -f, but NOT --force-with-lease (substring trap)
has_bare_force() {
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])(--force([[:space:]]|$)|-f([[:space:]]|$))'
}
has_no_verify() {
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])--no-verify([[:space:]]|$)'
}
# A git subcommand that ACTUALLY accepts `--no-verify` (the flag is a real hook-skip
# only on these: commit/push/merge/rebase/cherry-pick/am/revert; pull forwards to
# merge). SUBCOMMAND-anchored, mirroring F13: gating on `is_git && has_no_verify`
# (bare `git` word + `--no-verify` substring) false-positived on prose that merely
# mentions both - e.g. `gh issue create --title "ban --no-verify in git workflows"`
# carries the `git` word and the `--no-verify` substring yet runs NO git hook-bearing
# subcommand. Requiring one of the accepting subcommands removes that whole class. The
# irreducible residue (`git commit -m "...--no-verify..."`, since commit DOES accept the
# flag) is a documented accepted limitation (see DESIGN F30). Tolerates global opts
# between `git` and the subcommand (`git -C <dir> commit ...`) via the loose word match.
has_noverify_subcmd() {
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])(commit|push|merge|rebase|cherry-pick|am|revert|pull)([[:space:]]|$)'
}
# A `git` INVOCATION anywhere in the clause: the bare word `git` at a word boundary
# (start, or preceded by a non-[alnum_-] char) followed by whitespace or end-of-clause.
# Tolerates an env prefix (FOO=bar git ...) and `git -C <dir> ...`. Whole-word so a
# path like `/usr/bin/gitk` or a token `legitimate` is NOT matched (the leading
# boundary requires a non-word char, and `gitk`/`legit...` fail the trailing boundary).
is_git() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])git([[:space:]]|$)'
}
# A `gh pr merge ... --admin` INVOCATION. SUBCOMMAND-anchored (mirrors F13's `pr merge`
# anchoring, the same fix that stopped `gh pr create --title 'merge ...'` from matching):
# `--admin` is a real branch-protection bypass ONLY on `gh pr merge` - it is the ONLY gh
# subcommand that accepts the flag (verified against `gh <sub> --help`). Anchoring on the
# `pr ... merge` subcommand instead of a bare `gh` word + `--admin` substring removes the
# whole prose/quoted-arg false-positive class: `gh pr create --title "... --admin"`,
# `gh issue comment -b "document the --admin flag"`, and `git commit -m "... gh ... --admin"`
# all carry the `gh`+`--admin` substrings but have no `pr merge`, so none match now. The
# `gh` word may carry global flags before `pr` (`gh -R o/r pr merge`); `pr` and `merge` may
# be separated by `-R`/flags too, hence the tolerant `pr` ... `merge` ordering check.
# `--admin` itself stays whole-word (boundary start/space left, space/= /end right) so
# `--admin`, `--admin=true`, trailing `--admin` match but `--administrator` does NOT.
is_gh_admin() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])gh([[:space:]]|$)' || return 1
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])pr([[:space:]]|$)' || return 1
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])merge([[:space:]]|$)' || return 1
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])--admin([[:space:]=]|$)'
}

# NOTE (#105, supersedes the 2026-06-06 allow-list-omission gate): `gh pr merge` is now
# MARKER-GATED here (is_pr_merge, below), like merge-by-API. Rationale: the old gate omitted
# `gh pr merge` from the allow-list so CC PROMPTED the human - but that prompt drove "always
# allow" clicks that re-granted a blanket `gh pr *` rule (re-opening bot-merge; the recurring
# doctor shadow FAIL). Moving the gate to the FLOOR fixes that at the root: a deny OUTRANKS the
# allow-list, so a blanket shadow can no longer defeat it, AND `gh pr merge` can be allow-listed
# so a SOLO/non-marker session (the maintainer's own /merge-pr) runs prompt-free. The original
# objection (a deny blocks the human's own merge) is moot: the deny is MARKER-GATED (solo is not
# denied) and in a marker-active team session the human already merges from a SEPARATE terminal
# (no marker there). See skills/orchestrate/design/DESIGN-deterministic-floor.md + #105.

# merge-by-API: gh + api + a pulls/<n>/merge path AND a mutating method/field.
# A bare GET (no method, no field) is a merge-STATUS check and is allowed.
# Separator-tolerant (honest-path): gh/pflag accept the method/field value glued
# or '='-joined (--method=PUT, -XPUT, -X=PUT, --field=merge_method=..., -fkey=val),
# so the matchers accept a space, '=', or no separator - all forms gh actually
# parses into a real PUT/POST merge. A space-only match would miss these.
is_merge_api() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])gh([[:space:]]|$)' || return 1
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])api([[:space:]]|$)' || return 1
  printf '%s' "$cmd" | grep -Eq 'pulls/[0-9]+/merge' || return 1
  printf '%s' "$cmd" | grep -Eq '(--method[[:space:]=]+|-X[[:space:]=]*)(PUT|POST)' && return 0
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])(--(field|input|raw-field)[[:space:]=]|-[fF][[:space:]=]?[^[:space:]])' && return 0
  return 1
}

# gh pr merge (the CLI squash/merge), marker-gated (#105). Matches gh + `pr` followed by `merge`,
# tolerating global FLAGS (and their values) between `pr` and `merge` - so `gh pr -R owner/repo
# merge 5` and `gh pr --repo owner/repo merge 5` are caught alongside the simple adjacent form.
# NOT the word "merge" anywhere in a different subcommand - `gh pr comment`/`gh pr create`/`gh pr
# view` bodies mentioning merge are NOT matched because the regex requires `merge` as a whole word
# immediately after optional flag groups (each starting with `-`), not any token sequence.
# Global flags before `pr` (e.g. `gh -R o/r pr merge`) are already handled by clause 1 (the `gh`
# word match) and clause 2 finds `merge` after `pr` even with global flags between them. Same
# motivation is_gh_admin documents (gh accepts `-R`/flags between `pr` and `merge`), but clause 2
# uses a TIGHTER flag-group regex - not is_gh_admin's independent-word greps - because this path
# lacks the `--admin` narrowing and must NOT match `pr <subcommand> ... merge` (e.g. comment bodies).
# `--admin` forms are already Tier-1 (is_gh_admin, always denied); this is the marker-gated path.
# Accepted F30 limitation: a body literally containing the phrase "pr merge" (with only flags
# between them) trips it (whole-string grep, no shell-quote parsing) - rare, reword or use `!`.
is_pr_merge() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])gh([[:space:]]|$)' || return 1
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])pr([[:space:]]+-[^[:space:]]+([[:space:]]+[^-[:space:]][^[:space:]]*)?)*[[:space:]]+merge([[:space:]]|$)'
}

# THIS session's marker present AND fresh. Keyed by $TMUX (sanitized) so one
# session's marker never gates another, and a non-tmux (solo) session - which can
# never be an orchestrate session - is never gated. mtime via GNU `stat -c %Y` with a
# BSD `stat -f %m` fallback. GNU-FIRST is deliberate: GNU's `stat -f` is --file-system and
# succeeds-with-garbage on an unknown %m (so it can't be the fallback), whereas BSD cleanly
# REJECTS `-c` (illegal option) so its fallback fires. Works on Linux/CI + macOS; without it
# stat fails on the off-platform and Tier-2 silently fails OPEN.
# THIS session's sanitized key. LC_ALL=C forces byte-oriented tr so the key is
# locale-independent and matches BOTH the setup script's byte-mode python
# sanitization (re.sub on UTF-8 bytes) AND orchestrate-authorize-merge.sh. Without
# it a multibyte $TMUX would sanitize to a different length under a UTF-8 vs C locale,
# silently diverging the sides' keys (a silent fail-open). Factored (#263 Piece B) so
# the marker gate and the merge-auth token check can NEVER drift on key derivation.
# Returns non-zero (no key) only when NEITHER identifier is available - then this is a
# session the floor can never gate, exactly as an $TMUX-less session was before #312.
#
# #312 - TWO-STEP PRECEDENCE. tmux is no longer REQUIRED to run a gated orchestrate
# session (the iTerm2 / in-process backend is supported and preferred), so the key falls
# back to the Claude Code session id. The order and the shapes are BOTH load-bearing:
#
#   1. $TMUX  -> the sanitized value, BYTE-IDENTICAL to the pre-#312 key. NO prefix. This
#      is a hard compatibility requirement, not style: reshaping it would ORPHAN the marker
#      of every CURRENTLY-ARMED session the moment this guard is redeployed, silently
#      dropping its Tier-2 gate. tmux keeps precedence so an existing tmux session is
#      bit-for-bit unaffected by this change.
#   2. $CLAUDE_CODE_SESSION_ID -> `ccsid_` + sanitized. Claude Code exports this into every
#      child process, so the guard (a PreToolUse hook subprocess) and orchestrate-setup.py
#      (a Bash-tool subprocess) both read the SAME value. VERIFIED: a subagent's id is
#      byte-identical to its lead's, so in-process teammates - what a teammate IS outside
#      tmux - key identically to the lead and stay gated.
#      The `ccsid_` prefix NAMESPACES the schemes so a sanitized session id can never
#      collide with a sanitized $TMUX.
#   3. neither -> non-zero, fail closed (no key -> no marker -> not gated), as today.
#
# WHY THIS IS THE RIGHT BOUNDARY: the key must be shared by the lead AND its teammates but
# NOT by the maintainer's separate plain terminal (which is the documented human-merge
# escape hatch). $TMUX does that across panes; the session id does it across in-process
# teammates, and the human's other terminal is a DIFFERENT Claude Code session -> different
# id -> ungated. Both halves hold. (A per-pane id like $TERM_SESSION_ID does NOT: teammates
# would fall outside the marker - the gate off for exactly the processes it must gate.)
#
# ===== DERIVATION REGISTRY - FIVE live copies. Update them TOGETHER. =====
#   1. THIS FILE, `_session_keys()`                      - the deny authority (gates merges)
#   2. scripts/orchestrate-setup.py `_session_key()`     - ARMS the marker (first-precedence)
#   3. scripts/orchestrate-authorize-merge.sh            - writes the merge-auth token
#   4. scripts/orchestrate-steer.sh `_session_keys()`    - advisory nudges (marker-gated rules)
#   5. commands/merge-pr.md (the marker-detect snippet)  - routes solo-vs-handoff
#
# This registry is LOAD-BEARING, not bookkeeping: it previously listed only 1-3, and #312's
# first pass updated exactly those three - leaving 4 and 5 silently on the old tmux-only
# derivation. An incomplete registry IS the drift mechanism. If you add a sixth copy, add it
# here; better, do not add one.
#
# All copies MUST agree byte for byte: drift means the marker is armed under one key and
# looked up under another, and the gate goes SILENTLY OFF. TWO suites pin this, and BOTH must
# be run after touching this function - they cover different halves:
#   - test-orchestrate-setup.py pins CROSS-LANGUAGE agreement (it derives the key through the
#     REAL bash function AND the real python one and asserts byte equality), plus end-to-end
#     arm-then-deny.
#   - test-orchestrate-guard.py pins this guard's own GATING BEHAVIOR under each scheme
#     (#312 cases), including the arm/check asymmetry.
# Do not edit one side alone.
#
# COLLISION, precisely: `_` is NOT in `A-Za-z0-9`, so tr maps it to itself and `ccsid_` is a
# CONVENTION, not a reserved namespace - a crafted $TMUX of literally `ccsid_a_b` would key
# the same as session id `a-b`. Unreachable in practice (a real $TMUX is a SOCKET PATH and
# always begins with `/`, which sanitizes to a leading `_`), and out of the threat model
# (honest bot, not adversarial evasion). Stated as a limitation, not a guarantee.
_sanitize_key() {
  printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9' '_'
}

# EVERY key this session could have armed under, FIRST-PRECEDENCE FIRST ($TMUX, then ccsid).
# Non-zero if none. This is the guard's ONLY key derivation - there is deliberately no
# separate single-key helper, because a second one would be dead code that the cross-language
# test could pin while the gate actually used the other (a test passing on code the guard
# never runs is worse than no test).
#
# The FIRST line is the ARMING key: it corresponds exactly to what orchestrate-setup.py's
# `_session_key()` returns and what orchestrate-authorize-merge.sh writes under, so that is
# what the cross-language agreement test compares against.
#
# Failure is propagated honestly: a sanitize failure (e.g. `tr` unreachable on a stripped
# PATH) returns NON-ZERO rather than emitting a silent empty / bare-`ccsid_` key. A single
# `printf '%s%s' "$prefix" "$(...)"` pipeline would return PRINTF's status and mask a 127.
#
# WHY THIS EXISTS (and why the gate does NOT just use _session_key). Be precise about the
# reachability here, because an overstated rationale in the floor is its own defect:
#
# $TMUX does NOT change within a session. This hook is spawned BY claude with CLAUDE's env,
# fixed at launch, so a command's own env fiddling (`env -u TMUX`, `tmux new-session`) never
# reaches the hook. Launched inside tmux -> every check sees $TMUX; launched outside -> none
# do. The scheme cannot flip under the guard's feet.
#
# The REAL asymmetry is the ARM side: orchestrate-setup.py `up` runs as a Bash TOOL CALL, so
# ITS env IS command-controllable (`env -u TMUX ... up`, or any env-sanitizing wrapper) while
# the guard's never is. So the two sides can disagree WITHOUT $TMUX ever changing: `up` arms
# under ccsid while the guard, holding claude's real $TMUX, looks under the tmux key, finds
# nothing, and ALLOWS the merge. Narrow, but real - and silent, which is what makes it worth
# ten lines.
#
# Matching ANY candidate is strictly more fail-CLOSED and provably cannot OVER-gate: a
# different session's ccsid is unique to it, and a different tmux session yields a different
# tmux key, so the maintainer's separate merge terminal is still never gated.
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

# ACTIVE if ANY candidate key (#312: tmux AND/OR ccsid) has a fresh marker. Checking every
# candidate - not just first-precedence - is what stops an env change between ARM and CHECK
# from silently orphaning the marker and disarming the gate. See _session_keys().
marker_active() {
  local key marker mtime now age_h
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    marker="$FLOOR_DIR/$key"
    [ -f "$marker" ] || continue
    mtime=$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker" 2>/dev/null) || continue
    # Fail OPEN (abandon ALL remaining candidates, not just this one) if date fails: an empty
    # `now` would arithmetic to 0, making age_h negative and the marker wrongly read as active
    # (fail-CLOSED). `date` failing is environmental, so it would fail for every candidate
    # anyway - returning here is equivalent and simpler than continuing. Deliberately unlike
    # the `mtime=... || continue` above, which is a PER-MARKER condition.
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

# #263 Piece B: does a fresh, session-scoped merge-auth token AUTHORIZE the current
# `gh pr merge` clause? The token (armed by orchestrate-authorize-merge.sh ONLY after
# the readiness oracle PASSed) lives at $FLOOR_DIR/merge-auth/<session-key> and binds
# {pr, head_sha, expiry}. DENY ON DOUBT: any missing / unreadable / non-JSON / expired
# / SHA-mismatched token returns non-zero so the caller keeps the deny. NO network I/O
# (a local file read only) - the network readiness check already happened, out of the
# floor, in the authorize helper. The SHA pin is the strong bind: the merge MUST carry
# `--match-head-commit <sha>` equal to token.head_sha, and gh itself refuses the merge
# unless that SHA is the PR's current head - so a token cannot authorize a different PR
# or a moved HEAD, and the floor need not re-parse the PR number. Reads $cmd.
merge_authorized() {
  local key tok msha tsha texp now cpr tpr
  # #312: follow the SAME candidate set as marker_active, so an arm-side/check-side scheme
  # disagreement cannot strand a legitimately-armed token under an unread name (which would
  # deny every authorized merge - the safe direction, but a broken feature). Each candidate
  # token is still this session's OWN, oracle-verified and SHA-pinned below, so accepting any
  # of them widens nothing: the bind, not the filename, is what authorizes the merge.
  tok=""
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    if [ -f "$FLOOR_DIR/merge-auth/$key" ]; then
      tok="$FLOOR_DIR/merge-auth/$key"
      break
    fi
  done <<EOF
$(_session_keys)
EOF
  [ -n "$tok" ] || return 1
  # Deny on an AMBIGUOUS pin: gh's pflag honors the LAST --match-head-commit, but we
  # validate one occurrence; if the command carries MORE THAN ONE, refuse rather than
  # risk validating a different SHA than gh will actually enforce (deny-on-doubt).
  [ "$(printf '%s' "$cmd" | grep -oE -e '--match-head-commit([[:space:]=]|$)' | wc -l | tr -d '[:space:]')" -le 1 ] || return 1
  # `-e`: the pattern begins with `--`, which grep would otherwise parse as an option.
  msha=$(printf '%s' "$cmd" | grep -oE -e '--match-head-commit[[:space:]=]+[0-9a-fA-F]{40,}' | grep -oE '[0-9a-fA-F]{40,}' | head -1)
  [ -n "$msha" ] || return 1
  tsha=$(jq -r '.head_sha // empty' "$tok" 2>/dev/null) || return 1
  texp=$(jq -r '.expiry // empty' "$tok" 2>/dev/null) || return 1
  [ -n "$tsha" ] && [ -n "$texp" ] || return 1
  case "$texp" in ''|*[!0-9]*) return 1 ;; esac   # non-numeric expiry -> deny
  now=$(date +%s) || return 1
  [ "$now" -lt "$texp" ] || return 1              # expired -> deny
  [ "$(printf '%s' "$msha" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$tsha" | tr '[:upper:]' '[:lower:]')" ] || return 1
  # Bind the PR too (defense-in-depth beyond the SHA + gh's own head check): two PRs
  # CAN share a head SHA (e.g. one head branch is the head of a base->main and a
  # base->develop PR), so require the merge command's target PR to equal token.pr.
  # Extract the bare integer arg after `merge` (tolerating flags between); deny if it
  # is absent/unparsable or mismatched (deny-on-doubt).
  # Extract the target PR as the token IMMEDIATELY after `merge` (anchored at the clause
  # start), with NO flags permitted between. This is deliberately strict: allowing flags
  # before the pr re-opens a value-flag divergence - gh's value-taking flags (-b/--body/
  # -t/--subject/-A/--author-email/-F/--body-file) can carry a bare-integer VALUE that a
  # valueless-flag regex would read as the pr while gh merges a DIFFERENT positional pr
  # (and a quoted "-body \"merge N\"" could smuggle a number too). Requiring pr-first denies
  # that ENTIRE class (you cannot out-enumerate gh's flags; demand the one shape we emit).
  # The `^` anchor prevents scanning later/quoted content; any non-pr-first layout falls to
  # deny-on-doubt. The sanctioned command authorize-merge prints is exactly pr-first:
  # `gh pr merge <pr> --squash --match-head-commit <sha>`.
  # The trailing (space|end) requires the pr to be a COMPLETE token so a malformed
  # `gh pr merge 265abc` (which gh treats as a branch, not PR 265) does not extract 265.
  cpr=$(printf '%s' "$cmd" | grep -oE '^[[:space:]]*gh[[:space:]]+pr[[:space:]]+merge[[:space:]]+[0-9]+([[:space:]]|$)' | grep -oE '[0-9]+' | head -1)
  [ -n "$cpr" ] || return 1
  tpr=$(jq -r '.pr // empty' "$tok" 2>/dev/null) || return 1
  case "$tpr" in ''|*[!0-9]*) return 1 ;; esac
  [ "$cpr" = "$tpr" ] || return 1
  return 0
}

# --- (1)+(2) hard denies, evaluated PER-CLAUSE ----------------------------
# The matchers grep the whole string, so a token in one clause of a compound
# command (e.g. `git checkout main && git push origin feat`) would otherwise trip
# a deny meant for another clause - a real false-positive on routine one-liners.
# Evaluate the Tier-1 (always) and Tier-2 (marker-gated merge-by-API) HARD denies
# against each shell clause independently. Backslash-newline CONTINUATIONS are joined
# first (so `git push origin \<nl>main` stays ONE clause and is still caught), then the
# separators && || ; | AND a bare newline each start a new clause. A bare newline is a real
# command separator: `echo hi<nl>git push origin main` is TWO commands and the second must
# still be caught - the old `tr '\n' ' '` collapse merged them into one `echo`-led clause and
# silently hid the push (FP2-round2 regression caught by the adversarial pass).
# Over-splitting only ever REDUCES false-positives: a genuine `git push ... main`
# keeps push and main adjacent in the same clause, so it still blocks. The matchers
# are pure greps (no side effects); the marker stat stays short-circuited (only
# inside the merge branch). `$orig_cmd` is restored afterwards for the advisory gate.
# Process substitution (not a pipe) so `exit 2` exits the script, not a subshell.
#
# Tier-2 covers BOTH the merge-by-API path (`gh api ... pulls/N/merge` mutating) AND the
# `gh pr merge` CLI (`is_pr_merge`, #105). Both Tier-1 and Tier-2 are exit-2 HARD denies,
# so a single first-match per-clause loop is correct (no exit-0 branch to be pre-empted).
# History: the allow-list-omission approach (prompting the human) was tried but an "always
# allow" click re-opened the bot-merge hole; REVISED to floor-gated deny (2026-06-15, #105).
orig_cmd="$cmd"
# Tracks whether ANY clause is a real push INVOCATION (command-position anchored), for the
# advisory gate after the loop. Initialized here so it is always defined under `set -u`.
is_push_clause=0
# Perf short-circuit: every per-clause check needs one of the word `push` (is_push,
# incl. safe-push), a `gh` word (merge-by-API or gh --admin), a `git` word (git
# --no-verify), or the tokens `--no-verify` / `--admin` (the broadened Tier-1 flags).
# If NONE is anywhere in the command, no check can fire - skip the clause split entirely
# so ordinary pipelines stay O(1) rather than O(clauses) and the ~5ms budget holds.
if printf '%s' "$orig_cmd" | grep -Eq 'push|--no-verify|--admin|(^|[^[:alnum:]_-])(gh|git)([[:space:]]|$)'; then
  while IFS= read -r clause || [ -n "$clause" ]; do
    cmd="$clause"
    if is_push && has_main_dest; then
      echo "BLOCKED: refusing to push main/master from Claude. Never allowed; if you (the human) truly intend it, run it yourself via the ! prefix or the GitHub UI." >&2
      exit 2
    fi
    if is_push && has_bare_force; then
      echo "BLOCKED: refusing a non-lease force push from Claude. Use --force-with-lease, or run it yourself via ! if truly intended." >&2
      exit 2
    fi
    if is_git && has_no_verify && has_noverify_subcmd; then
      echo "BLOCKED: refusing 'git ... --no-verify'. It skips git hooks (pre-commit/commit-msg/pre-push); fix the hook failure rather than bypassing it." >&2
      exit 2
    fi
    if is_gh_admin; then
      echo "BLOCKED: refusing 'gh ... --admin'. It overrides branch protection and required reviews and is not part of this workflow; satisfy the requirement (land the reviews/checks) instead of bypassing it." >&2
      exit 2
    fi
    # Tier-2 (marker-gated): the merge-by-API path AND (#105) the `gh pr merge` CLI. Both are
    # allowed in a solo/non-marker session and HARD-DENIED while THIS session's marker is fresh.
    if is_merge_api && marker_active; then
      echo "BLOCKED: merge-by-API is not allowed from Claude during an orchestrate session. The maintainer merges from a separate plain terminal or the GitHub UI." >&2
      exit 2
    fi
    # #105: the `gh pr merge` CLI, marker-gated. SOLO/non-marker -> allowed (so the maintainer's
    # /merge-pr just works prompt-free); marker active -> denied (a bot cannot merge in a team
    # session; the human merges from a SEPARATE terminal where no marker is present). A floor DENY
    # outranks the allow-list, so this stays robust even if an "always allow" click re-grants a
    # blanket gh-pr rule - the recurring shadow can no longer defeat the gate. `gh pr merge --admin`
    # is already Tier-1 (is_gh_admin) above, denied even solo.
    if is_pr_merge && marker_active; then
      # #263 Piece B: a fresh human-armed merge-auth token whose head_sha matches the
      # pinned --match-head-commit AUTHORIZES this merge (readiness was gated at arm
      # time by the deterministic oracle, out of the floor). Absent/invalid token ->
      # keep the deny. merge-by-API (is_merge_api, above) is NOT relaxed - one
      # sanctioned path. This RELAXES a deny only; it never weakens Tier-1 or the
      # merge-by-API deny, both of which are evaluated and exit before this point.
      if merge_authorized; then
        : # authorized; fall through (ALLOW)
      else
        echo "BLOCKED: 'gh pr merge' in an orchestrate session (marker active) needs a fresh merge-auth token. Run 'orchestrate-authorize-merge.sh <pr>' (it runs the readiness gate and, on PASS, arms a token), then merge with 'gh pr merge <pr> --squash --match-head-commit <sha>'. The maintainer may also merge from a SEPARATE plain terminal or the GitHub UI; in a solo session this command is allowed." >&2
        exit 2
      fi
    fi
    # Record a real push INVOCATION in THIS clause (command-position anchored) for the
    # advisory gate below. A "push" substring in a quoted arg / prose does NOT set this.
    # (#186) A pure tag push is exempt from the advisory ONLY - this runs AFTER every
    # Tier-1/Tier-2 deny above, so it can never relax a hard block, only the nudge.
    is_push && ! is_tag_only_push && is_push_clause=1
  done < <(printf '%s' "$orig_cmd" | awk '{ rec = (NR==1 ? $0 : rec "\n" $0) } END { gsub(/\\\n/, "", rec); gsub(/&&|[|][|]|;|[|]/, "\n", rec); print rec }')
fi
cmd="$orig_cmd"

# --- (3) prep-pr-ok advisory gate (feature pushes), LAST -------------------
# Fires when ANY clause was a real push INVOCATION (is_push_clause, set per-clause in the
# loop above using the command-position-anchored matchers) - NOT merely a "push" substring
# somewhere on the line. Per-clause detection is required so `make && git push origin feat`
# still hits the advisory (the whole-command anchor would miss a push after the first clause).
# The override (prep-pr-ok) is matched whole-command since it may sit in a trailing comment
# after a pipe; it can ONLY satisfy this advisory - a push-to-main already hard-blocked in the
# loop (`git push origin main # prep-pr-ok` stays blocked: Tier-1 fired before we got here).
if [ "$is_push_clause" -eq 1 ]; then
  if printf '%s' "$cmd" | grep -q 'prep-pr-ok'; then
    exit 0
  fi
  echo "BLOCKED: git push must be preceded by /orchestrate:prep-pr (gate + review + squash). If you have already run the gate this turn, append the literal comment # prep-pr-ok to override." >&2
  exit 2
fi

exit 0
