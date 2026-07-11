#!/usr/bin/env bash
# pr-unreplied-comments.sh -- List unreplied bot review comments on a PR
#
# Usage: pr-unreplied-comments.sh [--wait] [--count-only] [--full] [--latest-per-reviewer] [--coverage-only] [--allow-stale] [--check-resolved] [--audit] [--itemized] <pr_number> [repo]
#
# Options:
#   --audit / --all        COMPLETE-COVERAGE audit mode (distinct from the default
#                          gating mode). Enumerates EVERY CodeRabbit + Codoki review
#                          comment -- inline THREADS (findings) AND issue-level review
#                          summaries -- and prints a per-comment table: TYPE, AUTHOR,
#                          LOCATION (file:line or "(issue-level)"), REPLIED (yes/NO),
#                          RESOLVED (yes/NO/n-a). FINDINGS (inline threads) need a
#                          reply AND a resolved thread; issue-level SUMMARIES are
#                          informational (no thread to resolve) and never affect the
#                          exit code. Exit 0 ONLY when every finding is replied AND
#                          every finding-thread resolved; non-zero otherwise -- so it
#                          can gate a "fully audited, nothing missed" claim. Uses
#                          GraphQL (reviewThreads.isResolved) for thread resolution.
#                          Mutually exclusive with --count-only / --pending-only /
#                          --coverage-only (exits 1 on a bad combo).
#   --wait                 Poll with geometric cooldown (15s/30s/60s/120s) until bot reviews
#                          stabilize. Use after pushing to ensure all bot comments have landed.
#   --count-only           Output only the count of unreplied comments (for scripting/polling).
#   --full                 Output complete comment bodies instead of truncating to 120 chars.
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
#                          --coverage-only (scripting/polling modes that
#                          should stay cheap and unblocking).
#   --check-resolved       Also emit a non-fatal "UNRESOLVED-ADVISORY:" line for each
#                          bot-rooted review thread whose isResolved is false (GraphQL-only
#                          signal, absent from the REST comments API). Wires thread-
#                          resolution into the DEFAULT path so a consumer can gate ANY
#                          PR-state claim (blocked/idle/waiting-on-you/merge-ready) on
#                          resolution, not just on unreplied comments. BEST-EFFORT and
#                          ADVISORY: never changes the exit code; a query error degrades to
#                          one stderr note. Mutually exclusive with --audit (which already
#                          reports per-finding resolution). Suppressed in --count-only.
#   --itemized             CHECKLIST display mode (#252). Prints ONE checkable
#                          pipe-delimited line per ACTIONABLE bot finding across ALL
#                          THREE classes -- inline threads and review-BODY findings are
#                          UNREPLIED-filtered; issue-level actionable comments have no
#                          reply thread so they are always listed (replied:n/a) -- so no
#                          class can be dropped by an inline-only glance. Each line is
#                          "<class> | <user> | <loc> | <excerpt> | replied:<..> resolved:<..>";
#                          inline lines carry a per-thread resolved yes/no/? (matched
#                          by path+line via GraphQL). A REPORT, not a gate: a non-empty
#                          list still exits 0. Mutually exclusive with --count-only /
#                          --pending-only / --coverage-only / --audit (exits 1 on a bad combo).
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
# Note on addressed-state filtering: the inline section (type 1) and review-body
# section (type 2) DO filter out comments that $me has already replied to (or
# referenced). The issue-level actionable selection (type 3) does NOT apply any
# addressed-state filter -- it surfaces ALL matching bot issue comments every
# run, because issue comments have no reply-threading to key an "addressed"
# check off of. Consumers use judgment to decide which issue-level items still
# need action.
#
# Outside-diff findings (gate correctness, #132): CodeRabbit carries findings it
# cannot post as inline comments in an "Outside diff range comments (N)" collapsible
# inside the review BODY. These are real actionable findings with no inline thread.
# The default gating count (the "Review-body comments with actionable findings: N"
# line) ADDS the sum of those N values across ALL surviving CR review bodies, so a
# review body with "Actionable comments posted: 1" + "Outside diff range comments (6)"
# reports 7, not 1. The sum is taken over ALL CR submissions (never latest-per-reviewer:
# an APPROVED later review does not clear an outside-diff Major from an earlier one).
#
# Staleness advisory (#93): in the default display mode this script also emits a
# non-fatal "STALE-ADVISORY: <bot> verdict updated <ts> predates current HEAD <sha>"
# line for any bot verdict (latest review or in-place-edited issue comment) whose
# timestamp predates the current HEAD push. It is ADVISORY ONLY -- the exit code is
# UNCHANGED -- so /handle-review and /merge-pr can re-read/re-trigger before trusting
# a verdict that was made against pre-HEAD code (Codoki edits its single comment in
# place: created_at fixed, updated_at advances, verdict can flip on the same id).
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

