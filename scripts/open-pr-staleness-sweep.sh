#!/usr/bin/env bash
# open-pr-staleness-sweep.sh <merged-pr-number> [repo]   (issue #282)
#
# The MERGE-SIDE sweep: a merge advances a base branch, which silently leaves every OTHER open PR
# on that base BEHIND. This runs at /post-merge-cleanup, notices those PRs, and either refreshes
# them (only when that is provably safe) or SURFACES them to the lead. It is ADVISORY: it can never
# block cleanup.
#
# THE SAFETY HINGE (get this exactly right - the unsafe direction destroys work):
#   reviewed = (reviews[] non-empty) OR (review-thread/comment count > 0)
#     NOT `reviewDecision`: that field is NULL on a PR carrying review COMMENTS with no submitted
#     decision - i.e. exactly a PR carrying bot findings and cited fix SHAs, the one we must NOT
#     touch. So the predicate is built from the presence of review ACTIVITY, not a decision.
#   behind + NOT reviewed   -> refresh with a PLAIN `gh pr update-branch <n>`. DEFAULT merge-commit
#                              mode, which is ADDITIVE. `--rebase` is NEVER passed on ANY path: it
#                              rewrites every commit SHA, orphaning the fix SHAs cited in review
#                              replies and emptying the bot's incremental-review delta.
#   behind + REVIEWED       -> SURFACE only, as a one-line entry. NO mutation: a HEAD-moving commit
#                              dismisses a bot's prior approval and disturbs the review delta, so
#                              the lead (not this sweep) decides when to move that ref.
#   predicate INDETERMINATE (unreadable / absent / malformed field, or the read failed)
#                           -> treat as REVIEWED -> SURFACE. FAIL TOWARD SURFACE, NEVER TOWARD
#                              ACTING.
#   CROSS-REPOSITORY (fork) PR -> SKIP + SURFACE. NEVER measured, NEVER mutated. Its
#                              `headRefName` is a branch in the FORK, so `origin/<head>` is at best
#                              absent and at worst a COMPLETELY UNRELATED same-named origin branch
#                              (dev / patch-1 / fix) - measuring against it yields a meaningless
#                              behind-count that could route a fork PR into update-branch. An
#                              UNREADABLE `isCrossRepository` is treated AS cross-repo (fail toward
#                              not-acting, the same direction as the reviewed predicate).
#   HEAD FETCH FAILED       -> UNKNOWN -> SURFACE. Same rigor base-freshness.sh applies to the base:
#                              a stale local `origin/<head>` would OVER-state the behind count, so a
#                              failed fetch never yields a confident answer.
#   update-branch unavailable / unpermitted / errors -> degrade to REPORT-ONLY, print the behind
#                              list, continue. (The `Bash(gh pr update-branch *)` allow-list entry
#                              is the maintainer's to grant; until then this degradation is the
#                              normal, expected path - see templates/required-permissions.md.)
#
# The behind check REUSES scripts/base-freshness.sh (a sibling); the fetch + rev-list idiom is not
# reimplemented here. Each PR is measured against its OWN baseRefName, so a backport / release-base
# PR is correct by construction (never measured against main).
#
# FAIL-OPEN CONTRACT (a DELIBERATE divergence from stale-branch-sweep.sh, which fails CLOSED):
#   exit 0 on EVERY operational path, INCLUDING a read failure, so this can NEVER block
#   /post-merge-cleanup. exit 2 ONLY on a MALFORMED INVOCATION (unknown flag / non-numeric PR).
#   RATIONALE for the divergence: stale-branch-sweep fails closed because it DELETES branches - a
#   misread there destroys work. This sweep's worst unsafe act is a wrongly-refreshed PR, and that
#   is already gated by the fail-toward-SURFACE predicate above. So blocking cleanup on a read
#   failure buys no safety and costs the maintainer a wedged cleanup. Report, degrade, continue.
#
# Usage:
#   open-pr-staleness-sweep.sh <merged-pr-number> [repo]
#
# Arguments:
#   merged-pr-number  The PR that just merged. It is EXCLUDED from the sweep.
#   repo              owner/name slug (optional; resolved via `gh repo view` if omitted).
#
# Exit codes:
#   0  Always, on every operational path (swept / nothing to do / degraded to report-only /
#      read failure). Advisory by contract.
#   2  Malformed invocation only (unknown flag, missing or non-numeric PR number, extra argument).
set -uo pipefail   # deliberately NOT -e: a failed read must degrade, never abort (fail open).

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

