#!/usr/bin/env bash
# pr-unreplied-comments.sh -- List unreplied bot review comments on a PR
#
# Usage: pr-unreplied-comments.sh [--wait] [--count-only] [--full] [--trigger-cr] [--latest-per-reviewer] [--coverage-only] [--allow-stale] <pr_number> [repo]
#
# Options:
#   --wait                 Poll with geometric cooldown (15s/30s/60s/120s) until bot reviews
#                          stabilize. Use after pushing to ensure all bot comments have landed.
#   --count-only           Output only the count of unreplied comments (for scripting/polling).
#   --full                 Output complete comment bodies instead of truncating to 120 chars.
#   --trigger-cr           Trigger a CodeRabbit review. If rate-limited, wait the remaining
#                          time (parsed from the most recent rate limit message) then post
#                          @coderabbitai review. Exits after posting (skips comment detection).
#   --latest-per-reviewer  For the review-body section, show only the most recent review
#                          per reviewer. Suppresses accumulated summaries from earlier
#                          rounds when the latest review has superseded them.
#                          The CHANGES_REQUESTED merge-blocker section ALWAYS uses
#                          latest-per-reviewer (regardless of this flag), because a
#                          superseded CHANGES_REQUESTED is not a real merge blocker.
#   --coverage-only        Output only the codecov coverage advisory (patch %, threshold
#                          state, URL) as JSON. Exits after printing. Used by /merge-pr
#                          and /review-stack for programmatic coverage readouts. Returns
#                          {"status":"none"} when codecov has not commented on the PR.
#   --allow-stale          Suppress the base-branch-freshness gate. Default behavior:
#                          before listing unreplied comments, the script compares
#                          origin/<headRef> with origin/<baseRef>. If the head branch is
#                          behind base, it prints a freshness section and exits 2 instead
#                          of listing comments, forcing a rebase before triage. The
#                          freshness gate does NOT run for --count-only / --pending-only /
#                          --coverage-only / --trigger-cr (scripting/polling modes that
#                          should stay cheap and unblocking).
#
# Checks four comment types:
#   1. Inline review comments (PR diff comments)      -- reply_type: "inline"   (use 3-arg reply-comment.sh)
#   2. Review-body comments (summary attached to reviews) -- reply_type: "top-level" (use 2-arg reply-comment.sh)
#   3. Issue-level comments (general PR conversation) -- reply_type: "top-level" (use 2-arg reply-comment.sh)
#   4. CHANGES_REQUESTED merge blockers (informational, not counted)
#
# Additionally, if codecov[bot] has posted a coverage summary, a "Coverage
# advisory" section is printed (informational, not counted as unreplied).
# Codecov comments do not require a reply -- they are coverage reports, not
# review threads. The advisory surfaces the patch coverage % and threshold
# state so /handle-review and /review-stack can flag regressions.
#
# Each entry includes reply_type to indicate which reply form to use:
#   "inline"    -- the ID is a pull request review comment; use reply-comment.sh <pr> <id> <body>
#   "top-level" -- the ID is a review or issue object; use reply-comment.sh <pr> <body>
#
# Each inline comment also includes commit_id (short) to identify stale-diff comments.
#
# Bot logins checked (defined once in BOT_LOGIN_FILTER):
#   coderabbitai[bot], Copilot, copilot-pull-request-reviewer[bot],
#   github-advanced-security[bot], github-actions[bot], greptile-apps[bot],
#   codoki-pr-intelligence[bot]
# Codecov (codecov[bot]) is tracked separately -- see the coverage advisory
# section and count_pending_reviewers(). It is NOT in BOT_LOGIN_FILTER because
# its coverage comments are informational and should not count as unreplied.
# github-actions[bot] is the generic actor for workflow-posted comments
# (label gates, docs-drift advisories, CodeQL alert sticky notes, custom
# review checks). Including it surfaces actionable workflow feedback that
# previously slipped past the unreplied-comments gate.
# greptile-apps[bot] posts a single COMMENTED review ~20 min AFTER CR APPROVES
# and its inline findings (e.g. P2 doc-contradiction catches) need triage like
# any other bot reviewer. Included so /handle-review surfaces them.
# codoki-pr-intelligence[bot] (Codoki) posts COMMENTED reviews with empty review
# bodies; its findings live entirely in inline comments plus an issue-comment
# summary, so it must be allow-listed here or its findings slip past the gate.

