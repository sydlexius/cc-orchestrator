#!/usr/bin/env bash
# gh-react.sh codoki-ack <pr> [owner/repo] [--react +1|-1]   (issue #234)
# gh-react.sh ack <pr> [owner/repo] [--bot coderabbit|copilot|codoki|auto] [--react +1|-1]  (#338)
#
# `ack` is the BOT-AGNOSTIC actuator: it reacts (default +1) on each reviewer bot's
# ROOT object that is not already acked by the CURRENT gh user. `codoki-ack` stays a
# behavior-identical ALIAS of the original Codoki reader/actuator (below), which
# ship-gate-preflight.sh calls; `ack` never changes what that gate reads or blocks on.
# Ack TARGET per bot (resolved by AUTHOR + body marker, never by position):
#   coderabbit  the issue-level walkthrough comment by `coderabbitai[bot]` carrying
#               `<!-- This is an auto-generated comment: summarize by coderabbit.ai -->`
#               (LATEST by created_at; CR's other auto-generated comments never match).
#   codoki      the same summary `codoki-ack` resolves (shared resolver).
#   copilot     NO reactable root object: Copilot posts a pull-request REVIEW, and the
#               reactions API has no endpoint for a review, only for comments. Reported
#               as no-target, never guessed onto an inline comment.
#   auto        (default) every bot above with a root object on the PR; a Copilot review
#               is reported as no-target (best-effort read, never fatal).
# Idempotent: a target where the current user already has a +1 or -1 is SKIPPED.
# `ack` EXIT CODES: 0 = every target acked (posted now or already acked);
#   3 = NOTHING TO ACK (no reactable root object for the requested bot(s));
#   2 = usage error OR ACK FAILED (a read, the current-user lookup, or a POST failed;
#       remaining targets are still attempted, nothing is reported as acked falsely).
#
# Least-privilege wrapper for the Codoki ROOT-SUMMARY ACK surface. Codoki posts an
# ISSUE-LEVEL review-summary comment (author login `codoki-pr-intelligence[bot]`,
# identified by the `<!-- CODOKI_REVIEW_COMMENT -->` marker, with a summary-body
# header heuristic as a format-drift fallback -- NOT a blind "any Codoki comment"
# pick, which could select a non-summary comment). That comment has NO isResolved
# and NEVER appears in a `reviewThreads` GraphQL query, so the resolve-thread /
# unreplied-comments surfaces cannot see its 👍/👎 ack. This wrapper is the one
# canonical way to (a) READ that ack state for the ship-gate oracle and (b) POST
# the reaction for a human actuation.
#
# CONSTRUCTION / LEAST-PRIVILEGE GUARANTEE: this wrapper performs ONLY GETs
# (list issue comments, read a comment's reactions; `ack` adds the current `user`
# and the PR's reviews) and the SINGLE reactions POST
# (`POST repos/<repo>/issues/comments/<id>/reactions` with content=+1 or -1). Every
# endpoint is built from a validated numeric pr / comment-id and a validated repo;
# no caller input reaches a /merge, --admin, an arbitrary endpoint, or a -X verb.
# It is NOT a general `gh api` mutation surface and is NEVER a reason to broaden the
# allow-list beyond this one script.
#
# ACK RULE (SETTLED, issue #234): ack satisfaction = ANY NON-BOT login's reaction
# (+1 OR -1) on the LATEST Codoki summary. A bot login's reaction NEVER counts (the
# lead session acts as a human account, and the maintainer may react on their own
# account - both non-bot, both satisfy). A -1 (rebut) ADDITIONALLY requires an
# `@codoki` reply comment (non-bot author, posted at/after the summary) to exist.
# No Codoki summary present => READ reports "no-summary" and PASSES (never
# fail-closed on absence). A tool failure (gh/jq error, unresolvable id) exits
# NONZERO with a LOUD stderr message - never a silent "not applicable".
#
# Canonical source: cc-orchestrator repo root; deployed Option-A into ~/.claude/scripts/.
set -euo pipefail

