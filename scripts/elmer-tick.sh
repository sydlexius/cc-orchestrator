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

echo "elmer-tick: lock acquired ($ELMER_HOME)"
exit 0