set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

# Single source of truth for bot login detection.
# Used in jq select() expressions -- must be valid jq.
BOT_LOGIN_FILTER='(
  .user.login == "coderabbitai[bot]" or
  .user.login == "Copilot" or
  .user.login == "copilot-pull-request-reviewer[bot]" or
  .user.login == "github-advanced-security[bot]" or
  .user.login == "github-actions[bot]" or
  .user.login == "greptile-apps[bot]" or
  .user.login == "codoki-pr-intelligence[bot]"
)'

# --- Parse arguments ---
wait_mode=false
count_only=false
pending_only=false
full_mode=false
trigger_cr=false
latest_per_reviewer=false
coverage_only=false
allow_stale=false
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --wait) wait_mode=true; shift ;;
    --count-only) count_only=true; shift ;;
    --pending-only) pending_only=true; shift ;;
    --full) full_mode=true; shift ;;
    --trigger-cr) trigger_cr=true; shift ;;
    --latest-per-reviewer) latest_per_reviewer=true; shift ;;
    --coverage-only) coverage_only=true; shift ;;
    --allow-stale) allow_stale=true; shift ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

pr_number="${1:?Usage: pr-unreplied-comments.sh [--wait] [--count-only] [--pending-only] [--trigger-cr] [--latest-per-reviewer] <pr_number> [repo]}"
repo="${2:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
me=$(gh api user --jq .login)

# --- Trigger CodeRabbit review with rate-limit awareness ---
if [ "$trigger_cr" = true ]; then
  # Check for the most recent rate limit message from CodeRabbit.
  # Format: "Please wait **N minutes and N seconds**" with created_at timestamp.
  rate_limit_comment=$(gh api "repos/$repo/issues/$pr_number/comments" --paginate \
    --jq '[.[] | select(
      .user.login == "coderabbitai[bot]" and
      (.body | test("rate limited by coderabbit"; "i"))
    )] | sort_by(.created_at) | last // empty')

  if [ -n "$rate_limit_comment" ]; then
    body=$(echo "$rate_limit_comment" | jq -r '.body')
    created_at=$(echo "$rate_limit_comment" | jq -r '.created_at')

    # Parse wait duration from "**N minutes and N seconds**" or "**N seconds**"
    wait_minutes=0
    wait_seconds=0
    if echo "$body" | grep -qoP '\*\*\d+ minutes?'; then
      wait_minutes=$(echo "$body" | grep -oP '\*\*\K\d+(?= minutes?)' | head -1)
    fi
    if echo "$body" | grep -qoP '\d+ seconds?\*\*'; then
      wait_seconds=$(echo "$body" | grep -oP '(\d+)(?= seconds?\*\*)' | head -1)
    fi
    total_wait=$(( ${wait_minutes:-0} * 60 + ${wait_seconds:-0} ))

    if [ "$total_wait" -gt 0 ]; then
      # Calculate elapsed time since the rate limit message was posted.
      created_epoch=$(date -d "$created_at" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$created_at" +%s 2>/dev/null || echo 0)
      now_epoch=$(date +%s)
      elapsed=$(( now_epoch - created_epoch ))
      remaining=$(( total_wait - elapsed + 10 ))  # +10s buffer

      if [ "$remaining" -gt 0 ]; then
        echo "CodeRabbit rate-limited. Waiting ${remaining}s (${wait_minutes}m${wait_seconds}s limit, ${elapsed}s elapsed, +10s buffer)..."
        sleep "$remaining"
      else
        echo "Rate limit expired (${elapsed}s elapsed, ${total_wait}s limit). Proceeding."
      fi
    fi
  fi

  echo "Triggering CodeRabbit review on PR #$pr_number..."
  gh api "repos/$repo/issues/$pr_number/comments" -f body="@coderabbitai review" --silent
  echo "Done. CodeRabbit review triggered."
  exit 0
fi

# --- Core function: count unreplied bot comments ---
count_unreplied() {
  local all_comments
  all_comments=$(gh api "repos/$repo/pulls/$pr_number/comments" --paginate)

  local bot_ids
  bot_ids=$(echo "$all_comments" | jq '[.[] | select(
    '"$BOT_LOGIN_FILTER"'
    and .in_reply_to_id == null
  ) | .id]')

  local my_reply_targets
  my_reply_targets=$(echo "$all_comments" | jq --arg me "$me" '[.[] |
    select(.user.login == $me and .in_reply_to_id != null) |
    .in_reply_to_id]')

  local unreplied_ids
  unreplied_ids=$(jq -n --argjson bot "$bot_ids" --argjson replied "$my_reply_targets" \
    '[$bot[] | . as $id | if ($replied | any(. == $id)) then empty else $id end]')

  echo "$unreplied_ids" | jq -r 'length' | tr -d '[:space:]'
}

