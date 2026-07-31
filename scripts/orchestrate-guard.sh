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

# =====================================================================================
# PREFILTER FRAGMENT REGISTRY  (#324)
# =====================================================================================
# Defined HERE, above the special modes, because `--assert-coverage` reads
# `$_PREFILTER_PARTS` and `set -u` is in force - a later definition would abort that mode.
#
# THE COUPLING THIS SOLVES. Every per-clause deny lives inside a loop that is gated by a
# single perf short-circuit (see "Perf short-circuit" far below). If a deny's trigger token
# is absent from that short-circuit, the loop is never entered and THE DENY SILENTLY NEVER
# FIRES - it reads as correct, passes `bash -n`, and denies nothing. That happened twice, both
# times in an out-of-tree credential deny applied only to the DEPLOYED copy (#327 drift, so the
# tokens below are NOT reproducible from this repo's history): once when the deny was added with
# no short-circuit edit at all, and again when the hand-written fix registered most of its
# matcher's flags but omitted three (`--client-secret`, `--access-token`, `--auth-token`),
# leaving them matched-but-unreachable.
#
# THE RULE. Each deny declares the cheap token(s) that must appear ANYWHERE in the raw
# command for that deny to be able to fire. The short-circuit is the DERIVED UNION of these
# fragments - never a hand-maintained parallel list. A fragment must be WEAKER than (or
# equal to) its matcher: it may admit commands the matcher rejects (costing only a wasted
# clause split), but it must NEVER reject a command the matcher would block. That direction
# is the whole safety property, and `--assert-coverage` proves it per-vector.
#
# ADDING A DENY: declare its fragment here, add it to _PREFILTER_PARTS below, and add a
# BLOCK vector to `--assert-coverage` plus `test-orchestrate-guard.py`. assert-coverage fails
# loudly if a BLOCK vector cannot clear the short-circuit, so a forgotten fragment can no
# longer ship inert. Anything expensive (marker/token reads, git, network) stays OUT.
_PF_PUSH='push'                     # is_push (git push + safe-push, incl. main/force denies)
_PF_NO_VERIFY='--no-verify'         # has_no_verify (git hook-skip)
_PF_ADMIN='--admin'                 # is_gh_admin (branch-protection bypass)
# has_signing_bypass (#333). TWO spellings, so the fragment is an alternation: the flag,
# and the config knob that disables signing for one call. Deliberately WEAKER than the
# matcher (it admits `gpgsign=true`, which the matcher then rejects) - weaker is the safe
# direction, since over-admitting only costs a wasted clause split.
_PF_SIGN_BYPASS='(--no-gpg-sign|commit\.gpgsign)'
_PF_GH_GIT='(^|[^[:alnum:]_-])(gh|git)([[:space:]]|$)'   # is_git / is_merge_api / is_pr_merge

# The derived union. One `grep -Eq` at run time, exactly as before: these are concatenated
# at load, so the O(1) fast path is preserved and no extra process is spawned.
_PREFILTER_PARTS="$_PF_PUSH"
_PREFILTER_PARTS="$_PREFILTER_PARTS|$_PF_NO_VERIFY"
_PREFILTER_PARTS="$_PREFILTER_PARTS|$_PF_ADMIN"
_PREFILTER_PARTS="$_PREFILTER_PARTS|$_PF_SIGN_BYPASS"
_PREFILTER_PARTS="$_PREFILTER_PARTS|$_PF_GH_GIT"

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

# --- assert-coverage (#324): prove no deny is INERT behind the perf short-circuit ---------
# THE PROPERTY. Every per-clause deny lives inside a loop gated by one short-circuit grep. A
# deny whose trigger token is missing from that grep never fires: the loop is never entered.
# A plain block/allow vector does NOT catch this - it passes for the tokens that happen to be
# prefiltered while a missing one sits inert until some future command uses it. So this mode
# asserts the stronger, direction-correct property per vector:
#
#     for every BLOCK vector: the short-circuit MUST admit it
#
# A fragment is allowed to be WEAKER than its matcher (admitting extra commands only wastes a
# clause split). It must never be STRONGER, because that silently disables the deny. This
# checks exactly that asymmetry, which is why it catches a forgotten fragment that
# `--self-test` and the full block/allow harness both pass straight through.
#
# Vectors are the guard's OWN payload shapes, kept here so the check travels with the file
# (a fragment edit and its proof cannot drift into separate files). Add one line per new deny.
if [ "${1:-}" = "--assert-coverage" ]; then
  ac_fail=0
  # Each entry: <label>|<command that MUST be blocked>. Payloads live in this string table,
  # never on a Bash command line (the live hook greps command lines - see CLAUDE.md ISOLATION).
  ac_vectors="push-main|git push origin main
