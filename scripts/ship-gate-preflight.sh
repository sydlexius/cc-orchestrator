#!/usr/bin/env bash
# ship-gate-preflight.sh <pr> [owner/repo]
#
# DETERMINISTIC merge-readiness ORACLE (#110). The single source of truth for
# whether a PR may be presented as a ship-gate or declared MERGE-READY. It reads
# GitHub's statusCheckRollup (the same rollup the merge button respects) plus the
# authoritative unreplied-comments check, and FAILS CLOSED on every ambiguous or
# unknown state. There is NO "required vs non-required" distinction on purpose: a
# red or still-running check is a block. This exists because a lead once declared
# a PR "merge-ready" while a non-required check was red and rationalized it away
# (mxlrcgo-svc #261, 2026-06-15). The fix is a gate that cannot be reasoned
# around, not a promise.
#
# CONTRACT
#   Positional args:
#     <pr>          PR number (required).
#     [owner/repo]  Repo slug (optional; resolved from `gh repo view` if omitted).
#   Flags:
#     --codoki-only            Settlement mode: check ONLY whether the Codoki check
#                              has COMPLETED with an acceptable conclusion. A MISSING
#                              check BLOCKS (strict). Skips the full enumeration AND
#                              pr-unreplied-comments.sh. Used by codoki-quota-watch.sh
#                              (rate-limit recovery), which needs the strict contract.
#     --codoki-gate            LIVENESS variant for pr-watch.sh (#237): like
#                              --codoki-only, but Codoki-AUTO-REVIEW-OFF aware. A
#                              MISSING check is "satisfied" (exit 0) UNLESS Codoki is
#                              actually expected (a manual `@codoki` trigger comment is
#                              present) -> then it BLOCKs (exit 2) awaiting the check. So
#                              an untriggered PR with no Codoki check never wedges
#                              pr-watch (the auto-review-off default), while a genuinely
#                              triggered one still waits. Present-but-unsettled/failed
#                              still BLOCKs. Fails toward SATISFIED on lookup error
#                              (liveness, not the merge gate).
#     --codoki-pattern <name>  Override the Codoki check name to match in
#                              --codoki-only mode (default: "Codoki PR Review").
#     --diagnose | --why       READ-ONLY diagnosis mode (#275): on a non-mergeable
#                              PR, reconcile branches/<base>/protection against the
#                              PR's live check/review/thread/ack state and emit one
#                              `REASON:` line per unmet rule (unresolved
#                              conversation, missing/failing required check,
#                              REVIEW_REQUIRED/CHANGES_REQUESTED, behind-base,
#                              Codoki ack, draft, conflicts). A DIAGNOSTIC, not the
#                              merge gate: emits NO head SHA, relaxes nothing. Exit 0
#                              = no blocking rule; exit 2 = blocked (reasons printed)
#                              or fail-closed (PR state unreadable). Protection read
#                              is best-effort (needs admin scope; degrades to a NOTE).
#   Exit codes:
#     0  PASS  - all gates green (full mode: every check terminal+acceptable AND
#               0 actionable review-body findings AND reviewDecision not an active
#               CHANGES_REQUESTED AND the Codoki root-summary ack is met-or-absent
#               (#234); codoki-only: Codoki settled OK).
#     1  USAGE - bad/missing arguments.
#     2  BLOCK - a gate failed, OR any lookup/parse error (gh/jq/helper failure).
#               BLOCK is the fail-closed code: an unknown state is ALWAYS a block.
#
# CHECK ACCEPTANCE (statusCheckRollup contexts, both rollup shapes):
#   CheckRun     (__typename == "CheckRun"):     PASS requires status == COMPLETED
#                AND conclusion in {SUCCESS, NEUTRAL, SKIPPED}. Anything else
#                (IN_PROGRESS, QUEUED, PENDING, FAILURE, CANCELLED, TIMED_OUT,
#                ACTION_REQUIRED, STARTUP_FAILURE, STALE, null/empty, or any
#                status != COMPLETED) BLOCKS.
#   StatusContext(__typename == "StatusContext"): PASS requires state == SUCCESS
#                ONLY. Any other state (PENDING, EXPECTED, ERROR, FAILURE) BLOCKS.
#   EMPTY rollup (no checks at all): BLOCKS in full mode. A PR with no checks is
#                NOT "verified" - we do not vacuously pass an unverified PR.
#
# FULL-MODE REVIEW GATE (default; not in --codoki-only):
#   After every check passes, run `~/.claude/scripts/pr-unreplied-comments.sh
#   <pr> <repo>` and parse its `Review-body comments with actionable findings: N`
#   line. N>0 BLOCKS. A missing/erroring helper BLOCKS (fail closed). The line is
#   only PRINTED when N>0, so its ABSENCE on a clean (exit-0) helper run means
#   N==0 and PASSES. The full gate = all checks green AND N==0.
#
# FULL-MODE REVIEW-DECISION GATE (#117; couples GitHub's `reviewDecision` with the
#   actionable-findings count above - it is NOT an independent veto):
#   GitHub's reviewDecision is one of APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED
#   / null. It is deliberately NOT a standalone block because it reads STALE: after
#   a fix-push that the reviewer (e.g. CodeRabbit) has not yet re-reviewed it stays
#   CHANGES_REQUESTED even though every finding is addressed. Blocking on it alone
#   would wrongly fail an already-fixed PR. The two signals are coupled by ORDERING:
#   the findings>0 gate runs FIRST and blocks unconditionally, so an ACTIVE
#   CHANGES_REQUESTED - which by definition still carries unaddressed actionable
#   findings (N>0) - is already caught there. Any CHANGES_REQUESTED that survives to
#   the review-decision gate therefore has N==0: it is SUPERSEDED/resolved and
#   PASSES. Net effect: an active CHANGES_REQUESTED is caught (via its findings); a
#   superseded one is never blocked. The review-decision gate's own block is the
#   FAIL-CLOSED case only: an UNRECOGNIZED reviewDecision value (not in the
#   APPROVED/CHANGES_REQUESTED/REVIEW_REQUIRED/null set) is ambiguous and BLOCKS,
#   mirroring the unknown-__typename posture above. (Documented trade-off of the
#   coupling: a CHANGES_REQUESTED with NO helper-recognized actionable findings -
#   e.g. a bare human "request changes" with no inline findings - reads as N==0 and
#   PASSES; the authoritative unreplied-comments check is the backstop there.)
#
# FOLLOW-UP (required; the oracle alone does NOT close the dogfood gap mxlrcgo-svc
#   #261): the EXTERNAL callers must INVOKE this oracle. (a) pr-watch.sh should
#   read the `Codoki PR Review` check via `--codoki-only` off statusCheckRollup
#   instead of the reviews API (and only block on CR when CR was actually
#   triggered, per #34). (b) /merge-pr Step 1 should call this oracle as its
#   merge-readiness gate. Those callers are user-level today; wiring them is a
#   tracked follow-up that becomes in-repo once #30 PR B brings the
#   commands+helpers into the plugin. "Oracle created" is NOT "gap closed".
set -euo pipefail

# Acceptable CheckRun conclusions (terminal + green-enough).
ACCEPTABLE_CONCLUSIONS='["SUCCESS","NEUTRAL","SKIPPED"]'

codoki_only=false
codoki_gate=false
diagnose=false
codoki_pattern="Codoki PR Review"
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --codoki-only) codoki_only=true; shift ;;
    --codoki-gate) codoki_gate=true; shift ;;
    --diagnose|--why) diagnose=true; shift ;;
    --codoki-pattern)
      if [ "$#" -lt 2 ]; then
        echo "usage: ship-gate-preflight.sh: --codoki-pattern requires a value" >&2
        exit 1
      fi
      codoki_pattern="$2"; shift 2 ;;
    --) shift; while [ "$#" -gt 0 ]; do args+=("$1"); shift; done ;;
    -*) echo "usage: ship-gate-preflight.sh: unknown flag '$1'" >&2; exit 1 ;;
    *) args+=("$1"); shift ;;
  esac
done

# The three modes are MUTUALLY EXCLUSIVE (strict settle check vs the liveness gate vs
# the read-only diagnosis are distinct contracts). Specifying more than one is ambiguous
# - e.g. --codoki-only + --codoki-gate would silently switch a strict check into liveness
# behavior - so reject it with a usage error (Copilot review on #277).
mode_count=0
[ "$codoki_only" = true ] && mode_count=$((mode_count + 1))
[ "$codoki_gate" = true ] && mode_count=$((mode_count + 1))
[ "$diagnose" = true ] && mode_count=$((mode_count + 1))
if [ "$mode_count" -gt 1 ]; then
  echo "usage: ship-gate-preflight.sh: --codoki-only, --codoki-gate, and --diagnose are mutually exclusive" >&2
  exit 1
fi

pr="${args[0]:-}"
repo="${args[1]:-}"
if [ -z "$pr" ]; then
  echo "usage: ship-gate-preflight.sh <pr> [owner/repo] [--codoki-only|--codoki-gate] [--codoki-pattern <name>] [--diagnose|--why]" >&2
  exit 1