# --- Core function: check for pending bot reviewers ---
# Checks two sources:
#   1. requested_reviewers API (reviewers that haven't started)
#   2. Commit statuses (CodeRabbit reports "review in progress" as a pending status)
count_pending_reviewers() {
  local requested
  requested=$(gh api "repos/$repo/pulls/$pr_number/requested_reviewers" \
    --jq '[(.users // [])[] | .login | select(test("copilot|bot"; "i"))] | length' 2>/dev/null || echo "0")

  # Check combined commit status for bot reviews still in progress (e.g. CodeRabbit).
  # The combined status endpoint deduplicates by context, returning only the latest
  # status per context. The raw /statuses endpoint returns ALL statuses including
  # superseded "pending" entries, which causes false positives.
  local head_sha
  head_sha=$(gh api "repos/$repo/pulls/$pr_number" --jq '.head.sha' 2>/dev/null || echo "")
  local status_pending=0
  if [ -n "$head_sha" ]; then
    # Include codecov alongside coderabbit/copilot: codecov posts "pending"
    # commit statuses while coverage is being calculated; treating it as
    # pending keeps the wait loop honest for coverage-gated merges.
    status_pending=$(gh api "repos/$repo/commits/$head_sha/status" \
      --jq '[.statuses // [] | .[] | select(.state == "pending" and (.context | test("coderabbit|copilot|codecov"; "i")))] | length' 2>/dev/null || echo "0")
  fi

  echo $(( ${requested:-0} + ${status_pending:-0} )) | tr -d '[:space:]'
}

# --- Core function: build the codecov coverage advisory ---
# Parses the latest codecov[bot] issue comment on the PR and extracts:
#   - patch_pct: the "Patch coverage is `NN.NN%`" value (float, or null)
#   - threshold_state: "pass" | "fail" | "unknown" based on the leading
#                      emoji (:white_check_mark: / :x: / other)
#   - report_url: the first link to app.codecov.io found in the body
#   - comment_id: the GitHub issue comment id (for linking)
# Prints a JSON object. Returns {"status":"none"} when codecov has not
# posted on this PR. This is informational only and never contributes to
# the unreplied-comment count.
build_coverage_advisory() {
  local issue_comments codecov_comment body comment_id patch_pct threshold_state report_url

  issue_comments=$(gh api "repos/$repo/issues/$pr_number/comments" --paginate 2>/dev/null || echo '[]')
  codecov_comment=$(echo "$issue_comments" | jq '[.[] | select(.user.login == "codecov[bot]")] | sort_by(.created_at) | last // empty')

  if [ -z "$codecov_comment" ] || [ "$codecov_comment" = "null" ]; then
    echo '{"status":"none"}'
    return
  fi

  body=$(echo "$codecov_comment" | jq -r '.body')
  comment_id=$(echo "$codecov_comment" | jq -r '.id')

  # Patch coverage percentage. The line looks like:
  #   "Patch coverage is `29.78177%` with ..."
  # Matches the first decimal number inside backticks following "Patch coverage is".
  # shellcheck disable=SC2016  # literal grep pattern; no shell expansion intended.
  patch_pct=$(echo "$body" | grep -oE 'Patch coverage is[[:space:]]+`[0-9]+\.?[0-9]*%`' | head -1 \
    | grep -oE '[0-9]+\.?[0-9]*' | head -1)

  # Threshold state from the leading status emoji on the patch-coverage line.
  # :white_check_mark: signals pass; :x: signals fail; absence signals unknown.
  if echo "$body" | grep -q ':white_check_mark:.*[Pp]atch coverage'; then
    threshold_state="pass"
  elif echo "$body" | grep -q ':x:.*[Pp]atch coverage'; then
    threshold_state="fail"
  else
    threshold_state="unknown"
  fi

  # First codecov report URL in the body.
  report_url=$(echo "$body" | grep -oE 'https://app\.codecov\.io/[^)"[:space:]]+' | head -1 || true)

  jq -n \
    --arg status "present" \
    --arg pct "${patch_pct:-}" \
    --arg state "$threshold_state" \
    --arg url "${report_url:-}" \
    --argjson id "$comment_id" \
    '{status: $status, patch_pct: (if $pct == "" then null else ($pct | tonumber) end),
      threshold_state: $state, report_url: (if $url == "" then null else $url end),
      comment_id: $id}'
}

