#!/usr/bin/env bash
# elmer-tick.sh -- THE ONE WRITER of the unattended review requester ("elmer").
#
# It services the queue that `elmer-enqueue.sh` fills: pick the head entry, verify it
# is still valid, post ONE `@coderabbitai review`, and drain it. Nothing else in the
# system ever posts a review trigger.
#
# AUTHORITY. Triggering a CodeRabbit review is normally the maintainer's EXCLUSIVE
# purview (~/.claude/CLAUDE.md). This script is the ONE carve-out, approved
# 2026-07-30, and it is NOT agent trigger authority: it is the maintainer's own
# trigger, mechanized, with the TIMING delegated to a script instead of to judgment.
# Every bound in that carve-out is enforced HERE, in code, not by the caller's
# discretion -- one writer, queue-derived only, incremental-only, an hourly cap, and
# silence on any unrecognized state. Read the carve-out before changing this file;
# widening any bound below means amending CLAUDE.md first, not after.
#
# ONE WRITER, PHYSICALLY. The design puts the queuer and the poster in different
# PROCESSES: TLs enqueue via /orchestrate:request-review (a surface with no posting
# verb at all), and only this script posts. That leaves exactly one way to get two
# writers -- two `/elmer-loop` windows open at once, which is a single keystroke away
# -- so this script takes an flock on the queue root and exits QUIETLY (0) when
# another tick holds it. Quietly, because a second window is a normal thing for a
# human to do, not an error to report.
#
# Exit codes:
#   0  Did its job, INCLUDING the no-op cases: nothing queued, throttled by CR, the
#      hourly cap is spent, or another tick holds the lock. A loop calling this on a
#      timer must not treat "nothing to do" as failure.
#   1  POSTED NOTHING because an entry was refused (stale SHA, PR closed, a review
#      already exists at this head). The entry stays queued and the reason is surfaced.
#   2  SETUP ERROR -- bad args, unresolvable repo, missing dependency, or a gh read
#      failure. NEVER reported as "nothing to do": a read failure that reads as an
#      all-clear is how an unattended loop goes silently wrong.
#
# Env:
#   ELMER_HOME       maildir root (default ~/.claude/elmer). Set by the harness.
#   ELMER_MAX_PER_HR hard posts-per-hour cap (default 4). See the cap section.
#   ELMER_DRY_RUN=1  do everything except the post; print the exact command instead.
set -euo pipefail

# -h / --help: print this script's header comment block as usage, then exit.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

if [ "$#" -gt 0 ]; then
  echo "usage: elmer-tick.sh   (no arguments; it services the queue)" >&2
  exit 2
fi

# Resolve this script's own directory so the sibling cr-quota-watch.sh is found
# whether elmer-tick.sh runs from the repo or from the deployed ~/.claude/scripts path.
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

ELMER_HOME="${ELMER_HOME:-$HOME/.claude/elmer}"
inbox="$ELMER_HOME/inbox"
drained="$ELMER_HOME/drained"
mkdir -p "$inbox" "$drained"

# --- The lock: one writer, enforced ------------------------------------------------
# flock(1) is not present on stock macOS, so a portable mkdir-based lock is used
# instead: mkdir is atomic on every filesystem that matters here, and unlike a
# lockfile+PID scheme it needs no liveness probe to be CORRECT -- only to be
# self-healing, which the stale-age check below provides.
#
# The lock is released by a trap on EXIT. A tick that dies without running its trap
# (SIGKILL, power loss) leaves the directory behind, which would wedge the loop
# permanently -- so a lock older than the stale threshold is broken and reclaimed.
# The threshold is generous relative to a tick's real duration (a few gh calls),
# because breaking a LIVE tick's lock is far worse than waiting one extra cycle.
LOCK_DIR="$ELMER_HOME/.tick.lock"
LOCK_STALE_SECS="${ELMER_LOCK_STALE_SECS:-900}"

lock_age_secs() {
  # Portable mtime read: BSD stat (macOS) then GNU stat (Linux). An unreadable
  # mtime returns empty, and the caller treats that as NOT stale -- refusing to
  # break a lock it cannot reason about.
  local t now
  t="$(stat -f %m "$LOCK_DIR" 2>/dev/null || stat -c %Y "$LOCK_DIR" 2>/dev/null || true)"
  [ -n "$t" ] || return 1
  now="$(date +%s)"
  echo $(( now - t ))
}

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    return 0
  fi
  local age
  if age="$(lock_age_secs)" && [ "$age" -gt "$LOCK_STALE_SECS" ]; then
    echo "note: breaking a stale tick lock (${age}s old, threshold ${LOCK_STALE_SECS}s)" >&2
    rmdir "$LOCK_DIR" 2>/dev/null || true
    mkdir "$LOCK_DIR" 2>/dev/null || return 1
    return 0
  fi
  return 1
}

