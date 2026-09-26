#!/usr/bin/env bash
# cr-quota-watch.sh <PR#> [owner/repo]   ("elmer" Increment 0)
#
# READ-ONLY surfacer for CodeRabbit's own quota announcements. It POSTS NOTHING and
# triggers nothing, so it can NEVER consume a review slot. Triggering a CR review
# remains the maintainer's exclusive purview; this script only reads.
#
# WHY IT EXISTS: CR already reports its remaining quota, but a human never reliably
# sees it. Two different sentences carry the signal, and they use DIFFERENT NOUN
# PHRASES -- a matcher tuned to one silently misses the other:
#
#   1. The rate-limit reply (to a status query or a blocked trigger):
#        "... Your next review will be available in 59 minutes."
#   2. The acknowledgment appended to a review CR actually performed:
#        "... Your next INCLUDED review will be available in 54 minutes."
#      GitHub wraps this one in a <details> block that renders COLLAPSED: the visible
#      summary is just "Action performed", so the quota sentence is invisible unless
#      someone clicks to expand it. It is also the BETTER signal -- it reports the
#      spent slot at the moment the review lands, with no extra query -- and it
#      appears ONLY when the limit is actually reached. Invisible to a human,
#      trivially readable via the API.
#   3. (#454, measured 2026-09) The banner in CR's SUMMARY comment:
#        "**Next included review available in 49 minutes."
#      Capital N and NO "will be", so the match is case-insensitive and "will be" is
#      optional. CR EDITS that summary IN PLACE, so its countdown is anchored to the
#      comment's `updated_at`, not `created_at` (dating it from creation reported ~26m
#      for a fresh 59-minute notice on a summary created 33 minutes before the edit).
#   4. (measured 2026-09-26, the MAJORITY form) The summary's REMAINING-SLOT COUNT:
#        "**Included review availability:** 0 reviews are currently available. ..."
#      A zero count has NO countdown at all; see BAN below.
#
# Result: the throttle FEELS arbitrary when it has in fact been announced every time.
# This converts that invisible announcement into one visible terminal line.
#
# MATCHER (measured across 21 real instances, 2026-07-24..27, plus 2026-07-30, plus
# the #454 summary banner 2026-09; case-insensitive):
#   next (included )?review (will be )?available in <TERM>((, and |, | and )<TERM>)*\.
#   where TERM = (\d+) (second|minute|hour)s?   -- terms are SUMMED (#467)
# The duration is always RELATIVE -- never a wall-clock time, never a timezone, never
# a date. A naive `(\d+) minutes` breaks on real messages: "1 minute." is SINGULAR and
# "4 seconds." is a DIFFERENT UNIT. `hour` and values above 59 are accepted even though
# nothing measured exceeded 59 minutes; it costs nothing and avoids a silent parse
# failure. A COMPOUND "1 hour and 5 minutes." (#467) used to match nothing and so read
# as "no limit", exit 0 -- a false all-clear. Now: when the CR phrase up to
# "available in" is recognized but the duration is not a shape above, the reading is
# LIMITED with an UNKNOWN deadline (see UNKNOWN_CEILING below), never "no limit".
# A body without the CR phrase at all is unrecognized -> report nothing, never guess.
#
# ACCOUNT-WIDE (#456): CR's limit is per ACCOUNT, so the scan reads the queried PR AND
# the repo's most recently updated PRs, then applies the selection rule below to all
# of them together (see SCAN_LIMIT below).
#
# TRAP: the retired Codoki service used an ABSOLUTE UTC timestamp
# ("Next available slot: 2026-06-22 04:50:02 UTC"), and transcripts are full of them.
# Reading one as a relative duration would be badly wrong, so this matcher requires
# CR's exact "available in <N> <unit>." shape AND requires the CR bot login.
#
# NON-MONOTONIC BY DESIGN: CR's limits are adaptive, so a later reading can be LARGER
# than an earlier one (canticle #656 read 53 minutes, then 51 minutes an HOUR later).
# Never count down locally from an old reading: every deadline is computed from its
# OWN comment's timestamp. SELECTION RULE (#454 review):
#   - A LIMITED signal is dated by `updated_at` (falling back to `created_at` when
#     absent/unparseable): an edited-in-place summary's countdown was written at its
#     last edit.
#   - An AVAILABLE ("Reviews are available now") signal is dated by `created_at` ONLY.
#     An edit to an old reply (or to anything else in that comment) does not re-assert
#     availability, and dating it by the edit let an old reply outrank a fresh limit.
#   - Among the limited signals NEWER than the newest available one, the LARGEST
#     deadline wins -- not the newest signal. An edit can refresh a comment's
#     `updated_at` without refreshing a stale banner inside it (a human ticking a
#     checkbox in CR's summary), so "most recently touched" is not "most recent
#     reading"; taking the max fails only toward LIMITED, never toward a false
#     all-clear. With no such limited signal, the newest available one wins.
#
# Usage:
#   cr-quota-watch.sh <PR#> [owner/repo]
#
# Arguments:
#   PR#         PR number (required, numeric). Always scanned; the recently updated
#               PRs are scanned beside it.
#   owner/repo  Repo slug (optional; resolved via `gh repo view` if omitted).
#
# Exit codes:
#   0  No ACTIVE limit -- no signal found, the selected signal's deadline has passed,
#      or CR reported reviews available. (Also the state a caller may act on.)
#   1  LIMITED -- the selected signal's deadline is still in the future; the remaining
#      time and the Pacific-labeled deadline are surfaced. An unparseable duration is
#      also exit 1, and its line carries the literal token "deadline UNKNOWN".
#   2  SETUP ERROR -- bad/missing args, repo unresolvable, a gh read failure (the PR
#      list or ANY scanned PR's comments), or a failure evaluating the comments. A read
#      or evaluation failure is NEVER reported as "no limit": that would be a false
#      all-clear.
set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

