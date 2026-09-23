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
#         CR-waiting is OPT-IN (#173): the `cr-review` requirement is treated as
#         SATISFIED by DEFAULT, and we wait ONLY when there is positive evidence CR
#         will review this PR. SATISFIED means ANY of: (a) CodeRabbit reviewed HEAD
#         with a non-DISMISSED, non-CHANGES_REQUESTED review (the original path -
#         preserved), OR (b) an explicit opt-out -- the `norabbit` label, or a CR
#         "Review skipped" check (#34), OR (c) CR is NOT expected -- no existing
#         review, CR not in requested_reviewers, and no `@coderabbitai review`
#         trigger comment. On this org CR auto-review is OFF, so an untriggered PR
#         posts NO check at all; the old opt-OUT logic waited the full timeout for a
#         review that never lands (#173). We WAIT (pending cr-review) only when CR is
#         expected: a review is in-flight (existing review, incl. DISMISSED), CR is a
#         requested reviewer, or a review was triggered.
#
#         Codoki SETTLED: Codoki posts its verdict as a `Codoki PR Review` entry in
#         statusCheckRollup, NOT in the reviews API, so it is invisible to a
#         reviews-API poll. This script defers Codoki detection to the deterministic
#         oracle `ship-gate-preflight.sh --codoki-gate` (#237: Codoki-auto-review-OFF
#         aware, so an untriggered PR with no Codoki check never hangs), which reads statusCheckRollup
#         (#110). Exit 0 from the oracle = Codoki settled; exit 2 = not yet settled
#         (stays pending); exit 1 = oracle usage error (fail-open, not blocked). When
#         the oracle is not installed, Codoki gating is skipped (falls back to the
#         CR + CI + mergeable gates only).
#
#     review-blocked head=<sha8> by=<login[,login...]>
#         The latest HEAD review from ANY reviewer in the blocking set is
#         CHANGES_REQUESTED. Exit 0. Consumer next action: /handle-review.
#         Reviewer-agnostic (#195): not just CodeRabbit -- any bot OR human whose
#         most-recent review on the current HEAD requests changes. The `by=` field
#         lists every such reviewer's login. The blocking set defaults to "any
#         reviewer"; set PR_WATCH_BLOCKING_REVIEWERS to a comma/space-separated login
#         list to RESTRICT which reviewers can trip this terminal (e.g. to ignore a
#         specific bot's CHANGES_REQUESTED). The quiet-period gate applies here too.
#         NOTE: `settled` is UNCHANGED and still requires green CI + a merge-ready
#         mergeable_state; the "stop on comment-count increment / not-necessarily-
#         green" idea was evaluated and REJECTED (see #195) -- it would collapse the
#         two terminals and hand /merge-pr a red PR.
#
#     thread-blocked head=<sha8> unresolved=<n> failing=<n> by=<login[,login...]>
#         (#441) CI is terminal (no pending check; failing=<n> may be nonzero, it
#         counts checks in a FAILURE/ERROR/CANCELLED/TIMED_OUT/ACTION_REQUIRED/
#         STARTUP_FAILURE state from the SAME checks read - so "CI terminal" is
#         NOT "CI green"; an unreadable checks read pends as ci(unknown) and never
#         reaches this terminal, so failing is never a guessed zero), nothing else is pending
#         (no CR / Codoki wait), review-blocked did NOT fire (it keeps priority),
#         mergeable_state is `blocked`, AND a GraphQL reviewThreads READ shows at
#         least one thread with isResolved == false. `by=` is the de-duplicated
#         first-comment author of each unresolved thread. The quiet-period gate
#         applies. Exit 0. Consumer next action: /handle-review (a round that only
#         replies + resolves threads makes NO commit; verify progress with
#         pr-unreplied-comments.sh, not a new commit).
#         POSITIVE EVIDENCE ONLY: branch-protection settings are never read, and
#         `blocked` alone never terminates (it is an aggregate, #399/#334). An
#         UNREADABLE thread read (gh failure, GraphQL errors, malformed body) is
#         NOT zero threads: it emits neither thread-blocked nor settled and the PR
#         stays pending as `threads(unreadable)`. Only 100 threads are fetched; a
#         totalCount above that is REPORTED on stderr (unresolved is then a lower
#         bound), or pends as `threads(truncated)` when every fetched thread is
#         resolved.
#         KNOWN LIMIT: cr_was_triggered is NOT scoped to the current head. A
#         historical `@coderabbitai review` comment plus a push AFTER CR's last
#         review leaves `cr-review` pending (CR is "expected" but has not reviewed
#         the new head), so the sole-pending rule is never met and thread-blocked
#         is unreachable; the watch times out. The #399 stderr line names it
#         (`pending=cr-review,merge(blocked)`). Tracked separately, not fixed here.
#
#     merged head=<sha8>
#     closed head=<sha8>
#         (#435) The PR is already MERGED (merged == true) or CLOSED without merge.
#         Checked first on every poll, before any other gate; no quiet period (the
#         state cannot revert to in-progress). Exit 0. Consumer next action:
#         merged -> /post-merge-cleanup; closed -> report it, no action.
#
#     timeout: waited <secs>s pending=<list>      [stderr]   Exit 1.
#     setup error: <message>                      [stderr]   Exit 2.
#
#   Progress (#399): stdout stays silent until a terminal, but ONE stderr line
#   `pr-watch: pending=<list>` is emitted each time the composed pending set
#   CHANGES (never on an unchanged poll), so a watch held on a human gate (e.g.
#   `merge(blocked)` after a push dismissed an approval) names what it holds on.
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
# CodeRabbit is the reviewer the SETTLE path is opt-in-gated on (#173). The
# review-blocked terminal, by contrast, is reviewer-agnostic (#195). The script is
# silent until a terminal state holds; the consumer dispatches off the single stdout
# line at the end.
CR_LOGIN='coderabbitai[bot]'