# --- Parse arguments (a malformed invocation is the ONLY non-zero exit) ---
merged_pr=""
repo_arg=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --) shift; break ;;
    -*) echo "open-pr-staleness-sweep: unknown flag '$1'" >&2
        echo "usage: open-pr-staleness-sweep.sh <merged-pr-number> [repo]" >&2
        exit 2 ;;
    *)  if [ -z "$merged_pr" ]; then merged_pr="$1"; shift
        elif [ -z "$repo_arg" ]; then repo_arg="$1"; shift
        else echo "open-pr-staleness-sweep: unexpected extra argument '$1'" >&2; exit 2; fi ;;
  esac
done
if [ "$#" -gt 0 ]; then
  echo "open-pr-staleness-sweep: unexpected extra argument '$1'" >&2
  exit 2
fi
case "$merged_pr" in
  ''|*[!0-9]*)
    echo "open-pr-staleness-sweep: <merged-pr-number> must be a number (got '${merged_pr}')" >&2
    echo "usage: open-pr-staleness-sweep.sh <merged-pr-number> [repo]" >&2
    exit 2 ;;
esac

# --- Non-interactive git (fail fast, never hang) for this script's OWN head fetch ---
# base-freshness.sh sets its own; the sweep's head fetch below needs the SAME guarantee. Append
# `-o BatchMode=yes` LAST so it overrides any caller BatchMode=no (ssh honors the last -o).
export GIT_TERMINAL_PROMPT=0
if [ -n "${GIT_SSH_COMMAND:-}" ]; then
  GIT_SSH_COMMAND="$GIT_SSH_COMMAND -o BatchMode=yes"
else
  GIT_SSH_COMMAND="ssh -o BatchMode=yes"
fi
export GIT_SSH_COMMAND

# --- Locate the behind-check helper (sibling; repo layout AND deployed ~/.claude/scripts layout) ---
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
freshness="$script_dir/base-freshness.sh"
if [ ! -r "$freshness" ]; then
  echo "open-pr-staleness-sweep: base-freshness.sh not found next to this script ($freshness); skipping the staleness sweep (advisory)."
  exit 0
fi

# --- Resolve repo (fail OPEN: a failure skips the advisory sweep, it never blocks cleanup) ---
repo="${repo_arg:-${GITHUB_REPOSITORY:-}}"
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "open-pr-staleness-sweep: could not resolve the repo (pass [repo] or set GITHUB_REPOSITORY); skipping the staleness sweep (advisory)."
  exit 0
fi

# --- Read phase (READ-ONLY) ---
# NO SILENT CAP: `gh pr list` pages up to --limit. If the result COUNT equals the limit the set may
# be TRUNCATED, so the report must say so and must never claim a clean "nothing to do" (the sweep
# would then be silent about PRs it never even looked at).
list_limit=100
if ! open_prs="$(gh pr list --repo "$repo" --state open --limit "$list_limit" --json number --jq '.[].number' 2>/dev/null)"; then
  echo "open-pr-staleness-sweep: could not read the open PRs for $repo; skipping the staleness sweep (advisory, fail open)."
  exit 0
fi
listed_n=0
for _n in $open_prs; do listed_n=$((listed_n + 1)); done
truncated=false
[ "$listed_n" -ge "$list_limit" ] && truncated=true

refreshed=()      # behind + not reviewed -> update-branch succeeded
needs_lead=()     # behind + reviewed / indeterminate -> SURFACE only
degraded=()       # behind + not reviewed, but update-branch failed / unpermitted
unknowns=()       # freshness could not be determined
cross_repo=()     # fork PR -> never measured, never mutated