if ! acquire_lock; then
  # QUIET success: another tick is working, which is the designed outcome of a second
  # loop window, not a fault.
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# --- The hourly cap: a bound that does not consult the queue -----------------------
# The carve-out requires a hard posts-per-hour cap INDEPENDENT of queue depth. That
# independence is the point: every other guard in this script reasons about whether a
# PARTICULAR entry deserves a post, so a bug in that reasoning is unbounded. The cap
# bounds the blast radius of ANY such bug, including one not yet written.
#
# The count comes from the drained/ records, which are the audit trail of what was
# actually posted -- not from a counter file that could drift from reality. Each
# drained entry carries `triggered_at`; the cap counts those inside the trailing hour.
#
# Reading a drained entry that is malformed or missing its timestamp counts it AS a
# recent post (see the || echo below). That is deliberate: an unreadable record must
# never buy an extra post, and the conservative direction here is to post less.
ELMER_MAX_PER_HR="${ELMER_MAX_PER_HR:-4}"

posts_last_hour() {
  local cutoff n=0 f ts
  cutoff="$(( $(date +%s) - 3600 ))"
  # A glob with no matches expands to the literal pattern under default shell
  # options, so the -e guard is what makes an empty drained/ read as zero.
  for f in "$drained"/*.json; do
    [ -e "$f" ] || continue
    ts="$(jq -r '.triggered_at // empty' "$f" 2>/dev/null || true)"
    if [ -z "$ts" ]; then
      # Unreadable/malformed: count it, never discount it.
      n=$(( n + 1 )); continue
    fi
    # A timestamp that will not parse is likewise counted, not skipped.
    local epoch
    epoch="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null \
             || date -u -d "$ts" +%s 2>/dev/null || echo "")"
    if [ -z "$epoch" ] || [ "$epoch" -ge "$cutoff" ]; then
      n=$(( n + 1 ))
    fi
  done
  echo "$n"
}

recent="$(posts_last_hour)"
if [ "$recent" -ge "$ELMER_MAX_PER_HR" ]; then
  echo "elmer-tick: hourly cap reached ($recent/$ELMER_MAX_PER_HR posts in the last hour); nothing posted."
  exit 0
fi

# --- Pick the head entry: stillwater first, then FIFO ------------------------------
# Queue policy from the design. stillwater outranks everything because it is the
# repo whose reviews the maintainer actually waits on; within a tier, oldest first,
# so nothing starves behind a busy repo.
#
# Only ONE entry is selected per tick, and that is a correctness requirement rather
# than throttling: CR publishes a countdown only once its limit is ALREADY reached,
# never a remaining-slot count, so "no announced limit" means EITHER plenty of budget
# OR one review from the wall. Posting a batch on a single all-clear reading would
# blow past the wall unseen. Post one, then re-read the signal.
pick_entry() {
  local f best="" best_at="" repo at
  for f in "$inbox"/*.json; do
    [ -e "$f" ] || continue
    repo="$(jq -r '.repo // ""' "$f" 2>/dev/null || true)"
    at="$(jq -r '.enqueued_at // ""' "$f" 2>/dev/null || true)"
    # An entry we cannot read is left alone rather than guessed at: it will be
    # surfaced by the triage/audit path, not silently posted for.
    [ -n "$repo" ] && [ -n "$at" ] || continue
    # Sort key puts the priority repo ahead of everything, then orders by ISO
    # timestamp, which sorts lexically because it is fixed-width UTC.
    case "$repo" in
      */stillwater) at="0 $at" ;;
      *)            at="1 $at" ;;
    esac
    if [ -z "$best" ] || [[ "$at" < "$best_at" ]]; then
      best="$f"; best_at="$at"
    fi
  done
  [ -n "$best" ] || return 1
  printf '%s\n' "$best"
}

if ! entry="$(pick_entry)"; then
  echo "elmer-tick: queue empty; nothing to do."
  exit 0
fi

e_repo="$(jq -r '.repo' "$entry")"
e_pr="$(jq -r '.pr' "$entry")"
e_sha="$(jq -r '.commit_sha' "$entry")"
e_form="$(jq -r '.form // "incremental"' "$entry")"

# --- Refuse `full` outright, whatever the entry says -------------------------------
# The carve-out permits the incremental form ONLY. This is checked HERE, at the
# posting site, and not merely at enqueue: a hand-edited queue entry is exactly the
# path that would otherwise smuggle a `full review` past the gate, and a full review
# re-surfaces resolved threads and owes a human decision.
if [ "$e_form" != "incremental" ]; then
  echo "REFUSED: entry requests form '$e_form'; only 'incremental' is permitted." >&2
  echo "         $entry" >&2
  exit 1
fi

# --- The PR must still be open, and still at the gated SHA -------------------------
# A gh read failure is a SETUP ERROR (exit 2), never a refusal and never a silent
# skip: an unattended loop that treats an unreadable PR as "nothing to do" goes wrong
# quietly, which is the failure mode this whole design is built to avoid.
if ! pr_json="$(gh pr view "$e_pr" --repo "$e_repo" --json headRefOid,state 2>/dev/null)"; then
  echo "setup error: could not read PR #$e_pr ($e_repo). Not posting; entry stays queued." >&2
  exit 2
fi
pr_state="$(jq -r '.state' <<<"$pr_json")"
pr_head="$(jq -r '.headRefOid' <<<"$pr_json")"

