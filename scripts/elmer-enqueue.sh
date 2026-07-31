#!/usr/bin/env bash
# elmer-enqueue.sh <PR#> [owner/repo] --receipt <path> [--form incremental]
#
# The RECEIPT-GATED queue writer for the unattended review requester ("elmer").
# A TL files a review request here; a SEPARATE single writer (elmer-tick.sh) is the
# only thing that ever posts a trigger. This script NEVER posts, never mutates
# GitHub, and never touches the CodeRabbit trigger surface.
#
# WHY THE RECEIPT GATE EXISTS: a TL must not be able to queue a review for code that
# has not passed /prep-pr. That requirement is enforced MECHANICALLY, not on the
# honor system, by a string compare:
#
#   the receipt's `commit_sha`  ==  the PR's CURRENT headRefOid
#
# `gate-runner.py --receipt <path>` writes a schema-validated `gate-receipt/v1`
# ({commit_sha, tree_sha, worktree, result, steps[], producer}) as a byproduct of a
# real gate run. A TL who ran /prep-pr and then pushed two more commits holds a
# receipt whose commit_sha no longer matches HEAD, so this REFUSES with a pointer
# back to /prep-pr. This is the house "VERIFY, DO NOT CLASSIFY" pattern (#337):
# check the FACT, never the CLAIM ("I ran prep-pr").
#
# Threat model matches the deterministic floor: an honest TL on the obvious path,
# NOT an adversary. A hand-forged receipt defeats this, and is out of scope --
# which is why `producer` must be `gate-runner` and the schema id must match: those
# reject an ACCIDENTALLY hand-rolled or wrong-tool artifact, not a determined forger.
#
# IDEMPOTENCY is by construction: an entry lives in inbox/ until triggered, then
# moves to drained/. A PR+SHA present in EITHER directory is never queued again.
# Checking GitHub for "has a review already happened" would be RACY (CR takes
# minutes to post, so a tick 30s later sees nothing and fires twice); the drain
# record closes that window the instant the post succeeds.
#
# TRIGGER FORM: `incremental` only. `full` is REFUSED outright regardless of what
# the caller asks for -- it re-surfaces resolved threads and owes a human decision,
# so it is never something an unattended loop may request.
#
# Usage:
#   elmer-enqueue.sh <PR#> [owner/repo] --receipt <path> [--form incremental]
#
# Exit codes:
#   0  ENQUEUED   - entry written to $ELMER_HOME/inbox/
#   1  REFUSED    - the gate said no (missing/invalid/failing/stale receipt, a
#                   closed or merged PR, a `full` form, or an already-queued
#                   PR+SHA). A refusal NEVER leaves a partial entry behind and is
#                   never silent.
#   2  SETUP ERROR- bad args, unresolvable repo, or a gh read failure. A read
#                   failure is NEVER treated as "fine, enqueue it": that would be
#                   the one wrong answer that matters here.
#
# Env: ELMER_HOME overrides the maildir root (default ~/.claude/elmer). Used by the
# harness so a test never touches the real queue.
set -euo pipefail

case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