CODOKI_LOGIN="codoki-pr-intelligence[bot]"
CODOKI_MARKER="<!-- CODOKI_REVIEW_COMMENT -->"
CR_MARKER="<!-- This is an auto-generated comment: summarize by coderabbit.ai -->"

die() { echo "gh-react: $1" >&2; exit 2; }

# Whole-string numeric check via a bash `case` glob (no external tool): rejects
# any non-digit INCLUDING an embedded newline, and the empty string.
is_num() { case "$1" in (*[!0-9]*|'') return 1 ;; (*) return 0 ;; esac; }

# Validate a repo value (owner/name) as a whole string (mirrors gh-comment.sh):
# exactly one slash, strict charset, no traversal/metachars/newline.
validate_repo() {
  case "$1" in
    (''|*[!A-Za-z0-9._/-]*|*/*/*|/*|*/|*..*)
      die "repo must be owner/name ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+); got: '${1}'"
      ;;
  esac
}

resolve_repo() {
  local r="${GITHUB_REPOSITORY:-}"
  if [ -z "$r" ]; then
    r="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  fi
  [ -n "$r" ] || die "no repo (set GITHUB_REPOSITORY=owner/name or run in a gh-resolvable repo)"
  validate_repo "$r"
  printf '%s' "$r"
}

# --- `ack` (#338): react on each bot's root object not already acked by me ----
# Needs: $repo $pr $react $bot $issue_comments $summary_id (the Codoki resolver's
# pick). Prints one `ACK: <bot> -- <state>` line per bot considered; exit codes in
# the header. A failure on one target never hides another's outcome.
ack_one() {  # <bot> <comment-id> -> 0 acked/already, 1 failed
  local b="$1" id="$2" rx n
  is_num "$id" || { echo "gh-react: non-numeric ${b} target id ('${id}') -- refusing to POST" >&2; return 1; }
  rx="$(gh api "repos/${repo}/issues/comments/${id}/reactions" --paginate)" \
    || { echo "gh-react: could not read reactions on ${b} comment ${id} -- NOT acking blind" >&2; return 1; }
  # PAGINATION-SAFE: `--paginate` emits one JSON array PER PAGE, concatenated, so a
  # per-document count prints one number per page ("0\n1"), the numeric test errors
  # and reads false, and a reaction of mine on page 2 was missed -> a duplicate POST.
  # Slurp (-s) and flatten (.[][]) so the count spans every page, and REFUSE (never
  # POST) on anything that is not a single whole number.
  n="$(jq -rs --arg me "$me" '[ .[][] | select((.user.login // "") == $me)
        | select(.content == "+1" or .content == "-1") ] | length' <<<"$rx")" \
    || { echo "gh-react: could not parse reactions on ${b} comment ${id}" >&2; return 1; }
  is_num "$n" \
    || { echo "gh-react: unreadable reaction count on ${b} comment ${id} ('${n}') -- NOT acking blind" >&2; return 1; }
  if [ "$n" -gt 0 ]; then
    echo "ACK: ${b} -- already-acked (comment ${id} carries a reaction by ${me}; skipped)"
    return 0
  fi
  if gh api -X POST "repos/${repo}/issues/comments/${id}/reactions" -f "content=${react}" >/dev/null; then
    echo "ACK: ${b} -- posted ${react} on comment ${id} (PR #${pr} ${repo})"
    return 0
  fi
  echo "gh-react: POST of ${react} on ${b} comment ${id} FAILED" >&2
  return 1
}

