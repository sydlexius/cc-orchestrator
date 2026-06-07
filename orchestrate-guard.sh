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
#   Tier-2 = orchestrate-marker-gated MERGE only (merge-by-API: `gh api ... pulls/N/merge`
#            mutating). Fires ONLY when THIS session's marker is present and fresh.
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
# A real git push INVOCATION: the `push` SUBCOMMAND following `git` and any global
# options (-C <dir>, -c <kv>, --flag). NOT the bare word "push" appearing in a commit
# message or other prose - matching that wrongly blocked a `git commit` whose message
# said "push to main". Tolerates an env prefix (FOO=bar git push) and the lead's
# routine `git -C <worktree> push`. (-C/-c consume their following arg; other -flags
# do not.) Known residual: a literal "git push" phrase inside a commit message still
# matches - a far narrower prose case than the reported one; string hook, honest-path.
looks_like_git_push() {
  printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])git([[:space:]]+(-[Cc][[:space:]]+[^[:space:]]+|-[^[:space:]]+))*[[:space:]]+push([[:space:]]|$)'
}
# A real safe-push INVOCATION: the wrapper at a COMMAND position - clause start,
# after an optional env prefix (FOO=bar), a bash/sh wrapper, and/or a path
# (scripts/, ~/.claude/scripts/, ./). NOT the word "safe-push" inside a commit
# message or other prose (same prose-false-positive class that looks_like_git_push
# fixes for the push subcommand). Per-clause splitting puts a `cd x && safe-push
# ...` invocation at its own clause start, so this anchor still catches it.
looks_like_safe_push() {
  printf '%s' "$cmd" | grep -Eq '^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*((bash|sh)[[:space:]]+)?([^[:space:]]*/)?safe-push(\.sh)?([[:space:]]|$)'
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

# NOTE: `gh pr merge` is intentionally NOT matched/gated by this hook (REVISED
# 2026-06-06). The hook cannot PROMPT on this Claude Code (it honors a hard deny but
# ignores `permissionDecision:"ask"`), so the human-approval prompt for `gh pr merge`
# is produced by the ALLOW-LIST instead (settings.json lists the non-merge `gh pr`
# subcommands but NOT `merge`, so CC prompts; a human approves, an auto-mode bot
# stalls). Only the merge-by-API path below stays hook-gated. See DESIGN.

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

# THIS session's marker present AND fresh. Keyed by $TMUX (sanitized) so one
# session's marker never gates another, and a non-tmux (solo) session - which can
# never be an orchestrate session - is never gated. mtime via BSD `stat -f %m`
# (macOS, the ship target) with a GNU `stat -c %Y` fallback so the gate also works
# on Linux/CI - without the fallback, stat fails there and Tier-2 silently fails OPEN.
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
  mtime=$(stat -f %m "$marker" 2>/dev/null || stat -c %Y "$marker" 2>/dev/null) || return 1
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
# against each shell clause independently. EXISTING newlines are collapsed to spaces
# FIRST so a backslash-continued push to main (`git push origin \<nl>main`) stays ONE
# clause and is still caught; only the separators && || ; | start a new clause.
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
    # Tier-2 (marker-gated): ONLY the merge-by-API path. `gh pr merge` is deliberately
    # NOT matched - it is gated by the allow-list (-> CC prompts the human), since a
    # PreToolUse hook on this CC can hard-deny but cannot prompt.
    if is_merge_api && marker_active; then
      echo "BLOCKED: merge-by-API is not allowed from Claude during an orchestrate session. Merge via 'gh pr merge' (it prompts you for approval) or the GitHub UI." >&2
      exit 2
    fi
  done < <(printf '%s' "$orig_cmd" | tr '\n' ' ' | sed -E 's/(&&|[|][|]|;|[|])/\n/g')
fi
cmd="$orig_cmd"

# --- (3) prep-pr-ok advisory gate (feature pushes), LAST -------------------
# Whole-command (not per-clause): the override may sit in a trailing comment after
# a pipe. It is checked LAST and can ONLY satisfy this advisory gate - it can never
# reach a hard deny above (so `git push origin main # prep-pr-ok` stays blocked,
# since main+push share one clause and Tier-1 already fired in the loop).
if is_push; then
  if printf '%s' "$cmd" | grep -q 'prep-pr-ok'; then
    exit 0
  fi
  echo "BLOCKED: git push must be preceded by /prep-pr (gate + review + squash). If you have already run the gate this turn, append the literal comment # prep-pr-ok to override." >&2
  exit 2
fi

exit 0
