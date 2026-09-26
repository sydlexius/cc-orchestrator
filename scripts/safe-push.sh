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
#   1. Resolves the branch (argument, or the current symbolic-ref) and its local tip
#      (refs/heads/<branch>, which need not be the checked-out HEAD).
#   2. Runs `git push` with full output captured to a log ONLY (not mirrored
#      into the caller's context). On success the one-line verification is all
#      the caller needs; on failure a BOUNDED tail of the log is surfaced. The
#      complete transcript always lives in the log file.
#   3. After push returns, queries `git ls-remote origin refs/heads/<branch>` and
#      verifies the remote SHA matches the local branch tip.
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
#   bash safe-push.sh <branch> --base release/1.2   # measure freshness against a NON-DEFAULT base
#   bash safe-push.sh <branch> --stale-ok           # DECLARE an intentional behind-base upload
#   bash safe-push.sh <branch> --ungated            # DECLARE a push with no gate receipt (#318)
#
#   NEVER pipe safe-push (`| tail`, `| head`, `| tee` ...): without `pipefail` a
#   pipeline returns the LAST command's exit code, so a refusal reads as 0 (#432).
#   Its own exit code is the verdict.
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
# GATE RECEIPT (#318): before any network step the wrapper REFUSES (exit 1) unless a passing
# `gate-receipt/v1` from gate-runner binds the TREE being pushed. The receipt is
# `<git-dir>/prep-pr-receipt.json` of the worktree that has <branch> checked out (so a push by
# name from a shared checkout finds the builder's receipt), else of the caller's own checkout.
# Missing / invalid / failing / stale (tree differs) / holding worktree dirty = REFUSED with a
# pointer to /prep-pr. --ungated is the
# declared-intent escape for a push that genuinely has no gate (a repo without gate-runner, a
# human's deliberate push); it is CONSUMED, never forwarded, and says so loudly on stderr.
#
# NOTE: the branch name must be the FIRST argument; `-u origin` is added
# automatically and must NOT be passed by the caller. Invoking it as
# `safe-push.sh -u origin <branch>` is a misuse: the leading `-u` is rejected
# (exit 2) rather than silently consumed, which previously produced a confusing
# `fatal: refs/remotes/origin/HEAD cannot be resolved to branch` error (#35).
# Likewise git-push argument order, `safe-push.sh origin <branch>`: a configured
# remote name followed by another word is rejected (exit 2) naming the correct
# form (#432); the remote is always origin and is never taken from the caller.
#
# Exit codes:
#   0 -- push succeeded AND the remote ref matches the local branch tip
#   1 -- push exited non-zero, the remote ref does not match the local branch tip, the push
#        was REFUSED as a stale-base upload (#330: definitively BEHIND the base and no
#        --stale-ok declared; --base <name> corrects a wrong base), was REFUSED for a
#        missing/invalid/failing/stale gate receipt (#318; --ungated declares none), was REFUSED as a
#        silent rewrite (no --rewrite/--rebased), the remote is
#        ahead (diverged) and must be integrated first, or the remote tip is not
#        in local history (run `git fetch origin` first so it can be classified)
#   2 -- invalid invocation (a leading flag, a leading remote name, a forwarded non-flag word or
#        ref-widening flag such as --all/--tags/--mirror: one branch per call) / not in a git
#        repo / cannot resolve branch (the named local branch does not exist)

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