ack_main() {
  local cr_id="" targets=0 failed=0 copilot_n=""
  me="$(gh api user --jq .login 2>/dev/null)" && [ -n "$me" ] \
    || die "could not resolve the current gh user -- cannot check for an existing ack, refusing to POST"
  if [ "$bot" = coderabbit ] || [ "$bot" = auto ]; then
    # Slurp + flatten (.[][]) so a walkthrough on ANY page of the paginated
    # issue-comment list is considered and the LATEST across all pages wins.
    cr_id="$(jq -rs --arg m "$CR_MARKER" '[ .[][] | select((.user.login // "") == "coderabbitai[bot]")
        | select((.body // "") | contains($m)) ] | sort_by(.created_at) | last | .id // empty' \
        <<<"$issue_comments")" || die "could not parse issue comments for the CodeRabbit walkthrough"
    if [ -n "$cr_id" ]; then
      targets=$((targets+1)); ack_one coderabbit "$cr_id" || failed=$((failed+1))
    elif [ "$bot" = coderabbit ]; then
      echo "ACK: coderabbit -- no-target (no CodeRabbit walkthrough comment on PR #${pr})"
    fi
  fi
  if [ "$bot" = codoki ] || [ "$bot" = auto ]; then
    if [ -n "$summary_id" ]; then
      targets=$((targets+1)); ack_one codoki "$summary_id" || failed=$((failed+1))
    elif [ "$bot" = codoki ]; then
      echo "ACK: codoki -- no-target (no Codoki review summary on PR #${pr})"
    fi
  fi
  if [ "$bot" = copilot ]; then
    echo "ACK: copilot -- no-target (Copilot posts a PR review, which has no reactions endpoint)"
  elif [ "$bot" = auto ]; then
    # Best-effort: only informs the report; a failed read never changes the outcome.
    # Per-ITEM --jq output (one id per line), since --paginate applies --jq per PAGE.
    copilot_n="$(gh api "repos/${repo}/pulls/${pr}/reviews" --paginate \
      --jq '.[] | select((.user.login // "") | test("^(Copilot|copilot-pull-request-reviewer\\[bot\\])$")) | .id' \
      2>/dev/null)" || copilot_n=""
    if [ -n "$copilot_n" ]; then
      echo "ACK: copilot -- no-target (Copilot reviewed, but a PR review has no reactions endpoint)"
    fi
  fi
  [ "$failed" -eq 0 ] || return 2
  if [ "$targets" -eq 0 ]; then
    [ "$bot" != auto ] || echo "ACK: none -- no reviewer bot root object to ack on PR #${pr} (${repo})"
    return 3
  fi
  return 0
}

sub="${1:-}"
[ -n "$sub" ] || die "usage: gh-react.sh codoki-ack|ack <pr> [owner/repo] [--bot coderabbit|copilot|codoki|auto] [--react +1|-1]"
shift

case "$sub" in
  codoki-ack|ack) ;;
  *) die "unknown subcommand '${sub}' (supported: 'ack', 'codoki-ack')" ;;
esac

pr=""
repo=""
react=""
bot=""
react_empty_set=""  # 1 when the LAST --react given was explicitly empty
while [ "$#" -gt 0 ]; do
  case "$1" in
    --react) [ "$#" -ge 2 ] || die "--react requires a value (+1 or -1)"; react="$2"
             react_empty_set=""; [ -n "$react" ] || react_empty_set=1; shift 2 ;;
    --react=*) react="${1#--react=}"; react_empty_set=""; [ -n "$react" ] || react_empty_set=1; shift ;;
    --bot) [ "$sub" = ack ] || die "--bot is only valid with 'ack'"
           [ "$#" -ge 2 ] || die "--bot requires a value"; bot="$2"
           [ -n "$bot" ] || die "--bot requires a non-empty value (coderabbit|copilot|codoki|auto)"; shift 2 ;;
    --bot=*) [ "$sub" = ack ] || die "--bot is only valid with 'ack'"; bot="${1#--bot=}"
             [ -n "$bot" ] || die "--bot requires a non-empty value (coderabbit|copilot|codoki|auto)"; shift ;;
    -*) die "unknown flag '${1}'" ;;
    *)
      if [ -z "$pr" ]; then pr="$1"
      elif [ -z "$repo" ]; then repo="$1"
      else die "unexpected extra argument '${1}'"
      fi
      shift ;;
  esac
done

is_num "$pr" || die "pr must be numeric (got: '${pr}')"
if [ -n "$react" ]; then
  case "$react" in
    +1|-1) ;;
    *) die "--react must be '+1' or '-1' (got: '${react}')" ;;
  esac