# Same bot set as a JSON array for membership tests against GraphQL's
# .author.login (suffix-less) and REST's .user.login. Shared by --audit and the
# --check-resolved advisory; isbot() in each consumer normalizes the "[bot]"
# suffix (GraphQL drops it).
BOT_LOGINS_JSON='["coderabbitai[bot]","Copilot","copilot-pull-request-reviewer[bot]","github-advanced-security[bot]","github-actions[bot]","greptile-apps[bot]","codoki-pr-intelligence[bot]"]'

# --- Parse arguments ---
wait_mode=false
count_only=false
pending_only=false
full_mode=false
latest_per_reviewer=false
coverage_only=false
allow_stale=false
audit_mode=false
check_resolved=false
itemized=false
# Parse flags and positionals in ONE pass so a flag is recognized wherever it sits,
# NOT only before the <pr> positional (#259). The prior leading-only loop stopped at
# the first non-flag, so `<pr> --itemized` left `--itemized` as $2=[repo] -> a cryptic
# `gh api repos/--itemized/...` 404. Now: any `-`-leading token is a flag (unknown ->
# loud usage error, never a silent repo), `--` ends flag parsing, everything else is a
# positional. This makes `<pr> --flag` work AND guarantees a `[repo]` can never be a flag.
positionals=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --wait) wait_mode=true; shift ;;
    --count-only) count_only=true; shift ;;
    --pending-only) pending_only=true; shift ;;
    --full) full_mode=true; shift ;;
    --latest-per-reviewer) latest_per_reviewer=true; shift ;;
    --coverage-only) coverage_only=true; shift ;;
    --allow-stale) allow_stale=true; shift ;;
    --audit|--all) audit_mode=true; shift ;;
    --check-resolved) check_resolved=true; shift ;;
    --itemized) itemized=true; shift ;;
    --) shift; while [ "$#" -gt 0 ]; do positionals+=("$1"); shift; done ;;
    -*) echo "Unknown flag: $1 (a '-'-leading token is never accepted as <pr> or [repo])" >&2; exit 1 ;;
    *) positionals+=("$1"); shift ;;
  esac
done
set -- "${positionals[@]+"${positionals[@]}"}"
if [ "$#" -gt 2 ]; then
  echo "Usage: too many positional arguments (expected <pr> [repo]); got: $*" >&2
  exit 1
fi

# --itemized is a human-readable one-line-per-finding CHECKLIST display mode; it is
# incompatible with the numeric/scripting early-exit modes (their outputs replace the
# full enumeration --itemized needs). Combining them is a usage error.
if [ "$itemized" = true ]; then
  if [ "$count_only" = true ] || [ "$pending_only" = true ] || \
     [ "$coverage_only" = true ] || [ "$audit_mode" = true ]; then
    echo "Usage: --itemized is mutually exclusive with --count-only / --pending-only / --coverage-only / --audit" >&2
    exit 1
  fi
  # Force full bodies into the three arrays so the itemized excerpt() can skip the
  # leading HTML-comment / <details> noise real bots emit and surface the first line
  # of actual prose (the truncated non-full body_expr keeps only that noise). The
  # verbose display blocks are suppressed in itemized mode, so this only affects the
  # data excerpt() reads, never any printed JSON.
  full_mode=true
fi

# --audit is a distinct complete-coverage mode; combining it with the gating /
# scripting early-exit modes is a usage error (their outputs are incompatible).
if [ "$audit_mode" = true ]; then
  if [ "$count_only" = true ] || [ "$pending_only" = true ] || \
     [ "$coverage_only" = true ]; then
    echo "Usage: --audit is mutually exclusive with --count-only / --pending-only / --coverage-only" >&2
    exit 1
  fi
  # --check-resolved wires the same isResolved signal into the DEFAULT path; in
  # --audit that signal is already part of the per-finding table, so combining
  # them is redundant and incompatible (the audit path exits before the advisory).
  if [ "$check_resolved" = true ]; then
    echo "Usage: --check-resolved is mutually exclusive with --audit (--audit already reports thread resolution)" >&2
    exit 1
  fi
fi