# emit_log_tail: on a failure, surface a BOUNDED tail of the push transcript (kept in
# full in $LOG) plus the log path -- so the caller can diagnose without the entire
# stream being mirrored into context on every push. Factored so all failure branches
# emit identical, delimited output and cannot drift.
emit_log_tail() {
  echo "safe-push: --- last ${LOG_TAIL_LINES:-30} lines of $LOG ---" >&2
  tail -n "${LOG_TAIL_LINES:-30}" "$LOG" >&2 2>/dev/null || true
  echo "safe-push: --- end of $LOG ---" >&2
}

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
  # git-push argument order (`safe-push.sh origin <branch>`) parsed `origin` as the BRANCH and
  # failed with a "refs/heads/origin does not exist" that never named the real mistake (#432).
  # Reject a configured remote name with a usage error that does, when it is followed by a
  # non-flag word OR there is no local branch of that name (so `safe-push.sh origin` and
  # `safe-push.sh origin --dry-run` get the real remedy too; a genuine local branch named like
  # a remote, with no second word, is still pushed). NOT silently accepted-and-dropped:
  # safe-push only ever pushes to origin, so accepting `origin` would invite `upstream <b>`
  # pushing to origin anyway. Remotes are captured first (no `git remote | grep -q` pipe,
  # which pipefail can turn into a false negative on SIGPIPE).
  remotes=$(git remote 2>/dev/null || true)
  case $'\n'"$remotes"$'\n' in
    *$'\n'"$branch"$'\n'*)
      if { [ -n "${2:-}" ] && [ "${2#-}" = "$2" ]; } \
         || ! git rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1; then
        echo "safe-push: '$branch' is a git remote, not a branch: the remote is implicit (origin); use: safe-push.sh <branch> [flags]" >&2
        exit 2
      fi ;;
  esac
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
stale_ok=0
ungated=0
base_override=""
push_args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --rewrite|--rebased) rewrite_intent=1; shift ;;
    --stale-ok) stale_ok=1; shift ;;
    --ungated) ungated=1; shift ;;
    --base)
      # A VALUE is mandatory. Defaulting a missing one would silently measure against the
      # wrong base, which is the exact false-BEHIND this flag exists to prevent.
      if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
        echo "safe-push: --base requires a branch NAME (e.g. --base release/1.2)." >&2
        echo "           Usage: safe-push.sh <branch> [--base <name>] [--stale-ok] [git-push flags]" >&2
        exit 2
      fi
      base_override="$2"; shift 2 ;;
    # ONE BRANCH, EXACTLY (#318 R2). Every gate here (receipt, freshness, rewrite classification,
    # remote verification) is about <branch>; a forwarded word is an extra REFSPEC to git and a
    # --all/--mirror/--tags style flag widens the push, so either would push refs nothing checked.
    # Flags that take a SEPARATE value keep it; any other non-flag word is refused.
    -o|--push-option|--receive-pack|--exec|--repo)
      if [ "$#" -lt 2 ]; then
        echo "safe-push: $1 requires a value." >&2; exit 2
      fi
      push_args+=("$1" "$2"); shift 2 ;;
    --all|--branches|--mirror|--tags|--delete|-d|--prune|--)
      echo "safe-push: '$1' would push (or delete) refs other than '$branch', which nothing here checks; refused." >&2
      echo "           Usage: safe-push.sh <branch> [flags] - one branch per call." >&2
      exit 2 ;;
    -*) push_args+=("$1"); shift ;;
    *)
      echo "safe-push: extra argument '$1' is not a flag: git would push it as a SECOND refspec, ungated; refused." >&2
      echo "           Usage: safe-push.sh <branch> [flags] - one branch per call." >&2
      exit 2 ;;
  esac
done

local_sha=$(git rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)
if [ -z "$local_sha" ]; then
  echo "safe-push: local branch 'refs/heads/$branch' does not exist" >&2
  exit 2
fi