fi
if [ "$sub" = ack ]; then
  # An EXPLICIT empty --react is a usage error under `ack` (it would otherwise
  # silently become the +1 default); codoki-ack keeps empty = READ mode.
  [ -z "$react_empty_set" ] || die "--react requires a non-empty value (+1 or -1) with 'ack'"
  [ -n "$react" ] || react="+1"
  case "${bot:=auto}" in
    coderabbit|copilot|codoki|auto) ;;
    *) die "--bot must be coderabbit|copilot|codoki|auto (got: '${bot}')" ;;
  esac
fi
if [ -z "$repo" ]; then
  repo="$(resolve_repo)"
else
  validate_repo "$repo"
fi

# --- Resolve the LATEST Codoki summary comment ------------------------------
# Fetch issue-level comments (a GET; --paginate merges pages into one JSON array).
# A gh failure here is a hard, LOUD error (never a silent skip).
issue_comments="$(gh api "repos/${repo}/issues/${pr}/comments" --paginate)" \
  || die "could not fetch issue comments for PR #${pr} (${repo}) -- ack state UNVERIFIABLE (LOUD failure, not 'n/a')"

# Resolve the Codoki review-SUMMARY comment, identifying it (in order):
#   1. the `<!-- CODOKI_REVIEW_COMMENT -->` marker (Codoki's stable summary id), then
#   2. a summary-body heuristic (the `### Codoki PR Review` header) if the marker
#      format ever drifts.
# It does NOT blindly fall back to "any Codoki comment": Codoki posts MULTIPLE
# issue-level comments (the summary PLUS others), so picking the latest author-login
# match could select a NON-summary comment that happens to carry a stray reaction and
# report `acked` while the real summary is unacked -- a false-PASS into the merge
# oracle, the exact class #234 exists to prevent (#234 hostile-review MEDIUM). When no
# comment matches the marker OR the header heuristic, we refuse to guess: `summary_id`
# is empty -> READ reports no-summary -> the ack gate PASSes (genuinely no summary to
# ack), and if Codoki comments DO exist a diagnostic is emitted (possible format drift).
# Among matches the LATEST by created_at wins.
codoki_summary_id() {
  # SLURP + flatten (.[][]): `--paginate` emits one JSON array PER PAGE, so a per-page
  # resolve printed one id per page with a summary, and the newline-joined result failed
  # is_num -- the target was never acked. One pass over every page picks ONE latest id.
  jq -rs --arg login "$CODOKI_LOGIN" --arg marker "$CODOKI_MARKER" '
  [ .[][] | select((.user.login // "") == $login) ] as $all
  | ([ $all[] | select((.body // "") | contains($marker)) ]) as $marked
  | ([ $all[] | select((.body // "") | test("Codoki PR Review"; "i")) ]) as $heuristic
  | (if ($marked | length) > 0 then $marked
     elif ($heuristic | length) > 0 then $heuristic
     else [] end)
  | sort_by(.created_at) | last | .id // empty
' <<<"$issue_comments" 2>/dev/null
}
summary_id="$(codoki_summary_id)" \
  || die "could not parse issue comments for PR #${pr} (${repo}) -- ack state UNVERIFIABLE"

if [ "$sub" = ack ]; then
  ack_rc=0; ack_main || ack_rc=$?
  exit "$ack_rc"
fi

# Loud diagnostic if Codoki commented but no comment is a recognized summary (marker
# AND header both absent -- Codoki's format may have changed). Not a block: genuinely
# no summary to ack, but surface the drift rather than silently pass.
if [ -z "$summary_id" ]; then
  _codoki_n="$(jq -r --arg login "$CODOKI_LOGIN" \
    '[ .[] | select((.user.login // "") == $login) ] | length' <<<"$issue_comments" 2>/dev/null || echo 0)"
  if [ "${_codoki_n:-0}" -gt 0 ]; then
    echo "gh-react: WARNING: ${_codoki_n} Codoki comment(s) on PR #${pr} but none is a recognized review summary (marker '<!-- CODOKI_REVIEW_COMMENT -->' and '### Codoki PR Review' header both absent -- Codoki format may have changed); treating as no-summary." >&2
  fi
fi

summary_created="$(jq -r --argjson id "${summary_id:-null}" '
  [ .[] | select(.id == $id) ] | (.[0].created_at // "")
' <<<"$issue_comments" 2>/dev/null || true)"

# --- POST mode: actuate the reaction ----------------------------------------
if [ -n "$react" ]; then
  [ -n "$summary_id" ] \
    || die "no Codoki summary comment found on PR #${pr} (${repo}) -- cannot post an ack to a nonexistent summary (LOUD failure)"
  is_num "$summary_id" || die "resolved a non-numeric summary id ('${summary_id}') -- refusing to POST"
  echo "gh-react: posting reaction '${react}' to Codoki summary comment ${summary_id} on PR #${pr} (${repo})" >&2
  if [ "$react" = "-1" ]; then
    echo "gh-react: NOTE a -1 (rebut) also requires an @codoki reply comment -- post it via gh-comment.sh post ${pr} '@codoki ...'" >&2
  fi
  # Endpoint fixed + numeric id only; content rides as DATA via -f.
  exec gh api -X POST "repos/${repo}/issues/comments/${summary_id}/reactions" -f "content=${react}"
fi

# --- READ mode: report the ack state for the oracle -------------------------
if [ -z "$summary_id" ]; then
  echo "CODOKI-ACK: no-summary -- no Codoki review-summary comment on PR #${pr} (${repo}); ack gate PASSES"
  exit 0
fi
is_num "$summary_id" || die "resolved a non-numeric summary id ('${summary_id}')"

reactions="$(gh api "repos/${repo}/issues/comments/${summary_id}/reactions" --paginate)" \
  || die "could not read reactions on Codoki summary ${summary_id} (PR #${pr} ${repo}) -- ack state UNVERIFIABLE (LOUD failure)"

# A NON-BOT reaction is any +1/-1 whose reacting login does NOT end in "[bot]".
# A jq error here means malformed reactions JSON -> die LOUDLY (the module's
# tool-failure contract), NOT a silent degrade to 0 that would read as unacked.
nonbot_plus="$(jq -r '[ .[] | select(.content == "+1") | select(((.user.login // "") | endswith("[bot]")) | not) ] | length' <<<"$reactions")" \
  || die "could not parse reactions on Codoki summary ${summary_id} -- ack state UNVERIFIABLE"
nonbot_minus="$(jq -r '[ .[] | select(.content == "-1") | select(((.user.login // "") | endswith("[bot]")) | not) ] | length' <<<"$reactions")" \
  || die "could not parse reactions on Codoki summary ${summary_id} -- ack state UNVERIFIABLE"

# An @codoki reply = a NON-BOT issue comment mentioning @codoki, posted at/after
# the summary. Required to satisfy a -1 (rebut). A jq error here dies loudly too.
codoki_reply="$(jq -r --arg since "$summary_created" '
  [ .[]
    | select(((.user.login // "") | endswith("[bot]")) | not)
    | select((.body // "") | test("@codoki"; "i"))
    | select($since == "" or (.created_at >= $since)) ] | length
' <<<"$issue_comments")" \
  || die "could not parse issue comments for the @codoki-reply check -- ack state UNVERIFIABLE"

if [ "${nonbot_plus:-0}" -gt 0 ]; then
  echo "CODOKI-ACK: acked -- non-bot 👍 on Codoki summary ${summary_id} (PR #${pr} ${repo})"
  exit 0
fi
if [ "${nonbot_minus:-0}" -gt 0 ]; then
  if [ "${codoki_reply:-0}" -gt 0 ]; then
    echo "CODOKI-ACK: acked -- non-bot 👎 (rebut) on Codoki summary ${summary_id} WITH an @codoki reply (PR #${pr} ${repo})"
    exit 0
  fi
  echo "CODOKI-ACK: unacked -- non-bot 👎 on Codoki summary ${summary_id} but NO @codoki reply comment (PR #${pr} ${repo}); a rebut needs an @codoki reply"
  exit 0
fi
echo "CODOKI-ACK: unacked -- Codoki summary ${summary_id} carries no non-bot 👍/👎 reaction (PR #${pr} ${repo}); react via gh-react.sh codoki-ack ${pr} --react +1|-1"
exit 0
