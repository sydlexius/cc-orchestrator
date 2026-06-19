#!/usr/bin/env bash
# pr-watch.sh -- Wait for a PR to reach a terminal review/CI state. Silent until done.
#
# Usage:
#   pr-watch.sh <pr_number> [repo] [timeout_secs]
#
# Defaults:
#   repo          auto-detected via `gh repo view` from the current dir
#   timeout_secs  1800 (30 min)
#
# Behavior:
#   Polls every 30s (silent). Exits when one of these terminal states holds:
#
#     settled head=<sha8> mergeable=<state>
#         All of: the CodeRabbit reviewer requirement is SATISFIED, the Codoki
#         check is settled, every CI check is in a terminal state, and GitHub's
#         mergeable_state is in the merge-ready set (clean / unstable / has_hooks).
#         Exit 0. Consumer next action: /merge-pr.
#
#         CR requirement SATISFIED means ANY of: (a) CodeRabbit reviewed HEAD with
#         a non-DISMISSED, non-CHANGES_REQUESTED review (CR enabled, the original
#         path - preserved), OR (b) CR will NOT review this PR -- the `norabbit`
#         label is present, or CR posted a "Review skipped" check (CR auto-review
#         disabled). Without (b), a norabbit / CR-disabled PR would wait the full
#         timeout for a review that never lands (#34). When CR IS enabled the
#         original wait-for-the-real-review behavior is unchanged.
#
#         Codoki SETTLED: Codoki posts its verdict as a `Codoki PR Review` entry in
#         statusCheckRollup, NOT in the reviews API, so it is invisible to a
#         reviews-API poll. This script defers Codoki detection to the deterministic
#         oracle `ship-gate-preflight.sh --codoki-only`, which reads statusCheckRollup
#         (#110). Exit 0 from the oracle = Codoki settled; exit 2 = not yet settled
#         (stays pending); exit 1 = oracle usage error (fail-open, not blocked). When
#         the oracle is not installed, Codoki gating is skipped (falls back to the
#         CR + CI + mergeable gates only).
#
#     review-blocked head=<sha8> by=<participant>
#         CodeRabbit's latest review on HEAD is CHANGES_REQUESTED. Exit 0.
#         Consumer next action: /handle-review.
#
#     timeout: waited <secs>s pending=<list>      [stderr]   Exit 1.
#     setup error: <message>                      [stderr]   Exit 2.
#
# Why mergeable_state matters:
#   `gh pr checks` only returns checks GitHub has been told about so far. Late-
#   registering workflows (label-triggered, paths-filter dispatch, codecov post-
#   back) can appear AFTER the visible set is terminal, producing a false-settle.
#   mergeable_state aggregates branch protection's view of "every required check
#   has reported AND no CHANGES_REQUESTED is active", so it stays `blocked` until
#   every required-but-not-yet-registered check arrives and clears.
#
# Why the empty-string trap matters (don't hand-roll this):
#   GitHub's check `conclusion` field can be the empty string `""` mid-flight.
#   jq's `// alternative` operator only falls back on null/false, so a hand-rolled
#   `(.conclusion // "in_progress")` reports `=` not `=in_progress`, and a grep
#   for `=in_progress` returns 0 -> premature SETTLED. This script uses `state`
#   (which goes through GitHub's bucket-mapping and never returns "") to avoid it.
#
# Why the quiet-period gate matters:
#   CodeRabbit posts inline comments seconds AFTER its CI check transitions to
#   SUCCESS. Greptile lands its single COMMENTED review ~20 min AFTER CR APPROVES
#   (well after CR has gone quiet). If mergeable_state happens to read `clean` in
#   either window before the next bot's findings land, a strict snapshot would
#   emit `settled` prematurely and hand the consumer a half-formed triage list.
#   Defense: count items from allow-listed bot authors (CR + Greptile +
#   github-actions) and require the count to be unchanged across two consecutive
#   polls before terminating. The poll interval is calibrated for CR's seconds-
#   scale trickle; Greptile's minutes-scale latency is covered by the same gate
#   because the count keeps incrementing until Greptile's review actually lands.