pr_number="${1:?Usage: pr-unreplied-comments.sh [--wait] [--count-only] [--pending-only] [--latest-per-reviewer] [--itemized] <pr_number> [repo]}"
repo="${2:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
me=$(gh api user --jq .login)

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
# Parses the latest codecov[bot] issue comment on the PR for informational fields,
# then derives the GATING threshold_state from the codecov/patch CHECK-RUN (not the
# comment). Fields:
#   - patch_pct: the "Patch coverage is `NN.NN%`" value (float, or null) -- from the comment
#   - threshold_state: "pass" | "fail" | "none" -- from the codecov/patch check-run
#                      conclusion on the PR head SHA. This is the ONLY gating signal.
#   - comment_glyph: "pass" | "uncovered" | "unknown" -- ADVISORY ONLY, from the leading
#                    emoji on the comment's patch line. NOT a gate (see #239 below).
#   - report_url: the first link to app.codecov.io found in the body
#   - comment_id: the GitHub issue comment id (for linking)
# Prints a JSON object. Returns {"status":"none"} when codecov has not
# posted on this PR. This is informational only and never contributes to
# the unreplied-comment count.
#
# #239: codecov prints a leading :x: on the patch line WHENEVER any patch line is
# uncovered -- independent of whether the patch THRESHOLD passed. Deriving the gating
# state from that glyph made a passing gate read "fail" and spuriously paused /merge-pr
# (a wasted maintainer round-trip on every PR with any uncovered patch line). The gating
# truth is the codecov/patch check-run conclusion, read via the check-runs API (NOT the
# legacy commits/<sha>/status endpoint, which codecov leaves empty). The comment glyph is
# retained as advisory-only context under comment_glyph.
build_coverage_advisory() {
  local issue_comments codecov_comment body comment_id patch_pct comment_glyph report_url
  local script_dir head_sha patch_conclusion threshold_state

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

  # Comment glyph -- ADVISORY ONLY (#239). :white_check_mark: => pass; :x: => uncovered
  # patch lines present (NOT a threshold failure); absence => unknown. Never gates.
  if echo "$body" | grep -q ':white_check_mark:.*[Pp]atch coverage'; then
    comment_glyph="pass"
  elif echo "$body" | grep -q ':x:.*[Pp]atch coverage'; then
    comment_glyph="uncovered"
  else
    comment_glyph="unknown"
  fi

  # GATING threshold_state: the codecov/patch check-run conclusion on the PR head SHA.
  # New reads go through the read-only gh-api-get.sh wrapper (least privilege; GET-only,
  # no allow-list broadening). gh-api-get.sh is co-located (both are bundled PR-lifecycle
  # helpers deployed to the same dir). A lookup miss/error degrades to "none" (advisory-
  # only, never a spurious gating "fail"); a genuine regression is still blocked upstream
  # by the required coverage CI check via the CI-green / ship-gate gate.
  script_dir=$(unset CDPATH; cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || script_dir="."
  head_sha=$("$script_dir/gh-api-get.sh" "repos/$repo/pulls/$pr_number" --jq '.head.sha' 2>/dev/null || echo "")
  patch_conclusion=""
  if [ -n "$head_sha" ]; then
    # per_page=100 (single page, no --paginate concat) covers commits with many
    # checks; codecov/patch on a later default page would otherwise read as absent.
    patch_conclusion=$("$script_dir/gh-api-get.sh" "repos/$repo/commits/$head_sha/check-runs?per_page=100" \
      --jq '[.check_runs[] | select(.name == "codecov/patch")] | max_by(.started_at) | .conclusion // empty' 2>/dev/null || echo "")
  fi
  case "$patch_conclusion" in
    success)
      threshold_state="pass" ;;
    failure|cancelled|timed_out|action_required|startup_failure|stale)
      threshold_state="fail" ;;
    *)
      # not found / pending / neutral / skipped: no gating codecov signal -> advisory-only.
      threshold_state="none" ;;
  esac

  # First codecov report URL in the body.
  report_url=$(echo "$body" | grep -oE 'https://app\.codecov\.io/[^)"[:space:]]+' | head -1 || true)

  jq -n \
    --arg status "present" \
    --arg pct "${patch_pct:-}" \
    --arg state "$threshold_state" \
    --arg glyph "$comment_glyph" \
    --arg url "${report_url:-}" \
    --argjson id "$comment_id" \
    '{status: $status, patch_pct: (if $pct == "" then null else ($pct | tonumber) end),
      threshold_state: $state, comment_glyph: $glyph,
      report_url: (if $url == "" then null else $url end),
      comment_id: $id}'
}