push-force|git push --force origin feat
safe-push-main|scripts/safe-push.sh main
git-no-verify|git commit --no-verify -m x
git-sign-bypass-flag|git commit --no-gpg-sign -m x
git-sign-bypass-config|git -c commit.gpgsign=false commit -m x
gh-admin|gh pr merge 1 --admin
merge-by-api|gh api -X PUT repos/o/r/pulls/1/merge
pr-merge-cli|gh pr merge 1 --squash"
  while IFS='|' read -r ac_label ac_cmd; do
    [ -n "$ac_label" ] || continue
    # 1. the short-circuit must ADMIT it (the property under test)
    # `-e` for the same reason as the live use site, and treat ONLY exit 1 as a genuine skip so
    # a malformed union is reported as a failure here instead of masquerading as "not admitted".
    ac_pf_rc=0
    printf '%s' "$ac_cmd" | grep -Eq -e "$_PREFILTER_PARTS" || ac_pf_rc=$?
    if [ "$ac_pf_rc" -eq 2 ]; then
      echo "BROKEN UNION: the derived \$_PREFILTER_PARTS is not a valid ERE (grep exit 2), so" >&2
      echo "  the live short-circuit cannot evaluate it. Check the fragments for an unbalanced" >&2
      echo "  bracket/paren or an empty branch." >&2
      ac_fail=1
      break
    fi
    if [ "$ac_pf_rc" -ne 0 ]; then
      echo "INERT: prefilter SKIPS a BLOCK vector, so its deny can never fire: $ac_label" >&2
      ac_fail=1
      continue
    fi
    # 2. and the guard must actually block it end-to-end (catches a matcher that regressed
    #    independently of the prefilter). Tier-2 vectors need a fresh marker, so accept a
    #    block under either condition rather than teaching this mode the marker lifecycle.
    ac_rc=0
    printf '%s' "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"$ac_cmd\"}}" \
      | "$0" >/dev/null 2>&1 || ac_rc=$?
    if [ "$ac_rc" -ne 2 ]; then
      case "$ac_label" in
        merge-by-api|pr-merge-cli)
          # marker-GATED: ALLOW (exit 0) in a solo session is correct by design. But pin that
          # exactly rather than accepting "anything but 2": a guard that ABORTS (rc=1, or any
          # error) is not an intentional allow, and treating it as one would let this leg pass
          # silently on a crashing guard - a green check that proves nothing.
          if [ "$ac_rc" -ne 0 ]; then
            echo "GUARD ERROR: $ac_label expected exit 0 (solo allow) or 2 (gated block), got $ac_rc" >&2
            ac_fail=1
          fi
          ;;
        *)
          echo "NOT BLOCKED: $ac_label expected exit 2, got $ac_rc" >&2
          ac_fail=1
          ;;
      esac
    fi
  done <<AC_EOF
$ac_vectors
AC_EOF
  # --- part 2: prove every FRAGMENT is load-bearing (no shielded-by-overlap blind spot) -----
  # WHY THIS IS NEEDED. Part 1 alone can pass while a fragment is missing, because a vector may
  # clear the short-circuit via a DIFFERENT fragment. Measured example: dropping $_PF_NO_VERIFY
  # keeps `git commit --no-verify -m x` admitted, because the same string also carries the `git`
  # word matched by $_PF_GH_GIT. The vector looks like it covers the no-verify fragment and does
  # not. A coverage vector is only diagnostic for fragment F when F is the SOLE reason the
  # command clears the prefilter.
  #
  # So: for each fragment, assert at least one BLOCK vector is admitted by that fragment ALONE.
  # A fragment with no such vector is UNPROVEN - it could be deleted with every test still
  # green, which is exactly how an inert deny ships.
  # Iterate name|pattern pairs rather than `eval`-ing a variable name. Two reasons: the linter
  # can see every assignment (indirect expansion reads as an unassigned variable), and the deny
  # authority never evals a derived string. NB a comment line must not START with the linter's
  # name or it is parsed as a directive and errors out.
  ac_frags="_PF_PUSH|$_PF_PUSH