# --- Pending-only mode: just print pending bot reviewer count and exit ---
if [ "$pending_only" = true ]; then
  count_pending_reviewers
  exit 0
fi

# --- Coverage-only mode: print the codecov advisory as JSON and exit ---
if [ "$coverage_only" = true ]; then
  build_coverage_advisory
  exit 0
fi

# --- Base branch freshness gate ----------------------------------------------
# Compares the PR's published head branch with its base. If origin/<headRef>
# is behind origin/<baseRef>, exit 2 with a clear rebase pointer instead of
# listing unreplied comments. This is the deterministic guard that prevents
# starting a fix cycle on stale state: every triage entry point reads this
# script first, so a stale base is impossible to miss.
#
# Skipped for --count-only (numeric output for poll loops), --allow-stale
# (explicit opt-out), and the early-exit modes above. The fetch is best-
# effort: a network failure prints a degraded "freshness: unknown" line and
# proceeds rather than blocking when offline. Refs that don't resolve locally
# (e.g. the head branch was never fetched) also fall through to "unknown".
if [ "$count_only" = false ] && [ "$allow_stale" = false ]; then
  base_ref=$(gh pr view "$pr_number" --repo "$repo" --json baseRefName --jq .baseRefName 2>/dev/null || echo "")
  head_ref=$(gh pr view "$pr_number" --repo "$repo" --json headRefName --jq .headRefName 2>/dev/null || echo "")
  if [ -n "$base_ref" ] && [ -n "$head_ref" ]; then
    git fetch origin "$base_ref" "$head_ref" --quiet 2>/dev/null || true
    base_sha=$(git rev-parse --short "origin/$base_ref" 2>/dev/null || echo "")
    head_sha=$(git rev-parse --short "origin/$head_ref" 2>/dev/null || echo "")
    behind=$(git rev-list --count "origin/$head_ref..origin/$base_ref" 2>/dev/null || echo "")
    echo "=== Base branch freshness ==="
    if [ -z "$base_sha" ] || [ -z "$head_sha" ] || [ -z "$behind" ]; then
      echo "  status:   unknown (could not resolve origin/$head_ref or origin/$base_ref)"
      echo "  hint:     'git fetch origin' may be required; or pass --allow-stale to skip"
      echo ""
    elif [ "$behind" -eq 0 ]; then
      echo "  base:     $base_ref ($base_sha)"
      echo "  head:     $head_ref ($head_sha)"
      echo "  status:   OK (head is up to date with base)"
      echo ""
    else
      echo "  base:     $base_ref ($base_sha)"
      echo "  head:     $head_ref ($head_sha)"
      echo "  behind:   $behind commit(s)"
      echo ""
      echo "STOP: head branch is behind base. Rebase before starting triage:"
      echo "  cd <worktree-for-$head_ref>"
      echo "  git fetch origin $base_ref"
      echo "  git rebase origin/$base_ref"
      echo ""
      echo "Bypass with --allow-stale if you intentionally want to read comments"
      echo "without rebasing first (e.g. for a stale-diff fast path)."
      exit 2
    fi
  fi
