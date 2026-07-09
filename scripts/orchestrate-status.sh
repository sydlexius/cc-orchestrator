#!/usr/bin/env bash
# orchestrate-status.sh (#223) -- one compact line per in-flight PR, COMPOSED
# from existing digests, so the orchestrate lead reads ~one line per PR instead
# of a kilobyte of raw `gh pr list` JSON. Part of the TL context-minimization
# epic (#220); see skills/orchestrate/design/DESIGN-tl-context-minimization.md.
#
# Each line:
#   #<num> <state> checks:<GREEN|RED|PENDING|NONE> review:<decision|none> \
#     merge:<mergeStateStatus|?> unreplied:<N|?>  <title>
# composed from a SINGLE `gh pr view` (state / checks / review / merge) plus
# `pr-unreplied-comments.sh --count-only` (the unreplied-finding count).
#
# CHARTER -- READ-ONLY (invariant, do not weaken): this oracle issues ONLY
# `gh pr list` / `gh pr view` / `gh repo view` reads and the read-only
# `--count-only` helper. It NEVER mutates, and it is NEVER a reason to widen the
# `gh pr` allow-list beyond the read subcommands (`gh pr view:*` / `gh pr list:*`).
# Any future change that adds a mutation or a new allow-list entry here is out of
# charter. Fails soft per-PR (a bad view / helper degrades that one line), but
# fails LOUD (exit 2) when it cannot even determine the in-flight PR set.
#
# Usage:
#   orchestrate-status.sh [<pr>...] [<owner/repo>]
#     no PR args  -> report every OPEN PR (`gh pr list --state open`)
#     PR args     -> report exactly those PRs (skips the list call)
#     repo arg    -> owner/repo slug (default: the current repo via `gh repo view`)
set -euo pipefail

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; /^set -euo/d'; }

prs=()
repo=""
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    *[0-9]) [[ "$arg" =~ ^[0-9]+$ ]] && { prs+=("$arg"); continue; } ;;
  esac
  case "$arg" in
    */*) repo="$arg" ;;
    *) printf 'orchestrate-status: unrecognized argument %q\n' "$arg" >&2; exit 1 ;;
  esac
done

# Resolve the repo slug once (a read) if not given explicitly.
if [ -z "$repo" ]; then
  repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || echo "")
fi

# In-flight PR set: explicit args, else every open PR. Capture `gh pr list` into
# a variable first (NOT `mapfile < <(gh ...)`, whose exit status is mapfile's, not
# gh's) so a real list failure propagates and we can fail LOUD.
if [ "${#prs[@]}" -eq 0 ]; then
  repo_flag=(); [ -n "$repo" ] && repo_flag=(--repo "$repo")
  # Do NOT suppress gh's stderr on this loud-fail path: its diagnostic (auth /
  # missing repo context / rate limit) is the actual root cause, and swallowing it
  # behind the generic message below makes the exit-2 undebuggable. Let it bubble.
  if ! pr_list_out=$(gh pr list "${repo_flag[@]}" --state open --json number --jq '.[].number'); then
    echo "orchestrate-status: could not determine the in-flight PR set (gh pr list failed)" >&2
    exit 2
  fi
  [ -n "$pr_list_out" ] && mapfile -t prs <<<"$pr_list_out"
fi

if [ "${#prs[@]}" -eq 0 ]; then
  echo "No in-flight open PRs."
  exit 0
fi

helper="${HOME}/.claude/scripts/pr-unreplied-comments.sh"

# jq: reduce statusCheckRollup to a single checks verdict, emit a TSV row.
# A rollup element is a CheckRun (.conclusion) or a StatusContext (.state); take
# whichever is present. RED wins over PENDING wins over GREEN; empty -> NONE.
read -r -d '' JQ <<'JQEOF' || true
def cs:
  (.statusCheckRollup // []) as $r
  | if ($r | length) == 0 then "NONE"
    elif any($r[]; ((.conclusion // .state) // "" | ascii_upcase) as $u
                   | ($u == "FAILURE" or $u == "ERROR" or $u == "CANCELLED"
                      or $u == "TIMED_OUT" or $u == "ACTION_REQUIRED"
                      or $u == "STARTUP_FAILURE" or $u == "STALE")) then "RED"
    elif any($r[]; ((.conclusion // .state) // "" | ascii_upcase) as $u
                   | ($u != "SUCCESS" and $u != "NEUTRAL" and $u != "SKIPPED")) then "PENDING"
    else "GREEN" end;
[ (.state // "?"),
  cs,
  ((.reviewDecision // "none") | if . == "" then "none" else . end),
  (.mergeStateStatus // "?"),
  (.title // "") ] | @tsv
JQEOF

repo_flag=(); [ -n "$repo" ] && repo_flag=(--repo "$repo")

for n in "${prs[@]}"; do
  if ! row=$(gh pr view "$n" "${repo_flag[@]}" \
               --json state,title,mergeStateStatus,reviewDecision,statusCheckRollup \
               --jq "$JQ" 2>/dev/null); then
    echo "#$n ERROR (gh pr view failed)"
    continue
  fi
  IFS=$'\t' read -r state checks review merge title <<<"$row"

  # Unreplied count via the read-only helper; degrade to "?" on any failure.
  unrep="?"
  if [ -x "$helper" ]; then
    if c=$("$helper" --count-only "$n" "$repo" 2>/dev/null); then
      c=${c//[[:space:]]/}
      [[ "$c" =~ ^[0-9]+$ ]] && unrep="$c"
    fi
  fi

  printf '#%s %s checks:%s review:%s merge:%s unreplied:%s  %s\n' \
    "$n" "$state" "$checks" "$review" "$merge" "$unrep" "${title:0:60}"
done
