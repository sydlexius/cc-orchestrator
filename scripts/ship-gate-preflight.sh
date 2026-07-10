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
#     --codoki-only            Settlement mode for pr-watch.sh: check ONLY whether
#                              the Codoki check has COMPLETED with an acceptable
#                              conclusion. Skips the full enumeration AND skips
#                              pr-unreplied-comments.sh.
#     --codoki-pattern <name>  Override the Codoki check name to match in
#                              --codoki-only mode (default: "Codoki PR Review").
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
codoki_pattern="Codoki PR Review"
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --codoki-only) codoki_only=true; shift ;;
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

pr="${args[0]:-}"
repo="${args[1]:-}"
if [ -z "$pr" ]; then
  echo "usage: ship-gate-preflight.sh <pr> [owner/repo] [--codoki-only] [--codoki-pattern <name>]" >&2
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

# FAIL CLOSED: any gh failure -> BLOCK.
json="$(gh pr view "$pr" --repo "$repo" --json statusCheckRollup,reviewDecision 2>/dev/null)" || {
  echo "BLOCK: 'gh pr view' failed for #$pr in $repo" >&2
  exit 2
}

# FAIL CLOSED: malformed JSON -> BLOCK. Validate before any field access.
if ! jq -e . >/dev/null 2>&1 <<<"$json"; then
  echo "BLOCK: malformed JSON from 'gh pr view' for #$pr in $repo" >&2
  exit 2
fi

if [ "$codoki_only" = true ]; then
  # Settlement mode: ONLY the Codoki check must be COMPLETED with an acceptable
  # conclusion. Missing / incomplete / failed -> BLOCK. (StatusContext-shaped
  # Codoki entries are also accepted via the same name/state mapping.)
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
  case "$verdict" in
    OK)
      echo "RESULT: PASS -- Codoki check '$codoki_pattern' settled (COMPLETED, acceptable conclusion). [#$pr $repo]"
      exit 0 ;;
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

# A context is BAD if it is not terminal-and-acceptable. CheckRun: status must be
# COMPLETED and conclusion in the acceptable set. StatusContext: state must be
# SUCCESS (it has no .status, so default-COMPLETED makes only .state decide, and
# SUCCESS is the only acceptable state). Discriminate on __typename so a
# StatusContext is never wrongly treated as a (possibly-conclusion-bearing)
# CheckRun. Anything not matching the per-shape PASS rule blocks (fail closed).
bad="$(jq -r --argjson ok "$ACCEPTABLE_CONCLUSIONS" '
  (.statusCheckRollup // [])[]
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
  echo "BLOCK: $findings actionable review-body finding(s) unaddressed on #$pr -- run '$helper --itemized --allow-stale $pr $repo' for the itemized breakdown." >&2
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

echo "RESULT: PASS -- all checks green, 0 actionable review-body findings, reviewDecision=${review_decision:-<none>} (not an active CHANGES_REQUESTED), Codoki root ack ${ack_verdict}. [#$pr $repo]"
exit 0