if [ "$pr_state" != "OPEN" ]; then
  echo "REFUSED: PR #$e_pr ($e_repo) is $pr_state; a closed PR is never reviewed." >&2
  exit 1
fi

# THE STALENESS GATE. The entry was admitted because a gate receipt matched the head
# at enqueue time; if the head has moved since, that receipt no longer describes what
# a review would read. Posting anyway would review code that never passed the gate --
# precisely what the receipt exists to prevent. Refuse and surface; the TL re-runs
# /prep-pr on the new head. This is verify-do-not-classify (#337): compare the fact,
# do not infer intent.
if [ "$pr_head" != "$e_sha" ]; then
  echo "REFUSED: PR #$e_pr ($e_repo) has moved since it was gated." >&2
  echo "         queued: ${e_sha:0:12}   current: ${pr_head:0:12}" >&2
  echo "         Re-run /prep-pr on the new head to re-queue. Entry left in place." >&2
  exit 1
fi

# --- Is CR throttled right now? ----------------------------------------------------
# Ask BEFORE posting. Whether a trigger posted DURING a limit is harmless (CR ignores
# it) or costly (burns the slot without reviewing) is UNMEASURED, so the conservative
# order is the one that cannot be wrong: query first, post only on a clear reading.
#
# cr-quota-watch.sh exit contract: 0 = no active limit, 1 = limited, 2 = setup error.
# The 2 case must NOT fall through to a post -- a failed read is not an all-clear.
quota_rc=0
if [ -x "$SELF_DIR/cr-quota-watch.sh" ]; then
  "$SELF_DIR/cr-quota-watch.sh" "$e_pr" "$e_repo" >/dev/null 2>&1 || quota_rc=$?
else
  echo "setup error: cr-quota-watch.sh not found beside $0; refusing to post blind." >&2
  exit 2
fi
case "$quota_rc" in
  0) : ;;
  1) echo "elmer-tick: CodeRabbit is throttled; nothing posted. Entry stays queued."
     exit 0 ;;
  *) echo "setup error: quota read failed (rc=$quota_rc). Not posting." >&2
     exit 2 ;;
esac

# --- Has CR already reviewed this exact head? --------------------------------------
# Cheap insurance against a double-post that the drain record cannot catch: a review
# triggered by the MAINTAINER by hand leaves no queue entry, so only GitHub knows
# about it. Note this check is sound in the direction it is used -- it can only
# SUPPRESS a post, never authorize one -- which is why the racy inverse (asking
# GitHub "has a review happened" as the idempotency mechanism) is still not used.
if revs="$(gh pr view "$e_pr" --repo "$e_repo" --json reviews 2>/dev/null)"; then
  if [ "$(jq -r --arg s "$e_sha" \
        '[.reviews[]? | select(.commit_id == $s)] | length' <<<"$revs" 2>/dev/null || echo 0)" -gt 0 ]; then
    echo "REFUSED: a review already exists at ${e_sha:0:12}; not spending a slot." >&2
    exit 1
  fi
fi

# --- THE POST ----------------------------------------------------------------------
# The only outward write in the entire elmer system. The trigger string is a FIXED
# literal, never composed from entry data: an entry is queue data, and letting it
# reach the comment body is how a hand-edited entry would post something other than
# an incremental review.
TRIGGER='@coderabbitai review'

if [ "${ELMER_DRY_RUN:-0}" = "1" ]; then
  echo "DRY RUN: would post to $e_repo #$e_pr at ${e_sha:0:12}:"
  echo "         $TRIGGER"
  exit 0
fi

if ! post_out="$(gh pr comment "$e_pr" --repo "$e_repo" --body "$TRIGGER" 2>&1)"; then
  echo "setup error: the post failed; entry stays queued for the next tick." >&2
  printf '%s\n' "$post_out" >&2
  exit 2
fi

# --- Drain: the record IS the idempotency mechanism ---------------------------------
# Written the INSTANT the post succeeds, closing the double-post window immediately.
# Asking GitHub "has a review happened yet" would be racy in the dangerous direction:
# CR takes minutes to post, so a tick 30 seconds later would see nothing and fire
# again. Anything in drained/ is never re-posted, and the directory doubles as the
# permanent audit trail the carve-out requires.
#
# A drain failure after a SUCCESSFUL post is the one genuinely bad state here (the
# next tick could re-post), so it is loud and exits 2 rather than pretending success.
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
base="$(basename "$entry")"
tmp="$drained/.$base.tmp.$$"
if jq --arg t "$now" --arg out "$post_out" \
     '. + {triggered_at: $t, trigger: "'"$TRIGGER"'", response: $out}' \
     "$entry" > "$tmp" 2>/dev/null && mv -f "$tmp" "$drained/$base"; then
  rm -f "$entry"
  echo "elmer-tick: POSTED an incremental review request -- $e_repo #$e_pr at ${e_sha:0:12}"
  echo "            drained: $drained/$base"
  exit 0
fi
rm -f "$tmp"
echo "setup error: POSTED but could not write the drain record for $entry." >&2
echo "             Move or delete it by hand before the next tick, or it may re-post." >&2
exit 2
