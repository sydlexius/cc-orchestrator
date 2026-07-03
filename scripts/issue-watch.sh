#!/usr/bin/env bash
# issue-watch.sh -- Wait for a GitHub ISSUE to show new activity. Silent until done.
#
# Usage:
#   issue-watch.sh [--author <login>] <issue_number> [repo] [timeout_secs]
#
# Defaults:
#   repo          auto-detected via `gh repo view` from the current dir
#   timeout_secs  1800 (30 min)
#   poll cadence  every 30s (ISSUE_WATCH_POLL_INTERVAL override, numeric seconds)
#
# The issue-side counterpart to pr-watch.sh. An issue has no CI or review-decision,
# so instead of evaluating a rich terminal state this polls a lightweight snapshot
# {comment_count, state, labels, assignees} captured at watch-start and fires on the
# FIRST change. Spiritually closer to pr-unreplied-comments' count polling than to
# pr-watch's terminal-state model. Only activity AFTER watch-start counts (the
# baseline is the state when the watch begins).
#
# Terminal lines (one on stdout, then exit 0):
#   closed      issue=<n>                              state -> CLOSED
#   reopened    issue=<n>                              state -> OPEN (was CLOSED)
#   new-comment issue=<n> author=<login> id=<cid>      a comment appeared (id not in baseline)
#     <body>                                           ...full body on the following lines
#   plan-ready  issue=<n> author=<login> id=<cid>      --author comment appeared AND stabilized
#     <body>
#   labeled     issue=<n> +<label>... -<label>...      labels added/removed
#   assigned    issue=<n> +<login>... -<login>...      assignees added/removed
#
#   timeout: waited <secs>s                            [stderr]   Exit 1.
#   setup error: <message>                             [stderr]   Exit 2.
#
# Change priority when several move in the same poll: closed, then the comment
# terminal (new-comment / plan-ready), then labeled, then assigned, then reopened.
# The consumer dispatches off the single terminal line.
#
# --author <login> mode (the CodeRabbit Coding-Plan case): narrows the comment
# trigger to that author AND auto-stabilizes. A CR Coding Plan lands as ONE comment
# that self-edits over ~10-15 min, so firing on first appearance would surface a
# half-written plan. Instead, once a NEW comment from <login> appears, the loop keeps
# polling until that comment's body is BYTE-IDENTICAL across two consecutive polls,
# then emits `plan-ready`. `closed` still fires immediately, regardless of --author.
# Without --author, any new comment (from anyone) trips `new-comment` on first sight.
#
# Robustness: a transient gh/jq error on a single poll is retried on the next poll
# (fail-open on polls, mirroring pr-watch); only hard setup errors (bad args, no
# repo) exit 2. Silent until a terminal state holds.
set -uo pipefail

# -h / --help: print this header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

if ! command -v gh >/dev/null 2>&1; then
  echo "setup error: gh (GitHub CLI) is required but not installed" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
author=""
while [ "${1:-}" = --author ]; do
  author="${2:-}"
  if [ -z "$author" ]; then
    echo "setup error: --author requires a login" >&2
    exit 2
  fi
  shift 2
done

if [ $# -lt 1 ] || [ $# -gt 3 ]; then
  echo "usage: issue-watch.sh [--author <login>] <issue_number> [repo] [timeout_secs]" >&2
  exit 2
fi

issue="$1"
repo="${2:-}"
timeout_secs="${3:-1800}"
poll_interval="${ISSUE_WATCH_POLL_INTERVAL:-30}"
case "$poll_interval" in ''|*[!0-9]*) poll_interval=30 ;; esac