# --- Argument parsing / startup validation ---
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: cr-quota-watch.sh <PR#> [owner/repo]" >&2
  exit 2
fi
pr="$1"
repo="${2:-}"
if ! [[ "$pr" =~ ^[0-9]+$ ]]; then
  echo "setup error: PR# must be numeric, got: $pr" >&2
  exit 2
fi
# Normalize "01" to "1" (base-10, never octal) so the dedupe below and the
# "account-wide" label compare the same spelling GitHub returns.
pr=$(( 10#$pr ))
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "setup error: could not resolve repo (pass owner/repo, or run inside a gh-aware repo)" >&2
  exit 2
fi

CR_LOGIN='coderabbitai[bot]'

# ACCOUNT-WIDE SCAN BOUND (#456). CR's limit is per ACCOUNT, not per PR: measured on
# canticle 2026-09-23, a review on #1053 moved #1052's countdown from 8 to 59 minutes
# while #1053's own summary never showed a notice, so reading only the queried PR
# misses an active limit. The scan covers the SCAN_LIMIT most-recently-UPDATED PRs
# (state=all: a notice can sit on a just-merged PR) PLUS the queried PR, which is
# always read even when the list omits it. A PR's `updated_at` moves on every NEW
# comment or review, so a fresh notice puts its PR at the top of that list. ASSUMPTION,
# UNMEASURED: whether CR's IN-PLACE edit of its summary also moves the PR's `updated_at`;
# if not, a quiet PR whose only fresh signal is an edited banner can fall outside the
# bound. The bound is a COUNT,
# not a time window: a window could drop a long notice on an otherwise quiet PR, and
# the count alone keeps a tick at 1 list read + at most SCAN_LIMIT+1 comment reads.
SCAN_LIMIT=10

# UNKNOWN-DEADLINE CEILING (#467). A recognized CR limit phrase whose duration this
# matcher cannot parse is LIMITED with an UNKNOWN deadline, never "no limit" (that was
# the false all-clear: "1 hour and 5 minutes." used to match nothing). It still needs
# SOME deadline or one unparseable notice, kept alive by the account-wide scan, would
# wedge the unattended loop until CR happened to post "available now". So it gets an
# ASSUMED deadline of notice-time + 1h (maintainer decision 2026-09-26): no CR throttle
# longer than an hour has been seen in a long time, and an over-long wait costs real
# review throughput. If 1h proves short, the post draws a "Review rate limited." reply
# and elmer-tick's #455 re-admission recovers it. It competes under the normal
# largest-deadline rule, and a newer "available" clears it.
UNKNOWN_CEILING=3600

# --- List the recently updated PRs (READ-ONLY; per_page IS the bound, no --paginate) ---
# A read failure, a non-array body, or a PR number that is not a positive integer is a
# setup error: a scan that silently covered fewer PRs would be a partial false all-clear.
list_raw="$(gh api "repos/$repo/pulls?state=all&sort=updated&direction=desc&per_page=$SCAN_LIMIT" 2>/dev/null)" || {
  echo "setup error: could not list recently updated PRs for $repo (gh api read failed)" >&2
  exit 2
}
if ! scan_prs="$(printf '%s' "$list_raw" | jq -r --argjson lim "$SCAN_LIMIT" '
  if type != "array" then error("the PR list is not a JSON array") else . end
  | .[:$lim][]
  | (if type == "object" then .number else null end)
  | if type == "number" and . == floor and . > 0 then tostring else error("bad PR number") end
' 2>/dev/null)"; then
  echo "setup error: could not parse the recently updated PR list for $repo" >&2
  exit 2
fi
# An EMPTY list (empty gh output, or `[]`) is never legitimate for state=all in a repo
# that has the queried PR, so it is a failed read, not "nothing else to scan".
if [ -z "$scan_prs" ]; then
  echo "setup error: the recently updated PR list for $repo is empty" >&2
  exit 2
fi

# The queried PR first, then the listed ones, deduplicated. Every entry is a validated
# integer, so the unquoted word-split below is safe.
targets="$pr"
while IFS= read -r n; do
  [ -n "$n" ] || continue
  # jq keeps a number's literal spelling, so 1e3 survives `tostring` as "1E+3".
  case "$n" in
    *[!0-9]*) echo "setup error: non-integer PR number in the list for $repo: $n" >&2; exit 2 ;;
  esac
  case " $targets " in
    *" $n "*) ;;
    *) targets="$targets $n" ;;
  esac