# --- GATE RECEIPT (#318) ---------------------------------------------------------------
# The floor's `# prep-pr-ok` override is a literal string: it attests that a gate ran without
# checking. This leg is the check, one layer OUT of the floor (which must never read a receipt:
# DESIGN-fixround-push-gate.md). It runs BEFORE the freshness block and every network step: it is
# local and cheap, and a push it refuses should not first pay for a fetch.
#
# BIND BY TREE, NOT COMMIT: /prep-pr gates and may then SQUASH, so the pushed commit's SHA can
# differ from the receipt's commit_sha while the tree is identical. Plus a clean-worktree check
# on the worktree HOLDING the branch (a receipt says what HEAD's tree was, not that the files the
# gate saw were all committed). With no holding worktree there is no working state to check.
#
# FAIL CLOSED on the push it guards: every "could not tell" (no python3, an unreadable
# receipt or git-dir, a status that errors) REFUSES. The one exit is the declared --ungated.
receipt_refuse() {
  echo "safe-push: REFUSING to push '$branch': $1" >&2
  echo "          Produce a receipt for this tree: gate-runner.py --receipt <git-dir>/prep-pr-receipt.json in" >&2
  echo "          the worktree holding the branch (what /prep-pr and the /handle-review Step 7 gated push run)." >&2
  echo "          No gate exists for this push (a repo without gate-runner, a deliberate human push)?" >&2
  echo "          Re-run with --ungated to declare it." >&2
  exit 1
}
if [ "$ungated" -eq 1 ]; then
  echo "safe-push: --ungated DECLARED: the gate-receipt check was SKIPPED for this push." >&2
  echo "          Nothing verified that a gate passed on the tree being pushed." >&2