if ! [[ "$issue" =~ ^[0-9]+$ ]]; then
  echo "setup error: issue_number must be numeric, got: $issue" >&2
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
# Snapshot helpers
# ---------------------------------------------------------------------------
# fetch_state -- emit "STATE\tLABELS\tASSIGNEES" for the issue, or empty on any
# gh/jq error (caller retries next poll). LABELS/ASSIGNEES are sorted, comma-joined
# so a set-membership change is a plain string inequality. Accepted limitation: a
# label NAME containing a literal comma garbles only the `+`/`-` decomposition in
# the `labeled` line (the change is still detected and fired correctly); assignee
# logins cannot contain commas.
fetch_state() {
  local j
  j=$(gh issue view "$issue" --repo "$repo" --json state,labels,assignees 2>/dev/null) || return 1
  [ -z "$j" ] && return 1
  printf '%s' "$j" | jq -r '
    (.state // "")
    + "\t" + ([.labels[]?.name] | sort | join(","))
    + "\t" + ([.assignees[]?.login] | sort | join(","))
  ' 2>/dev/null || return 1
}

# fetch_comments -- emit the issue comments as a single JSON array (paginated),
# or return non-zero on any gh error. Each element carries id, user.login, body.
# CRITICAL: capture gh's exit status BEFORE the jq slurp. A bare
# `gh ... | jq -s 'add // []'` swallows a gh failure -- jq on empty input emits
# `[]` and exits 0 -- which would let a transient blip on the BASELINE poll record
# an empty comment set and then fire a false new-comment/plan-ready on the real
# pre-existing comments (the fail-open-retry must trigger instead).
fetch_comments() {
  local raw
  raw=$(gh api --paginate "repos/$repo/issues/$issue/comments" 2>/dev/null) || return 1
  printf '%s' "$raw" | jq -s 'add // []' 2>/dev/null
}

# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------
start=$(date +%s)
baseline_captured=0
base_state=""; base_labels=""; base_assignees=""; base_ids=""
# --author stabilization carries the previous poll's target (id + body) so a
# body unchanged across two consecutive polls fires plan-ready.
prev_target_id=""; prev_target_body=""

while :; do
  state_line=$(fetch_state)
  comments_json=$(fetch_comments)

  # Transient error on this poll -> skip comparison, retry after the interval
  # (still subject to the timeout below). Only hard setup errors exit non-zero.
  if [ -z "$state_line" ] || [ -z "$comments_json" ]; then
    now=$(date +%s)
    if [ $(( now - start )) -ge "$timeout_secs" ]; then
      echo "timeout: waited $(( now - start ))s" >&2
      exit 1
    fi
    sleep "$poll_interval"
    continue
  fi

  cur_state="${state_line%%$'\t'*}"
  rest="${state_line#*$'\t'}"
  cur_labels="${rest%%$'\t'*}"
  cur_assignees="${rest#*$'\t'}"
  cur_ids=$(printf '%s' "$comments_json" | jq -r '[.[].id | tostring] | sort | join(",")' 2>/dev/null)

  if [ "$baseline_captured" -eq 0 ]; then
    base_state="$cur_state"; base_labels="$cur_labels"
    base_assignees="$cur_assignees"; base_ids="$cur_ids"
    baseline_captured=1
  else
    # (1) closed -- highest priority, fires immediately even in --author mode.
    if [ "$cur_state" = "CLOSED" ] && [ "$base_state" != "CLOSED" ]; then
      echo "closed issue=$issue"
      exit 0
    fi

    # (2) comment terminal. base_ids is the comma-joined id set at watch-start; a
    # "new" comment is an id absent from it. --author narrows to that login and
    # requires stabilization; the default path fires on the newest new comment.
    # We take the NEWEST new comment (.[-1]; gh returns comments ascending by
    # creation) so in --author mode a self-editing CR Coding Plan wins even if the
    # author posted an ancillary comment first -- otherwise the oldest new comment
    # would latch and mask the plan (hostile-review finding).
    new_comment=$(printf '%s' "$comments_json" | jq -r --arg base ",$base_ids," --arg author "$author" '
      [ .[]
        | select((",\(.id),") as $k | ($base | contains($k)) | not)
        | select($author == "" or (.user.login == $author)) ]
      | (if length > 0 then (.[-1] | "\(.id)\t\(.user.login)") else "" end)
    ' 2>/dev/null)

    if [ -n "$new_comment" ]; then
      nc_id="${new_comment%%$'\t'*}"
      nc_author="${new_comment#*$'\t'}"
      nc_body=$(printf '%s' "$comments_json" | jq -r --arg id "$nc_id" '.[] | select((.id|tostring) == $id) | .body' 2>/dev/null)

      if [ -z "$author" ]; then
        echo "new-comment issue=$issue author=$nc_author id=$nc_id"
        printf '%s\n' "$nc_body"
        exit 0
      fi
      # --author mode: stabilize. Fire only when the SAME target id is byte-identical
      # to the previous poll; otherwise remember it and keep polling.
      if [ "$nc_id" = "$prev_target_id" ] && [ "$nc_body" = "$prev_target_body" ]; then
        echo "plan-ready issue=$issue author=$nc_author id=$nc_id"
        printf '%s\n' "$nc_body"
        exit 0
      fi
      prev_target_id="$nc_id"; prev_target_body="$nc_body"
    else
      # No qualifying target this poll -> reset the stabilization carry.
      prev_target_id=""; prev_target_body=""
    fi

    # (3) labels changed.
    if [ "$cur_labels" != "$base_labels" ]; then
      added=$(comm -13 <(printf '%s' "$base_labels" | tr ',' '\n' | sort) \
                       <(printf '%s' "$cur_labels" | tr ',' '\n' | sort) | sed '/^$/d' | sed 's/^/+/')
      removed=$(comm -23 <(printf '%s' "$base_labels" | tr ',' '\n' | sort) \
                         <(printf '%s' "$cur_labels" | tr ',' '\n' | sort) | sed '/^$/d' | sed 's/^/-/')
      echo "labeled issue=$issue $(printf '%s %s' "$added" "$removed" | tr '\n' ' ' | tr -s ' ' | sed 's/ $//')"
      exit 0
    fi

    # (4) assignees changed.
    if [ "$cur_assignees" != "$base_assignees" ]; then
      added=$(comm -13 <(printf '%s' "$base_assignees" | tr ',' '\n' | sort) \
                       <(printf '%s' "$cur_assignees" | tr ',' '\n' | sort) | sed '/^$/d' | sed 's/^/+/')
      removed=$(comm -23 <(printf '%s' "$base_assignees" | tr ',' '\n' | sort) \
                         <(printf '%s' "$cur_assignees" | tr ',' '\n' | sort) | sed '/^$/d' | sed 's/^/-/')
      echo "assigned issue=$issue $(printf '%s %s' "$added" "$removed" | tr '\n' ' ' | tr -s ' ' | sed 's/ $//')"
      exit 0
    fi

    # (5) reopened -- lowest priority (a close would have fired above).
    if [ "$cur_state" = "OPEN" ] && [ "$base_state" = "CLOSED" ]; then
      echo "reopened issue=$issue"
      exit 0
    fi
  fi

  now=$(date +%s)
  if [ $(( now - start )) -ge "$timeout_secs" ]; then
    echo "timeout: waited $(( now - start ))s" >&2
    exit 1
  fi
  sleep "$poll_interval"
done