fi

# --- Wait mode: geometric cooldown polling ---
if [ "$wait_mode" = true ]; then
  intervals=(15 30 60 120)

  # Quick check first -- skip cooldown entirely if no pending reviewers
  pending=$(count_pending_reviewers)
  current=$(count_unreplied)

  if [ "$pending" -eq 0 ]; then
    echo "No pending bot reviewers. Skipping cooldown."
  else
    echo "Waiting for bot reviews to stabilize on PR #$pr_number..."
    prev_count=$current

    for i in "${!intervals[@]}"; do
      delay=${intervals[$i]}
      echo "  Poll $((i+1))/4: waiting ${delay}s..."
      sleep "$delay"

      pending=$(count_pending_reviewers)
      current=$(count_unreplied)

      echo "  Poll $((i+1))/4: pending reviewers=$pending, unreplied comments=$current"

      if [ "$pending" -eq 0 ] && [ "$current" -eq "$prev_count" ]; then
        echo "  Stable: no pending reviewers and comment count unchanged ($current)."
        break
      fi

      prev_count=$current
    done
  fi

  if [ "$pending" -ne 0 ]; then
    echo ""
    echo "WARNING: Reviews may not be fully stable after 4 polls (~3.75 min)."
    echo "  Pending bot reviewers: $pending"
    echo "  Unreplied comments: $current"
    echo "  Consider running again or proceeding with caution."
  fi

  echo ""
fi

# --- Collect and display unreplied comments ---
found=0

# Get HEAD commit for stale-diff detection
head_sha=$(gh api "repos/$repo/pulls/$pr_number" --jq '.head.sha[:7]' 2>/dev/null || echo "unknown")

# 1. Inline review comments
all_comments=$(gh api "repos/$repo/pulls/$pr_number/comments" --paginate)

bot_ids=$(echo "$all_comments" | jq '[.[] | select(
  '"$BOT_LOGIN_FILTER"'
  and .in_reply_to_id == null
) | .id]')

my_reply_targets=$(echo "$all_comments" | jq --arg me "$me" '[.[] |
  select(.user.login == $me and .in_reply_to_id != null) |
  .in_reply_to_id]')

unreplied_ids=$(jq -n --argjson bot "$bot_ids" --argjson replied "$my_reply_targets" \
  '[$bot[] | . as $id | if ($replied | any(. == $id)) then empty else $id end]')

inline_count=$(echo "$unreplied_ids" | jq 'length')

if [ "$inline_count" -gt 0 ]; then
  if [ "$count_only" = false ]; then
    echo "=== Unreplied inline review comments: $inline_count (HEAD: $head_sha) ==="
    echo ""
    if [ "$full_mode" = true ]; then
      body_expr='.body'
    else
      body_expr='(.body | split("\n")[0][:120])'
    fi
    echo "$all_comments" | jq --argjson ids "$unreplied_ids" '[.[] |
      select(.id as $id | $ids | any(. == $id)) |
      {id, type: "inline", reply_type: "inline",
       user: .user.login, path, line: .original_line,
       commit: (.commit_id[:7]),
       stale: ((.commit_id[:7]) != "'"$head_sha"'"),
       body: '"$body_expr"'}]'
    echo ""
  fi
  found=$((found + inline_count))
fi

# 2. Review-body comments with actionable findings
# CodeRabbit embeds "Outside diff range" findings in review bodies that cannot
# be posted as inline comments. These appear in CHANGES_REQUESTED or COMMENTED
# reviews. Copilot's "Pull request overview" reviews are summaries only and
# are excluded.
all_reviews=$(gh api "repos/$repo/pulls/$pr_number/reviews" --paginate)

if [ "$full_mode" = true ]; then
  rb_body_expr='.body'
else
  # Skip leading blank lines and quote-only lines (CR review bodies often
  # start with "\n> [!CAUTION]" etc. -- a naive [0] yields "" and hides
  # real findings as if they were empty-body APPROVED acks).
  rb_body_expr='((.body | split("\n") | map(select(test("^[[:space:]>]*$") | not)) | (.[0] // ""))[:120])'