else
  # The worktree that has <branch> checked out, by EXACT `branch refs/heads/<b>` line. MORE than
  # one (a `worktree add --force`) REFUSES: which receipt and which working state would be ambiguous.
  # A DETACHED worktree does not hold the branch (git lets it be checked out elsewhere), so it is
  # skipped - EXCEPT one mid-rebase of <branch> (git records that in rebase-*/head-name), which
  # REFUSES: the branch is being rewritten there and its working state is not a gated tree.
  holder=""
  n_holders=0
  wt_cur=""
  wt_list=$(git worktree list --porcelain 2>/dev/null || true)
  while IFS= read -r wt_line; do
    case "$wt_line" in "worktree "*) wt_cur="${wt_line#worktree }" ;; esac
    if [ "$wt_line" = "branch refs/heads/$branch" ]; then
      [ -n "$holder" ] || holder="$wt_cur"
      n_holders=$((n_holders + 1))
    elif [ "$wt_line" = "detached" ]; then
      wt_gd=$(git -C "$wt_cur" rev-parse --absolute-git-dir 2>/dev/null || true)
      for hn in "$wt_gd/rebase-merge/head-name" "$wt_gd/rebase-apply/head-name"; do
        if [ -n "$wt_gd" ] && [ -f "$hn" ] && [ "$(cat "$hn" 2>/dev/null)" = "refs/heads/$branch" ]; then
          receipt_refuse "'$wt_cur' is mid-rebase of '$branch'; finish or abort the rebase first."
        fi
      done
    fi
  done <<<"$wt_list"
  if [ "$n_holders" -gt 1 ]; then
    receipt_refuse "$n_holders worktrees have it checked out; remove the extra checkout so one worktree holds it."
  fi
  if [ -n "$holder" ]; then
    receipt_dir=$(git -C "$holder" rev-parse --absolute-git-dir 2>/dev/null || true)
    [ -n "$receipt_dir" ] || receipt_refuse "cannot resolve the git-dir of '$holder', the worktree holding it (removed without 'git worktree prune'?)."
  else
    receipt_dir="$git_dir"
  fi
  receipt="$receipt_dir/prep-pr-receipt.json"
  [ -f "$receipt" ] || receipt_refuse "no gate receipt at $receipt."
  command -v python3 >/dev/null 2>&1 || receipt_refuse "python3 is required to read the receipt and was not found."
  # FULL VALIDATOR when present: orchestrate_schemas.py (the gate-receipt/v1 source of truth) is
  # found beside this script on the repo/plugin legs. It is NOT in HELPER_NAMES - adding it there
  # pulls in the steer canonical-list lockstep - so a DEPLOYED copy (~/.claude/scripts, which is
  # what the pr-shipper runs) usually has none, and refusing on its absence would refuse every
  # shipper push. Absent, the INLINE check below still runs and still refuses on any doubt.
  validator=""
  for cand in "$(dirname "$0")/orchestrate_schemas.py" \
              "${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/scripts/orchestrate_schemas.py}"; do
    if [ -n "$cand" ] && [ -f "$cand" ]; then validator="$cand"; break; fi
  done
  if [ -n "$validator" ]; then
    if ! v_out=$(python3 "$validator" --validate gate-receipt/v1 "$receipt" 2>&1); then
      receipt_refuse "the receipt at $receipt is not a valid gate-receipt/v1 ($(printf '%s' "$v_out" | tr '\n' ' '))."
    fi
  else
    echo "safe-push: note: orchestrate_schemas.py not found; checking the receipt's load-bearing fields only." >&2
  fi
  # INLINE CHECK, ALWAYS RUN (validator present or not). It verifies exactly the fields this push
  # decision rests on: the object is JSON, schema == gate-receipt/v1, producer == gate-runner (a
  # wrong-tool or hand-rolled artifact, not a forger: the threat model is an honest agent on the
  # obvious path, as in elmer-enqueue.sh), result == pass, and tree_sha is a 40-hex SHA (printed
  # lower-cased for the tree bind below). It does NOT verify commit_sha, worktree or steps[]: the
  # bind is by TREE, and none of those change what is pushed. On success it prints the tree; on
  # any doubt it prints the reason and exits nonzero, which REFUSES.
  # STDOUT ONLY is captured: a stray interpreter warning on stderr must not reach the tree bind.
  if ! r_tree=$(python3 -c 'import json, re, sys
def die(msg):
    print(msg)
    sys.exit(1)
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    die("the receipt is not readable JSON (%s)" % e)
if not isinstance(d, dict):
    die("the receipt is not a JSON object")
for k, want in (("schema", "gate-receipt/v1"), ("producer", "gate-runner"), ("result", "pass")):
    if d.get(k) != want:
        die("receipt %s is %s, expected %s%s" % (k, json.dumps(d.get(k)), json.dumps(want),
            " (the gate did not pass)" if k == "result" else ""))
t = d.get("tree_sha")
if not isinstance(t, str) or not re.fullmatch("[0-9a-fA-F]{40}", t):
    die("receipt tree_sha is %s, not a 40-hex SHA" % json.dumps(t))
print(t.lower())' "$receipt" 2>/dev/null); then
    receipt_refuse "$(printf '%s' "${r_tree:-the receipt could not be checked}" | tr '\n' ' ')"
  fi
  branch_tree=$(git rev-parse --verify --quiet "refs/heads/$branch^{tree}" 2>/dev/null || true)
  [ -n "$branch_tree" ] || receipt_refuse "cannot resolve the tree of refs/heads/$branch."
  if [ "$r_tree" != "$branch_tree" ]; then
    receipt_refuse "STALE receipt: it gated tree $r_tree, but refs/heads/$branch is tree $branch_tree (the branch changed after the gate ran)."
  fi
  if [ -n "$holder" ]; then
    if ! wt_status=$(git -C "$holder" status --porcelain --untracked-files=normal 2>/dev/null); then
      receipt_refuse "cannot read the status of '$holder', the worktree holding it."
    fi
    [ -z "$wt_status" ] || receipt_refuse "'$holder' (the worktree holding it) has uncommitted or untracked changes, so the gated files may not be the pushed ones."
  fi
  echo "safe-push: gate receipt verified (tree $branch_tree)." >&2
fi

# --- BASE FRESHNESS (#330) ------------------------------------------------------------
# The rewrite classifier below asks only about this branch's OWN remote ref. It never asks
# whether the branch is behind the BASE, so the first additive upload of a stale-base branch passed
# clean and opened a PR on a stale base. base-freshness.sh (#282) already answers exactly that
# question; it was simply unwired here, and a check that exists but is not wired where it
# matters is indistinguishable from no check (the #324 shape).
#
# GIT-ONLY, DELIBERATELY. No `gh` is called here and none should be: this is the most-used
# script in the repo, and a `gh` lookup would put a network dependency (and a rate-limit /
# auth failure mode) on every push. PR review state is therefore not consulted - the caller
# DECLARES intent with --stale-ok, mirroring the existing --rewrite intent flag.
#
# --base IS THE FIX FOR A NON-DEFAULT BASE, NOT --stale-ok. A backport off release/1.2
# measured against origin/HEAD yields a FALSE behind-count. If the only escape were the
# override, backport authors would learn to reach for it reflexively, training the override
# to mean "dismiss the guard" rather than "the gate genuinely passed" - the corrosion #345
# documents. A wrong base gets a CORRECT remedy instead.
#
# BLOCKS ONLY ON A DEFINITIVE BEHIND (the helper's exit 1). Its exit 0 covers fresh AND
# unknown by design, so an unreachable origin, a shallow clone, or an unresolvable base
# DEGRADES to a report and never blocks a push.
if [ "$stale_ok" -eq 1 ]; then
  echo "safe-push: --stale-ok declared; base-freshness gate skipped for this push." >&2
else
  # Resolve the base git-only: an explicit --base wins, else the recorded default branch.
  # Never hard-coded, so a non-main base is correct by construction (the helper's contract).
  fresh_base="$base_override"
  if [ -z "$fresh_base" ]; then
    fresh_base=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
    fresh_base="${fresh_base#origin/}"
  fi
  if [ -z "$fresh_base" ]; then
    # origin/HEAD is commonly unset on a fresh clone. Say so and PROCEED: guessing `main`
    # here would reintroduce exactly the hard-coded base the helper refuses to assume.
    echo "safe-push: freshness: unknown - no base could be resolved (origin/HEAD unset; pass --base <name> to check)." >&2
  else
    bf=""
    for cand in "$(dirname "$0")/base-freshness.sh" "${CLAUDE_PLUGIN_ROOT:-}/scripts/base-freshness.sh"; do
      [ -f "$cand" ] && { bf="$cand"; break; }
    done
    if [ -z "$bf" ]; then
      echo "safe-push: freshness: unknown - base-freshness.sh not found; skipping the check." >&2
    else
      # Capture rather than inherit stdout: the helper's one labeled line is surfaced on
      # stderr with safe-push's own prefix, keeping this wrapper's output shape stable.
      #
      # MEASURE THE BRANCH BEING PUSHED, NOT THE CHECKOUT (#457). safe-push pushes <branch> by
      # NAME, so HEAD may be any other branch: measuring HEAD falsely refused a fresh branch
      # pushed from a stale checkout and falsely passed a stale one pushed from a fresh checkout.
      # The full refs/heads/ form is unambiguous for THIS rev-parse (a same-named tag cannot shadow
      # it) and makes the helper's labeled line name the branch actually measured. The push below
      # needs the same treatment separately (#466).
      set +e
      fresh_out=$(bash "$bf" "$fresh_base" "refs/heads/$branch" 2>&1)
      fresh_rc=$?
      set -e
      case "$fresh_rc" in
        1)
          echo "safe-push: REFUSING a stale-base push of '$branch'." >&2
          echo "          $fresh_out" >&2
          echo "          Refresh ADDITIVELY, never with a rebase (a rewrite orphans every fix SHA cited in review replies):" >&2
          echo "            git merge origin/$fresh_base       # with '$branch' checked out" >&2
          echo "            gh pr update-branch <n>            # for an OPEN PR (default merge-commit mode)" >&2
          echo "          Wrong base? Pass --base <name> (e.g. a backport's real base) - that is the fix, not the override." >&2
          echo "          Deliberately uploading behind-base WIP? Re-run with --stale-ok to declare it." >&2
          exit 1 ;;
        0)
          # fresh OR unknown - both non-blocking. Stay quiet on a plain 'fresh', but ALWAYS
          # surface an unknown: a degraded answer is the one a caller most needs to see.
          #
          # MATCH THE FULL LABEL, NOT `*fresh*`. The word "freshness" CONTAINS "fresh", so the
          # substring glob also matched `freshness: unknown - ...` and silently discarded every
          # degraded report - the exact "reports nothing and is believed" failure this whole
          # gate exists to prevent, hidden inside the gate itself. An exit-code-only assertion
          # kept it green (CR caught it; harness assertion added alongside this fix).
          case "$fresh_out" in
            *"freshness: fresh"*) : ;;
            *)
              # An `if`, not `[ -n ... ] && echo`: under `set -euo pipefail` an empty
              # $fresh_out makes the test the arm's LAST command, so the arm returns 1 and
              # errexit kills a push that had passed every gate.
              if [ -n "$fresh_out" ]; then
                echo "safe-push: $fresh_out" >&2
              fi ;;
          esac ;;
        *)
          echo "safe-push: freshness: unknown - base-freshness.sh exited $fresh_rc; proceeding." >&2 ;;
      esac
    fi
  fi
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