_PF_NO_VERIFY|$_PF_NO_VERIFY
_PF_ADMIN|$_PF_ADMIN
_PF_SIGN_BYPASS|$_PF_SIGN_BYPASS
_PF_GH_GIT|$_PF_GH_GIT"
  while IFS='|' read -r ac_frag_name ac_frag; do
    [ -n "$ac_frag_name" ] || continue
    # SUBSUMED fragments: deliberately NOT independently provable, with the reason recorded.
    # Running this check for the first time surfaced that two fragments are strictly redundant,
    # which is worth stating rather than papering over with a contrived vector:
    #   _PF_NO_VERIFY - its deny is `is_git && has_no_verify && has_noverify_subcmd`, so a
    #                   blockable command ALWAYS carries the `git` word and is already admitted
    #                   by _PF_GH_GIT.
    #   _PF_ADMIN     - `is_gh_admin` is anchored on `gh ... pr ... merge`, so a blockable
    #                   command ALWAYS carries the `gh` word, likewise already admitted.
    #   _PF_SIGN_BYPASS - same shape as _PF_NO_VERIFY: its deny is
    #                   `is_git && has_signing_bypass && has_noverify_subcmd`, so a blockable
    #                   command ALWAYS carries the `git` word (#333).
    # They are kept because they are free (one alternation branch, no extra process) and they
    # keep each deny's trigger self-documenting at its declaration site. If either matcher is
    # ever loosened to fire WITHOUT a gh/git word, its fragment stops being redundant and must
    # move out of this list and gain an isolating vector.
    case "$ac_frag_name" in
      _PF_NO_VERIFY|_PF_ADMIN|_PF_SIGN_BYPASS) continue ;;
    esac
    ac_sole=0
    while IFS='|' read -r ac_label ac_cmd; do
      [ -n "$ac_label" ] || continue
      # admitted by THIS fragment...
      # `-e` is REQUIRED: a fragment can begin with `--` (e.g. `--no-verify`), which grep
      # would otherwise parse as one of its own flags ("unrecognized option").
      printf '%s' "$ac_cmd" | grep -Eq -e "$ac_frag" || continue
      # ...and by NO other fragment (so this vector ISOLATES it). Same name|pattern table,
      # skipping the fragment under test.
      ac_others=0
      while IFS='|' read -r ac_other_name ac_other; do
        [ -n "$ac_other_name" ] || continue
        [ "$ac_other_name" = "$ac_frag_name" ] && continue
        if printf '%s' "$ac_cmd" | grep -Eq -e "$ac_other"; then ac_others=1; break; fi
      done <<AC_FRAGS2
$ac_frags
AC_FRAGS2
      if [ "$ac_others" -eq 0 ]; then ac_sole=1; break; fi
    done <<AC_EOF2
$ac_vectors
AC_EOF2
    if [ "$ac_sole" -eq 0 ]; then
      echo "UNPROVEN FRAGMENT: $ac_frag_name has no BLOCK vector that it ALONE admits, so" >&2
      echo "  deleting it would leave every test green. Add an isolating vector." >&2
      ac_fail=1
    fi
  done <<AC_FRAGS
