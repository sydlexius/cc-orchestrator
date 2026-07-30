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
#
# Result: the throttle FEELS arbitrary when it has in fact been announced every time.
# This converts that invisible announcement into one visible terminal line.
#
# MATCHER (measured across 21 real instances, 2026-07-24..27, plus 2026-07-30):
#   available in (\d+) (second|minute|hour)s?\.
# The duration is always RELATIVE -- never a wall-clock time, never a timezone, never
# a date. A naive `(\d+) minutes` breaks on real messages: "1 minute." is SINGULAR and
# "4 seconds." is a DIFFERENT UNIT. `hour` and values above 59 are accepted even though
# nothing measured exceeded 59 minutes; it costs nothing and avoids a silent parse
# failure. Anything else is unrecognized -> report nothing, never guess.
#
# TRAP: the retired Codoki service used an ABSOLUTE UTC timestamp
# ("Next available slot: 2026-06-22 04:50:02 UTC"), and transcripts are full of them.
# Reading one as a relative duration would be badly wrong, so this matcher requires
# CR's exact "available in <N> <unit>." shape AND requires the CR bot login.
#
# NON-MONOTONIC BY DESIGN: CR's limits are adaptive, so a later reading can be LARGER
# than an earlier one (canticle #656 read 53 minutes, then 51 minutes an HOUR later).
# Never count down locally from an old reading. This script therefore takes the NEWEST
# signal outright and computes its deadline from that comment's own timestamp.
#
# Usage:
#   cr-quota-watch.sh <PR#> [owner/repo]
#
# Arguments:
#   PR#         PR number (required, numeric).
#   owner/repo  Repo slug (optional; resolved via `gh repo view` if omitted).
#
# Exit codes:
#   0  No ACTIVE limit -- no signal found, the newest signal's deadline has passed, or
#      CR reported reviews available. (Also the state a caller may act on.)
#   1  LIMITED -- the newest signal's deadline is still in the future; the remaining
#      time and the Pacific-labeled deadline are surfaced.
#   2  SETUP ERROR -- bad/missing args, repo unresolvable, or a gh read failure. A read
#      failure is NEVER reported as "no limit": that would be a false all-clear.
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
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "setup error: could not resolve repo (pass owner/repo, or run inside a gh-aware repo)" >&2
  exit 2
fi

CR_LOGIN='coderabbitai[bot]'

# --- Read issue comments (READ-ONLY, paginated) ---
# Capture gh's exit status BEFORE the jq pipe: `gh ... 2>/dev/null | jq -s 'add // []'`
# emits `[]` even when gh FAILS (network/auth/404), masking a read error as an empty
# comment set -- which here would read as a false "no limit" all-clear. So the GET is
# its own checked command, then piped.
raw="$(gh api --paginate "repos/$repo/issues/$pr/comments" 2>/dev/null)" || {
  echo "setup error: could not read issue comments for PR #$pr ($repo) (gh api read failed)" >&2
  exit 2
}
comments="$(printf '%s' "$raw" | jq -s 'add // []' 2>/dev/null || true)"
if [ -z "$comments" ]; then
  echo "setup error: could not parse issue comments for PR #$pr ($repo)" >&2
  exit 2
fi

# --- Select the NEWEST quota signal ---
# Emitted as tab-separated: kind, deadline epoch, the raw duration phrase, the noun
# phrase CR used. Timestamps are parsed with `try/catch` so one malformed comment
# cannot abort the whole read.
RX='next (?<inc>included )?review will be available in (?<n>[0-9]+) (?<unit>second|minute|hour)s?\.'

signal="$(printf '%s' "$comments" | jq -r --arg login "$CR_LOGIN" --arg rx "$RX" '
  [ .[]
    | select((.user.login // "") == $login)
    | (.body // "") as $b
    | ((try (.created_at | fromdateiso8601) catch null)) as $t
    | select($t != null)
    | if ($b | test($rx)) then
        ($b | capture($rx)) as $m
        | ($m.n | tonumber) as $n
        | (if $m.unit == "second" then 1 elif $m.unit == "minute" then 60 else 3600 end) as $mult
        | { kind: "limited",
            t: $t,
            deadline: ($t + ($n * $mult)),
            raw: "\($m.n) \($m.unit)\(if $n == 1 then "" else "s" end)",
            noun: (if ($m.inc // "") != "" then "included review" else "review" end) }
      elif ($b | test("Reviews are available now")) then
        { kind: "available", t: $t, deadline: $t, raw: "", noun: "review" }
      else empty end
  ]
  | sort_by(.t) | last
  | if . == null then "" else "\(.kind)\t\(.deadline)\t\(.raw)\t\(.noun)" end
' 2>/dev/null || true)"

if [ -z "$signal" ]; then
  echo "CR quota: no quota signal from CodeRabbit on PR #$pr ($repo). No announced limit."
  exit 0
fi

kind="$(printf '%s' "$signal" | cut -f1)"
deadline="$(printf '%s' "$signal" | cut -f2)"
raw="$(printf '%s' "$signal" | cut -f3)"
noun="$(printf '%s' "$signal" | cut -f4)"

if [ "$kind" = "available" ]; then
  echo "CR quota: CodeRabbit reports reviews are available now on PR #$pr ($repo)."
  exit 0
fi

now="$(date -u +%s)"
remaining=$(( deadline - now ))

if [ "$remaining" -le 0 ]; then
  echo "CR quota: the last announced limit on PR #$pr ($repo) ($raw) has EXPIRED. No active limit."
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

echo "CR quota: that ${noun} consumed your slot on PR #$pr ($repo) -- CodeRabbit announced $raw; next ${noun} in ~${left} (at $(fmt_pacific "$deadline"))."
exit 1
