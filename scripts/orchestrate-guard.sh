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
# Spec: ~/.claude/skills/orchestrate/DESIGN-deterministic-floor.md
# NO `set -e`: a grep no-match returns 1 and is normal control flow here.
set -u

# P3-A: per-session marker is a $TMUX-keyed file under FLOOR_DIR (refcounting).
# No $TMUX => not an orchestrate session => never gated (see marker_active).
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
  if [ "$rc" -eq 2 ]; then
    echo "orchestrate-guard self-test PASS (Tier-1 push-main blocked)"
    exit 0
  fi
  echo "orchestrate-guard self-test FAIL: expected exit 2, got $rc - guard is failing OPEN" >&2
  exit 1
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
# (no marker there). See DESIGN-deterministic-floor.md + #105.

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

# gh pr merge (the CLI squash/merge), marker-gated (#105). Matches gh + the ADJACENT `pr merge`
# subcommand (pr immediately followed by the whole word merge) - NOT the word "merge" anywhere -
# so a `gh pr comment`/`gh pr create` body that merely mentions merge is not a false-positive.
# Global flags before `pr` (e.g. `gh -R o/r pr merge`) keep pr+merge adjacent, so they still match.
# `--admin` forms are already Tier-1 (is_gh_admin, always denied); this is the marker-gated path.
# Accepted F30 limitation: a body literally containing the adjacent phrase "pr merge" trips it
# (whole-string grep, no shell-quote parsing) - rare, reword or use the human `!` escape.
is_pr_merge() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])gh([[:space:]]|$)' || return 1
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])pr[[:space:]]+merge([[:space:]]|$)'
}

# THIS session's marker present AND fresh. Keyed by $TMUX (sanitized) so one
# session's marker never gates another, and a non-tmux (solo) session - which can
# never be an orchestrate session - is never gated. mtime via GNU `stat -c %Y` with a
# BSD `stat -f %m` fallback. GNU-FIRST is deliberate: GNU's `stat -f` is --file-system and
# succeeds-with-garbage on an unknown %m (so it can't be the fallback), whereas BSD cleanly
# REJECTS `-c` (illegal option) so its fallback fires. Works on Linux/CI + macOS; without it
# stat fails on the off-platform and Tier-2 silently fails OPEN.
marker_active() {
  [ -n "${TMUX:-}" ] || return 1
  local key marker mtime now age_h
  # LC_ALL=C forces byte-oriented tr so the key is locale-independent and matches the
  # setup script's byte-mode python sanitization (re.sub on UTF-8 bytes). Without it, a
  # multibyte $TMUX would sanitize to a different length under a UTF-8 vs C locale,
  # silently diverging the two sides' keys (a silent fail-open of the gate).
  key=$(printf '%s' "$TMUX" | LC_ALL=C tr -c 'A-Za-z0-9' '_')
  marker="$FLOOR_DIR/$key"
  [ -f "$marker" ] || return 1
  mtime=$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker" 2>/dev/null) || return 1
  # Fail OPEN (return inactive) if date fails: an empty `now` would arithmetic to 0,
  # making age_h negative and the marker wrongly read as active (fail-CLOSED).
  now=$(date +%s) || return 1
  age_h=$(( (now - mtime) / 3600 ))
  [ "$age_h" -lt "$TTL_HOURS" ]
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
# Tier-2 is the merge-by-API path ONLY (`gh api ... pulls/N/merge` mutating). Both
# Tier-1 and Tier-2 are exit-2 HARD denies, so a single first-match per-clause loop is
# correct (no exit-0 branch to be pre-empted). `gh pr merge` is NOT gated here - the
# allow-list makes Claude Code prompt the human for it (the hook cannot prompt on this
# CC). REVISED 2026-06-06 after a live test proved CC ignores `permissionDecision:ask`.
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
      echo "BLOCKED: 'gh pr merge' is not allowed from Claude during an orchestrate session (marker active). The maintainer merges from a SEPARATE plain terminal or the GitHub UI; in a solo session this command is allowed." >&2
      exit 2
    fi
    # Record a real push INVOCATION in THIS clause (command-position anchored) for the
    # advisory gate below. A "push" substring in a quoted arg / prose does NOT set this.
    is_push && is_push_clause=1
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
  echo "BLOCKED: git push must be preceded by /prep-pr (gate + review + squash). If you have already run the gate this turn, append the literal comment # prep-pr-ok to override." >&2
  exit 2
fi

exit 0
