#!/usr/bin/env bash
# gh-react.sh codoki-ack <pr> [owner/repo] [--react +1|-1]   (issue #234)
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
# (list issue comments, read a comment's reactions) and the SINGLE reactions POST
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

sub="${1:-}"
[ -n "$sub" ] || die "usage: gh-react.sh codoki-ack <pr> [owner/repo] [--react +1|-1]"
shift

case "$sub" in
  codoki-ack) ;;
  *) die "unknown subcommand '${sub}' (only 'codoki-ack' is supported)" ;;
esac

pr=""
repo=""
react=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --react) [ "$#" -ge 2 ] || die "--react requires a value (+1 or -1)"; react="$2"; shift 2 ;;
    --react=*) react="${1#--react=}"; shift ;;
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
summary_id="$(jq -r --arg login "$CODOKI_LOGIN" --arg marker "$CODOKI_MARKER" '
  [ .[] | select((.user.login // "") == $login) ] as $all
  | ([ $all[] | select((.body // "") | contains($marker)) ]) as $marked
  | ([ $all[] | select((.body // "") | test("Codoki PR Review"; "i")) ]) as $heuristic
  | (if ($marked | length) > 0 then $marked
     elif ($heuristic | length) > 0 then $heuristic
     else [] end)
  | sort_by(.created_at) | last | .id // empty
' <<<"$issue_comments" 2>/dev/null)" \
  || die "could not parse issue comments for PR #${pr} (${repo}) -- ack state UNVERIFIABLE"

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