fi

review_bodies_raw=$(echo "$all_reviews" | jq '[.[] | select(
  .body != "" and .body != null and
  '"$BOT_LOGIN_FILTER"' and
  (.body | test("Outside diff range|Potential issue|Refactor suggestion|Actionable comments posted|Nitpick|CAUTION|Duplicate comments"; "i")) and
  (.body | test("^## Pull request overview"; "") | not)
)]')

# A review body is "addressed" when every inline comment belonging to it has
# been replied to by $me. Each inline comment has a pull_request_review_id
# linking it to the review submission. Review bodies whose inline findings
# are all replied are round summaries with no further action -- filter them
# out so they stop polluting /handle-review triage.
#
# Pure outside-diff reviews (no associated inline comments) are suppressed ONLY
# when $me has posted a later comment (issue-level or inline) that REFERENCES
# this review by its id (e.g. via reply-comment.sh --review <id>, which stamps
# the id into the body). A bare later comment no longer counts as an ack, so a
# genuinely unaddressed outside-diff finding is never silently hidden -- it
# stays surfaced until something actually references it.
_rb_tmpdir=$(mktemp -d)
trap 'rm -rf "$_rb_tmpdir"' EXIT
echo "$review_bodies_raw" > "$_rb_tmpdir/reviews.json"
echo "$all_comments"      > "$_rb_tmpdir/comments.json"
echo "$unreplied_ids"     > "$_rb_tmpdir/unreplied.json"
gh api "repos/$repo/issues/$pr_number/comments" --paginate 2>/dev/null > "$_rb_tmpdir/issue_comments.json" || echo '[]' > "$_rb_tmpdir/issue_comments.json"