fi

if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)" || {
    echo "BLOCK: could not resolve repo (pass owner/repo explicitly)" >&2
    exit 2
  }
  if [ -z "$repo" ]; then
    echo "BLOCK: could not resolve repo (pass owner/repo explicitly)" >&2
    exit 2
  fi
fi

# --- DIAGNOSE MODE (#275) --------------------------------------------------
# READ-ONLY diagnosis of WHY a PR is not mergeable. GitHub's mergeStateStatus is a
# single opaque token (BLOCKED / BEHIND / DIRTY / ...) that never names WHICH branch-
# protection rule is unmet, so the lead has to hand-reconcile branches/<base>/
# protection against the PR's live state (canticle #460 was misreported as "needs
# admin-merge" when it was blocked SOLELY by an unresolved conversation). This mode
# reconciles them and emits one `REASON:` line per unmet rule. It is a DIAGNOSTIC,
# NOT the merge gate: it emits no --match-head-commit SHA and nothing here relaxes
# the floor or touches the allow-list. Exit 0 = no blocking rule detected; exit 2 =
# blocked (reasons printed) OR fail-closed (core PR state unreadable). The branch-
# protection read is BEST-EFFORT (it needs admin scope): a 403/absent read degrades
# to a NOTE + PR-state-only reasons, so a low-privilege token still gets a diagnosis.
if [ "$diagnose" = true ]; then
  dj="$(gh pr view "$pr" --repo "$repo" --json state,mergeStateStatus,mergeable,reviewDecision,baseRefName,headRefName,isDraft,statusCheckRollup 2>/dev/null)" || {
    echo "BLOCK: 'gh pr view' failed for #$pr in $repo (cannot diagnose)." >&2
    echo "RESULT: BLOCKED -- PR state unreadable. [#$pr $repo]" >&2
    exit 2
  }
  if ! jq -e . >/dev/null 2>&1 <<<"$dj"; then
    echo "BLOCK: malformed JSON from 'gh pr view' for #$pr (cannot diagnose)." >&2
    echo "RESULT: BLOCKED -- PR state unreadable. [#$pr $repo]" >&2
    exit 2
  fi
  pr_state="$(jq -r '(.state // "") | ascii_upcase' <<<"$dj")"
  # A merged/closed PR is not "blocked" - short-circuit before diagnosing rules.
  case "$pr_state" in
    MERGED) echo "MERGED: #$pr is already merged (nothing to diagnose). [$repo]"; exit 0 ;;
    CLOSED) echo "CLOSED: #$pr is closed and not merged (nothing to diagnose). [$repo]"; exit 0 ;;
  esac
  mss="$(jq -r '(.mergeStateStatus // "") | ascii_upcase' <<<"$dj")"
  mergeable="$(jq -r '(.mergeable // "") | ascii_upcase' <<<"$dj")"
  rdec="$(jq -r '(.reviewDecision // "") | ascii_upcase' <<<"$dj")"
  base="$(jq -r '.baseRefName // ""' <<<"$dj")"
  is_draft="$(jq -r '(.isDraft // false)' <<<"$dj")"
  echo "DIAGNOSE #$pr $repo: mergeStateStatus=${mss:-<none>} mergeable=${mergeable:-<none>} reviewDecision=${rdec:-<none>} base=${base:-<none>}"
  reasons=0

  # Branch protection (best-effort; the endpoint needs admin scope).
  prot='{}'; prot_readable=false
  if [ -n "$base" ] && p="$(gh api "repos/$repo/branches/$base/protection" 2>/dev/null)" && jq -e . >/dev/null 2>&1 <<<"$p"; then
    prot="$p"; prot_readable=true
  else
    echo "NOTE: branch protection for '${base:-?}' not readable (needs admin scope, or the branch is unprotected); diagnosing from PR state only."
  fi

  # (1) Draft.
  if [ "$is_draft" = true ]; then
    echo "REASON: PR is a DRAFT (mark ready-for-review to merge)."; reasons=$((reasons+1))
  fi

  # (2) Unresolved review conversations (GraphQL isResolved; the REST API cannot see it).
  owner="${repo%%/*}"; name="${repo##*/}"; unresolved="?"
  # shellcheck disable=SC2016  # GraphQL $owner/$name/$number are query variables, not shell expansions.
  if tj="$(gh api graphql -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){totalCount nodes{isResolved}}}}}' -F owner="$owner" -F name="$name" -F number="$pr" 2>/dev/null)"; then
    unresolved="$(jq -r 'try ([.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved != true)] | length) catch "?"' <<<"$tj")"
    # Truncation guard (mirrors the FULL-mode thread gate): only 100 threads are
    # fetched, so on totalCount>fetched the unresolved count is a LOWER BOUND - say so
    # rather than let a >100-thread PR read as fewer/zero unresolved conversations.
    rt_total="$(jq -r 'try (.data.repository.pullRequest.reviewThreads.totalCount) catch "null"' <<<"$tj")"
    rt_nodes="$(jq -r 'try (.data.repository.pullRequest.reviewThreads.nodes | length) catch "null"' <<<"$tj")"
    if [ "$rt_total" != "null" ] && [ "$rt_nodes" != "null" ] && [ "$rt_total" -gt "$rt_nodes" ] 2>/dev/null; then
      echo "NOTE: only ${rt_nodes} of ${rt_total} review threads fetched (first:100); the unresolved-conversation count is a lower bound."
    fi
  fi
  crr="$(jq -r '(.required_conversation_resolution.enabled // false)' <<<"$prot")"
  if [ "$unresolved" != "?" ] && [ "$unresolved" -gt 0 ] 2>/dev/null && { [ "$crr" = true ] || [ "$prot_readable" = false ]; }; then
    extra=""; [ "$crr" = true ] && extra=" (require_conversation_resolution)"
    echo "REASON: unresolved review conversation(s): ${unresolved}${extra}."; reasons=$((reasons+1))
  fi

  # (3) Required status checks unsatisfied (missing / not-success). Only when
  # protection is readable - the required set is unknowable otherwise.
  if [ "$prot_readable" = true ]; then
    req_ctx="$(jq -r '[ (.required_status_checks.contexts // [])[], (.required_status_checks.checks[]?.context) ] | map(select(. != null)) | unique | .[]' <<<"$prot" 2>/dev/null || true)"
    while IFS= read -r ctx; do
      [ -z "$ctx" ] && continue
      v="$(jq -r --arg c "$ctx" --argjson ok '["SUCCESS","NEUTRAL","SKIPPED"]' '
        [ (.statusCheckRollup // [])[] | select((.name // .context // "") == $c)
          | (.conclusion // .state // .status // "") | ascii_upcase ] as $states
        | if ($states | length) == 0 then "MISSING"
          elif ($states | map(select(. as $s | $ok | index($s))) | length) == ($states | length) then "OK"
          else "BAD:" + ($states | join(",")) end' <<<"$dj")"
      case "$v" in
        OK) : ;;
        MISSING) echo "REASON: required check '$ctx' is MISSING (never reported)."; reasons=$((reasons+1)) ;;
        BAD:*) echo "REASON: required check '$ctx' is ${v#BAD:} (not success)."; reasons=$((reasons+1)) ;;
      esac
    done <<<"$req_ctx"
  fi

  # (4) Review requirement.
  case "$rdec" in
    REVIEW_REQUIRED) echo "REASON: review required -- reviewDecision=REVIEW_REQUIRED (approving review(s) missing)."; reasons=$((reasons+1)) ;;
    CHANGES_REQUESTED) echo "REASON: changes requested -- reviewDecision=CHANGES_REQUESTED (a reviewer is blocking)."; reasons=$((reasons+1)) ;;
  esac

  # (5) Behind base / merge conflicts.
  if [ "$mss" = "BEHIND" ]; then
    strict=""; [ "$(jq -r '(.required_status_checks.strict // false)' <<<"$prot")" = true ] && strict=" (strict status checks require up-to-date)"
    echo "REASON: branch is BEHIND base '$base'${strict} -- update-branch / rebase."; reasons=$((reasons+1))
  fi
  if [ "$mergeable" = "CONFLICTING" ] || [ "$mss" = "DIRTY" ]; then
    echo "REASON: merge conflicts with base (mergeable=${mergeable}) -- rebase/resolve."; reasons=$((reasons+1))
  fi

  # (6) Codoki root-summary ack (reuse the canonical reader).
  reactor="${HOME}/.claude/scripts/gh-react.sh"
  if [ -x "$reactor" ]; then
    ack_rc=0; ack_out="$("$reactor" codoki-ack "$pr" "$repo" 2>/dev/null)" || ack_rc=$?
    av="$(printf '%s\n' "$ack_out" | sed -n 's/.*CODOKI-ACK:[[:space:]]*\([A-Za-z-]*\).*/\1/p' | tail -n1)"
    if [ "$av" = "unacked" ]; then
      echo "REASON: Codoki root-summary ack is UNMET (react via gh-react.sh codoki-ack $pr --react +1|-1; a 👎 also needs an @codoki reply)."; reasons=$((reasons+1))
    elif [ "$ack_rc" -ne 0 ] || [ -z "$av" ]; then
      # Honest about what the diagnostic could NOT check (the ack reader failed),
      # rather than silently emitting no signal. Advisory NOTE, not a counted REASON.
      echo "NOTE: Codoki root-summary ack state could not be verified (gh-react.sh exit ${ack_rc}); check it manually if the PR is Codoki-reviewed."
    fi
  fi

  # Catch-all: a blocked-ish state we could not attribute to a specific rule above.
  if [ "$reasons" -eq 0 ]; then
    case "$mss" in
      CLEAN|HAS_HOOKS|UNSTABLE|"")
        echo "MERGEABLE: no blocking branch-protection rule detected (mergeStateStatus=${mss:-<none>})."
        exit 0 ;;
      UNKNOWN)
        # GitHub has not finished computing the merge state (transient). Do NOT
        # fabricate a rule; report indeterminate and fail-closed so a caller re-runs.
        echo "INDETERMINATE: mergeStateStatus=UNKNOWN (GitHub still computing the merge state); re-run in a few seconds." >&2
        echo "RESULT: INDETERMINATE -- merge state not yet computed. [#$pr $repo]" >&2
        exit 2 ;;
      *)
        enabled="$(jq -r '[ (if (.required_conversation_resolution.enabled // false) then "conversation-resolution" else empty end),
                            (if (.required_linear_history.enabled // false) then "linear-history" else empty end),
                            (if (.required_signatures.enabled // false) then "signed-commits" else empty end),
                            (if (.required_status_checks.strict // false) then "strict-checks" else empty end) ] | join(", ")' <<<"$prot" 2>/dev/null || echo "")"
        echo "REASON: blocked by an unmet rule not individually diagnosed (mergeStateStatus=${mss}); enabled protections: ${enabled:-unknown}."
        reasons=$((reasons+1)) ;;
    esac
  fi

  echo "RESULT: BLOCKED -- ${reasons} unmet rule(s). [#$pr $repo]"
  exit 2
fi

# FAIL CLOSED: any gh failure -> BLOCK. headRefOid is fetched in the SAME snapshot
# as the checks (#263 Piece A) so the SHA the oracle emits on PASS is exactly the
# commit whose statusCheckRollup it validated - no second read to drift off.
# mergeStateStatus joins the SAME snapshot (#334). It was previously fetched only in
# DIAGNOSE mode - a WHY-is-it-blocked explainer invoked AFTER the fact - so the GATING
# path never read it and could return PASS on a PR GitHub was actively BLOCKING. Measured:
# a PASS emitted against mergeStateStatus=BLOCKED, where an unsigned commit had tripped a
# required_signatures rule this oracle does not evaluate rule-by-rule. For a check whose
# entire contract is fail-CLOSED, a false PASS is the worst defect it can have.
#
# THE POINT IS NOT TO ENUMERATE MORE RULES. The old DIAGNOSE jq selected a hand-picked set
# of rule types and silently skipped the rest, which is how required_signatures became
# invisible; adding a fourth type would leave a fifth. Instead ASK GITHUB FOR ITS VERDICT -
# mergeStateStatus already aggregates every rule, including ones this script has never
# heard of. Evaluate the aggregate, do not re-derive it.
json="$(gh pr view "$pr" --repo "$repo" --json statusCheckRollup,reviewDecision,headRefOid,mergeStateStatus,baseRefName 2>/dev/null)" || {
  echo "BLOCK: 'gh pr view' failed for #$pr in $repo" >&2
  exit 2
}

# FAIL CLOSED: malformed JSON -> BLOCK. Validate before any field access.
if ! jq -e . >/dev/null 2>&1 <<<"$json"; then
  echo "BLOCK: malformed JSON from 'gh pr view' for #$pr in $repo" >&2
  exit 2
fi

# codoki_review_triggered -- true (exit 0) when there is POSITIVE evidence Codoki
# WILL review this PR even though its check has not posted yet: a NON-Codoki author
# posted an `@codoki` mention (the manual review request, now that org auto-review is
# OFF). Excludes Codoki's OWN comments (its summaries can quote `@codoki`), mirroring
# cr_was_triggered in pr-watch.sh (#173). FAILS toward NOT-triggered (returns 1) on any
# gh/jq error: this backs a LIVENESS gate (--codoki-gate), so an ambiguous state must
# NOT wedge pr-watch waiting for a review that may never come (the merge gate is the
# fail-closed backstop, not this). Trigger-syntax assumption: a bare `@codoki` mention
# counts; tune the regex if Codoki adopts an explicit subcommand.
codoki_review_triggered() {
  gh api --paginate "repos/$repo/issues/$pr/comments" 2>/dev/null \
    | jq -s 'add // []' 2>/dev/null \
    | jq -e '[ .[]
        | select(((.user.login // "") != "codoki-pr-intelligence[bot]")
                 and ((.body // "") | test("@codoki(?![a-z0-9._-])"; "i"))) ]
             | length > 0' >/dev/null 2>&1
}

if [ "$codoki_only" = true ] || [ "$codoki_gate" = true ]; then
  # Settlement verdict shared by both modes: is the `Codoki PR Review` check present
  # and COMPLETED with an acceptable conclusion? -> OK / MISSING / BAD:<detail>.
  # (StatusContext-shaped Codoki entries are accepted via the same name/state mapping.)
  verdict="$(jq -r --arg pat "$codoki_pattern" --argjson ok "$ACCEPTABLE_CONCLUSIONS" '
    [ (.statusCheckRollup // [])[]
      | { name: (.name // .context // "unknown"),
          known: ((.__typename // "") | (. == "CheckRun" or . == "StatusContext")),
          status: (.status // "COMPLETED" | ascii_upcase),
          result: ((.conclusion // .state // "") | ascii_upcase) }
      | select(.name == $pat) ] as $matches
    | if ($matches | length) == 0 then "MISSING"
      elif ($matches | map(select(.known and .status == "COMPLETED" and (.result as $r | $ok | index($r)))) | length) == ($matches | length)
        then "OK"
      else "BAD:" + ($matches | map("\(.name) known=\(.known) status=\(.status) result=\(.result)") | join("; "))
      end
  ' <<<"$json")" || {
    echo "BLOCK: jq parse of statusCheckRollup failed (#$pr)" >&2
    exit 2
  }

  # --codoki-gate (#237): Codoki-AUTO-REVIEW-OFF-aware MERGE-READINESS gate. A MISSING
  # Codoki check is a reason to WAIT only when Codoki is actually EXPECTED (a manual
  # @codoki trigger is present); with auto-review OFF and no trigger, Codoki is NOT
  # expected, the requirement is SATISFIED, and pr-watch must not hang on a check that
  # will never post. --codoki-only keeps its STRICT "is it settled" contract unchanged
  # (codoki-quota-watch.sh depends on it). This is a deliberate LIVENESS inversion of
  # the fail-closed FULL-mode posture; the FULL-mode merge gate stays the backstop.
  if [ "$codoki_gate" = true ] && [ "$verdict" = "MISSING" ]; then
    if codoki_review_triggered; then
      verdict="EXPECTED"      # @codoki triggered; check not posted yet -> wait
    else
      verdict="NOT-EXPECTED"  # auto-review off + untriggered -> satisfied, do not wait
    fi
  fi

  case "$verdict" in
    OK)
      echo "RESULT: PASS -- Codoki check '$codoki_pattern' settled (COMPLETED, acceptable conclusion). [#$pr $repo]"
      exit 0 ;;
    NOT-EXPECTED)
      echo "RESULT: PASS -- Codoki not expected on #$pr (auto-review off, no @codoki trigger, no check present); requirement satisfied. [#$pr $repo]"
      exit 0 ;;
    EXPECTED)
      echo "BLOCK: Codoki review triggered (@codoki) on #$pr but its check has not posted yet. Not settled." >&2
      echo "RESULT: BLOCK -- Codoki triggered, awaiting its check. [#$pr $repo]" >&2
      exit 2 ;;
    MISSING)
      echo "BLOCK: Codoki check '$codoki_pattern' not found in statusCheckRollup (#$pr). Not settled." >&2
      echo "RESULT: BLOCK -- Codoki not settled. [#$pr $repo]" >&2
      exit 2 ;;
    *)
      echo "BLOCK: Codoki check not settled: ${verdict#BAD:} (#$pr)" >&2
      echo "RESULT: BLOCK -- Codoki not settled. [#$pr $repo]" >&2
      exit 2 ;;
  esac
fi

# --- FULL MODE -------------------------------------------------------------

# (#263 Piece A) The validated head SHA, emitted on PASS so the downstream
# authorize-merge step can pin --match-head-commit to exactly the commit whose
# gates this run cleared. Parsed here (full mode only; --codoki-only never needs
# it). ascii_downcase normalizes; empty/null/absent is caught at the PASS gate
# below (a PASS with no pinnable SHA is useless), not here.
head_sha="$(jq -r '(.headRefOid // "") | ascii_downcase' <<<"$json")" || {
  echo "BLOCK: jq parse of headRefOid failed (#$pr)" >&2
  exit 2
}

# Count of checks (empty rollup -> 0 -> BLOCK below).
count="$(jq -r '(.statusCheckRollup // []) | length' <<<"$json")" || {
  echo "BLOCK: jq parse of statusCheckRollup failed (#$pr)" >&2
  exit 2
}
if [ "$count" -eq 0 ]; then
  echo "BLOCK: no checks on #$pr (empty statusCheckRollup). An unverified PR is not merge-ready." >&2
  echo "RESULT: BLOCK -- no checks. [#$pr $repo]" >&2
  exit 2
fi

# --- EXPECTED-SET RECONCILIATION (#375) ------------------------------------
# The guard above is a COUNT, not a coverage check: one green irrelevant check
# satisfies it, and everything below is a purely NEGATIVE test hunting for entries
# that are BAD. A check that never ran is not bad, it is ABSENT -- so on a PR based
# off a non-default branch the oracle passed while Build, Test, Lint and both CodeQL
# analyses had never been scheduled. Measured live on stillwater#3021: 1 of 18
# required contexts present, and mergeStateStatus=CLEAN agreeing, because the ruleset
# targets ~DEFAULT_BRANCH and off that branch there is no rule to violate.
#
# That is why #334's fix does not cover this. It adopted GitHub's aggregate verdict
# precisely to stop hand-picking rule subsets, and the aggregate is genuinely clean
# here. Silence is not consent.
#
# Design of record: skills/orchestrate/design/DESIGN-expected-check-set.md (Option C).
# Deliberately reconciles the required-CONTEXT set only and leaves every other rule
# type to GitHub's aggregate -- enumerating rule types is the #334 defect class.
base_ref="$(jq -r '.baseRefName // ""' <<<"$json")" || base_ref=""
# NOT guarded into a silent skip. An earlier draft treated an unreadable default
# branch as "no fallback available" and fell through to a PASS emitting nothing --
# byte-identical to a PR that legitimately had no expected set. A single transient
# `gh repo view` failure therefore restored the exact #3021 false PASS, silently, on
# the oracle that arms the floor merge-auth token. The emptiness is now a BLOCK at
# the one place it can matter (below), so all three unreadable inputs -- base ref,
# base rules, default branch -- fail the same way.
# CAPTURE, THEN DECIDE -- never `|| echo ""`. `gh --jq` writes its ERROR BODY to
# STDOUT and exits nonzero (the same contract the legacy leg below documents at
# length), so the substitution did NOT yield an empty string on failure: it yielded
# the payload. The `[ -z ]` guard was therefore skipped, the payload was used as a
# BRANCH NAME, `rules/branches/<error text>` answered `[]`, and the gate PASSed --
# the very fail-open the guard below was written to close, still reachable on the
# transport failure it names. Reject anything that is not a plausible single ref:
# a `null` (which --jq prints for an absent field), whitespace, or JSON punctuation.
if default_branch="$(gh repo view "$repo" --json defaultBranchRef --jq .defaultBranchRef.name 2>/dev/null)"; then
  case "$default_branch" in
    null|*[[:space:]]*|*'{'*|*'}'*|*'"'*) default_branch="" ;;
  esac
else
  default_branch=""
fi

# Required contexts for a ref, from `rules/branches/<ref>`. That endpoint is the
# authority AND needs no admin scope -- branches/<ref>/protection 404s without it,
# which would make the common case indistinguishable from "unreadable".
# Echoes one context per line; returns 1 when the ref cannot be read at all.
_required_contexts_for() {
  local _ref="$1" _doc _rules _legacy _union
  _doc="$(gh api "repos/$repo/rules/branches/$_ref" 2>/dev/null)" || return 1
  # An EMPTY body is not malformed and must not read as unreadable: `jq -e .` exits 4 on
  # empty input, which would turn a benign empty response into a BLOCK on every merge.
  # Normalize it to the empty rule array it means, then insist the rest parses.
  [ -n "$_doc" ] || _doc='[]'
  # TYPE, not merely parseability. `jq -e .` proves only that the body IS JSON, never
  # that it is the ARRAY OF RULES this endpoint contracts -- and the extraction below
  # ran as a plain assignment with `2>/dev/null`, so its status was DISCARDED and
  # pipefail never saw it. Any jq runtime error therefore yielded an EMPTY set, which
  # routes to "this ref requires nothing" and PASSes. Measured false PASSes: an
  # enveloped `{"rules":[...]}` (the shape a paginated response would take, `.[]` over
  # an object exits 5), one non-object element poisoning the array, and
  # `{"message":"Not Found"}` served with HTTP 200 -- all with a genuinely missing
  # required check. This is the same defect class as the two fixed below, at the one
  # call site the fix did not reach: an unreadable set must never render as an absent one.
  # EVERY element must be an object, not just the document an array. Tolerating a junk
  # element and extracting from the rest is the wrong direction here: the junk could BE
  # the malformed required_status_checks rule, and skipping it would drop a required
  # context while reporting a clean read -- a false PASS dressed as resilience.
  jq -e 'type == "array" and all(type == "object")' >/dev/null 2>&1 <<<"$_doc" || return 1
  # As a JSON ARRAY, not lines: the union below is validated ONCE, and a value cannot be
  # validated after it has been flattened into a newline-delimited stream (which is the
  # very representation the newline defect exploits).
  _rules="$(jq -c '[.[] | select(.type == "required_status_checks")
          | .parameters.required_status_checks[]?.context]' <<<"$_doc" 2>/dev/null)" || return 1

  # UNION with legacy branch protection. The two authorities are evaluated together by
  # GitHub and they DISAGREE on this repo -- rulesets require 4 contexts, legacy
  # requires 1 -- so reading either alone under-reports. Legacy happens to be a strict
  # SUBSET today, which makes the union a no-op and is exactly why shipping without it
  # would have looked correct until the first legacy-only required context appeared.
  #
  # BEST-EFFORT, deliberately asymmetric to the rules read above: this endpoint needs
  # admin scope and 404s with "Branch not protected" wherever legacy protection simply
  # is not configured (measured: 200 on cc-orchestrator, 404 on stillwater). Treating
  # that absence as unreadable would block every merge on a rulesets-only repo, so a
  # failure here contributes nothing rather than failing the gate. The union can only
  # ADD contexts, so degrading it errs toward the pre-existing behavior, never toward
  # passing something the rules endpoint already requires.
  # DISCARD STDOUT ON A NONZERO EXIT. `--jq` does NOT suppress gh's error body: on a
  # 404 it writes the raw error JSON to STDOUT and exits 1. A bare `|| true` swallows
  # the status while KEEPING that payload, so
  #   {"message":"Branch not protected",...}
  # was unioned in as a required CONTEXT NAME and every PR on a repo without legacy
  # protection blocked on a fabricated check that can never appear in a rollup.
  # Measured live on stillwater before this fix. Capture first, then decide.
  # FETCH RAW, THEN PARSE SEPARATELY. `gh --jq` exits 1 for TWO unrelated reasons --
  # the endpoint 404'd, or the FILTER failed on the body -- and keying on exit status
  # alone conflated them. A 200 whose body was HTML, truncated, or `null` therefore
  # degraded the union to rulesets-only EMITTING NOTHING, which is exactly the
  # "a legacy-only required context silently vanishes" failure the union exists to
  # prevent. Splitting the fetch from the parse makes the two distinguishable:
  # ABSENT contributes nothing SILENTLY (a rulesets-only repo must not be nagged on
  # every merge), PRESENT-BUT-UNPARSEABLE contributes nothing LOUDLY.
  _legacy="[]"
  if _lraw="$(gh api "repos/$repo/branches/$_ref/protection" 2>/dev/null)"; then
    # `type == "object"` first: this endpoint contracts an object, and a degenerate 200
    # carrying `null` (or an array, or a scalar) would otherwise filter to nothing and
    # read as "legacy requires no checks" -- indistinguishable from a real empty answer.
    # NOT `jq -e`: it exits 4 on NO OUTPUT, and a branch that legitimately requires no
    # legacy contexts produces exactly that -- the same empty-vs-unreadable conflation
    # this whole change exists to close, and the trap the rules read above already
    # documents. `error()` exits 5 for the shape failure; empty output exits 0.
    if _lparsed="$(jq -c 'if type == "object" then . else error("not an object") end
                          | [.required_status_checks.contexts[]?, (.required_status_checks.checks[]?.context)]' \
                     <<<"$_lraw" 2>/dev/null)"; then
      # An EMPTY 200 body is benign, not malformed -- `jq -c` over empty stdin exits 0
      # with NO OUTPUT, and the rules leg twelve lines above normalizes exactly this
      # case for exactly this reason. Without the guard the empty string reaches
      # `--argjson`, which rejects it (exit 2) and turns a best-effort leg into a
      # SILENT BLOCK: control never reaches the `else`, so the unparseable NOTE below
      # does not fire either. That is the "an unread authority looks identical to an
      # absent one" defect this block exists to prevent, inverted.
      # An `[ -n ] && assign` here would be the LAST command of this branch, so an empty
      # body would make the branch exit 1 and `set -e` would kill the whole oracle.
      if [ -n "$_lparsed" ]; then _legacy="$_lparsed"; fi
    else
      # NOT a BLOCK: this leg is best-effort by design and the rules leg above already
      # carries the authority. But never silent -- an unread authority that looks
      # identical to an absent one is the defect, whichever way the verdict falls.
      echo "NOTE: legacy branch protection for '$_ref' is present but unparseable; the expected set uses the rulesets API alone (#375)." >&2
    fi
  fi

  # VALIDATE THE UNION, ONCE, WHILE IT IS STILL STRUCTURED. Both legs are checked here
  # rather than at each source, because a per-leg guard is exactly what the first round
  # got wrong twice: a rule applied at one call site and not its sibling reads as
  # enforced while the other half sails through. Two invariants, both fail-CLOSED:
  #
  #   EVERY context is a STRING. A non-string (42, null, an object) would otherwise be
  #   rendered by `-r` into the required-set as a name -- a pretty-printed JSON fragment
  #   spanning several LINES, which is the fabricated-check-name defect this change was
  #   opened to fix, arriving from the payload instead of from an error body.
  #
  #   NO CONTEXT CONTAINS A NEWLINE. The set is carried newline-delimited from here on,
  #   so a context named "alpha\nbeta" split into two independent requirements and two
  #   unrelated checks named `alpha` and `beta` satisfied a required check that never
  #   ran (measured, both legs). Such a name is pathological rather than adversarial, so
  #   reject it rather than moving three pipelines to NUL-delimited for a case that
  #   should not exist. It must be rejected BEFORE the flattening: a value cannot be
  #   validated after it has been flattened into the representation that loses it.
  #
  # A failure here is a nonzero exit the caller already turns into a BLOCK.
  # NO `select(. != null)`. Discarding a null would DROP a required context while
  # reporting a clean read -- the under-reporting direction, which is the one that
  # PASSes. Both legs use `?` operators, so an ABSENT field yields nothing at all; a
  # null can only arrive by being genuinely present as null, i.e. malformed. Malformed
  # is unreadable, not empty. That distinction is the entire subject of this change.
  _union="$(jq -rn --argjson a "$_rules" --argjson b "$_legacy" \
              '($a + $b)
               | if all(type == "string" and (test("\n") | not)) then unique | .[]
                 else error("unusable context name") end' 2>/dev/null)" || return 1
  # `|| true` because `grep -v` exits 1 when it filters EVERYTHING out, and under the
  # caller's `pipefail` that status becomes this function's return value -- which the
  # caller reads as "unreadable" and turns into a BLOCK. An EMPTY union is a valid
  # result (a ref that requires no checks); it is the input the fallback exists to
  # handle, so it must not be reported as a failure to read.
  # `LC_ALL=C` because `sort -u` dedupes on COLLATION equality, not byte equality, and
  # collation is locale-dependent, while the comparison downstream is byte-exact
  # (`grep -Fxq`) and GitHub check names are case-SENSITIVE -- `CI` and `ci` can both be
  # required. No locale was found locally that actually collapses a byte-distinct pair,
  # so this is a structural guarantee rather than a fix for a measured failure: it makes
  # the dedupe agree with the comparison by construction instead of by ambient setting.
  printf '%s\n' "$_union" | grep -v '^$' | LC_ALL=C sort -u || true
}

expected=""; expected_src=""
if [ -n "$base_ref" ]; then
  if expected="$(_required_contexts_for "$base_ref")"; then
    expected_src="base '$base_ref'"
  else
    echo "BLOCK: cannot read the rules for base '$base_ref' on #$pr. An unverifiable gate is not a passed gate (#375)." >&2
    echo "RESULT: BLOCK -- expected check set unreadable. [#$pr $repo]" >&2
    exit 2
  fi
else
  echo "BLOCK: cannot determine the base branch of #$pr; the expected check set is unknowable." >&2
  echo "RESULT: BLOCK -- base branch unreadable. [#$pr $repo]" >&2
  exit 2
fi

# THE FALLBACK PREDICATE IS ON REQUIRED CHECKS, NEVER ON RULE PRESENCE (design F4).
# stillwater returns exactly one rule for a non-default ref -- copilot_code_review,
# from an auto-review ruleset, carrying ZERO required checks. A predicate keyed on
# "does this ref have rules" would call that base governed, reconcile against an
# empty set, and pass the identical false green through a longer path.
if [ -z "$expected" ]; then
  # Only the FALLBACK needs the default branch, and the fallback only applies when the
  # base is a DIFFERENT ref. Demanding it unconditionally turned a flaky `gh repo view`
  # into a false BLOCK for any repo whose default branch simply requires no checks --
  # fail-closed, so not a safety hole, but wrong.
  if [ -n "$default_branch" ] && [ "$base_ref" = "$default_branch" ]; then
    : # base IS the default branch and it requires nothing; nothing to fall back to.
  elif [ -z "$default_branch" ]; then
    # Only fatal HERE, where the fallback is actually needed: a repo whose base
    # already carries its own required set never reaches this branch, so a flaky
    # default-branch lookup cannot block it.
    echo "BLOCK: base '$base_ref' requires no checks and the default branch is unreadable (#$pr); the expected check set cannot be determined." >&2
    echo "RESULT: BLOCK -- expected check set unreadable. [#$pr $repo]" >&2
    exit 2
  fi
  if [ "$base_ref" != "$default_branch" ]; then
    if expected="$(_required_contexts_for "$default_branch")"; then
      expected_src="default branch '$default_branch' (fallback: base '$base_ref' requires no checks)"
    else
      echo "BLOCK: base '$base_ref' requires no checks and the default branch's rules are unreadable (#$pr)." >&2
      echo "RESULT: BLOCK -- expected check set unreadable. [#$pr $repo]" >&2
      exit 2
    fi
  fi
fi

if [ -n "$expected" ]; then
  present="$(jq -r '[.statusCheckRollup[]? | (.name // .context)] | map(select(. != null)) | unique | .[]' <<<"$json" 2>/dev/null || echo "")"
  missing=""
  while IFS= read -r _ctx; do
    [ -n "$_ctx" ] || continue
    printf '%s\n' "$present" | grep -Fxq -- "$_ctx" || missing="${missing}${missing:+, }${_ctx}"
  done <<<"$expected"
  if [ -n "$missing" ]; then
    # "did not run (absent from the rollup)", never "NEVER RAN": GitHub populates the
    # rollup incrementally, so a required check not yet SCHEDULED is absent rather than
    # never-run. The verdict is the same either way (block), but the message must not
    # assert more than was observed.
    echo "BLOCK: required check(s) did not run on #$pr (absent from the rollup): $missing" >&2
    echo "  expected set from $expected_src; an absent check is not a passing check (#375)." >&2
    echo "  If a push just landed, re-run once the checks have been scheduled." >&2
    echo "RESULT: BLOCK -- required checks missing. [#$pr $repo]" >&2
    exit 2
  fi
  echo "NOTE: required check set reconciled against $expected_src -- all present."
else
  # NEVER silent (M4). A skipped reconciliation and a clean one used to produce
  # identical output, so a PASS could not be audited for whether the check even ran --
  # which is the same class of defect this whole change exists to close.
  echo "NOTE: no required check set applies to base '$base_ref'; reconciliation skipped."
fi

# A context is BAD if it is not terminal-and-acceptable. CheckRun: status must be
# COMPLETED and conclusion in the acceptable set. StatusContext: state must be
# SUCCESS (it has no .status, so default-COMPLETED makes only .state decide, and
# SUCCESS is the only acceptable state). Discriminate on __typename so a
# StatusContext is never wrongly treated as a (possibly-conclusion-bearing)
# CheckRun. Anything not matching the per-shape PASS rule blocks (fail closed).
bad="$(jq -r --argjson ok "$ACCEPTABLE_CONCLUSIONS" '
  # LATEST-PER-NAME REDUCTION for CheckRuns (#301 Part 1). GitHub CANCELS in-flight
  # duplicates on every new push, so a superseded CANCELLED run sits in the rollup
  # alongside the LATER same-name run that SUCCEEDED - and evaluating the list flat
  # blocked on the corpse. That is normal push behavior, not an edge case.
  #
  # THIS MOVES IN THE RELAXING DIRECTION (it makes the gate block LESS), which for a
  # fail-closed oracle is the dangerous one, so the sort is specified defensively:
  #
  #   PRIMARY KEY = startedAt, NOT completedAt. Sorting by completedAt is a FALSE-GREEN:
  #   a newer IN_PROGRESS run has completedAt = null, so a stale SUCCESS would sort
  #   above it and win. startedAt is monotonic per run and always set for a started run.
  #
  #   NULL TIMESTAMPS SORT AS NEWEST, never oldest. A QUEUED re-run has startedAt = null
  #   and MUST beat an old SUCCESS, so the gate blocks on the pending re-run rather than
  #   the stale pass. Ordering nulls last would invert exactly this case.
  #
  # ONLY CheckRuns are reduced. StatusContexts have no re-run/name-collision semantics,
  # and the unknown-__typename entries must keep their fail-closed path untouched - so
  # both bypass the grouping entirely.
  ( (.statusCheckRollup // [])
    | map(select((.__typename // "") == "CheckRun"))
    | group_by(.name // .context // "unknown")
    | map( sort_by( [ (if .startedAt   == null then "9999" else .startedAt   end),
                      (if .completedAt == null then "9999" else .completedAt end) ] )
           | last )
  ) as $latest_runs
  | ( (.statusCheckRollup // []) | map(select((.__typename // "") != "CheckRun")) ) as $others
  | ($latest_runs + $others)[]
  | (.__typename // "") as $t
  | if $t == "StatusContext" then
      { name: (.context // "unknown"),
        kind: "StatusContext",
        status: "-",
        result: ((.state // "") | ascii_upcase),
        bad: (((.state // "") | ascii_upcase) != "SUCCESS") }
    elif $t == "CheckRun" then
      { name: (.name // .context // "unknown"),
        kind: "CheckRun",
        status: ((.status // "") | ascii_upcase),
        result: ((.conclusion // "") | ascii_upcase) }
      | .bad = ( (.status != "COMPLETED") or ((.result | IN($ok[])) | not) )
    else
      # FAIL CLOSED: an unknown or absent __typename is ambiguous - the check
      # shape cannot be verified, so it BLOCKS rather than falling through to the
      # CheckRun pass-path (gh always emits __typename to discriminate the union).
      { name: (.name // .context // "unknown"),
        kind: (if $t == "" then "UNKNOWN-TYPE" else $t end),
        status: "-",
        result: "UNKNOWN-TYPE",
        bad: true }
    end
  | select(.bad)
  | "  - \(.name) [\(.kind)]: status=\(.status) result=\(.result)"
' <<<"$json")" || {
  echo "BLOCK: jq parse of statusCheckRollup failed (#$pr)" >&2
  exit 2
}

if [ -n "$bad" ]; then
  echo "BLOCK: red / incomplete checks on #$pr (every one blocks, required or not):" >&2
  echo "$bad" >&2
  echo "RESULT: BLOCK -- checks not all green. [#$pr $repo]" >&2
  exit 2
fi

# All checks green. Now the authoritative review-body gate. FAIL CLOSED on a
# missing/erroring helper. The helper only prints the count line when N>0, so a
# clean (exit-0) run with no line means N==0.
helper="${HOME}/.claude/scripts/pr-unreplied-comments.sh"
if [ ! -x "$helper" ]; then
  echo "BLOCK: pr-unreplied-comments.sh not found/executable at $helper (cannot verify review state)." >&2
  echo "RESULT: BLOCK -- review gate unverifiable. [#$pr $repo]" >&2
  exit 2
fi

# Invoke the helper with --allow-stale. The helper exits 2 (printing a
# "STOP: head branch is behind base" section to STDOUT) when the PR branch is
# BEHIND base -- a DETERMINISTIC condition, not a transient. That base-freshness
# STOP is irrelevant to THIS gate: the oracle counts actionable review-body
# findings, which are readable regardless of base-freshness. Base-freshness is
# DELIBERATELY out of this oracle's scope -- it queries only
# `statusCheckRollup,reviewDecision` (see the `gh pr view --json` call above) and never reads
# mergeStateStatus, so there is no base-freshness gate HERE to lean on. That
# concern is owned at the lead-presentation layer instead (SKILL.md #235: never
# present a ship-gate on this oracle's PASS alone; after any rebase re-run the
# authoritative enumeration against current HEAD). Without --allow-stale the helper's
# behind-base exit 2 would mask as a generic "helper failed" and BLOCK every
# behind-base PR (#178). The combined stdout+stderr is captured to a temp file
# (the helper's diagnostic lines land on stdout) so a genuine failure surfaces
# its exit code + an output tail rather than being swallowed by 2>/dev/null.
#
# A bounded retry (3 attempts, no delay -- matching pr-watch.sh) absorbs a
# genuinely transient non-zero (e.g. a flaky gh/network blip) before declaring
# BLOCK. FAIL CLOSED is preserved: a persistent non-zero still BLOCKs, now with
# the exit code + output tail surfaced for diagnosis.
unreplied_capture="$(mktemp)"
helper_rc=0
for _attempt in 1 2 3; do
  helper_rc=0
  "$helper" --allow-stale "$pr" "$repo" >"$unreplied_capture" 2>&1 || helper_rc=$?
  if [ "$helper_rc" -eq 0 ]; then
    break
  fi
done
unreplied_out="$(cat "$unreplied_capture")"
if [ "$helper_rc" -ne 0 ]; then
  helper_tail="$(tail -n 5 "$unreplied_capture")"
  rm -f "$unreplied_capture"
  echo "BLOCK: pr-unreplied-comments.sh failed for #$pr after 3 attempt(s) (exit $helper_rc; cannot verify review state)." >&2
  echo "  --- helper output (last 5 lines) ---" >&2
  echo "$helper_tail" >&2
  echo "RESULT: BLOCK -- review gate unverifiable. [#$pr $repo]" >&2
  exit 2
fi
rm -f "$unreplied_capture"

# The helper prints the "Review-body comments with actionable findings: N" line
# ONLY when N>0, so three cases must be distinguished -- FAIL CLOSED on the third:
#   - line ABSENT              -> N=0 (clean)
#   - line present, NUMERIC N  -> that N (block if >0)
#   - line present, NON-NUMERIC count (format change / tamper) -> BLOCK: a present
#     but unparseable count means the review state cannot be verified.
findings_line="$(printf '%s\n' "$unreplied_out" \
  | grep -E 'Review-body comments with actionable findings:' | tail -n1 || true)"
if [ -n "$findings_line" ]; then
  findings="$(printf '%s\n' "$findings_line" \
    | sed -n 's/.*Review-body comments with actionable findings:[[:space:]]*\([0-9][0-9]*\).*/\1/p')"
  if ! printf '%s' "$findings" | grep -Eq '^[0-9]+$'; then
    echo "BLOCK: review-body findings line present but its count is non-numeric on #$pr (cannot verify review state)." >&2
    echo "RESULT: BLOCK -- review gate unverifiable (non-numeric count). [#$pr $repo]" >&2
    exit 2
  fi
else
  findings=0
fi

if [ "$findings" -gt 0 ]; then
  # NAME THE CLEARING ACTION (#289). A review-body finding has NO inline thread to resolve; it
  # clears when a comment of yours REFERENCES THE REVIEW ID. That channel already existed but
  # was invisible from this message, so on stillwater #2424 the maintainer replied "fixed in
  # <sha>" (no id), the gate never cleared, and they merged OVER the oracle. A fail-closed gate
  # that cannot tell you how to satisfy it trains the lead to override it - the worst possible
  # failure mode for the one deterministic gate in the pipeline.
  echo "BLOCK: $findings actionable review-body finding(s) unaddressed on #$pr -- run '$helper --itemized --allow-stale $pr $repo' for the itemized breakdown." >&2
  echo "  TO CLEAR: address each finding, then ack its review BY ID:" >&2
  echo "    reply-comment.sh --review <review-id> $pr \"<why it is addressed / the fix SHA>\"" >&2
  echo "  The review id is the ack token - a reply WITHOUT it does NOT clear the finding." >&2
  echo "RESULT: BLOCK -- $findings actionable review-body finding(s). [#$pr $repo]" >&2
  exit 2
fi

# --- REVIEW-DECISION GATE (#117) -------------------------------------------
# Reached only with all checks green AND findings==0 (the findings>0 gate above
# already exited). Couple GitHub's reviewDecision with that count: a
# CHANGES_REQUESTED surviving to here is SUPERSEDED (its findings are addressed)
# and PASSES; an active CHANGES_REQUESTED was already blocked via its findings.
# FAIL CLOSED on an unrecognized value (mirrors the unknown-__typename posture):
# GitHub emits only APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED / null, so
# anything else is ambiguous and BLOCKS. null/absent -> "" (no active decision).
review_decision="$(jq -r '(.reviewDecision // "") | ascii_upcase' <<<"$json")" || {
  echo "BLOCK: jq parse of reviewDecision failed (#$pr)" >&2
  echo "RESULT: BLOCK -- reviewDecision unverifiable. [#$pr $repo]" >&2
  exit 2
}
case "$review_decision" in
  ""|APPROVED|REVIEW_REQUIRED|CHANGES_REQUESTED) : ;;
  *)
    echo "BLOCK: unrecognized reviewDecision '$review_decision' on #$pr (cannot verify review state)." >&2
    echo "RESULT: BLOCK -- reviewDecision unverifiable. [#$pr $repo]" >&2
    exit 2 ;;
esac

# --- REVIEW-THREAD ENUMERATION GATE (#263 Piece A; FULL mode only) ----------
# BLOCK on ANY unresolved review thread (GraphQL isResolved == false). This is a
# FIRST-CLASS, explicitly-enumerated condition, distinct from the actionable-
# findings count above ("0 unreplied findings" != "0 unresolved threads": a thread
# can be replied-to yet unresolved, and vice versa) and NOT dependent on GitHub's
# branch-protection / mergeStateStatus being configured (the oracle must not assume
# repo settings). reviewThreads is not a `gh pr view --json` field, so it is a
# SEPARATE GraphQL read (the {checks,decision,SHA} snapshot above stays atomic; this
# is a fail-closed second read). FAIL CLOSED on every ambiguous state: a gh/query
# error, malformed JSON, or a paginated-TRUNCATED list (totalCount > nodes fetched,
# so the unfetched threads' state is unknown) all BLOCK.
owner="${repo%%/*}"; name="${repo##*/}"
# shellcheck disable=SC2016  # GraphQL $owner/$name/$number are query variables, NOT shell expansions.
threads_json="$(gh api graphql \
  -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){totalCount nodes{isResolved}}}}}' \
  -F owner="$owner" -F name="$name" -F number="$pr" 2>/dev/null)" || {
  echo "BLOCK: reviewThreads GraphQL query failed for #$pr in $repo (cannot verify thread state)." >&2
  echo "RESULT: BLOCK -- review threads unverifiable. [#$pr $repo]" >&2
  exit 2
}
# FAIL CLOSED on a GraphQL partial-error payload (non-empty top-level .errors) or a
# reviewThreads that is not an object - either means the thread state cannot be
# trusted even if a data sub-object looks present.
if ! jq -e '(((.errors // []) | length) == 0) and ((.data.repository.pullRequest.reviewThreads | type) == "object")' \
     >/dev/null 2>&1 <<<"$threads_json"; then
  echo "BLOCK: malformed / error-bearing reviewThreads response for #$pr (cannot verify thread state)." >&2
  echo "RESULT: BLOCK -- review threads unverifiable. [#$pr $repo]" >&2
  exit 2
fi
# Emit "MALFORMED", "TRUNC" (totalCount > fetched nodes), an unresolved count, or
# "OK". FAIL CLOSED by counting PROVABLY-resolved nodes (isResolved == literal true)
# and blocking the remainder, NOT by matching == false: reviewThreads.nodes elements
# are nullable, so a partial GraphQL error can null a thread while gh still returns
# `data`. A `== false` match would read a null / missing / non-bool isResolved (and
# a null node) as "resolved" and PASS an unready PR. Anything not provably true
# blocks. totalCount is VALIDATED as a present non-negative integer FIRST: a null/
# missing/non-integer totalCount (a partial error can null it) must NOT default to 0
# and silently disable the truncation guard; totalCount < nodes is impossible in a
# well-formed response and is MALFORMED.
thread_verdict="$(jq -r '
  .data.repository.pullRequest.reviewThreads as $rt
  | if ($rt.totalCount | type) != "number"
       or ($rt.nodes | type) != "array"
       or $rt.totalCount < 0
       or $rt.totalCount != ($rt.totalCount | floor)
    then "MALFORMED"
    else
      ($rt.totalCount) as $tc
      | ($rt.nodes | length) as $n
      | ([$rt.nodes[] | select(.isResolved == true)] | length) as $r
      | if $tc > $n then "TRUNC:\($tc)>\($n)"
        elif $tc < $n then "MALFORMED"
        elif $r < $n then "UNRESOLVED:\($n - $r)"
        else "OK" end
    end
' <<<"$threads_json")" || {
  echo "BLOCK: jq parse of reviewThreads failed (#$pr)." >&2
  echo "RESULT: BLOCK -- review threads unverifiable. [#$pr $repo]" >&2
  exit 2
}
case "$thread_verdict" in
  OK) : ;;
  MALFORMED)
    echo "BLOCK: reviewThreads response malformed on #$pr (totalCount/nodes not a well-formed non-negative-int/array pair)." >&2
    echo "RESULT: BLOCK -- review threads unverifiable (malformed). [#$pr $repo]" >&2
    exit 2 ;;
  TRUNC:*)
    echo "BLOCK: reviewThreads list truncated on #$pr (${thread_verdict#TRUNC:} threads; only 100 fetched, remainder unverifiable)." >&2
    echo "RESULT: BLOCK -- review threads unverifiable (truncated). [#$pr $repo]" >&2
    exit 2 ;;
  UNRESOLVED:*)
    echo "BLOCK: ${thread_verdict#UNRESOLVED:} unresolved review thread(s) on #$pr (GraphQL isResolved==false). Resolve them before merge." >&2
    echo "RESULT: BLOCK -- unresolved review thread(s). [#$pr $repo]" >&2
    exit 2 ;;
  *)
    echo "BLOCK: unrecognized reviewThreads verdict '$thread_verdict' on #$pr (fail closed)." >&2
    echo "RESULT: BLOCK -- review threads unverifiable. [#$pr $repo]" >&2
    exit 2 ;;
esac

# --- CODOKI-ROOT-ACK GATE (#234; FULL mode only) ---------------------------
# The merge-ready bar carries a review-response clause the thread-resolution +
# unreplied-comments checks CANNOT see: the 👍/👎 ack on Codoki's ISSUE-LEVEL
# review-SUMMARY comment (author login codoki-pr-intelligence[bot]). That comment
# has no isResolved and never appears in a reviewThreads query, so a merge-ready
# path once reported CLEAN while the ack was unmet. gh-react.sh is the canonical,
# least-privilege reader of that ack state. ACK RULE (settled): satisfaction = ANY
# NON-BOT login's +1/-1 on the LATEST summary (a -1 also needs an @codoki reply);
# NO summary => PASS (never fail-closed on absence). FAIL CLOSED on a missing/
# erroring reader (the ack cannot be verified), mirroring the review-gate posture.
# Deliberately NOT in --codoki-only mode (that stays a pure check-rollup signal).
reactor="${HOME}/.claude/scripts/gh-react.sh"
if [ ! -x "$reactor" ]; then
  echo "BLOCK: gh-react.sh not found/executable at $reactor (cannot verify the Codoki root ack)." >&2
  echo "RESULT: BLOCK -- Codoki ack unverifiable. [#$pr $repo]" >&2
  exit 2
fi
ack_out="$("$reactor" codoki-ack "$pr" "$repo" 2>&1)" || {
  echo "BLOCK: gh-react.sh codoki-ack failed for #$pr (cannot verify the Codoki root ack; fail closed)." >&2
  echo "  --- gh-react output ---" >&2
  printf '%s\n' "$ack_out" | tail -n 3 >&2
  echo "RESULT: BLOCK -- Codoki ack unverifiable. [#$pr $repo]" >&2
  exit 2
}
# Parse the 'CODOKI-ACK: <verdict>' line. no-summary/acked -> pass; unacked ->
# BLOCK; anything unrecognized -> fail closed.
ack_verdict="$(printf '%s\n' "$ack_out" \
  | sed -n 's/.*CODOKI-ACK:[[:space:]]*\([A-Za-z-]*\).*/\1/p' | tail -n1)"
case "$ack_verdict" in
  no-summary|acked) : ;;
  unacked)
    echo "BLOCK: Codoki root-summary ack is UNMET on #$pr (react 👍/👎 via gh-react.sh codoki-ack $pr --react +1|-1; a 👎 rebut also needs an @codoki reply)." >&2
    printf '%s\n' "$ack_out" | tail -n 1 >&2
    echo "RESULT: BLOCK -- Codoki root ack unmet. [#$pr $repo]" >&2
    exit 2 ;;
  *)
    echo "BLOCK: gh-react.sh returned an unrecognized ack verdict '${ack_verdict:-<none>}' on #$pr (cannot verify the Codoki root ack)." >&2
    echo "RESULT: BLOCK -- Codoki ack unverifiable. [#$pr $repo]" >&2
    exit 2 ;;
esac

# --- GITHUB'S OWN AGGREGATE VERDICT (#334; FULL mode only) -----------------
# Ask GitHub whether it would merge this, instead of re-deriving that from a subset of
# rules. mergeStateStatus aggregates EVERY branch-protection and ruleset rule, including
# ones this script has never heard of, so it closes the whole "evaluated a hand-picked
# subset, treated the remainder as absent" class rather than one instance of it.
#
# THE MERGEABLE SET, and why each member is a PASS:
#   CLEAN     - mergeable, nothing outstanding.
#   UNSTABLE  - a NON-REQUIRED check is failing or pending. GitHub still merges this, and
#               the required checks were already validated above via statusCheckRollup.
#   HAS_HOOKS - mergeable; a repo pre-receive hook will run. Same set pr-watch.sh treats
#               as merge-ready, so the two agree by construction.
#   BEHIND    - PASSES DELIBERATELY. Base-freshness is explicitly out of this oracle's
#               scope (base-freshness.sh owns it, and /prep-pr + the staleness sweep act
#               on it), so blocking here would move a documented boundary as a side effect
#               of a false-PASS fix. It is REPORTED in the PASS line instead.
# Everything else BLOCKS - notably BLOCKED (a rule is unmet) and DIRTY (conflicts).
# UNKNOWN is GitHub still computing: BLOCK, because "not yet known" must never read as
# "fine" in a fail-closed gate. Re-running resolves it in seconds.
merge_state="$(jq -r '(.mergeStateStatus // "") | ascii_upcase' <<<"$json" 2>/dev/null)" || merge_state=""
case "$merge_state" in
  CLEAN|UNSTABLE|HAS_HOOKS|BEHIND) : ;;
  "")
    echo "BLOCK: mergeStateStatus unreadable on #$pr; cannot confirm GitHub would merge this." >&2
    echo "RESULT: BLOCK -- merge state unverifiable. [#$pr $repo]" >&2
    exit 2 ;;
  UNKNOWN)
    echo "BLOCK: mergeStateStatus=UNKNOWN on #$pr (GitHub is still computing the merge state); re-run in a few seconds." >&2
    echo "RESULT: BLOCK -- merge state not yet computed. [#$pr $repo]" >&2
    exit 2 ;;
  *)
    echo "BLOCK: GitHub reports mergeStateStatus=${merge_state} on #$pr - it would NOT merge this, whatever the other gates say." >&2
    echo "       Run '$0 --diagnose $pr $repo' for the specific unmet rule (e.g. required_signatures, which this oracle does not evaluate rule-by-rule)." >&2
    echo "RESULT: BLOCK -- GitHub is blocking the merge (mergeStateStatus=${merge_state}). [#$pr $repo]" >&2
    exit 2 ;;
esac

# --- HEAD-SHA ATTESTATION (#263 Piece A; FULL mode only) -------------------
# Every other gate passed. Emit the validated head SHA so authorize-merge can pin
# --match-head-commit to it. FAIL CLOSED if the SHA is unreadable (empty/null/
# absent) or not a hex object id: a PASS that cannot name the commit it cleared is
# useless downstream and must not be issued. Parsed from the same snapshot as the
# checks (head_sha above), so it is definitionally the commit those checks ran on.
# Validate the ENTIRE value (bash [[ =~ ]] anchors the whole string), NOT `printf |
# grep` which is line-oriented and would pass a "<40hex>\nforged" head_sha - letting
# junk into the emitted attestation a downstream step pins.
if [[ ! "$head_sha" =~ ^[0-9a-f]{40,}$ ]]; then
  echo "BLOCK: headRefOid unreadable on #$pr (got '${head_sha:-<empty>}'); cannot attest the merge commit." >&2
  echo "RESULT: BLOCK -- head SHA unverifiable. [#$pr $repo]" >&2
  exit 2
fi

# --- REVIEW-DECISION DISAMBIGUATION (#315) ---------------------------------
# A bare `reviewDecision=<none>` inside a line beginning `RESULT: PASS` is ambiguous in
# the DANGEROUS direction: it reads as "review state is fine" while meaning only "not an
# active CHANGES_REQUESTED". reviewDecision is a BRANCH-PROTECTION artifact - GitHub
# populates it only when a review is REQUIRED - so null covers three very different
# states: someone approved HEAD, nobody has reviewed at all, or a push dismissed a review.
# Measured on a real PR whose reviews API showed an APPROVED on HEAD while the oracle
# printed the same `<none>` it prints for a never-reviewed PR.
#
# So when it is null, SAY WHICH by reading the reviews API - the record of what humans and
# bots actually did, as opposed to what branch protection requires. This is REPORTING
# only: it does not gate, because requiring an approval here would impose a review policy
# the repo has not set (auto-review is off org-wide, so plenty of legitimately-mergeable
# PRs carry no approval at all). It removes the ambiguity without inventing a rule.
#
# Fail SOFT: an unreadable reviews API degrades to the old bare token rather than blocking
# a PR that passed every real gate. A reporting nicety must never become a merge blocker.
# ONE reviews read, shared by the #315 disambiguation and the #301 coverage advisory.
# Hoisted out of the `-z "$review_decision"` branch deliberately: nested there, an
# APPROVED PR left $rv empty and the coverage check below silently took its
# "unverifiable" path - a check that reports nothing on exactly the PRs that HAVE been
# reviewed. Both consumers are fail-soft, so one unreadable read degrades both to a NOTE
# rather than blocking.
rv="$(gh api --paginate "repos/$repo/pulls/$pr/reviews" 2>/dev/null | jq -s 'add // []' 2>/dev/null)" || rv=""

review_note=""
if [ -z "$review_decision" ]; then
  if [ -n "$rv" ] && jq -e . >/dev/null 2>&1 <<<"$rv"; then
    # Latest review PER REVIEWER on the CURRENT head, so a stale CHANGES_REQUESTED that a
    # later APPROVED superseded does not misreport (that exact sequence is what #315 saw).
    appr="$(jq -r --arg h "$head_sha" '[.[] | select(.commit_id == $h)]
              | group_by(.user.login) | map(sort_by(.submitted_at) | last)
              | map(select(.state == "APPROVED")) | length' <<<"$rv" 2>/dev/null)" || appr=""
    total="$(jq -r 'length' <<<"$rv" 2>/dev/null)" || total=""
    case "$appr" in
      ''|*[!0-9]*) review_note=" (no decision required; approval state unreadable)" ;;
      0) if [ "${total:-0}" = "0" ]; then
           review_note=" (no decision required; NOBODY has reviewed this PR)"
         else
           review_note=" (no decision required; no APPROVED review on the current head)"
         fi ;;
      *) review_note=" (no decision required; ${appr} APPROVED review(s) on the current head)" ;;
    esac
  else
    review_note=" (no decision required; approval state unreadable)"
  fi
fi

# --- REVIEW COVERAGE (#301 Part 2; ADVISORY, fail-OPEN, NEVER gates) -------
# The gate measures thread state and CI state but never asks whether anyone reviewed the
# code that is actually about to merge. Fix-round commits pushed after the last review
# merge UNREVIEWED with every gate green - measured elsewhere at 3 unreviewed commits,
# including the fix for a bot's own MAJOR finding.
#
# WARN ONLY, by design. A docs-only fix round needs no re-review, and blocking every fix
# round would deadlock the push-first loop (push, then reply citing the SHA). So this
# surfaces the range and says explicitly that the LEAD decides.
#
# THE PREDICATE IS MEMBERSHIP, not recency: "is head among the reviewed commit oids". The
# tempting `newest_review.commit == head` false-WARNs whenever a reviewer re-reviews an
# EARLIER commit, which is ordinary when a bot revisits a thread.
#
# Reuses the reviews payload already fetched above for #315 rather than issuing a second
# read - and because that read is fail-SOFT, an unreadable API degrades to a NOTE here
# instead of flipping a PASS to a BLOCK. An advisory that can change the verdict is not
# advisory.
coverage_note=""
if [ -n "${rv:-}" ] && jq -e . >/dev/null 2>&1 <<<"$rv"; then
  reviewed_head="$(jq -r --arg h "$head_sha" '[.[] | select(.commit_id == $h)] | length' <<<"$rv" 2>/dev/null)" || reviewed_head=""
  rv_count="$(jq -r 'length' <<<"$rv" 2>/dev/null)" || rv_count=""
  case "$reviewed_head" in
    ''|*[!0-9]*) coverage_note=" NOTE: review coverage unreadable (advisory only; verdict unchanged)." ;;
    0)
      if [ "${rv_count:-0}" = "0" ]; then
        coverage_note=" WARN: NO review covers any commit on this PR - the head is entirely unreviewed. The LEAD decides whether that is acceptable (a docs-only change often is)."
      else
        last_reviewed="$(jq -r 'sort_by(.submitted_at) | last | (.commit_id // "")' <<<"$rv" 2>/dev/null)" || last_reviewed=""
        coverage_note=" WARN: no review covers the CURRENT head - commits ${last_reviewed:0:8}..${head_sha:0:8} are unreviewed. The LEAD decides whether they need a pass (a docs/comment-only fix round often does not)."
      fi ;;
    *) : ;;   # head IS among the reviewed oids - silent, nothing to say
  esac
else
  coverage_note=" NOTE: review coverage unverifiable (reviews read failed; advisory only, verdict unchanged)."
fi

# BEHIND is a PASS here (base-freshness is out of scope) but must be VISIBLE, not silent.
merge_note=""
[ "$merge_state" = "BEHIND" ] && merge_note=" (BEHIND base - out of this oracle's scope; refresh before merging)"

echo "RESULT: PASS -- all checks green, 0 actionable review-body findings, reviewDecision=${review_decision:-<none>}${review_note}, 0 unresolved review threads, mergeStateStatus=${merge_state}${merge_note}, Codoki root ack ${ack_verdict}, headRefOid=${head_sha}.${coverage_note} [#$pr $repo]"
exit 0