set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
if [ $# -lt 1 ] || [ $# -gt 3 ]; then
  echo "usage: pr-watch.sh <pr_number> [repo] [timeout_secs]" >&2
  exit 2
fi

pr="$1"
repo="${2:-}"
timeout_secs="${3:-1800}"
# Poll cadence. Overridable via PR_WATCH_POLL_INTERVAL (numeric seconds) so the
# external test harness can drive the loop without a 30s wait; defaults to 30 in
# all normal use. A non-numeric override is ignored (falls back to 30).
poll_interval="${PR_WATCH_POLL_INTERVAL:-30}"
case "$poll_interval" in ''|*[!0-9]*) poll_interval=30 ;; esac

if ! [[ "$pr" =~ ^[0-9]+$ ]]; then
  echo "setup error: pr_number must be numeric, got: $pr" >&2
  exit 2
fi
if ! [[ "$timeout_secs" =~ ^[0-9]+$ ]]; then
  echo "setup error: timeout_secs must be numeric, got: $timeout_secs" >&2
  exit 2
fi
if [ -z "$repo" ]; then
  repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)
fi
if [ -z "$repo" ]; then
  echo "setup error: could not resolve repo (pass it explicitly or run from inside a gh-aware repo)" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------
# CodeRabbit is the only required reviewer. The script is silent until a terminal
# state holds. The consumer dispatches off the single stdout line at the end.
CR_LOGIN='coderabbitai[bot]'

# Quiet-period allow-list: bots whose late-trickling comments would change a
# triager's view. CR posts findings as inline comments seconds AFTER its CI
# check goes SUCCESS; Greptile lands its single COMMENTED review ~20 min AFTER
# CR APPROVES. We count items from these authors and require the count to be
# unchanged across two consecutive polls before terminating. This adds ~30s of
# latency in the CR-only happy path and up to ~20 min when Greptile is enabled
# on the repo. It defends against premature settle in both windows.
#
# To add a new bot reviewer (e.g. CodeQL, Copilot, custom org-level bot):
# append its login to the disjunction. The script always reads the disjunction
# in this single location so updates are one-edit.
QUIET_AUTHORS_JQ='(.user.login == "coderabbitai[bot]" or .user.login == "github-actions[bot]" or .user.login == "greptile-apps[bot]" or .user.login == "codoki-pr-intelligence[bot]")'

# count_bot_activity -- emit a single integer: total reviews + pull-comments +
# issue-comments authored by an allow-listed bot. Stable count across two polls
# means the bot conversation has quiesced.
count_bot_activity() {
  local rev_n inline_n issue_n
  rev_n=$(echo "$reviews_json" | jq "[.[] | select($QUIET_AUTHORS_JQ)] | length")
  inline_n=$(gh api --paginate "repos/$repo/pulls/$pr/comments" 2>/dev/null \
    | jq -s 'add // []' \
    | jq "[.[] | select($QUIET_AUTHORS_JQ)] | length" 2>/dev/null || echo 0)
  issue_n=$(gh api --paginate "repos/$repo/issues/$pr/comments" 2>/dev/null \
    | jq -s 'add // []' \
    | jq "[.[] | select($QUIET_AUTHORS_JQ)] | length" 2>/dev/null || echo 0)
  echo $(( rev_n + inline_n + issue_n ))
}