done <<EOF
$scan_prs
EOF

# --- Read each scanned PR's issue comments (READ-ONLY, paginated) ---
# Issue comments carry all three signal shapes, including the #454 summary banner.
# Capture gh's exit status BEFORE the jq pipe: `gh ... 2>/dev/null | jq -s 'add // []'`
# emits `[]` even when gh FAILS (network/auth/404), masking a read error as an empty
# comment set -- which here would read as a false "no limit" all-clear. So the GET is
# its own checked command, then piped. ANY scanned PR's failure fails the whole read.
all_pages=""
scanned=0
for n in $targets; do
  raw="$(gh api --paginate "repos/$repo/issues/$n/comments" 2>/dev/null)" || {
    echo "setup error: could not read issue comments for PR #$n ($repo) (gh api read failed)" >&2
    exit 2
  }
  # gh returns `[]` for zero comments, never empty output; empty is a failed read.
  if [ -z "$raw" ]; then
    echo "setup error: empty issue-comments body for PR #$n ($repo)" >&2
    exit 2
  fi
  # Every page must be a JSON ARRAY before the `// []` empty-result fallback: `add // []`
  # alone turns a `null`/`false` page into `[]`, so an error body would read as "no quota
  # signal", a false all-clear. jq's status is CHECKED (malformed JSON fails here, exit 2).
  # Each comment is tagged with its source PR so the winning signal can be named.
  if ! page="$(printf '%s' "$raw" | jq -c -s --argjson src "$n" '
    if all(.[]; type == "array") then (add // []) else error("a comments page is not a JSON array") end
    | map(if type == "object" then . + {_pr: $src} else . end)
  ' 2>/dev/null)" || [ -z "$page" ]; then
    echo "setup error: could not parse issue comments for PR #$n ($repo)" >&2
    exit 2
  fi
  all_pages="$all_pages$page
"
  scanned=$(( scanned + 1 ))
done
if ! comments="$(printf '%s' "$all_pages" | jq -c -s 'add // []' 2>/dev/null)" || [ -z "$comments" ]; then
  echo "setup error: could not combine issue comments for $repo" >&2
  exit 2
fi

# --- Select the quota signal ACROSS ALL SCANNED PRs (see SELECTION RULE in the header) ---
# Emitted as tab-separated: kind, deadline epoch, the duration phrase, the noun phrase
# CR used, unknown (true|false), source PR. Timestamps are parsed with `try/catch` and a
# non-string body is coerced with `tostring`, so one malformed comment cannot abort the
# whole read. The "i" flag makes the match case-insensitive (#454).
#
# #467: the duration is one or more `<N> <unit>` terms joined by " and ", ", " or
# ", and " ("1 hour and 5 minutes."), SUMMED. PFX alone matching (the phrase is CR's
# but the duration is not a shape RX knows) is the UNKNOWN-deadline case above.
PFX='next (?<inc>included )?review (?:will be )?available in'

# THE AVAILABILITY BANNER (measured 2026-09-26, 488 PRs / 12 repos: the MAJORITY form of
# CR quota text). CR's summary comment carries a REMAINING-SLOT COUNT, edited in place
# (so dated by `updated_at`), in two wordings:
#   current: "**Included review availability:** 0 reviews are currently available. Your
#            included PR review attempts ... set your current allowance at 1 review per hour."
#   older:   "**Included review availability:** Your plan provides up to 1 included review
#            per hour; 0 remain after this review."
# A ZERO count means the included allowance is EXHAUSTED with NO countdown anywhere, so it
# takes the UNKNOWN-deadline path (LIMITED, 1h ceiling); it used to read as "no limit".
# N > 0 is evidence of budget: an "available" signal dated by `created_at` ONLY, like
# "Reviews are available now" above (an edit can move `updated_at` without refreshing N).
BAN='included review availability:[* ]*(?:(?<n1>[0-9]+) reviews? (?:is|are) currently available\.(?:[^\n]*?allowance at (?<a1>[0-9]+) reviews? per hour)?|your plan provides up to (?<a2>[0-9]+) included reviews? per hour; *(?<n2>[0-9]+) remains? after this review)'
TERM='[0-9]+ (?:second|minute|hour)s?'
RX="$PFX (?<dur>$TERM(?:(?:, and |, | and )$TERM)*)\\."

# jq's exit status is CHECKED: a runtime error here (a non-array response, a comment
# jq cannot index) used to be swallowed by `|| true` into an empty signal, which then
# read as "no quota signal" -- exit 0, a false all-clear. It is a setup error instead.
if ! signal="$(printf '%s' "$comments" | jq -r --arg login "$CR_LOGIN" --arg rx "$RX" \
    --arg pfx "$PFX" --arg ban "$BAN" --argjson ceil "$UNKNOWN_CEILING" '
  if type != "array" then error("issue comments are not a JSON array") else . end
  | [ .[]
    | select((.user.login // "") == $login)
    | (.body // "" | tostring) as $b
    | ._pr as $src
    | (try (.created_at | fromdateiso8601) catch null) as $tc
    | ((try (.updated_at | fromdateiso8601) catch null) // $tc) as $t
    | if $t == null and (($b | test($rx; "i")) or ($b | test($ban; "i"))
                          or ($b | test($pfx; "i"))) then
        { kind: "undatable", t: 0, deadline: 0, unknown: true, raw: "", noun: "", src: $src }
      elif ($b | test($rx; "i")) and $t != null then
        ($b | capture($rx; "i")) as $m
        | ([ $m.dur | scan("([0-9]+) (second|minute|hour)"; "i")
             | (.[0] | tonumber)
               * (.[1] | ascii_downcase
                  | if . == "second" then 1 elif . == "minute" then 60 else 3600 end) ]
           | add | [., 86400] | min) as $secs
        | { kind: "limited", t: $t, deadline: ($t + $secs), unknown: false,
            raw: ($m.dur | ascii_downcase), src: $src,
            noun: (if ($m.inc // "") != "" then "included review" else "review" end) }
      elif ($b | test($ban; "i")) and $t != null then
        ($b | capture($ban; "i")) as $m
        | (($m.n1 // $m.n2) | tonumber) as $n
        # A zero count is LIMITED (dated by updated_at); a positive count is AVAILABLE, and
        # an edit never re-asserts availability, so it is dated by created_at only.
        | (if $n == 0 then $t else ($tc // $t) end) as $bt
        | { kind: (if $n == 0 then "limited" else "available" end), t: $bt,
            deadline: (if $n == 0 then $t + $ceil else $bt end), unknown: ($n == 0),
            raw: "\($n) included review\(if $n == 1 then "" else "s" end) available, allowance \(($m.a1 // $m.a2) // "?")/hour",
            src: $src, noun: "included review" }
      elif ($b | test($pfx; "i")) and $t != null then
        ($b | capture($pfx; "i")) as $m
        | { kind: "limited", t: $t, deadline: ($t + $ceil), unknown: true,
            raw: ($b | capture($pfx + " ?(?<rest>[^\\n]{0,40})"; "i").rest
                  | gsub("[\\t\\r]"; " ")),
            src: $src,
            noun: (if ($m.inc // "") != "" then "included review" else "review" end) }
      elif ($b | test("Reviews are available now")) and $tc != null then
        { kind: "available", t: $tc, deadline: $tc, unknown: false, raw: "", noun: "review",
          src: $src }
      else empty end
  ]
  | if any(.[]; .kind == "undatable") then first(.[] | select(.kind == "undatable")) else
    ([ .[] | select(.kind == "available") | .t ] | max) as $ta
  | ([ .[] | select(.kind == "limited" and ($ta == null or .t >= $ta)) ] | max_by(.deadline))
    // ([ .[] | select(.kind == "available") ] | max_by(.t))
    end
  | if . == null then ""
    else "\(.kind)\t\(.deadline)\t\(.raw)\t\(.noun)\t\(.unknown)\t\(.src)" end
' 2>/dev/null)"; then
  echo "setup error: could not evaluate issue comments for $repo (jq failed)" >&2
  exit 2
fi

if [ -z "$signal" ]; then
  echo "CR quota: no quota signal from CodeRabbit across $scanned scanned PR(s) of $repo (queried PR #$pr). No announced limit."
  exit 0
fi

kind="$(printf '%s' "$signal" | cut -f1)"
deadline="$(printf '%s' "$signal" | cut -f2)"
raw="$(printf '%s' "$signal" | cut -f3)"
noun="$(printf '%s' "$signal" | cut -f4)"
unknown="$(printf '%s' "$signal" | cut -f5)"
src="$(printf '%s' "$signal" | cut -f6)"

# Name the source PR, and say so when it is not the one asked about: that is the
# account-wide case a per-PR read used to miss.
where="PR #$src ($repo)"
if [ "$src" != "$pr" ]; then
  where="$where [account-wide: queried PR #$pr]"
fi

# Q7: a recognized limit notice with no parseable timestamp cannot anchor even the
# assumed ceiling (anchoring it at "now" would re-arm it every tick and wedge the
# loop), and dropping it was a false all-clear, so it is a read failure: exit 2.
if [ "$kind" = "undatable" ]; then
  echo "setup error: a CodeRabbit quota notice on PR #$src ($repo) has no parseable timestamp" >&2
  exit 2
fi

if [ "$kind" = "available" ]; then
  if [ -n "$raw" ]; then
    echo "CR quota: CodeRabbit's summary banner reports $raw on $where."
  else
    echo "CR quota: CodeRabbit reports reviews are available now on $where."
  fi
  exit 0
fi

now="$(date -u +%s)"
remaining=$(( deadline - now ))

if [ "$remaining" -le 0 ]; then
  if [ "$unknown" = "true" ]; then
    echo "CR quota: the last announced limit on $where had NO usable countdown (\"$raw\") and is older than the $(( UNKNOWN_CEILING / 60 ))-minute assumed ceiling. No active limit."
  else
    echo "CR quota: the last announced limit on $where ($raw) has EXPIRED. No active limit."
  fi
  exit 0
fi

# Format an epoch as a US Pacific labeled time (house style). GNU/BSD dual-form.
fmt_pacific() {
  local epoch="$1"
  TZ="America/Los_Angeles" date -d "@$epoch" +'%H:%M %Z' 2>/dev/null \
    || TZ="America/Los_Angeles" date -r "$epoch" +'%H:%M %Z' 2>/dev/null \
    || echo "unknown"
}

# Round to the NEAREST minute rather than truncating: the deadline is computed from
# the comment's timestamp, so a plain truncation reports "49m" for what a reader just
# saw announced as 54 minutes 4 minutes ago. Sub-minute remainders show as seconds.
if [ "$remaining" -lt 60 ]; then
  left="${remaining}s"
else
  mins=$(( (remaining + 30) / 60 ))
  if [ "$mins" -ge 60 ]; then
    left="$(( mins / 60 ))h $(( mins % 60 ))m"
  else
    left="${mins}m"
  fi
fi

if [ "$unknown" = "true" ]; then
  echo "CR quota: CodeRabbit announced a limit on $where with NO usable countdown (\"$raw\"): LIMITED, deadline UNKNOWN (assumed ceiling ~${left} left, at $(fmt_pacific "$deadline"))."
  exit 1
fi

echo "CR quota: that ${noun} consumed your slot on $where -- CodeRabbit announced $raw; next ${noun} in ~${left} (at $(fmt_pacific "$deadline"))."
exit 1