pr=""; repo=""; receipt_path=""; form="incremental"
while [ "$#" -gt 0 ]; do
  case "$1" in
    # The `|| true` on a `shift 2` is a TRAP, not a safety net. With only one argument
    # left (a trailing `--receipt`), `shift 2` FAILS, the `|| true` swallows it, `$#`
    # never decreases, and this loop spins forever at 100% CPU with no output. An agent
    # that fumbles the trailing argument gets a Bash call that never returns - the same
    # class of harm as a permission prompt (a silent unattended stall), by a different
    # cause. Measured: rc=137 after a 4s SIGKILL on three separate arg shapes.
    # So the value is REQUIRED to exist before shifting past it.
    --receipt)
      [ "$#" -ge 2 ] || { echo "setup error: --receipt requires a path" >&2; exit 2; }
      receipt_path="$2"; shift 2 ;;
    --form)
      [ "$#" -ge 2 ] || { echo "setup error: --form requires a value" >&2; exit 2; }
      form="$2"; shift 2 ;;
    -*) echo "setup error: unknown flag: $1" >&2; exit 2 ;;
    */*) repo="$1"; shift ;;
    *) if [ -z "$pr" ]; then pr="$1"; else echo "setup error: unexpected argument: $1" >&2; exit 2; fi; shift ;;
  esac
done

if [ -z "$pr" ]; then
  echo "usage: elmer-enqueue.sh <PR#> [owner/repo] --receipt <path> [--form incremental]" >&2
  exit 2
fi
if ! [[ "$pr" =~ ^[0-9]+$ ]]; then
  echo "setup error: PR# must be numeric, got: $pr" >&2
  exit 2
fi
if [ -z "$receipt_path" ]; then
  echo "setup error: --receipt <path> is required (run /prep-pr to produce one)" >&2
  exit 2
fi

# `full` is refused BEFORE any other work: it is never enqueueable, so there is no
# point resolving a repo or reading a receipt for it.
if [ "$form" != "incremental" ]; then
  echo "REFUSED: trigger form '$form' is not enqueueable. Only 'incremental' is allowed;" >&2
  echo "         a full review re-surfaces resolved threads and is the maintainer's call." >&2
  exit 1
fi

if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
fi
if [ -z "$repo" ]; then
  echo "setup error: could not resolve repo (pass owner/repo, or run inside a gh-aware repo)" >&2
  exit 2
fi

command -v jq >/dev/null 2>&1 || { echo "setup error: jq is required" >&2; exit 2; }

# --- Read the receipt (local file; no network) ------------------------------
if [ ! -f "$receipt_path" ]; then
  echo "REFUSED: no gate receipt at $receipt_path." >&2
  echo "         Run /prep-pr (which writes one) before enqueuing a review request." >&2
  exit 1
fi

r_schema="$(jq -r '.schema // ""' "$receipt_path" 2>/dev/null || true)"
if [ -z "$r_schema" ]; then
  echo "REFUSED: gate receipt at $receipt_path is not readable JSON." >&2
  exit 1
fi
if [ "$r_schema" != "gate-receipt/v1" ]; then
  echo "REFUSED: gate receipt schema is '$r_schema', expected 'gate-receipt/v1'." >&2
  exit 1
fi

r_commit="$(jq -r '.commit_sha // ""' "$receipt_path" 2>/dev/null || true)"
r_result="$(jq -r '.result // ""' "$receipt_path" 2>/dev/null || true)"
r_producer="$(jq -r '.producer // ""' "$receipt_path" 2>/dev/null || true)"

if [ -z "$r_commit" ] || [ -z "$r_result" ]; then
  echo "REFUSED: gate receipt is missing commit_sha and/or result." >&2
  exit 1
fi
# The producer check rejects a hand-rolled or wrong-tool artifact. It is NOT an
# anti-forgery control (see the threat model in the header).
if [ "$r_producer" != "gate-runner" ]; then
  echo "REFUSED: gate receipt producer is '$r_producer', expected 'gate-runner'." >&2
  exit 1
fi
if [ "$r_result" != "pass" ]; then
  echo "REFUSED: the gate did not pass (receipt result='$r_result'). Fix the gate, re-run /prep-pr." >&2
  exit 1
fi

# --- Read the PR's CURRENT head (the fact the gate is checked against) -------
# gh's status is captured BEFORE the jq pipe: piping a failed gh into jq would
# yield empty fields that could read as a benign state. A read failure is exit 2.
pr_raw="$(gh pr view "$pr" --repo "$repo" --json headRefOid,state,number 2>/dev/null)" || {
  echo "setup error: could not read PR #$pr ($repo) (gh read failed)" >&2
  exit 2
}
head_sha="$(printf '%s' "$pr_raw" | jq -r '.headRefOid // ""' 2>/dev/null || true)"
pr_state="$(printf '%s' "$pr_raw" | jq -r '.state // ""' 2>/dev/null || true)"
if [ -z "$head_sha" ] || [ -z "$pr_state" ]; then
  echo "setup error: could not parse PR #$pr ($repo) head/state" >&2
  exit 2
fi

if [ "$pr_state" != "OPEN" ]; then
  echo "REFUSED: PR #$pr ($repo) is $pr_state; a review request is only meaningful on an open PR." >&2
  exit 1
fi

# THE GATE. Everything above is preamble; this is the check that makes the
# receipt load-bearing rather than decorative.
if [ "$r_commit" != "$head_sha" ]; then
  echo "REFUSED: STALE gate receipt for PR #$pr ($repo)." >&2
  echo "         gated commit: $r_commit" >&2
  echo "         PR head now:  $head_sha" >&2
  echo "         The branch moved after the gate ran. Re-run /prep-pr on the current HEAD." >&2
  exit 1
fi

# --- Idempotency: PR+SHA present in inbox/ OR drained/ is never re-queued -----
ELMER_HOME="${ELMER_HOME:-$HOME/.claude/elmer}"
inbox="$ELMER_HOME/inbox"
drained="$ELMER_HOME/drained"
mkdir -p "$inbox" "$drained"

slug="$(printf '%s' "$repo" | tr '/' '-')"
entry="${slug}--${pr}--${r_commit:0:12}.json"

if [ -e "$inbox/$entry" ]; then
  echo "REFUSED: PR #$pr ($repo) at ${r_commit:0:12} is ALREADY QUEUED ($inbox/$entry)." >&2
  exit 1
fi
if [ -e "$drained/$entry" ]; then
  echo "REFUSED: PR #$pr ($repo) at ${r_commit:0:12} was ALREADY TRIGGERED (see $drained/$entry)." >&2
  echo "         A drained entry is never re-posted. Push a change and re-run /prep-pr for a new request." >&2
  exit 1
fi

# --- Write the entry (atomic; a partial file must never be visible to the tick) ---
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
tmp="$inbox/.$entry.tmp.$$"
if ! jq -n \
    --arg repo "$repo" --arg pr "$pr" --arg sha "$r_commit" \
    --arg form "$form" --arg receipt "$receipt_path" --arg at "$now" \
    --arg by "${USER:-unknown}" \
    '{repo: $repo, pr: ($pr|tonumber), commit_sha: $sha, form: $form,
      receipt: $receipt, enqueued_at: $at, enqueued_by: $by}' > "$tmp" 2>/dev/null; then
  rm -f "$tmp"
  echo "setup error: could not write queue entry to $inbox" >&2
  exit 2
fi
mv -f "$tmp" "$inbox/$entry"

echo "ENQUEUED: PR #$pr ($repo) at ${r_commit:0:12} for an incremental CodeRabbit review."
echo "          entry: $inbox/$entry"
echo "          (this script posts nothing; elmer-tick.sh is the only writer)"
exit 0