# Blocking-reviewer set for the review-blocked terminal (#195). DEFAULT (unset or
# empty) is reviewer-agnostic: the latest HEAD review from ANY reviewer -- bot or
# human -- being CHANGES_REQUESTED trips review-blocked. Set
# PR_WATCH_BLOCKING_REVIEWERS to a comma/space-separated login list to RESTRICT the
# set (a reviewer outside it can no longer trip the terminal). The empty JSON array
# `[]` is the "match any" sentinel consumed by the per-poll jq below.
blocking_set_json='[]'
if [ -n "${PR_WATCH_BLOCKING_REVIEWERS:-}" ]; then
  # Split on commas AND whitespace, then build the login array in ONE jq pass:
  # `[inputs | select(length > 0)]` always emits exactly one array (empty when the
  # value reduces to zero tokens, e.g. a separator-only ",," or "   "). Deliberately
  # NO `grep` in this pipeline -- grep exits 1 on no-match, which under `set -o
  # pipefail` combined with a `|| echo '[]'` fallback double-emitted `[]\n[]` (a jq
  # STREAM, not a single JSON value), breaking the per-poll `--argjson` for the whole
  # watch and hanging it to timeout. Belt-and-suspenders: fall back to the match-any
  # sentinel `[]` if the result is somehow not a single JSON array.
  blocking_set_json=$(printf '%s' "$PR_WATCH_BLOCKING_REVIEWERS" \
    | tr ',' ' ' | tr -s ' \t' '\n' \
    | jq -Rn '[inputs | select(length > 0)]' 2>/dev/null || echo '[]')
  if ! printf '%s' "$blocking_set_json" | jq -e 'type == "array"' >/dev/null 2>&1; then
    blocking_set_json='[]'
  fi
fi

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
QUIET_AUTHORS_JQ='(.user.login == "coderabbitai[bot]" or .user.login == "github-actions[bot]" or .user.login == "greptile-apps[bot]" or .user.login == "codoki-pr-intelligence[bot]" or .user.login == "copilot-pull-request-reviewer[bot]")'

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

# cr_is_requested -- true (exit 0) when `coderabbitai[bot]` is in the PR's
# requested_reviewers list (the maintainer asked CR to review). Positive evidence
# CR will post a review. Fails CLOSED (returns 1 = not requested) on any gh/jq
# error so a transient API blip does not falsely keep an idle PR pending.
cr_is_requested() {
  gh api "repos/$repo/pulls/$pr/requested_reviewers" \
    --jq '.users[].login' 2>/dev/null \
    | grep -qxF "$CR_LOGIN"
}