$ac_frags
AC_FRAGS
  # --- part 3: prove the LIVE short-circuit actually uses the derived union -----------------
  # WHY. Parts 1 and 2 both evaluate `$_PREFILTER_PARTS`, the REGISTRY. If someone hand-writes
  # a literal alternation at the short-circuit's use site instead of interpolating the derived
  # variable, both parts keep passing while the guard runs a DIFFERENT, possibly incomplete
  # pattern - the exact hand-maintained-list bug this whole mode exists to prevent, just moved
  # one line. Caught by mutation testing: replacing the use site with a literal that omits the
  # gh/git branch passed parts 1 and 2 cleanly.
  #
  # HOW, and why NOT a source-text grep. The first version grepped this file for the use site's
  # literal text. Two defects, both found by adversarial review: `grep` matches COMMENT lines, so
  # a decoy comment carrying that text made the check pass while both Tier-2 denies went dead
  # (verified: merge payloads flipped from BLOCK to ALLOW with assert-coverage still PASSing);
  # and it pinned the exact spelling, so hardening the use site (adding `-e`) FAILED the check
  # that was supposed to protect it - the gate rejected the fix and passed the vulnerable form.
  #
  # Instead, extract the EXECUTABLE gate line (the `printf ... | grep -Eq ... "$orig_cmd"`
  # pipeline, excluding comments) and require that it references the registry variable by name.
  # That is spelling-tolerant about flags while still catching a hand-written literal, and a
  # comment can no longer satisfy it. Degrades to a WARNING when `$0` is unreadable, since a
  # read failure must never manufacture a false FAIL.
  if [ -r "$0" ]; then
    # Non-comment lines only (strip anything whose first non-blank char is `#`), narrowed to the
    # gate's own subject `$orig_cmd` piped into a grep. SC2016 is deliberate here and below: the
    # point is to find the LITERAL source text `$_PREFILTER_PARTS`, so it must not expand.
    # SELF-MATCH HAZARD, hit twice while writing this. Any search built from the gate's own
    # tokens also matches THIS code, because the searcher necessarily contains what it searches
    # for. Two earlier attempts reported their own source line as "the gate line".
    #
    # Fix: locate the gate by a UNIQUE MARKER COMMENT placed at the gate, then inspect the line
    # AFTER it. The marker string appears in exactly one other place (the grep below), and the
    # `-A 1 | tail -1` step means what gets INSPECTED is always the following line, never the
    # matching one - so a self-match cannot satisfy the check.
    ac_gate_line=$(grep -A 1 -F '#PREFILTER-GATE-BELOW' "$0" | tail -1)
    ac_gate_ok=0
    # SC2016 is deliberate: the point is to find the LITERAL source text `$_PREFILTER_PARTS`,
    # so single quotes are required and expansion must NOT happen.
    # shellcheck disable=SC2016
    if [ -n "$ac_gate_line" ]; then
      printf '%s' "$ac_gate_line" | grep -Fq '$_PREFILTER_PARTS' && ac_gate_ok=1
    fi
    if [ -z "$ac_gate_line" ]; then
      echo "DRIFT: cannot locate the clause-loop short-circuit line in \$0 - it was renamed or" >&2
      echo "  restructured, so this check can no longer verify it. Update assert-coverage." >&2
      ac_fail=1
    fi
    if [ -n "$ac_gate_line" ] && [ "$ac_gate_ok" -ne 1 ]; then
      echo "DRIFT: the clause-loop short-circuit does not interpolate \$_PREFILTER_PARTS." >&2
      echo "  A hand-written literal there defeats this whole check - the registry would be" >&2
      echo "  verified while the guard runs something else. Restore the derived union." >&2
      echo "  gate line: $ac_gate_line" >&2
      ac_fail=1
    fi
  else
    echo "assert-coverage WARN: cannot read \$0 ($0); skipped the use-site drift check" >&2
  fi

  if [ "$ac_fail" -ne 0 ]; then
    echo "assert-coverage FAIL - a deny is unreachable, regressed, or unproven (see above)" >&2
    exit 1
  fi
  echo "assert-coverage PASS (every BLOCK vector clears the short-circuit; every fragment is load-bearing)"
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
# QUOTE-TOLERANT BOUNDARIES. A shell-quoted flag (`git commit "--no-verify"`) is the
# SAME command once the shell strips the quotes, but a whitespace-only boundary does not
# match it, so the deny was escapable by adding two characters. Found by CodeRabbit on
# #359 against the new signing-bypass deny, and verified to affect this PRE-EXISTING
# --no-verify deny identically - so both are fixed together rather than leaving a known
# hole in the older one. The `'"` in the class is the same technique has_main_dest above
# already uses for the same reason; it is not new machinery.
#
# This does NOT claim to defeat quoting generally - `--no''-verify` and `$(printf ...)`
# still evade, and adversarial evasion remains explicitly out of the threat model (F30 /
# DESIGN). It closes the ACCIDENTAL and one-keystroke-deliberate spellings an honest
# operator actually types, which is what this floor is for.
has_no_verify() {
  printf '%s' "$cmd" | grep -Eq '(^|[[:space:]'\''"])--no-verify([[:space:]'\''"]|$)'
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
# Commit-signing bypass (#333): `--no-gpg-sign`, or `-c commit.gpgsign=<false-ish>`.
# SAME argument as the --no-verify deny - both silence a gate to keep moving - and the
# same SUBCOMMAND anchoring, so `gh issue create --title "ban --no-gpg-sign in git ..."`
# carries both words yet runs no signing-bearing subcommand and is ALLOWED.
#
# THE CONFIG LEG MATCHES ONLY THE DISABLING VALUE. `gpgsign=true` must pass: a command
# that ENABLES signing is the opposite of the act being denied, and denying it would be
# both wrong and a training signal that the override means "dismiss". git's boolean
# parser accepts false/no/off/0 (case-insensitively) and an EMPTY value as false, so all
# are matched; `[[:space:]]*=[[:space:]]*` tolerates spacing around the `=`.
#
# Deny-on-doubt does NOT apply to the enabling form: this is a POSITIVE match on a
# disabling spelling, not an inference from absence, so an unrecognized value simply
# does not match and the command is allowed. That is the correct direction - a novel
# spelling of "disable" is a gap to close with a case, never a reason to block signing.
# Boundaries are QUOTE-TOLERANT for the reason documented on has_no_verify above:
# `git commit "--no-gpg-sign"` and `git -c 'commit.gpgsign=false' commit` are the same
# commands after the shell strips the quotes, and a whitespace-only boundary let both
# through (CR, #359). The config leg needs the quote on BOTH sides - the opening quote
# may precede `commit.gpgsign` and the closing one may follow the value.
has_signing_bypass() {
  printf '%s' "$cmd" | grep -Eq -e '(^|[[:space:]'\''"])--no-gpg-sign([[:space:]'\''"]|$)' \
    -e '(^|[[:space:]'\''"])commit\.gpgsign[[:space:]]*=[[:space:]]*([Ff][Aa][Ll][Ss][Ee]|[Nn][Oo]|[Oo][Ff][Ff]|0)?([[:space:]'\''"]|$)'
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
# ===== DERIVATION REGISTRY - SIX live copies. Update them TOGETHER. =====
#   1. THIS FILE, `_session_keys()`                      - the deny authority (gates merges)
#   2. scripts/orchestrate-setup.py `_session_key()`     - ARMS the marker (first-precedence)
#   3. scripts/orchestrate-authorize-merge.sh            - writes the merge-auth token
#   4. scripts/orchestrate-steer.sh `_session_keys()`    - advisory nudges (marker-gated rules)
#   5. commands/merge-pr.md (the marker-detect snippet)  - routes solo-vs-handoff
#   6. scripts/orchestrate-resources.py `_marker_key()`   - lease liveness (GC reclaim)
#
# This registry is LOAD-BEARING, not bookkeeping. It listed 1-3, and #312's first pass updated
# exactly those three - leaving 4 and 5 on the old tmux-only derivation. Corrected to five; an
# adversarial round then found 6 (which #312 had newly BROKEN: a tmux-only key reported "no
# marker" for an armed non-tmux session, so the lease GC reclaimed a LIVE teammate's lease and
# double-allocated its port). An incomplete registry IS the drift mechanism - each miss was
# exactly a copy nobody thought to update. If you add a seventh, add it here; better, do not.
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
# Does ONE token file authorize the current $cmd? 0 = yes, non-zero = no (deny on ANY doubt).
# Split out of merge_authorized (#312) so EVERY candidate is VALIDATED rather than only the
# first one that happens to EXIST: with both identifiers set, an expired or malformed token
# under the first-precedence key would otherwise SHADOW a valid token under the second and
# deny an authorized merge. Safe direction, but it defeats the arm/check-mismatch recovery
# this candidate set exists to provide. Command-level checks (the ambiguous-pin refusal, the
# pr extraction) stay in the caller - they are properties of $cmd, not of a token.
_token_authorizes() {
  local tok="$1" msha="$2" cpr="$3" tsha texp now tpr
  [ -f "$tok" ] || return 1
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
  tpr=$(jq -r '.pr // empty' "$tok" 2>/dev/null) || return 1
  case "$tpr" in ''|*[!0-9]*) return 1 ;; esac
  [ "$cpr" = "$tpr" ] || return 1
  return 0
}