# --- Core function: fetch every reviewThread node (paginated) ----------------
# Echoes the accumulated reviewThreads.nodes JSON array (each node carries
# isResolved / path / line / comments(first:1){author}) and returns 0 on success;
# echoes nothing and returns 1 on any lookup / parse / cursor error. Shared by the
# --check-resolved advisory (unchanged behavior) and the --itemized resolved
# lookup. BEST-EFFORT: the callers decide how to degrade on a nonzero return (the
# --check-resolved advisory prints a stderr note; --itemized renders resolved "?").
fetch_review_thread_nodes() {
  local ftn_owner ftn_name ftn_nodes ftn_cursor ftn_page ftn_page_nodes ftn_has_next
  local ftn_cursor_arg
  ftn_owner="${repo%%/*}"
  ftn_name="${repo##*/}"
  ftn_nodes='[]'
  ftn_cursor='null'
  while : ; do
    if [ "$ftn_cursor" = "null" ]; then
      ftn_cursor_arg=(-F cursor=null)
    else
      ftn_cursor_arg=(-f cursor="$ftn_cursor")
    fi
    # shellcheck disable=SC2016  # GraphQL $owner/$name/$pr/$cursor are query variables, NOT shell expansions.
    ftn_page=$(gh api graphql \
      -f owner="$ftn_owner" -f name="$ftn_name" -F pr="$pr_number" "${ftn_cursor_arg[@]}" \
      -f query='
        query($owner:String!,$name:String!,$pr:Int!,$cursor:String){
          repository(owner:$owner,name:$name){
            pullRequest(number:$pr){
              reviewThreads(first:100,after:$cursor){
                pageInfo{ hasNextPage endCursor }
                nodes{ isResolved path line comments(first:1){ nodes{ fullDatabaseId author{ login } } } }
              }
            }
          }
        }' 2>/dev/null) || return 1
    ftn_page_nodes=$(echo "$ftn_page" | jq -c '.data.repository.pullRequest.reviewThreads.nodes // []' 2>/dev/null) || return 1
    ftn_nodes=$(jq -n --argjson acc "$ftn_nodes" --argjson new "$ftn_page_nodes" '$acc + $new' 2>/dev/null) || return 1
    ftn_has_next=$(echo "$ftn_page" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage // false' 2>/dev/null) || return 1
    [ "$ftn_has_next" = "true" ] || break
    ftn_cursor=$(echo "$ftn_page" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // empty' 2>/dev/null) || return 1
    [ -n "$ftn_cursor" ] || return 1
  done
  printf '%s\n' "$ftn_nodes"
  return 0
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

# --- Audit mode: complete-coverage enumeration of every bot comment (#132) ------
# Distinct from the default gating mode. Enumerates ALL CR + Codoki comments:
# inline THREADS (findings; need a reply AND a resolved thread) and issue-level
# review SUMMARIES (informational; no thread to resolve, never gate the exit). Uses
# GraphQL for reviewThreads.isResolved (the REST API does not expose it). PAGINATES
# reviewThreads fully (no silent cap at 100 threads) and FAILS CLOSED (exit 2) on
# any lookup error OR a thread with >100 comments (where "replied" is unprovable),
# so a complete-coverage claim is never made on truncated data. exit 1 only when a
# finding is unreplied or a thread unresolved; exit 0 when every finding is replied
# AND every thread resolved.
if [ "$audit_mode" = true ]; then
  owner="${repo%%/*}"
  name="${repo##*/}"
  # Bot logins to enumerate. Mirrors BOT_LOGIN_FILTER but as a JSON array for
  # membership tests against GraphQL's .author.login and REST's .user.login.
  audit_bots="$BOT_LOGINS_JSON"

  # Paginate reviewThreads FULLY (#132 no-silent-caps). The audit mode CLAIMS
  # complete coverage, so it must never stop at the first 100 threads: a PR with
  # >100 review threads would otherwise SILENTLY TRUNCATE and still print
  # "AUDIT: COMPLETE" / exit 0 -- false confidence. We loop on
  # pageInfo.hasNextPage, passing endCursor as `after`, accumulating EVERY thread
  # node. Each thread's inner comments(first:100) also carries pageInfo; a thread
  # with MORE than 100 comments FAILS CLOSED (exit 2) because "replied" cannot be
  # proven without seeing every comment in the thread. Any lookup error also fails
  # closed. -F cursor=null sends a real GraphQL null for the first page; -f
  # cursor=<endCursor> advances subsequent pages.
  audit_thread_nodes='[]'
  audit_cursor='null'
  while : ; do
    if [ "$audit_cursor" = "null" ]; then
      cursor_arg=(-F cursor=null)
    else
      cursor_arg=(-f cursor="$audit_cursor")
    fi
    # shellcheck disable=SC2016  # GraphQL $owner/$name/$pr/$cursor are query variables, NOT shell expansions.
    page_json=$(gh api graphql \
      -f owner="$owner" -f name="$name" -F pr="$pr_number" "${cursor_arg[@]}" \
      -f query='
        query($owner:String!,$name:String!,$pr:Int!,$cursor:String){
          repository(owner:$owner,name:$name){
            pullRequest(number:$pr){
              reviewThreads(first:100,after:$cursor){
                pageInfo{ hasNextPage endCursor }
                nodes{
                  isResolved
                  path
                  line
                  comments(first:100){
                    pageInfo{ hasNextPage }
                    nodes{ author{ login } }
                  }
                }
              }
            }
          }
        }' 2>/dev/null) || {
      echo "audit: GraphQL reviewThreads query failed for PR #$pr_number ($repo)" >&2
      exit 2
    }

    # FAIL CLOSED if any thread on this page has MORE comments than the single
    # page we fetched: we cannot guarantee we observed a human reply, so a
    # complete-coverage claim would be unprovable. NEVER print COMPLETE / exit 0.
    overflow=$(echo "$page_json" | jq '[.data.repository.pullRequest.reviewThreads.nodes // [] | .[] | select(.comments.pageInfo.hasNextPage == true)] | length')
    if [ "${overflow:-0}" -gt 0 ]; then
      echo "AUDIT INCOMPLETE: a review thread on PR #$pr_number ($repo) has more than 100 comments; cannot guarantee complete coverage." >&2
      exit 2
    fi

    page_nodes=$(echo "$page_json" | jq -c '.data.repository.pullRequest.reviewThreads.nodes // []')
    audit_thread_nodes=$(jq -n --argjson acc "$audit_thread_nodes" --argjson new "$page_nodes" '$acc + $new')

    has_next=$(echo "$page_json" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage // false')
    if [ "$has_next" != "true" ]; then
      break
    fi
    audit_cursor=$(echo "$page_json" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // empty')
    if [ -z "$audit_cursor" ]; then
      # hasNextPage true but no endCursor: cannot advance safely -> fail closed.
      echo "AUDIT INCOMPLETE: reviewThreads reported more pages but no endCursor for PR #$pr_number ($repo); cannot guarantee complete coverage." >&2
      exit 2
    fi
  done

  # Reshape the accumulated nodes back into the single-query envelope so the
  # findings jq below is unchanged.
  threads_json=$(jq -n --argjson nodes "$audit_thread_nodes" \
    '{data:{repository:{pullRequest:{reviewThreads:{nodes:$nodes}}}}}')

  issue_comments_audit=$(gh api "repos/$repo/issues/$pr_number/comments" --paginate 2>/dev/null) || {
    echo "audit: could not fetch issue comments for PR #$pr_number ($repo)" >&2
    exit 2
  }

  # FINDINGS: inline review threads whose ROOT comment author is a bot. A thread is
  # REPLIED when any comment in it is authored by a non-bot (a human reply); RESOLVED
  # is GitHub's reviewThread.isResolved (note: jq treats index 0 as truthy, so a bot
  # matched at array position 0 is still selected).
  #
  # BOT-LOGIN NORMALIZATION (#132): GraphQL's author.login returns bot logins WITHOUT
  # the "[bot]" suffix (e.g. "coderabbitai", "codoki-pr-intelligence") whereas REST's
  # user.login carries it ("coderabbitai[bot]"). audit_bots is the REST-form set
  # (summaries rely on it), so a raw membership test against the suffix-less GraphQL
  # login NEVER matches a bot -> 0 findings AND mis-counts a bot's own reply as a
  # human reply (replied:true). isbot() normalizes: a login matches if the set
  # contains it OR it + "[bot]" -- covering GraphQL's suffix-less form while still
  # matching non-suffixed entries (e.g. "Copilot"). Applied to BOTH the root-author
  # select and the replied (human-reply) computation. index 0 stays truthy via `or`.
  findings=$(echo "$threads_json" | jq -c --argjson bots "$audit_bots" '
    def isbot($l): ($bots | index($l)) or ($bots | index($l + "[bot]"));
    [ (.data.repository.pullRequest.reviewThreads.nodes // [])[]
      | (.comments.nodes // []) as $cs
      | ($cs[0].author.login // "") as $root
      | select(isbot($root))
      | {
          type: "finding",
          author: $root,
          location: (((.path // "?")) + ":" + ((.line // 0) | tostring)),
          replied: ([ $cs[] | (.author.login // "") as $a | select(isbot($a) | not) ] | length > 0),
          resolved: (.isResolved == true)
        } ]')

  # SUMMARIES: issue-level bot review comments (informational; no thread to resolve).
  # ACK STATE (#234): the Codoki summary (author codoki-pr-intelligence[bot]) carries
  # a 👍/👎 ROOT-SUMMARY ack that lives ONLY as a reaction (no isResolved, absent from
  # reviewThreads). Surface it from the embedded reactions counts: any +1 or -1 =>
  # ack=true (ACKED), else ack=false (UNACKED). This is INFORMATIONAL here (the exit
  # code is unchanged; the AUTHORITATIVE block is ship-gate-preflight.sh, which reads
  # the reacting logins via gh-react.sh to enforce the NON-BOT rule). Other bot
  # summaries have no ack surface -> ack=null (rendered "n-a").
  summaries=$(echo "$issue_comments_audit" | jq -c --argjson bots "$audit_bots" '
    [ .[] | select((.user.login // "") as $l | $bots | index($l))
      | { type: "summary", author: .user.login, location: "(issue-level)",
          replied: null, resolved: null,
          ack: (if (.user.login // "") == "codoki-pr-intelligence[bot]"
                then (((.reactions."+1" // 0) + (.reactions."-1" // 0)) > 0)
                else null end) } ]')

  audit_all=$(jq -n --argjson f "$findings" --argjson s "$summaries" '$f + $s')

  printf '%-9s %-34s %-32s %-8s %-12s\n' "TYPE" "AUTHOR" "LOCATION" "REPLIED" "RESOLVED/ACK"
  # RESOLVED/ACK column doubles as the ACK column for issue-level summaries (#234): a
  # Codoki summary shows ACKED / UNACKED (its ack has no thread to resolve), while
  # findings keep yes/NO thread-resolution and non-ack summaries show n-a.
  echo "$audit_all" | jq -r '.[] | [
    .type, .author, .location,
    (if .replied == true then "yes" elif .replied == false then "NO" else "n-a" end),
    (if .type == "summary" and .ack == true then "ACKED"
     elif .type == "summary" and .ack == false then "UNACKED"
     elif .resolved == true then "yes" elif .resolved == false then "NO" else "n-a" end)
  ] | @tsv' | while IFS=$'\t' read -r a_type a_author a_loc a_rep a_res; do
    printf '%-9s %-34s %-32s %-8s %-9s\n' "$a_type" "$a_author" "$a_loc" "$a_rep" "$a_res"
  done

  findings_n=$(echo "$findings" | jq 'length')
  summaries_n=$(echo "$summaries" | jq 'length')
  unreplied_n=$(echo "$findings" | jq '[.[] | select(.replied == false)] | length')
  unresolved_n=$(echo "$findings" | jq '[.[] | select(.resolved == false)] | length')
  replied_n=$(( findings_n - unreplied_n ))
  resolved_n=$(( findings_n - unresolved_n ))

  echo ""
  echo "Findings: $findings_n (replied: $replied_n, unreplied: $unreplied_n; resolved: $resolved_n, unresolved: $unresolved_n)"
  echo "Summaries (issue-level, informational): $summaries_n"

  if [ "$unreplied_n" -gt 0 ] || [ "$unresolved_n" -gt 0 ]; then
    echo "AUDIT: INCOMPLETE -- $unreplied_n unreplied finding(s), $unresolved_n unresolved thread(s) on PR #$pr_number."
    exit 1
  fi
  echo "AUDIT: COMPLETE -- all $findings_n finding(s) replied and all threads resolved on PR #$pr_number."
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
  if [ "$count_only" = false ] && [ "$itemized" = false ]; then
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

# Outside-diff finding count (#132): CodeRabbit carries findings it cannot post as
# inline comments inside an "Outside diff range comments (N)" collapsible in the
# review BODY. These are real actionable findings with no inline thread, so the
# gating count must ADD N (not just count the body as 1). Computed from the FULL
# bodies here, BEFORE the truncating display transform below would strip the block.
# Summed across ALL surviving CR review bodies -- never latest-per-reviewer: an
# APPROVED later review does not clear an outside-diff Major from an earlier
# COMMENTED one (stillwater#1931).
outside_diff_sum=$(echo "$review_bodies" | jq '
  [ .[] | (.body // "") | scan("Outside diff range comments \\(([0-9]+)\\)") ]
  | flatten | map(tonumber) | add // 0')

if [ "$latest_per_reviewer" = true ]; then
  review_bodies=$(echo "$review_bodies" | jq 'group_by(.user.login) | map(max_by(.id)) | flatten |
    map({id, type: "review-body", reply_type: "top-level", user: .user.login, state, body: '"$rb_body_expr"'})')
else
  review_bodies=$(echo "$review_bodies" | jq 'map({id, type: "review-body", reply_type: "top-level", user: .user.login, state, body: '"$rb_body_expr"'})')
fi

review_body_count=$(echo "$review_bodies" | jq 'length')
# Authoritative gating count = surviving review bodies + their outside-diff findings.
review_body_findings=$(( review_body_count + outside_diff_sum ))

if [ "$review_body_findings" -gt 0 ]; then
  if [ "$count_only" = false ] && [ "$itemized" = false ]; then
    echo "=== Review-body comments with actionable findings: $review_body_findings ==="
    echo ""
    echo "$review_bodies"
    echo ""
  fi
  found=$((found + review_body_findings))
fi

# 3. Issue-level comments (skip auto-generated summaries)
issue_comments=$(gh api "repos/$repo/issues/$pr_number/comments" --paginate)

if [ "$full_mode" = true ]; then
  ic_body_expr='.body'
else
  ic_body_expr='(.body | split("\n")[0][:120])'
fi

# Exclude issue-level INFORMATIONAL bot summaries that are not actionable findings, so
# the count (which --count-only echoes) agrees with --audit's authoritative accounting
# instead of over-counting (#272; observed on docs-only PR #271: --count-only=2 vs
# --audit=0). Two Codoki HTML-comment markers:
#   - CODOKI_INFO           -> ALWAYS informational; never a finding.
#   - CODOKI_REVIEW_COMMENT -> Codoki's issue-level review SUMMARY. Its "action" is the
#     ROOT-SUMMARY ack (a 👍/👎 reaction; #234), NOT a reply. Once ACKED (any +1/-1 on
#     the comment) it is handled, so drop it. An UNACKED summary INTENTIONALLY still
#     counts: the ack is a real pending action, and ship-gate-preflight.sh BLOCKs on it -
#     so keeping it counted keeps --count-only consistent with that gate.
# (CodeRabbit's own auto-generated summary is already dropped by the "auto-generated" test.)
actionable_issue=$(echo "$issue_comments" | jq --arg me "$me" '[.[] | select(
  '"$BOT_LOGIN_FILTER"' and
  (.body | test("auto-generated"; "i") | not) and
  (.body | test("<!--\\s*CODOKI_INFO") | not) and
  (((.body | test("<!--\\s*CODOKI_REVIEW_COMMENT")) and
    (((.reactions."+1" // 0) + (.reactions."-1" // 0)) > 0)) | not) and
  (.body | test("^\\s*$") | not)
) | {id, type: "issue-comment", reply_type: "top-level", user: .user.login, created_at,
     body: '"$ic_body_expr"'}]')

issue_count=$(echo "$actionable_issue" | jq 'length')

if [ "$issue_count" -gt 0 ]; then
  if [ "$count_only" = false ] && [ "$itemized" = false ]; then
    echo "=== Actionable issue-level bot comments: $issue_count ==="
    echo ""
    echo "$actionable_issue"
    echo ""
  fi
  found=$((found + issue_count))
fi

# --- Itemized checklist mode (#252) ------------------------------------------
# Emit ONE checkable pipe-delimited line per UNADDRESSED bot finding across ALL
# THREE classes (inline threads, review-BODY findings, issue-level actionable
# comments), so no class can be dropped by an inline-only glance. This is a
# REPORT/checklist, not a gate: a non-empty list still exits 0. Placed here, after
# all three arrays are computed, so it reuses them verbatim; it exits before the
# merge-blocker / coverage / stale / summary sections.
if [ "$itemized" = true ]; then
  # RESOLVED lookup (best-effort): fetch every reviewThread ONCE and match each inline
  # finding by its review-comment ID (rebase-safe). SKIPPED when there are no inline
  # findings to resolve (the result would be unused, #256 CR). A fetch failure -> "?".
  itemized_nodes='[]'
  itemized_resolved_ok=true
  if [ "$inline_count" -gt 0 ]; then
    if it_fetch=$(fetch_review_thread_nodes); then
      itemized_nodes="$it_fetch"
    else
      itemized_resolved_ok=false
    fi
  fi

  echo "=== Itemized triage checklist: $found finding(s) (HEAD: $head_sha) ==="

  # ONE jq invocation (excerpt() defined once, no cross-class drift) emitting the three
  # classes IN ORDER: inline (unreplied), then review-body, then issue-level.
  jq -n -r \
    --argjson inline "$all_comments" \
    --argjson reviewbody "$review_bodies" \
    --argjson issue "$actionable_issue" \
    --argjson ids "$unreplied_ids" \
    --argjson nodes "$itemized_nodes" \
    --arg ok "$itemized_resolved_ok" '
    def excerpt($b): (($b // "") | split("\n")
      | map(gsub("<!--.*?-->"; "")         # HTML comments (lazy; the body may contain >)
            | gsub("<[^>]*>"; "")          # HTML tags
            | gsub("\\|"; "/")             # a literal pipe would corrupt the | columns
            | gsub("[[:space:]]+"; " ")
            | gsub("^[ >#*]+"; "") | sub("[ *]+$"; ""))
      | map(select(. != "")) | (.[0] // "") | .[0:60]);
    # (review-comment fullDatabaseId)->isResolved index, built ONCE. Keyed on the thread
    # ROOT comment id (== the REST comment id), NOT path+line: `line` tracks the current
    # diff line while a comment carries its creation-time position, so a rebase would
    # misattribute isResolved and duplicate path:line pairs would collide (#256 CR Major).
    ( $nodes | map(select(.comments.nodes[0].fullDatabaseId != null))
      | map({key: (.comments.nodes[0].fullDatabaseId | tostring), value: .isResolved})
      | from_entries ) as $ridx |
    ( $inline | [ .[] | select(.id as $id | $ids | any(. == $id)) ] | .[]
      | (.user.login | sub("\\[bot\\]$"; "")) as $u
      | (.path // "?") as $p
      | (.original_line // 0) as $ln
      | ( if $ok != "true" then "?"
          else ( $ridx[(.id | tostring)]
                 | if . == null then "?" elif . == true then "yes" else "no" end )
          end ) as $res
      | "inline | \($u) | \($p):\($ln) | \(excerpt(.body)) | replied:no resolved:\($res)" ),
    # A CR review body can carry N "Outside diff range comments (N)" sub-findings that
    # count toward the header total but have no separate array element; annotate the line
    # with that subtotal so the header count == the visible accounting (#252 core).
    ( $reviewbody | .[]
      | (.user | sub("\\[bot\\]$"; "")) as $u
      | ([.body | scan("Outside diff range comments \\(([0-9]+)\\)")] | flatten | map(tonumber) | add // 0) as $od
      | "review-body | \($u) | (body) | \(excerpt(.body))\(if $od > 0 then " [+\($od) outside-diff]" else "" end) | replied:no resolved:n/a" ),
    ( $issue | .[]
      | (.user | sub("\\[bot\\]$"; "")) as $u
      | "issue-level | \($u) | (issue) | \(excerpt(.body)) | replied:n/a resolved:n/a" )'

  # Review-body findings have no inline thread; each clears only when addressed AND
  # the reviewer re-reviews a fresh SHA (a maintainer re-trigger for CodeRabbit).
  if [ "$review_body_count" -gt 0 ]; then
    echo "NOTE: review-body findings have no inline thread to resolve; each clears only when addressed AND the reviewer re-reviews a fresh SHA (a maintainer re-trigger for CodeRabbit)."
  fi
  exit 0
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
  if [ "$count_only" = false ] && [ "$itemized" = false ]; then
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
if [ "$count_only" = false ] && [ "$itemized" = false ]; then
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

# --- Staleness advisory (#93; non-fatal, exit code UNCHANGED) ----------------
# Surface when a bot VERDICT predates the current HEAD push so a consumer never
# trusts a review made against pre-HEAD code (Codoki edits its single comment in
# place: created_at fixed, updated_at advances, the verdict can flip on the same
# id). Compares each bot's LATEST verdict timestamp (review .submitted_at;
# issue-comment .updated_at) against the HEAD commit's committer date. ADVISORY
# ONLY: prints a parseable "STALE-ADVISORY:" line and does NOT change the exit
# code, so callers that branch on a non-zero exit are unaffected. Suppressed in
# --count-only (numeric-only) output.
if [ "$count_only" = false ] && [ "$itemized" = false ]; then
  head_full_sha=$(gh api "repos/$repo/pulls/$pr_number" --jq '.head.sha' 2>/dev/null || echo "")
  head_committer_date=$(gh api "repos/$repo/commits/$head_full_sha" --jq '.commit.committer.date' 2>/dev/null || echo "")
  if [ -n "$head_full_sha" ] && [ -n "$head_committer_date" ]; then
    short_head="${head_full_sha:0:8}"
    # Latest review per bot author whose submitted_at predates the HEAD push.
    echo "$all_reviews" | jq -r --arg hd "$head_committer_date" --arg sha "$short_head" '
      [ .[] | select('"$BOT_LOGIN_FILTER"') | select((.submitted_at // "") != "") ]
      | group_by(.user.login) | map(max_by(.submitted_at)) | .[]
      | select(.submitted_at < $hd)
      | "STALE-ADVISORY: \(.user.login) verdict updated \(.submitted_at) predates current HEAD \($sha)"'
    # Latest issue comment per bot author whose updated_at predates the HEAD push
    # (in-place edits advance updated_at while created_at stays fixed -> "(edited)").
    echo "$issue_comments" | jq -r --arg hd "$head_committer_date" --arg sha "$short_head" '
      [ .[] | select('"$BOT_LOGIN_FILTER"') | select((.updated_at // "") != "") ]
      | group_by(.user.login) | map(max_by(.updated_at)) | .[]
      | select(.updated_at < $hd)
      | (if .updated_at != .created_at then " (edited)" else "" end) as $e
      | "STALE-ADVISORY: \(.user.login) verdict updated \(.updated_at)\($e) predates current HEAD \($sha)"'
  fi
fi

# --- Unresolved-thread advisory (#145; --check-resolved; non-fatal, exit UNCHANGED)
# Wire the thread-RESOLUTION signal (reviewThread.isResolved - GraphQL-only; the
# REST comments API never exposes it) into the DEFAULT gating path, so a consumer
# can gate ANY PR-state claim (blocked / idle / waiting-on-you / merge-ready) on
# resolution, not just on unreplied comments. ADVISORY ONLY: prints parseable
# "UNRESOLVED-ADVISORY:" lines and NEVER changes the exit code (the STALE-ADVISORY
# contract). DELIBERATELY NOT the --audit loop: --audit FAILS CLOSED (exit 2) to
# back a complete-coverage claim, whereas this is BEST-EFFORT - a query error or a
# missing cursor degrades to one stderr note and is skipped, never a non-zero
# exit. It needs only each thread's ROOT author + isResolved, so it fetches
# comments(first:1) and has no >100-comments overflow case. Opt-in via
# --check-resolved; suppressed in --count-only (numeric-only) output.
if [ "$check_resolved" = true ] && [ "$count_only" = false ]; then
  # Pagination extracted to fetch_review_thread_nodes() (#252); behavior unchanged:
  # a successful fetch yields the accumulated nodes, any error degrades to the
  # stderr note below (this advisory NEVER changes the exit code).
  if cr_nodes=$(fetch_review_thread_nodes); then
    cr_ok=true
  else
    cr_ok=false
  fi
  if [ "$cr_ok" = true ]; then
    echo "$cr_nodes" | jq -r --argjson bots "$BOT_LOGINS_JSON" '
      def isbot($l): ($bots | index($l)) or ($bots | index($l + "[bot]"));
      .[]
      | ((.comments.nodes // [])[0].author.login // "") as $root
      | select(isbot($root))
      | select(.isResolved == false)
      | "UNRESOLVED-ADVISORY: \($root) thread at \((.path // "?")):\((.line // 0)) is unresolved"'
  else
    echo "UNRESOLVED-ADVISORY: could not fully enumerate review threads for PR #$pr_number ($repo); resolution state unverified" >&2
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