# cr_was_triggered -- true (exit 0) when an issue comment carries a CR REVIEW
# trigger command. Org-wide CR auto-review is OFF, so a review only happens when
# someone posts `@coderabbitai review` or `@coderabbitai full review`. We match
# ONLY those review-triggering forms (case-insensitive, tolerating surrounding
# text); a bare `@coderabbitai` mention or `@coderabbitai resolve`/`summary`
# engages CR WITHOUT requesting a review, so those must NOT count as positive
# evidence (matching them would re-introduce the false-wait bug, #173). Fails
# CLOSED (returns 1 = not triggered) on any gh/jq error.
cr_was_triggered() {
  # A trigger is a NON-CR author posting `@coderabbitai review` / `full review`.
  # Exclude coderabbitai[bot]'s OWN comments: its auto-generated summary/walkthrough
  # boilerplate quotes the literal `@coderabbitai review` as user instructions, which
  # would otherwise false-positive every CR-touched PR back into a hang (the #173 bug).
  gh api --paginate "repos/$repo/issues/$pr/comments" 2>/dev/null \
    | jq -s 'add // []' 2>/dev/null \
    | jq -e --arg cr "$CR_LOGIN" '[ .[]
        | select(((.user.login // "") != $cr)
                 and ((.body // "")
                      | test("@coderabbitai[[:space:]]+(full[[:space:]]+)?review\\b"; "i"))) ]
             | length > 0' >/dev/null 2>&1
}

# cr_review_expected -- composite: true (exit 0) when there is POSITIVE evidence
# CR will (or already did) review this PR. CR-waiting is OPT-IN: the caller only
# adds `cr-review` to the pending list when this returns true. Any of:
#   - a CR review already exists for HEAD ($cr_latest_state non-empty, incl.
#     DISMISSED -- CR engaged, may post again), OR
#   - CR is in requested_reviewers, OR
#   - a `@coderabbitai review` trigger comment is present.
# When none hold (the default for an auto-review-off PR that was never triggered),
# CR is NOT expected and the requirement is treated as satisfied -- the fix for
# #173, where the old opt-out logic waited the full timeout for a review that
# never lands. Note: the per-poll detection EXCLUDES the existing-review check
# (callers pass $cr_latest_state, computed each poll) from the fail-closed helpers,
# so a genuine in-flight review always keeps us waiting.
cr_review_expected() {
  [ -n "$cr_latest_state" ] && return 0
  cr_is_requested && return 0
  cr_was_triggered && return 0
  return 1
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

# read_thread_verdict -- ONE GraphQL `query` (never a mutation) over the PR's
# reviewThreads (#441), reusing ship-gate-preflight.sh's
# reviewThreads(first:100){totalCount nodes{isResolved}} shape plus each thread's
# first-comment author for the `by=` field. GraphQL reports a Bot actor's login
# WITHOUT the REST `[bot]` suffix (measured live: `copilot-pull-request-reviewer`),
# so a Bot author gets `[bot]` appended, keeping `by=` in the same login vocabulary
# as review-blocked and QUIET_AUTHORS_JQ. Prints exactly one verdict:
#   UNRESOLVED:<n>:<login,...>[:TRUNC:<nodes>/<total>]  >=1 node with isResolved == false
#   OK          every node resolved and none left unfetched
#   TRUNC       every FETCHED node resolved, but totalCount > nodes (rest unknown)
#   UNREADABLE  gh failure, top-level .errors, malformed body or field, a null
#               node, or a non-boolean isResolved
# UNREADABLE is NOT zero threads (the #375 "unreadable reads as nothing" class):
# the caller keeps the PR pending and emits neither thread-blocked nor settled.
read_thread_verdict() {
  local owner name tj v
  owner="${repo%%/*}"; name="${repo##*/}"
  # shellcheck disable=SC2016  # GraphQL $owner/$name/$number are query variables, NOT shell expansions.
  tj=$(gh api graphql \
    -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){totalCount nodes{isResolved comments(first:1){nodes{author{__typename login}}}}}}}}' \
    -f owner="$owner" -f name="$name" -F number="$pr" 2>/dev/null) || { echo UNREADABLE; return 0; }
  v=$(jq -r '
    if (((.errors // []) | length) != 0)
       or ((.data.repository.pullRequest.reviewThreads | type) != "object")
    then "UNREADABLE"
    else
      .data.repository.pullRequest.reviewThreads as $rt
      | if ($rt.totalCount | type) != "number" or ($rt.nodes | type) != "array"
           or $rt.totalCount < 0 or $rt.totalCount != ($rt.totalCount | floor)
           or $rt.totalCount < ($rt.nodes | length)
           or ([$rt.nodes[] | select((type != "object") or ((.isResolved | type) != "boolean"))] | length) > 0
        then "UNREADABLE"
        else
          ($rt.totalCount) as $tc | ($rt.nodes | length) as $n
          | [$rt.nodes[] | select(.isResolved == false)] as $open
          | if ($open | length) > 0 then
              "UNRESOLVED:\($open | length):"
              + ([$open[] | (.comments.nodes[0].author? // {}) as $a
                  | ($a.login // "unknown") as $l
                  | if ($a.__typename == "Bot") and ($l | endswith("[bot]") | not)
                    then $l + "[bot]" else $l end]
                 | unique | join(","))
              + (if $tc > $n then ":TRUNC:\($n)/\($tc)" else "" end)
            elif $tc > $n then "TRUNC"
            else "OK" end
        end
    end' <<<"$tj" 2>/dev/null) || { echo UNREADABLE; return 0; }
  case "$v" in
    UNRESOLVED:*|OK|TRUNC) echo "$v" ;;
    *) echo UNREADABLE ;;
  esac
}

# note_pending -- #399: emit ONE stderr line whenever the composed pending set
# CHANGES (never on an unchanged poll), so a watch held on a human gate names what
# it holds on instead of sitting byte-identical to one waiting on CI. stdout keeps
# its "silent until done" contract; only stderr gains the transition line.
prev_pending=""
note_pending() {
  if [ "$pending" != "$prev_pending" ]; then
    echo "pr-watch: pending=${pending}" >&2
    prev_pending="$pending"
  fi
}

prev_bot_count=""
pending=""
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
  # The same read carries `state` + `merged` for the MERGED/CLOSED terminal (#435):
  # GitHub reports mergeable_state `unknown` on a merged PR, which is never in the
  # merge-ready set, so without this the watch polled a finished PR to timeout.
  pr_meta=$(gh api "repos/$repo/pulls/$pr" --jq '[.head.sha, (.mergeable_state // "unknown"), (.state // ""), ((.merged // false) | tostring)] | join("|")' 2>/dev/null || true)
  cur_head=""; cur_mergeable_state=""; cur_pr_state=""; cur_merged=""
  IFS='|' read -r cur_head cur_mergeable_state cur_pr_state cur_merged <<<"$pr_meta" || true
  cur_mergeable_state="${cur_mergeable_state:-unknown}"
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

  # MERGED / CLOSED is terminal immediately (#435). Checked BEFORE the rest of the
  # loop body: a merged PR cannot revert to in-progress, so no quiet-period gate and
  # no further reads. Only the EXACT values count (`merged` == "true", state ==
  # "closed"); an absent or unrecognized state keeps polling, never terminates.
  if [ "$cur_merged" = "true" ]; then
    echo "merged head=${cur_head:0:8}"
    exit 0
  fi
  if [ "$cur_pr_state" = "closed" ]; then
    echo "closed head=${cur_head:0:8}"
    exit 0
  fi

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
    note_pending
    sleep "$poll_interval"
    continue
  fi
  cd_fail_count=0

  cr_latest_state=$(echo "$reviews_json" \
    | jq -r --arg head_date "$head_committer_date" --arg cr "$CR_LOGIN" '
        [.[] | select(.user.login == $cr and .submitted_at >= $head_date)]
        | sort_by(.submitted_at) | last | .state // ""')

  # Review-blocked is a distinct, reviewer-agnostic terminal (#195): the author
  # must address feedback before merge is possible, so the consumer dispatches to
  # /handle-review instead of waiting for a settle that cannot happen. Compute the
  # blocking reviewers = every reviewer whose LATEST review on the current HEAD is
  # CHANGES_REQUESTED, restricted to the blocking set when one is configured. Uses
  # the same committer-date filter as the CR settle check (a rebase rewrites review
  # commit_ids, so submitted_at >= head_committer_date is the safe "reviewed the
  # current state" test); group_by/last picks each reviewer's most-recent review so a
  # superseding APPROVED clears an earlier CHANGES_REQUESTED. Apply the quiet-period
  # gate here too -- a reviewer can post inline findings AFTER setting
  # CHANGES_REQUESTED, so a premature emission would hand a half-formed triage list.
  blocked_by=$(echo "$reviews_json" | jq -r \
    --arg head_date "$head_committer_date" \
    --argjson allow "$blocking_set_json" '
      [ .[] | select((.user.login // "") != "" and .submitted_at >= $head_date) ]
      | group_by(.user.login)
      | map(sort_by(.submitted_at) | last)
      | map(select(.state == "CHANGES_REQUESTED"))
      | map(.user.login)
      | if ($allow | length) > 0 then map(select(. as $l | $allow | index($l))) else . end
      | join(",")' 2>/dev/null || echo "")
  if [ -n "$blocked_by" ]; then
    cur_bot_count=$(count_bot_activity)
    if [ -n "$prev_bot_count" ] && [ "$cur_bot_count" = "$prev_bot_count" ]; then
      echo "review-blocked head=${cur_head:0:8} by=${blocked_by}"
      exit 0
    fi
    prev_bot_count="$cur_bot_count"
    pending="review-blocked(quiet-confirm)"
    note_pending
    sleep "$poll_interval"
    continue
  fi

  # Build the pending-criteria list. Empty list = settled.
  pending_list=()

  # CR-waiting is OPT-IN (#173): we add `cr-review` to pending ONLY when there is
  # positive evidence CR will review this PR. The DEFAULT is satisfied. APPROVED and
  # COMMENTED on HEAD qualify directly; "" (no review yet) and DISMISSED keep us
  # pending ONLY if CR is still expected (in-flight). The explicit opt-out fast-paths
  # (#34) -- the `norabbit` label or a CR "Review skipped" check -- stay as additional
  # satisfy conditions even if some stale expectation signal lingers. On this org CR
  # auto-review is OFF, so an untriggered PR posts NO check at all (no "Review skipped");
  # the old opt-out logic waited the full timeout for a review that never lands -- the
  # bug this inversion fixes. cr_review_expected() is positive evidence: an existing
  # review (incl. DISMISSED), CR in requested_reviewers, or a `@coderabbitai review`
  # trigger comment; its helpers fail CLOSED (assume not-expected) so an API blip
  # never hangs, while a genuine in-flight review (non-empty $cr_latest_state) always
  # keeps us waiting.
  case "$cr_latest_state" in
    APPROVED|COMMENTED) ;;
    *)
      if [ "$cr_norabbit" = true ] || cr_review_skipped; then
        : # explicit opt-out (norabbit / Review skipped) -> requirement satisfied
      elif cr_review_expected; then
        pending_list+=("cr-review")
      else
        : # no positive evidence CR will review (auto-review off, untriggered) -> satisfied
      fi
      ;;
  esac

  # Codoki settlement (#110, #237). Codoki posts its verdict as a `Codoki PR Review`
  # entry in statusCheckRollup (NOT the reviews API), so this script defers to the
  # deterministic oracle which reads that rollup. Uses --codoki-gate (NOT --codoki-only):
  # with org Codoki auto-review OFF, a MISSING check is the NORMAL state, and the strict
  # --codoki-only would BLOCK on it forever (the hang #237 fixes). --codoki-gate is
  # Codoki-OFF aware -- Codoki-waiting is OPT-IN, exactly like CR-waiting (#173): exit 0
  # = satisfied (settled OR not-expected -> do not block); exit 2 = expected-but-unsettled
  # (a manual @codoki trigger present, or check present-but-failing -> block on
  # "codoki-check"); exit 1 = oracle usage error -> fail open, do not block. The oracle
  # is skipped entirely when it is not installed.
  if [ -x "$CODOKI_ORACLE" ]; then
    if "$CODOKI_ORACLE" --codoki-gate "$pr" "$repo" >/dev/null 2>&1; then
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
  # The SAME read also yields failing_ci (#441 round 1): the count of checks in a
  # failed terminal state, reported on the thread-blocked line as `failing=<n>`.
  # ONE jq pass emits "<pending> <failing>" ONLY when the body is an array of
  # objects each carrying a string state; anything else (gh failure, malformed
  # body) leaves BOTH unset -> "unknown". So failing_ci is a number exactly when
  # pending_ci is, and an unreadable failing count can never read as zero: it rides
  # the ci(unknown) pending item, which keeps thread-blocked unreachable.
  ci_counts=$(gh pr checks "$pr" --repo "$repo" --json state \
    --jq 'if type == "array" and all(.[]; type == "object" and (.state | type) == "string")
          then "\([.[] | select(.state == "PENDING" or .state == "QUEUED" or .state == "IN_PROGRESS")] | length) \([.[] | select(.state == "FAILURE" or .state == "ERROR" or .state == "CANCELLED" or .state == "TIMED_OUT" or .state == "ACTION_REQUIRED" or .state == "STARTUP_FAILURE")] | length)"
          else "unknown" end' \
    2>/dev/null || echo "unknown")
  pending_ci="unknown"; failing_ci="unknown"
  if [[ "$ci_counts" =~ ^([0-9]+)\ ([0-9]+)$ ]]; then
    pending_ci="${BASH_REMATCH[1]}"; failing_ci="${BASH_REMATCH[2]}"
  fi
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
    note_pending
    sleep "$poll_interval"
    continue
  fi

  # thread-blocked (#441). Evaluated only when the SOLE pending item is
  # merge(blocked). That single test carries three of #441's conditions: CI is
  # terminal (any non-"0" pending_ci, including "unknown", adds a ci(...) item),
  # mergeable_state is `blocked`, and no CR/Codoki wait remains. Separate
  # pending_ci / mergeable tests would be implied by it, i.e. dead guards no test
  # could tell from absent, so they are deliberately not repeated. review-blocked
  # did not fire (it returned above, so it keeps priority). Then a
  # reviewThreads READ must show POSITIVE evidence of an unresolved thread.
  # Branch-protection settings are deliberately NEVER read (the legacy and ruleset
  # APIs disagree on the conversation-resolution requirement, the #375 split); an
  # unresolved thread is itself the evidence, and house policy requires every
  # thread resolved before merge anyway. `blocked` ALONE is an aggregate
  # (#399/#334) and never terminates the watch.
  if [ ${#pending_list[@]} -eq 1 ] && [ "${pending_list[0]}" = "merge(blocked)" ]; then
    thread_verdict=$(read_thread_verdict)
    case "$thread_verdict" in
      UNRESOLVED:*)
        # UNRESOLVED:<n>:<by>[:TRUNC:<nodes>/<total>]
        tv_rest="${thread_verdict#UNRESOLVED:}"
        tv_n="${tv_rest%%:*}"; tv_rest="${tv_rest#*:}"
        tv_by="${tv_rest%%:TRUNC:*}"
        if [ "$tv_rest" != "$tv_by" ]; then
          # Truncation is REPORTED, never silent (#441 AC g): only the first 100
          # threads were fetched, so <n> is a lower bound.
          echo "pr-watch: NOTE: only ${tv_rest##*:TRUNC:} review threads fetched (first:100); unresolved=${tv_n} is a lower bound" >&2
        fi
        # Quiet-period gate, same as settled/review-blocked. The baseline is tagged
        # `thread:` so a count taken on ANOTHER path cannot confirm this one. That
        # is REACHABLE: a settle-path (or review-blocked) baseline survives into the
        # next poll, which `continue`s without resetting it, so a PR reading `clean`
        # then `blocked` would otherwise emit thread-blocked on a SINGLE thread read.
        # What the tag buys is two consecutive polls on which THIS terminal's own
        # predicate held (unresolved thread + sole merge(blocked)), matching what
        # settled requires of its own; the bot-trickle defense itself is the count.
        # Pinned by the harness case "#441 (h)".
        cur_bot_count="thread:$(count_bot_activity)"
        if [ -n "$prev_bot_count" ] && [ "$cur_bot_count" = "$prev_bot_count" ]; then
          # failing_ci is numeric here by construction (see the checks read):
          # this branch requires pending_ci == "0", and both come from one parse.
          echo "thread-blocked head=${cur_head:0:8} unresolved=${tv_n} failing=${failing_ci} by=${tv_by}"
          exit 0
        fi
        prev_bot_count="$cur_bot_count"
        pending="merge(blocked),threads(quiet-confirm)"
        note_pending
        sleep "$poll_interval"
        continue
        ;;
      OK) : ;;                                         # all resolved -> blocked for another reason
      TRUNC) pending_list+=("threads(truncated)") ;;   # fetched all resolved, remainder unknown
      *) pending_list+=("threads(unreadable)") ;;      # an unreadable read is NOT zero threads
    esac
  fi

  # Hard criteria not met -- discard any pending bot-count baseline; if we land
  # back in the all-pass branch later we want to re-measure from scratch.
  prev_bot_count=""
  pending=$(IFS=,; echo "${pending_list[*]}")
  note_pending
  sleep "$poll_interval"
done