# Capture git push's full output to $LOG ONLY -- never mirrored into the caller's
# context. On success the one-line verification below is all the caller needs; on
# failure emit_log_tail surfaces a bounded tail. The if/then/else is REQUIRED for
# set -e safety: `if cmd; then` suspends set -e for the condition, so a non-zero
# push lands in the else with its real exit code intact. A bare
# `git push ... >"$LOG" 2>&1; push_status=$?` would instead ABORT the whole script
# at the push line under `set -euo pipefail` -- never capturing the code, never
# verifying the ref, never emitting the tail: the exact silent-failure this wrapper
# exists to prevent. (No pipe now, so `set -o pipefail` is neither needed nor used.)
echo "safe-push: pushing $branch ($local_sha) to origin" >&2
push_status=0
#
# FULL REFSPEC, NEVER THE BARE NAME (#466). A bare `git push origin <b>` resolves <b> as a SOURCE
# ref, and a same-named TAG makes that ambiguous: git fails "src refspec <b> matches more than
# one". refs/heads/<b>:refs/heads/<b> names exactly the branch this script classified and
# verifies; -u still records the upstream (the source is a local branch), and a forwarded
# --force-with-lease leases that same destination ref.
if git push -u origin "refs/heads/$branch:refs/heads/$branch" ${push_args[@]+"${push_args[@]}"} >"$LOG" 2>&1; then
  push_status=0
else
  push_status=$?
fi

# Independent verification: read the remote ref directly. ls-remote bypasses
# any local cache (no `git fetch` needed) and returns the authoritative SHA
# from origin. A "successful" push that somehow didn't update the ref (the
# silent-failure mode this wrapper guards against) will show here.
remote_line=$(git ls-remote origin "refs/heads/$branch" 2>/dev/null || true)
remote_sha=${remote_line%%$'\t'*}

if [ "$push_status" -ne 0 ]; then
  echo "safe-push: git push exited $push_status" >&2
  emit_log_tail
  exit 1
fi

if [ -z "$remote_sha" ]; then
  echo "safe-push: git push exited 0 but origin has no '$branch' ref" >&2
  echo "          local $branch: $local_sha" >&2
  emit_log_tail
  exit 1
fi

if [ "$remote_sha" != "$local_sha" ]; then
  echo "safe-push: git push exited 0 but origin/'$branch' does not match the local branch tip" >&2
  echo "          local:  $local_sha" >&2
  echo "          remote: $remote_sha" >&2
  emit_log_tail
  exit 1
fi

echo "safe-push: verified origin/$branch -> $remote_sha" >&2
exit 0