for n in $open_prs; do
  # NUMERIC exclusion of the just-merged PR (a string compare would let a zero-padded `007`
  # argument slip past gh's `7` - and the merged PR must NEVER be swept).
  case "$n" in
    ''|*[!0-9]*) ;;   # not a number (cannot happen via --jq .number); fall through and sweep it
    *) [ "$((10#$n))" -eq "$((10#$merged_pr))" ] && continue ;;
  esac

  # ONE snapshot per PR. The `?` sentinel marks a field that is absent / not the expected type
  # (malformed) - the INDETERMINATE shape, which routes to SURFACE.
  #
  # NOTE (verified against `gh pr view --json` on gh: the field list has NO `reviewThreads`):
  # the thread half of the predicate is read as `comments` - the strictly MORE CONSERVATIVE
  # stand-in (it counts issue-level bot summaries and human comments too, so it can only push a
  # PR toward SURFACE, never toward acting). Inline review threads always hang off a review, so
  # they are already covered by the `reviews[] non-empty` half.
  if ! snap="$(gh pr view "$n" --repo "$repo" \
        --json baseRefName,headRefName,reviews,comments,isCrossRepository \
        --jq '[ (if (.baseRefName|type)=="string" then .baseRefName else "?" end),
                (if (.headRefName|type)=="string" then .headRefName else "?" end),
                (if (.reviews|type)=="array" then (.reviews|length|tostring) else "?" end),
                (if (.comments|type)=="array" then (.comments|length|tostring) else "?" end),
                (if (.isCrossRepository|type)=="boolean" then (.isCrossRepository|tostring) else "?" end) ]
              | @tsv' 2>/dev/null)"; then
    snap=""
  fi
  if [ -z "$snap" ]; then
    needs_lead+=("#$n - could not read its review state; treated as REVIEWED (no action taken)")
    continue
  fi

  IFS=$'\t' read -r base_ref head_ref reviews_n threads_n cross <<<"$snap"
  base_ref="${base_ref:-?}"; head_ref="${head_ref:-?}"
  reviews_n="${reviews_n:-?}"; threads_n="${threads_n:-?}"; cross="${cross:-?}"

  # CROSS-REPOSITORY (fork) PR: skip BEFORE any measurement or mutation. Only an explicit
  # `false` clears it - `true`, `?` (absent/malformed) or anything else is treated as cross-repo.
  if [ "$cross" != "false" ]; then
    cross_repo+=("#$n - CROSS-REPOSITORY (fork) PR${cross:+ (isCrossRepository=$cross)}; not measurable against origin/<head>, never refreshed by the sweep")
    continue
  fi

  # REVIEWED PREDICATE. Default REVIEWED; only a BOTH-readable, BOTH-zero pair clears it.
  reviewed=true
  indeterminate=false
  case "$reviews_n" in ''|*[!0-9]*) indeterminate=true ;; esac
  case "$threads_n" in ''|*[!0-9]*) indeterminate=true ;; esac
  if [ "$indeterminate" = false ] && [ "$reviews_n" -eq 0 ] && [ "$threads_n" -eq 0 ]; then
    reviewed=false
  fi

  if [ "$base_ref" = "?" ] || [ "$head_ref" = "?" ]; then
    needs_lead+=("#$n - base/head ref unreadable; treated as REVIEWED (no action taken)")
    continue
  fi

  # Behind check, against THIS PR's OWN base (never an assumed main). The head must be FETCHED
  # first so origin/<head> is current; base-freshness fetches the base itself. A FAILED head fetch
  # is NOT tolerated into a measurement: a stale local origin/<head> OVER-states the behind count
  # (exactly what base-freshness refuses to do for the base), so it downgrades to UNKNOWN.
  if ! git fetch origin "$head_ref" --quiet >/dev/null 2>&1; then
    unknowns+=("#$n ($head_ref) - could not fetch origin/$head_ref (offline, auth-required, or no such branch); freshness NOT determined (no action taken)")
    continue
  fi
  fresh_out="$(bash "$freshness" "$base_ref" "origin/$head_ref" 2>/dev/null)"; fresh_rc=$?

  # HARDENED BEHIND GATING: a refresh is authorized ONLY when ALL THREE hold - the helper exited
  # EXACTLY 1, its output carries the explicit `freshness: behind` label, AND a numeric commit count
  # parsed out of it. ANY other status (0 fresh/unknown, 2 malformed, or a killed/incompatible
  # helper's odd exit code) or a non-numeric count routes to SURFACE (unknowns), NEVER to
  # update-branch. There is deliberately NO `?` fallback for behind_n - an unparseable count can
  # never reach the mutation.
  behind_n=""
  case "$fresh_out" in
    *"freshness: behind"*)
      behind_n="$(printf '%s' "$fresh_out" | sed -nE 's/.* is ([0-9]+) commit.*/\1/p' | head -1)" ;;
  esac
  if [ "$fresh_rc" -ne 1 ] || [ -z "$behind_n" ]; then
    case "$fresh_out" in
      *"freshness: fresh"*) : ;;   # up to date: nothing to do
      *) unknowns+=("#$n - freshness undetermined (${base_ref})") ;;
    esac
    continue
  fi

  if [ "$reviewed" = true ]; then
    if [ "$indeterminate" = true ]; then
      needs_lead+=("#$n ($head_ref) is $behind_n behind origin/$base_ref - review state INDETERMINATE, treated as REVIEWED (no action taken)")
    else
      needs_lead+=("#$n ($head_ref) is $behind_n behind origin/$base_ref - REVIEWED (reviews=$reviews_n, threads/comments=$threads_n); NOT refreshed automatically")
    fi
    continue
  fi

  # TOCTOU GUARD (#282): a review / comment can land BETWEEN the snapshot above and this mutation.
  # RE-READ the review state immediately before update-branch and proceed ONLY if it STILL shows
  # not-reviewed. An unreadable / indeterminate re-check is treated as REVIEWED -> SURFACE (fail
  # toward not-acting), the same direction as the snapshot-time predicate.
  if ! recheck="$(gh pr view "$n" --repo "$repo" --json reviews,comments \
        --jq '[ (if (.reviews|type)=="array" then (.reviews|length|tostring) else "?" end),
                (if (.comments|type)=="array" then (.comments|length|tostring) else "?" end) ]
              | @tsv' 2>/dev/null)"; then
    recheck=""
  fi
  re_reviewed=true
  if [ -n "$recheck" ]; then
    IFS=$'\t' read -r re_reviews_n re_comments_n <<<"$recheck"
    re_reviews_n="${re_reviews_n:-?}"; re_comments_n="${re_comments_n:-?}"
    re_indeterminate=false
    case "$re_reviews_n" in ''|*[!0-9]*) re_indeterminate=true ;; esac
    case "$re_comments_n" in ''|*[!0-9]*) re_indeterminate=true ;; esac
    if [ "$re_indeterminate" = false ] && [ "$re_reviews_n" -eq 0 ] && [ "$re_comments_n" -eq 0 ]; then
      re_reviewed=false
    fi
  fi
  if [ "$re_reviewed" = true ]; then
    needs_lead+=("#$n ($head_ref) is $behind_n behind origin/$base_ref - became REVIEWED (or its re-check was unreadable) between snapshot and refresh; NOT refreshed (no action taken)")
    continue
  fi

  # behind + NOT reviewed -> the one safe automatic action. DEFAULT merge-commit mode, additive.
  # NEVER --rebase.
  if gh pr update-branch "$n" --repo "$repo" >/dev/null 2>&1; then
    refreshed+=("#$n ($head_ref) was $behind_n behind origin/$base_ref - refreshed (merge-commit, additive)")
  else
    degraded+=("#$n ($head_ref) is $behind_n behind origin/$base_ref - update-branch unavailable/failed; REPORT-ONLY (refresh it yourself, or grant Bash(gh pr update-branch *))")
  fi