# cr_review_skipped -- true (exit 0) when CodeRabbit will NOT post a review on this
# PR, detected via a `gh pr checks` entry whose name matches CodeRabbit AND whose
# description signals a skip ("Review skipped", emitted when CR auto-review is
# disabled). This is re-checked each poll because the skipped check can land a few
# seconds after the PR opens. Fails CLOSED for waiting (returns 1 = not skipped) on
# any gh/jq error, so a transient blip never falsely settles a CR-enabled PR.
cr_review_skipped() {
  local cj
  cj=$(gh pr checks "$pr" --repo "$repo" --json name,state,description 2>/dev/null) || true
  [ -z "$cj" ] && return 1
  echo "$cj" | jq -e '
    [ .[] | select(((.name // "") | test("coderabbit"; "i"))
                   and ((.description // "") | test("review skipped"; "i"))) ]
    | length > 0' >/dev/null 2>&1
}

# norabbit label => the maintainer explicitly opted this PR out of CR review, so CR
# will never post one. Checked ONCE before the loop (label state is stable). Fails
# open (treated as not-labeled) on any gh error so detection falls through to the
# per-poll skipped-check fallback.
cr_norabbit=false
if gh pr view "$pr" --repo "$repo" --json labels --jq '.labels[].name' 2>/dev/null \
     | grep -qx "norabbit"; then
  cr_norabbit=true
fi

# Oracle path for Codoki settlement (#110). Resolved once; the per-poll check is
# skipped entirely when it is absent (fail-open to CR + CI + mergeable gates).
CODOKI_ORACLE="${HOME}/.claude/scripts/ship-gate-preflight.sh"

prev_bot_count=""
start=$(date +%s)

while true; do
  elapsed=$(( $(date +%s) - start ))
  if [ "$elapsed" -ge "$timeout_secs" ]; then
    echo "timeout: waited ${elapsed}s pending=${pending:-unknown}" >&2
    exit 1
  fi

  # Pull HEAD + mergeable_state in one call. mergeable_state is GitHub's own
  # branch-protection-aware merge-readiness aggregate -- see header for why.
  # Use `gh api` with explicit error suppression: a 404 (PR doesn't exist) prints
  # the error body to stdout, so validate the SHA shape before trusting it.
  pr_meta=$(gh api "repos/$repo/pulls/$pr" --jq '[.head.sha, (.mergeable_state // "unknown")] | join("|")' 2>/dev/null || true)
  cur_head="${pr_meta%%|*}"
  cur_mergeable_state="${pr_meta##*|}"
  if ! [[ "$cur_head" =~ ^[0-9a-f]{40}$ ]]; then
    # No valid HEAD sha -> 404 or transient API error. After 3 consecutive
    # failures (~90s) bail out as a setup error rather than spin to timeout.
    api_fail_count=$(( ${api_fail_count:-0} + 1 ))
    if [ "$api_fail_count" -ge 3 ]; then
      echo "setup error: cannot fetch PR #$pr from $repo (likely 404 or auth issue)" >&2
      exit 2
    fi
    sleep "$poll_interval"
    continue
  fi
  api_fail_count=0

  reviews_json=$(gh api --paginate "repos/$repo/pulls/$pr/reviews" 2>/dev/null | jq -s 'add // []' || echo '[]')

  # Get the HEAD commit's committer date. This is the authoritative "when did
  # the current PR state come into existence" timestamp. We use it instead of
  # the review's `commit_id` field because GitHub silently REWRITES the
  # `commit_id` on every existing review when a PR is rebased -- a stale
  # review that was submitted against the pre-rebase HEAD will appear to be
  # against the new HEAD, falsely satisfying `commit_id == cur_head`. The
  # committer date moves forward on every push, so `submitted_at >=
  # head_committer_date` is a safe "did CR review the current state" check.
  head_committer_date=$(gh api "repos/$repo/commits/$cur_head" \
    --jq '.commit.committer.date' 2>/dev/null || true)

  # If we couldn't fetch the committer date (transient API blip), do NOT fall
  # back to the legacy commit_id filter: GitHub rewrites commit_id on every
  # existing review when a PR is rebased, so that path reintroduces the exact
  # stale-review bug the committer-date check exists to fix. Instead retry the
  # poll a few times; if the date never resolves, bail with a setup error
  # rather than emit a possibly-stale verdict.
  if [ -z "$head_committer_date" ]; then
    cd_fail_count=$(( ${cd_fail_count:-0} + 1 ))
    if [ "$cd_fail_count" -ge 3 ]; then
      echo "setup error: could not fetch HEAD committer date for $cur_head after 3 attempts" >&2
      exit 2
    fi
    pending="head-date-fetch"
    sleep "$poll_interval"
    continue
  fi
  cd_fail_count=0

  cr_latest_state=$(echo "$reviews_json" \
    | jq -r --arg head_date "$head_committer_date" --arg cr "$CR_LOGIN" '
        [.[] | select(.user.login == $cr and .submitted_at >= $head_date)]
        | sort_by(.submitted_at) | last | .state // ""')

  # Review-blocked is a distinct terminal: the author must address feedback
  # before merge is possible. The consumer should dispatch to /handle-review
  # instead of waiting for a settle that cannot happen. Apply the quiet-period
  # gate here too -- CR can post inline findings AFTER setting CHANGES_REQUESTED,
  # so a premature emission would hand the consumer a half-formed triage list.
  if [ "$cr_latest_state" = "CHANGES_REQUESTED" ]; then
    cur_bot_count=$(count_bot_activity)
    if [ -n "$prev_bot_count" ] && [ "$cur_bot_count" = "$prev_bot_count" ]; then
      echo "review-blocked head=${cur_head:0:8} by=cr"
      exit 0
    fi
    prev_bot_count="$cur_bot_count"
    sleep "$poll_interval"
    continue
  fi

  # Build the pending-criteria list. Empty list = settled.
  pending_list=()

  # CR must have weighed in on HEAD with a non-DISMISSED review. APPROVED and
  # COMMENTED both qualify; only "" (no review yet) and DISMISSED keep us pending.
  # EXCEPTION (#34): if CR will not review this PR at all -- the `norabbit` label
  # is set, or CR posted a "Review skipped" check (auto-review disabled) -- the CR
  # requirement is SATISFIED and we do not wait for a review that never lands. When
  # CR IS enabled, none of those signals fire and the original wait is unchanged.
  case "$cr_latest_state" in
    APPROVED|COMMENTED) ;;
    *)
      if [ "$cr_norabbit" = true ] || cr_review_skipped; then
        : # CR will not review (norabbit / Review skipped) -> requirement satisfied
      else
        pending_list+=("cr-review")
      fi
      ;;
  esac

  # Codoki settlement (#110). Codoki posts its verdict as a `Codoki PR Review`
  # entry in statusCheckRollup (NOT the reviews API), so this script defers to the
  # deterministic oracle which reads that rollup. Exit 0 = settled (do not block);
  # exit 2 = check missing/incomplete/failed (block on "codoki-check"); exit 1 =
  # oracle usage error, a script bug rather than a PR state -> fail open, do not
  # block. The oracle is skipped entirely when it is not installed.
  if [ -x "$CODOKI_ORACLE" ]; then
    if "$CODOKI_ORACLE" --codoki-only "$pr" "$repo" >/dev/null 2>&1; then
      :
    else
      codoki_rc=$?
      if [ "$codoki_rc" -eq 2 ]; then
        pending_list+=("codoki-check")
      fi
    fi
  fi

  # Every CI check must be in a terminal state. Use `state` not `conclusion` --
  # state goes through gh's bucket mapping (SUCCESS|FAILURE|...|PENDING|...) and
  # never returns the empty string that traps hand-rolled jq fallbacks.
  pending_ci=$(gh pr checks "$pr" --repo "$repo" --json state \
    --jq '[.[] | select(.state == "PENDING" or .state == "QUEUED" or .state == "IN_PROGRESS")] | length' \
    2>/dev/null || echo "unknown")
  if [ "$pending_ci" != "0" ]; then
    pending_list+=("ci(${pending_ci})")
  fi

  # mergeable_state is the branch-protection-aware aggregate. clean/unstable/
  # has_hooks are merge-ready; blocked/behind/dirty/draft/unknown are not.
  case "$cur_mergeable_state" in
    clean|unstable|has_hooks) ;;
    *) pending_list+=("merge(${cur_mergeable_state})") ;;
  esac

  if [ ${#pending_list[@]} -eq 0 ]; then
    # All hard criteria pass. Apply the quiet-period gate: require bot-comment
    # count to be unchanged from the previous poll before declaring settled.
    # The first time we land here we set the baseline and poll again.
    cur_bot_count=$(count_bot_activity)
    if [ -n "$prev_bot_count" ] && [ "$cur_bot_count" = "$prev_bot_count" ]; then
      echo "settled head=${cur_head:0:8} mergeable=${cur_mergeable_state}"
      exit 0
    fi
    prev_bot_count="$cur_bot_count"
    pending="quiet-confirm"
    sleep "$poll_interval"
    continue
  fi

  # Hard criteria not met -- discard any pending bot-count baseline; if we land
  # back in the all-pass branch later we want to re-measure from scratch.
  prev_bot_count=""
  pending=$(IFS=,; echo "${pending_list[*]}")
  sleep "$poll_interval"
done