merge_authorized() {
  local key msha cpr
  # COMMAND-level checks first (properties of $cmd, identical for every candidate token).
  # Deny on an AMBIGUOUS pin: gh's pflag honors the LAST --match-head-commit, but we
  # validate one occurrence; if the command carries MORE THAN ONE, refuse rather than
  # risk validating a different SHA than gh will actually enforce (deny-on-doubt).
  [ "$(printf '%s' "$cmd" | grep -oE -e '--match-head-commit([[:space:]=]|$)' | wc -l | tr -d '[:space:]')" -le 1 ] || return 1
  # `-e`: the pattern begins with `--`, which grep would otherwise parse as an option.
  msha=$(printf '%s' "$cmd" | grep -oE -e '--match-head-commit[[:space:]=]+[0-9a-fA-F]{40,}' | grep -oE '[0-9a-fA-F]{40,}' | head -1)
  [ -n "$msha" ] || return 1
  # Extract the bare integer arg after `merge`; deny if absent/unparsable (deny-on-doubt).
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
  # Now try EVERY candidate token and take the first that FULLY validates. Do not stop at the
  # first that merely EXISTS: an expired/malformed token under the first-precedence key would
  # shadow a valid one under the second and deny an authorized merge. Each candidate is this
  # session's OWN token, oracle-verified and SHA+PR-pinned by _token_authorizes, so trying all
  # of them widens NOTHING - the bind, not the filename, is what authorizes the merge.
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    if _token_authorizes "$FLOOR_DIR/merge-auth/$key" "$msha" "$cpr"; then
      return 0
    fi
  done <<EOF
$(_session_keys)
EOF
  return 1
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
# Perf short-circuit: every per-clause check needs one of the cheap trigger tokens declared
# in the PREFILTER FRAGMENT REGISTRY above. If NONE is anywhere in the command, no check can
# fire - skip the clause split entirely so ordinary pipelines stay O(1) rather than
# O(clauses). The pattern is the DERIVED union `$_PREFILTER_PARTS`, NOT a hand-maintained
# copy: a deny whose token is missing here can never fire, and keeping a second list in sync
# by hand has already failed twice (#324).
#
# MEASURED COST (macOS, 2026-07-29), correcting a long-standing "~5ms budget" claim in this
# comment that was never true: ~12ms for a non-matching command (bash + jq startup dominates)
# and ~42ms for a single-clause match. The per-clause loop costs roughly 28ms per additional
# clause, so the short-circuit is load-bearing for long `&&` chains - a 200-clause pipeline
# that enters the loop takes seconds. Widening a fragment widens the population that pays
# that cost, so keep fragments cheap and specific.
#
# TWO NON-OBVIOUS HARDENINGS, both load-bearing (found by adversarial review of #324):
#
# `-e` keeps the PREFILTER FUNCTIONAL. The union is interpolated, and a fragment may legitimately
# begin with `--` (`$_PF_NO_VERIFY` does). Without `-e`, such a fragment landing FIRST makes grep
# parse the whole pattern as one of its own flags and error out. Note precisely what that costs
# GIVEN the fail-closed test below: the floor still holds (an error is not exit 1, so the loop is
# ENTERED and every deny fires) - what breaks is the OPTIMIZATION, since every command then takes
# the slow path. So `-e` is defense in depth, not the safety property; measured, dropping it
# changes no verdict. It is kept so registry ORDER is never load-bearing in either direction.
#
# FAIL CLOSED on a pattern error. `grep -Eq` exits 0 = matched, 1 = definitively did not match,
# 2 = the pattern itself is broken (unbalanced bracket/paren, empty subexpression). Only exit 1
# is a real "no trigger present", so ONLY exit 1 may skip the loop; anything else enters it and
# lets the per-clause matchers decide. A bare `if` treated an ERROR as a no-match and turned a
# malformed union into a total floor bypass - verified live: a mutated fragment made a
# push-to-main payload ALLOW. Note exit 2 is also how BSD/macOS grep reports an empty
# alternation branch that GNU grep silently accepts, so this also removes a real
# grep-implementation dependence rather than a theoretical one.
# The marker below is READ BY `--assert-coverage` (part 3) to locate this gate. Keep it on the
# line immediately ABOVE the gate; assert-coverage inspects the NEXT line and will FAIL loudly
# if the marker is missing or the gate stops interpolating the derived union.
#PREFILTER-GATE-BELOW
_pf_rc=0; printf '%s' "$orig_cmd" | grep -Eq -e "$_PREFILTER_PARTS" || _pf_rc=$?
if [ "$_pf_rc" -ne 1 ]; then
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
    if is_git && has_signing_bypass && has_noverify_subcmd; then
      echo "BLOCKED: refusing to disable commit signing. An unsigned commit passes locally and then blocks the PR at a required_signatures gate, where the only fix is a history rewrite + force-push that orphans every cited fix SHA. Commit signed (the default); if signing is genuinely broken, fix the signer rather than bypassing it." >&2
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