done

# --- Report phase (always advisory, always exit 0) ---
truncation_note="the open-PR list hit the --limit $list_limit cap ($listed_n returned), so it may be TRUNCATED: PRs beyond the cap were NOT examined"
total=$(( ${#refreshed[@]} + ${#needs_lead[@]} + ${#degraded[@]} + ${#unknowns[@]} + ${#cross_repo[@]} ))
if [ "$total" -eq 0 ]; then
  if [ "$truncated" = true ]; then
    # NEVER a clean "nothing to do" over a possibly-truncated set - that would be a lie.
    echo "open-pr-staleness-sweep: none of the $listed_n open PRs examined on $repo is behind its base, BUT $truncation_note."
  else
    echo "open-pr-staleness-sweep: no open PR on $repo is behind its base (nothing to do)."
  fi
  exit 0
fi

echo "open-pr-staleness-sweep: $repo (excluding the just-merged #$merged_pr) - advisory, never blocks:"
if [ "$truncated" = true ]; then
  echo "  NOTE: $truncation_note."
fi
if [ "${#refreshed[@]}" -gt 0 ]; then
  echo "  refreshed (behind + no review activity yet):"
  for line in "${refreshed[@]}"; do echo "    $line"; done
fi
if [ "${#needs_lead[@]}" -gt 0 ]; then
  echo "  NEEDS THE LEAD (behind + reviewed or indeterminate - a HEAD move would dismiss the review):"
  for line in "${needs_lead[@]}"; do echo "    $line"; done
fi
if [ "${#degraded[@]}" -gt 0 ]; then
  echo "  report-only (could not refresh):"
  for line in "${degraded[@]}"; do echo "    $line"; done
fi
if [ "${#unknowns[@]}" -gt 0 ]; then
  echo "  undetermined:"
  for line in "${unknowns[@]}"; do echo "    $line"; done
fi
if [ "${#cross_repo[@]}" -gt 0 ]; then
  echo "  skipped - cross-repository (fork) PRs, never measured or mutated:"
  for line in "${cross_repo[@]}"; do echo "    $line"; done
fi
exit 0