review_bodies=$(jq -n \
  --slurpfile reviews        "$_rb_tmpdir/reviews.json" \
  --slurpfile all_comments   "$_rb_tmpdir/comments.json" \
  --slurpfile unreplied      "$_rb_tmpdir/unreplied.json" \
  --slurpfile issue_comments "$_rb_tmpdir/issue_comments.json" \
  --arg me "$me" \
  '
  ($reviews[0]) as $reviews |
  ($all_comments[0]) as $all_comments |
  ($unreplied[0]) as $unreplied |
  ($issue_comments[0]) as $issue_comments |
  ($all_comments | map(select(.pull_request_review_id != null))
    | group_by(.pull_request_review_id)
    | map({key: (.[0].pull_request_review_id | tostring), value: [.[].id]})
    | from_entries) as $inline_by_review |
  (($issue_comments + $all_comments) | map(select(.user.login == $me))) as $my_comments |
  $reviews | map(
    . as $r |
    ($inline_by_review[($r.id | tostring)] // []) as $inline_ids |
    ($inline_ids | map(. as $id | $unreplied | any(. == $id)) | any) as $has_unreplied_inline |
    # A pure outside-diff finding is "addressed" only when a later $me comment
    # references this review by id; a bare later comment no longer suppresses it.
    ([$my_comments[] | select((.created_at > $r.submitted_at) and (((.body // "") | test(($r.id | tostring)))))] | length > 0) as $acked_by_reference |
    if ($inline_ids | length) > 0 then
      if $has_unreplied_inline then $r else empty end
    else
      if $acked_by_reference then empty else $r end
    end
  )
  ')

if [ "$latest_per_reviewer" = true ]; then
  review_bodies=$(echo "$review_bodies" | jq 'group_by(.user.login) | map(max_by(.id)) | flatten |
    map({id, type: "review-body", reply_type: "top-level", user: .user.login, state, body: '"$rb_body_expr"'})')
else
  review_bodies=$(echo "$review_bodies" | jq 'map({id, type: "review-body", reply_type: "top-level", user: .user.login, state, body: '"$rb_body_expr"'})')
fi

review_body_count=$(echo "$review_bodies" | jq 'length')

if [ "$review_body_count" -gt 0 ]; then
  if [ "$count_only" = false ]; then
    echo "=== Review-body comments with actionable findings: $review_body_count ==="
    echo ""
    echo "$review_bodies"
    echo ""
  fi
  found=$((found + review_body_count))
fi

# 3. Issue-level comments (skip auto-generated summaries)
issue_comments=$(gh api "repos/$repo/issues/$pr_number/comments" --paginate)

if [ "$full_mode" = true ]; then
  ic_body_expr='.body'
else
  ic_body_expr='(.body | split("\n")[0][:120])'
fi

actionable_issue=$(echo "$issue_comments" | jq --arg me "$me" '[.[] | select(
  '"$BOT_LOGIN_FILTER"' and
  (.body | test("auto-generated"; "i") | not) and
  (.body | test("^\\s*$") | not)
) | {id, type: "issue-comment", reply_type: "top-level", user: .user.login, created_at,
     body: '"$ic_body_expr"'}]')

issue_count=$(echo "$actionable_issue" | jq 'length')

if [ "$issue_count" -gt 0 ]; then
  if [ "$count_only" = false ]; then
    echo "=== Actionable issue-level bot comments: $issue_count ==="
    echo ""
    echo "$actionable_issue"
    echo ""
  fi
  found=$((found + issue_count))
fi

# 4. CHANGES_REQUESTED merge blockers
# Surface bot reviews whose LATEST submission is CHANGES_REQUESTED. Historical
# CHANGES_REQUESTED that have been superseded by a later APPROVED/COMMENTED
# review from the same reviewer are NOT merge blockers: GitHub's branch
# protection uses latest-per-reviewer semantics, so surfacing the older review
# would just be noise (it caused three distinct "merge isn't blocked" false
# positives this session alone). The unreplied-comment sections above still
# surface all rounds by default -- only this blocker section is pinned to
# latest-per-reviewer, since that's the only one whose semantics line up with
# GitHub's actual blocking logic.
blocking_reviews=$(echo "$all_reviews" | jq '[.[] | select('"$BOT_LOGIN_FILTER"')] |
  group_by(.user.login) | map(max_by(.id)) | flatten |
  map(select(.state == "CHANGES_REQUESTED")) |
  map({id, type: "merge-blocker", user: .user.login, state, submitted_at})')

blocker_count=$(echo "$blocking_reviews" | jq 'length')

if [ "$blocker_count" -gt 0 ]; then
  if [ "$count_only" = false ]; then
    echo "=== CHANGES_REQUESTED merge blockers: $blocker_count ==="
    echo ""
    echo "$blocking_reviews"
    echo ""
  fi
  # Do not add to found count -- these are informational, not unreplied comments.
  # The inline comment count already reflects the actionable items.
fi

# --- Coverage advisory (informational; not counted as unreplied) ---
# Codecov reports are coverage summaries, not review threads -- there is no
# reply flow and no way to resolve the "comment." Surface it here so the
# caller sees the patch coverage state without mistaking it for an action
# item. Omitted from --count-only output so the advisory cannot leak into
# a readiness-check numeric value.
if [ "$count_only" = false ]; then
  coverage_json=$(build_coverage_advisory)
  coverage_status=$(echo "$coverage_json" | jq -r '.status')
  if [ "$coverage_status" = "present" ]; then
    pct=$(echo "$coverage_json" | jq -r '.patch_pct // "?"')
    state=$(echo "$coverage_json" | jq -r '.threshold_state')
    url=$(echo "$coverage_json" | jq -r '.report_url // ""')
    echo "=== Coverage advisory (codecov, informational) ==="
    echo ""
    echo "  Patch coverage: ${pct}% (threshold: ${state})"
    if [ -n "$url" ]; then
      echo "  Report: $url"
    fi
    echo "  No reply required -- codecov comments are informational."
    echo ""
  fi
fi

# --- Summary ---
if [ "$count_only" = true ]; then
  echo "$found"
elif [ "$found" -eq 0 ] && [ "$blocker_count" -eq 0 ]; then
  echo "No unreplied bot comments on PR #$pr_number."
elif [ "$found" -eq 0 ] && [ "$blocker_count" -gt 0 ]; then
  echo "No unreplied comments, but $blocker_count CHANGES_REQUESTED blocker(s) present."
else
  echo "Total unreplied: $found"
fi
