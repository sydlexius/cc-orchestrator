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

# EVERY WRITE IN THIS FILE IS GUARDED, AND THE GUARD IS THE POINT ------------------
# The class, stated once here so no future write site has to rediscover it: under
# `set -e` a bare `echo`/`printf`/`sed`/`awk` onto a CLOSED or BROKEN fd (EBADF/EIO -
# a timer loop redirecting into a rotated or closed log) FAILS, and that failure
# becomes the script's exit status, OVERRIDING the code the code below deliberately
# intended. The damage is directional and always bad: a contractually-0 no-op and a
# deliberate exit 2 both DOWNGRADE to 1, and 1 is the one code the contract reserves
# for "POSTED NOTHING because an entry was REFUSED" - so the runbook sends the
# operator to hand an entry back to the TL that nothing ever refused.
#
# THE TEMPLATE, applied at EVERY write site below without exception:
#     { echo "..."; echo "..."; } >&2 || true
#     exit 2
# Group the writes, redirect ONCE, `|| true` so no write can change the exit status,
# and put the `exit` on its own line AFTER the guard so it carries the intended code
# whether or not the write landed. `set -e` is suspended inside a group that is the
# left operand of `||`, which is what makes the guard cover every command in the body
# and not just the last one.
#
# THE GUARD IS NOT A MUTE. `{ ... } >&2 || true` is one careless edit away from
# `{ ... } >/dev/null`, and BOTH satisfy every exit-code assertion while the second
# silently discards the report. The output must still be WRITTEN when the fd is fine;
# the harness asserts that separately at both ends (see the over-hardening cases).
#
# Three successive fix rounds each guarded ONE site and each left another unguarded,
# because the file was being fixed instance-by-instance. This is the whole-file sweep:
# the property to preserve is not "these lines are guarded" but "NO write anywhere in
# this file can change the exit status".
#
# TWO KINDS OF WRITE ARE DELIBERATELY NOT GUARDED, and guarding them would be a
# defect, not a completion of the sweep:
#
# 1. A write that is a function's RETURN CHANNEL - `echo $(( now - t ))` in
#    lock_age_secs, `printf '%s\n' "$i"` in lock_inode, `echo "$n"` in
#    posts_last_hour. Those go into a pipe the shell itself creates for `$(...)`;
#    no caller can close that fd, so the EBADF class cannot reach them, and a
#    `|| true` there would convert a genuinely failed computation into a silent
#    empty string that the cap and the lock then act on.
# 2. A write to a file THIS SCRIPT opened - the `>> "$UNREADABLE_LOG"` /
#    `>> "$STALE_LOG"` appends in scan_queue. A failure there is a real fault (a
#    TMPDIR that filled after mktemp succeeded) and must surface, not be swallowed.
#
# The distinguishing question is always the same: can the CALLER have handed this
# script a broken fd for this write? Only then does the guard belong.

# -h / --help: print this script's header comment block as usage, then exit.
# The awk write is guarded like every other: awk exits 2 on a write error, so with
# stdout closed an unguarded `-h` returned 2 - a SETUP ERROR reported for a help
# request that did exactly what it was asked to do.
case "${1:-}" in
  -h|--help) awk 'NR==1{next} /^#/{sub(/^#[[:space:]]?/,""); print; next} {exit}' "$0" || true
             exit 0 ;;
esac

if [ "$#" -gt 0 ]; then
  { echo "usage: elmer-tick.sh   (no arguments; it services the queue)"; } >&2 || true
  exit 2
fi

# Resolve this script's own directory so the sibling cr-quota-watch.sh is found
# whether elmer-tick.sh runs from the repo or from the deployed ~/.claude/scripts path.
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# jq is a HARD dependency, checked up front like elmer-enqueue.sh does. Without this
# every jq read degrades to an empty string behind a `|| true`, so a full queue reads
# as unreadable and the tick prints "queue empty; nothing to do" at exit 0 - a silent
# wrong answer under an environment fault, which is precisely what the exit contract
# above forbids. Fail-safe for POSTING is not the same as fail-safe for REPORTING.
# Probed by RUNNING it, not by `command -v`: a jq that is present but broken (a bad
# install, a wrapper that exits 127) passes a presence check and then fails every
# read. Both failure modes produce the same silent wrong answer, so both are caught.
echo '{}' | jq -e . >/dev/null 2>&1 || {
  { echo "setup error: jq is required and must be working (probe failed)."; } >&2 || true
  exit 2
}

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
# A MALFORMED INTEGER MUST NOT SILENTLY DISABLE THE BOUND IT CONFIGURES. `[ "$age"
# -gt "$LOCK_STALE_SECS" ]` with a non-integer returns 2 and prints "integer
# expression expected", and as an `if` condition a 2 simply reads FALSE - so
# ELMER_LOCK_STALE_SECS=nine would mean NO lock is ever reclaimed and the loop
# wedges forever, with only a stray stderr line to show for it. Validated at
# assignment instead, as a SETUP ERROR (exit 2), because a typo in the environment
# is exactly the "bad args / bad environment" class the contract reserves 2 for.
# `case` rather than a regex so a leading/trailing space is rejected too: `[[ =~
# ^[0-9]+$ ]]` would be fine here but the glob form matches the empty string and
# any non-digit byte in ONE pattern, and is the file's plainest shape.
case "$LOCK_STALE_SECS" in
  ''|*[!0-9]*)
    { echo "setup error: ELMER_LOCK_STALE_SECS must be a non-negative integer, got: '$LOCK_STALE_SECS'"; } >&2 || true
    exit 2 ;;
esac

lock_age_secs() {
  # Portable mtime read: GNU stat (Linux) FIRST, BSD stat (macOS) as the fallback.
  # THE ORDER IS LOAD-BEARING, and it is the house pattern (orchestrate-guard.sh
  # marker_active, orchestrate-steer.sh). GNU's `-f` is --file-system, so on Linux
  # `stat -f %m` SUCCEEDS-WITH-GARBAGE: it prints multi-line filesystem info to
  # STDOUT before the fallback's number is appended, `$( )` captures both, the
  # `[ -n ]` below passes, and the arithmetic dies on the garbage. BSD instead
  # cleanly REJECTS `-c` ("illegal option") with NOTHING on stdout, so it is safe
  # as the fallback and only GNU-first is correct on both platforms. Reproduced:
  # a BSD-first order made stale-lock recovery never fire on a GNU host, wedging
  # the loop permanently and silently. An unreadable mtime returns empty, and the
  # caller treats that as NOT stale -- refusing to break a lock it cannot reason about.
  local t now
  t="$(stat -c %Y "$LOCK_DIR" 2>/dev/null || stat -f %m "$LOCK_DIR" 2>/dev/null || true)"
  [ -n "$t" ] || return 1
  now="$(date +%s)"
  echo $(( now - t ))
}

lock_inode() {
  # The lock's IDENTITY, read the same portable way as its age: GNU stat (Linux)
  # FIRST, BSD stat (macOS) as the fallback -- same reasoning as lock_age_secs
  # above, and for the same reason it is not optional. GNU's `-f` is
  # --file-system and succeeds-with-garbage on stdout, so it can never be the
  # first probe; BSD cleanly REJECTS `-c` with nothing on stdout, so it can be the
  # fallback. An unreadable inode returns nonzero and the caller backs off rather
  # than breaking a lock it cannot identify - the same deny-on-doubt posture
  # lock_age_secs already takes with an unreadable mtime.
  local i
  i="$(stat -c %i "$LOCK_DIR" 2>/dev/null || stat -f %i "$LOCK_DIR" 2>/dev/null || true)"
  [ -n "$i" ] || return 1
  printf '%s\n' "$i"
}

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    return 0
  fi
  # IDENTITY FIRST, AGE SECOND. The inode is captured BEFORE the age is read so the
  # two readings describe the same directory, and re-checked immediately before the
  # break so the thing being broken is still the thing judged stale.
  local ino age now_ino
  ino="$(lock_inode)" || return 1
  if age="$(lock_age_secs)" && [ "$age" -gt "$LOCK_STALE_SECS" ]; then
    # BREAK BY RENAME, NOT BY rmdir+mkdir. The obvious form is TOCTOU-racy: every
    # contending tick independently sees "stale", rmdirs, and mkdirs, and their
    # windows interleave so SEVERAL come away believing they hold the lock. Measured
    # at 8 concurrent ticks against a stale lock: 2-4 posts per trial, where the
    # normal path gives exactly 1 every time. The self-healing path was breaking the
    # very mutual exclusion the lock exists to provide - and a wedged-then-reclaimed
    # lock is EXACTLY when a human opens a second window.
    #
    # `mv` of a directory onto a unique name is atomic on one filesystem and succeeds
    # for exactly ONE racer; every other tick's mv fails because the source is gone,
    # and they fall through to the normal contended path. The winner then mkdirs.
    #
    # ATOMICITY ALONE IS NOT ENOUGH, because mv answers "did I move A directory" and
    # never "did I move THE directory I judged stale". A racer reads the OLD lock's
    # age, is descheduled, and by the time it runs again the winner has already
    # broken the lock and mkdir'd a FRESH one at the same path. The racer's mv then
    # succeeds - against the wrong directory - and it too believes it holds the lock,
    # so two ticks post at once, which is precisely what the lock exists to prevent.
    # Measured with overlapping post intervals: 2-3 of 6 trials at 8 concurrent ticks.
    # The staleness reading offers no protection here: the fresh lock's mtime is NOW,
    # so a racer that re-read the age would correctly see "not stale" - the damage
    # comes from acting on a reading taken before the swap.
    #
    # So the break is conditioned on IDENTITY, not just on atomicity: re-read the
    # inode and refuse unless it is the same directory that was judged stale. An
    # unreadable inode is "cannot verify" and backs off, never breaks.
    now_ino="$(lock_inode)" || return 1
    [ "$now_ino" = "$ino" ] || return 1
    local dead="$LOCK_DIR.dead.$$"
    if mv "$LOCK_DIR" "$dead" 2>/dev/null; then
      { echo "note: breaking a stale tick lock (${age}s old, threshold ${LOCK_STALE_SECS}s)"; } >&2 || true
      rmdir "$dead" 2>/dev/null || true
      mkdir "$LOCK_DIR" 2>/dev/null || return 1
      return 0
    fi
    return 1
  fi
  return 1
}

# --- The single queue pass: pick the head entry AND record what is wrong with it ----
# Queue policy from the design. stillwater outranks everything because it is the repo
# whose reviews the maintainer actually waits on; within a tier, oldest first, so
# nothing starves behind a busy repo.
#
# Only ONE entry is selected per tick, and that is a correctness requirement rather
# than throttling: CR publishes a countdown only once its limit is ALREADY reached,
# never a remaining-slot count, so "no announced limit" means EITHER plenty of budget
# OR one review from the wall. Posting a batch on a single all-clear reading would
# blow past the wall unseen. Post one, then re-read the signal.
#
# ONE PASS, AT MOST ONCE PER RUN. Selection and health reporting read the same fields,
# so they are the same loop: `report_queue_health` runs this scan only if the normal
# path has not already run it, which is what lets the early exits report without
# adding a second jq pass per entry to the posting path.
#
# It runs in the MAIN shell, not a command substitution: a subshell's variables die
# with it, which is why the unreadable list was already being routed through a FILE.
SCANNED=0
PICKED=""

scan_queue() {
  SCANNED=1
  local f base best="" best_at="" repo at pr sha
  : > "$UNREADABLE_LOG"; : > "$STALE_LOG"
  for f in "$inbox"/*.json; do
    [ -e "$f" ] || continue
    # `${f##*/}` NOT `$(basename "$f")`. A basename that fails or yields empty makes
    # the test below read `[ -e "$drained/" ]`, TRUE for the directory itself, so
    # EVERY entry is skipped and the tick prints "queue empty" over a full queue -
    # the same silent wrong answer the jq probe exists to prevent, reintroduced
    # through a second unprobed dependency. Parameter expansion cannot fail, needs no
    # subprocess, and handles spaces/globs/newlines identically.
    base="${f##*/}"
    # ANYTHING ALREADY DRAINED IS NEVER RE-POSTED, and that is checked HERE rather
    # than trusted. The header long claimed this invariant while nothing in the tick
    # ever read `drained/` - the de-dup lived only in elmer-enqueue.sh, a different
    # process at a different time. The invariant then rested entirely on the `rm -f`
    # below always succeeding; an unwritable inbox (permissions drift, a restored
    # backup) leaves the entry in BOTH directories and the next tick posts it again.
    # A claimed invariant that no code enforces is worse than no claim at all.
    #
    # Suppressing the post is necessary but NOT sufficient: such a file is invisible
    # to the unreadable report too (it never reaches the jq read), so it would be
    # surfaced exactly once - by the exit-2 message of the tick that stranded it -
    # and then never again, while every later tick asserted "queue empty". It is
    # therefore RECORDED and reported as a stale inbox file the operator must remove.
    if [ -e "$drained/$base" ]; then
      printf '%s\n' "$f" >> "$STALE_LOG"
      continue
    fi

    repo="$(jq -r '.repo // ""' "$f" 2>/dev/null || true)"
    at="$(jq -r '.enqueued_at // ""' "$f" 2>/dev/null || true)"
    # An entry we cannot read is SKIPPED here and reported by report_queue_health -
    # never guessed at, and never silently invisible either (an earlier comment
    # claimed the triage path surfaced these, which it does not - triage takes PR
    # numbers and never reads the queue).
    # EVERY FIELD THE POSTING PATH LATER READS IS VALIDATED HERE, not just the two
    # the selection ORDER needs. Admitting an entry on `.repo` + `.enqueued_at`
    # alone let a well-formed JSON object missing `.pr` reach the read sites below,
    # where a bare `jq -r '.pr'` yields the literal STRING "null" - and the tick
    # then issued `gh pr comment null`, REPORTED IT AS A SUCCESSFUL POST, and
    # drained the entry. A missing `.commit_sha` is less destructive but permanently
    # wedging: the staleness gate compares against "null" forever, so the entry
    # exits 1 with "queued: null" on every tick and never drains.
    #
    # Routed to UNREADABLE_LOG like every other malformed shape, so a bad entry is
    # REPORTED rather than silently invisible. "null" is rejected explicitly because
    # jq renders a missing key that way and the `// ""` default below cannot tell a
    # JSON null from an absent key either. This is a strict NARROWING - it can only
    # make the tick post LESS - so it cannot weaken the exit contract.
    pr="$(jq -r '.pr // ""' "$f" 2>/dev/null || true)"
    sha="$(jq -r '.commit_sha // ""' "$f" 2>/dev/null || true)"
    if [ -z "$repo" ] || [ -z "$at" ] \
       || [ -z "$pr" ]  || [ "$pr" = "null" ] \
       || [ -z "$sha" ] || [ "$sha" = "null" ]; then
      printf '%s\n' "$f" >> "$UNREADABLE_LOG"
      continue
    fi
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
  PICKED="$best"
}

# A malformed or stranded entry is REPORTED, never silently invisible: a queue quietly
# accumulating files while the tick prints "queue empty" is exactly the silent wrong
# answer this script's contract forbids. Reported on EVERY path - empty queue, lock
# contention, cap spent, and the posting path alike - and always on stderr, so the
# contended path stays silent on stdout as its own contract requires.
report_queue_health() {
  [ "$SCANNED" = 1 ] || scan_queue
  # EVERY WRITE BELOW IS INSIDE ONE GUARDED GROUP, and that is load-bearing rather
  # than tidy. This function is called from the lock-contended exit and the cap-reached
  # exit, both of which are contractually 0 -- yet its writes used to be four bare
  # `>&2` commands, so under `set -e` a failing stderr write (EBADF: a timer loop
  # redirecting into a closed or rotated fd) exited 1 from a tick that refused nothing.
  # 1 is the code the contract reserves for "POSTED NOTHING because an entry was
  # REFUSED", and the loop runbook tells the operator to hand that entry back to the
  # TL, so the report about a broken queue was forging a refusal out of a no-op.
  # Reproduced with `elmer-tick.sh 2>&-` and an unreadable entry queued: rc=1 on both
  # paths, rc=0 without the entry. Same template as the exit-2 echo group below the
  # post: group the writes, redirect ONCE, `|| true` so no write can change the exit
  # status. `set -e` is suspended inside a group that is the left operand of `||`,
  # which is what makes the guard cover every command in the body and not just the last.
  # The scan stays OUTSIDE the group: it writes no output, and swallowing its status
  # would hide a genuine failure rather than an unwanted one.
  {
    if [ -s "$UNREADABLE_LOG" ]; then
      echo "elmer-tick: WARNING - $(wc -l < "$UNREADABLE_LOG" | tr -d ' ') unreadable queue entr(y/ies), skipped:"
      sed 's/^/  /' "$UNREADABLE_LOG"
    fi
    if [ -s "$STALE_LOG" ]; then
      echo "elmer-tick: WARNING - $(wc -l < "$STALE_LOG" | tr -d ' ') inbox entr(y/ies) already drained (stale, never re-posted):"
      sed 's/^/  /' "$STALE_LOG"
      echo "            Remove the stale inbox file(s) by hand."
    fi
  } >&2 || true
}

# --- Queue-health reporting, armed BEFORE the first early exit ---------------------
# The two health logs are created here, ABOVE the lock, because the report has to
# survive every exit path below it. They used to be created after the lock and after
# the cap, so the unreadable-entry report was DEAD on exactly the two exits that fire
# routinely - lock contention is the DESIGNED outcome of a second loop window, and the
# cap fires under any steady load - and a queue accumulating malformed files could
# stay invisible indefinitely while the comment claimed "reported on every path".
#
# BOTH mktemps ARE GUARDED, and each one arms the trap that covers it BEFORE the next
# runs. Two reasons, and neither is hypothetical:
#
# 1. EXIT CODE. Moving these above the lock put them on EVERY tick including the pure
#    no-ops, where an unwritable or full TMPDIR made an unguarded `$(mktemp)` exit 1
#    under `set -e` -- silently, with no message at all, on a path the contract defines
#    as 0. A bare 1 is read by the runbook as "an entry was refused", so the operator
#    is sent to re-queue an entry that nothing ever touched. An unavailable mktemp is a
#    SETUP ERROR, exactly like the jq probe above ("a dependency that is present but
#    broken"), so it exits 2 with a message naming what failed.
# 2. LEAK. The trap used to be armed only after BOTH calls, so a second mktemp that
#    failed stranded the first file in TMPDIR forever. Arming after the first closes
#    that window: whichever call fails, everything already created is cleaned up.
UNREADABLE_LOG="$(mktemp)" || {
  { echo "setup error: mktemp failed (TMPDIR unwritable or full); cannot arm queue-health reporting."; } >&2 || true
  exit 2
}
trap 'rm -f "$UNREADABLE_LOG" 2>/dev/null || true' EXIT
STALE_LOG="$(mktemp)" || {
  { echo "setup error: mktemp failed (TMPDIR unwritable or full); cannot arm queue-health reporting."; } >&2 || true
  exit 2
}
trap 'rm -f "$UNREADABLE_LOG" "$STALE_LOG" 2>/dev/null || true' EXIT

if ! acquire_lock; then
  # QUIET success on STDOUT: another tick is working, which is the designed outcome of
  # a second loop window, not a fault. Queue health still goes to stderr - "someone
  # else is busy" is not a reason to withhold a report about a broken queue.
  report_queue_health
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true; rm -f "$UNREADABLE_LOG" "$STALE_LOG" 2>/dev/null || true' EXIT

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
# VALIDATED FOR THE SAME REASON AS ELMER_LOCK_STALE_SECS ABOVE, and here the
# consequence is worse: `[ "$recent" -ge "$ELMER_MAX_PER_HR" ]` with a non-integer
# returns 2, which an `if` reads as FALSE, so the cap is SILENTLY DISABLED and the
# tick POSTS. Reproduced with ELMER_MAX_PER_HR=four and 10 drained records inside
# the hour: posted anyway. The cap is the one bound that is supposed to hold even
# when every other guard's reasoning is wrong, so it must never fail open on a typo.
case "$ELMER_MAX_PER_HR" in
  ''|*[!0-9]*)
    { echo "setup error: ELMER_MAX_PER_HR must be a non-negative integer, got: '$ELMER_MAX_PER_HR'"; } >&2 || true
    exit 2 ;;
esac

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
  report_queue_health
  { echo "elmer-tick: hourly cap reached ($recent/$ELMER_MAX_PER_HR posts in the last hour); nothing posted."; } || true
  exit 0
fi

# --- Pick the head entry: stillwater first, then FIFO ------------------------------
scan_queue
entry="$PICKED"
report_queue_health

if [ -z "$entry" ]; then
  # NOTHING PICKABLE IS NOT THE SAME AS NOTHING QUEUED, and saying the wrong one is the
  # silent-wrong-answer class this script's contract is built against. An inbox whose
  # every entry is also in `drained/` (the stranded-file state the exit-2 rm-failure
  # path leaves behind) selects nothing, and a flat "queue empty" is then a literally
  # false assertion about a directory with files in it - the operator reads it as "the
  # queue drained normally" and never looks. The stderr warning already names each
  # stale file; this makes the STDOUT line agree with it instead of contradicting it.
  {
    if [ -s "$STALE_LOG" ]; then
      echo "elmer-tick: no postable entries (see the stale-inbox warning above); nothing to do."
    else
      echo "elmer-tick: queue empty; nothing to do."
    fi
  } || true
  exit 0
fi

# The `// ""` defaults are BELT AND BRACES over scan_queue's validation, not the
# primary control: a bare `jq -r '.pr'` on a missing key yields the literal string
# "null", which composed straight into `gh pr comment null`. scan_queue now refuses
# such an entry outright, so these defaults only ever matter if that check is later
# weakened - in which case an empty string fails loudly downstream instead of
# posting to a PR named "null".
e_repo="$(jq -r '.repo // ""' "$entry")"
e_pr="$(jq -r '.pr // ""' "$entry")"
e_sha="$(jq -r '.commit_sha // ""' "$entry")"
e_form="$(jq -r '.form // "incremental"' "$entry")"

# --- Refuse `full` outright, whatever the entry says -------------------------------
# The carve-out permits the incremental form ONLY. This is checked HERE, at the
# posting site, and not merely at enqueue: a hand-edited queue entry is exactly the
# path that would otherwise smuggle a `full review` past the gate, and a full review
# re-surfaces resolved threads and owes a human decision.
if [ "$e_form" != "incremental" ]; then
  { echo "REFUSED: entry requests form '$e_form'; only 'incremental' is permitted."
    echo "         $entry"
  } >&2 || true
  exit 1
fi

# --- The PR must still be open, and still at the gated SHA -------------------------
# A gh read failure is a SETUP ERROR (exit 2), never a refusal and never a silent
# skip: an unattended loop that treats an unreadable PR as "nothing to do" goes wrong
# quietly, which is the failure mode this whole design is built to avoid.
if ! pr_json="$(gh pr view "$e_pr" --repo "$e_repo" --json headRefOid,state 2>/dev/null)"; then
  { echo "setup error: could not read PR #$e_pr ($e_repo). Not posting; entry stays queued."; } >&2 || true
  exit 2
fi
pr_state="$(jq -r '.state' <<<"$pr_json")"
pr_head="$(jq -r '.headRefOid' <<<"$pr_json")"

if [ "$pr_state" != "OPEN" ]; then
  { echo "REFUSED: PR #$e_pr ($e_repo) is $pr_state; a closed PR is never reviewed."; } >&2 || true
  exit 1
fi

# THE STALENESS GATE. The entry was admitted because a gate receipt matched the head
# at enqueue time; if the head has moved since, that receipt no longer describes what
# a review would read. Posting anyway would review code that never passed the gate --
# precisely what the receipt exists to prevent. Refuse and surface; the TL re-runs
# /prep-pr on the new head. This is verify-do-not-classify (#337): compare the fact,
# do not infer intent.
if [ "$pr_head" != "$e_sha" ]; then
  { echo "REFUSED: PR #$e_pr ($e_repo) has moved since it was gated."
    echo "         queued: ${e_sha:0:12}   current: ${pr_head:0:12}"
    echo "         Re-run /prep-pr on the new head to re-queue. Entry left in place."
  } >&2 || true
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
  { echo "setup error: cr-quota-watch.sh not found beside $0; refusing to post blind."; } >&2 || true
  exit 2
fi
case "$quota_rc" in
  0) : ;;
  1) { echo "elmer-tick: CodeRabbit is throttled; nothing posted. Entry stays queued."; } || true
     exit 0 ;;
  *) { echo "setup error: quota read failed (rc=$quota_rc). Not posting."; } >&2 || true
     exit 2 ;;
esac

# --- Has CR already reviewed this exact head? --------------------------------------
# Cheap insurance against a double-post that the drain record cannot catch: a review
# triggered by the MAINTAINER by hand leaves no queue entry, so only GitHub knows
# about it. Note this check is sound in the direction it is used -- it can only
# SUPPRESS a post, never authorize one -- which is why the racy inverse (asking
# GitHub "has a review happened" as the idempotency mechanism) is still not used.
# THE FIELD IS `commit.oid`, NOT `commit_id`. Verified live against the GitHub API:
# `gh pr view --json reviews` emits {author:{login}, state, commit:{oid}} and has NO
# `commit_id` key at all. The first cut of this guard compared `.commit_id == $s`,
# i.e. `null == <sha>`, which is ALWAYS false - so the guard never fired and read as
# correct. Its harness stub encoded the same invented shape, so the test and the bug
# agreed with each other and the mutation-proof passed against a fictional field.
# If this ever needs changing, re-verify the shape against a REAL PR first.
#
# Scoped to CodeRabbit's own login on purpose: a human approval or another bot's
# review at this SHA must NOT suppress a wanted CR pass. Only a CR review at this
# exact head means the slot would be spent on work already done.
#
# The login match is anchored at BOTH ends and admits exactly the two real forms,
# `coderabbitai` and `coderabbitai[bot]`. Anchored only at the front, it also matched
# `coderabbitai2` and `coderabbitai-impostor` - which can only ever SUPPRESS, so the
# cost is a silently skipped review the maintainer wanted, not a stray post.
if revs="$(gh pr view "$e_pr" --repo "$e_repo" --json reviews 2>/dev/null)"; then
  if [ "$(jq -r --arg s "$e_sha" \
        '[.reviews[]? | select((.commit.oid // "") == $s)
                      | select((.author.login // "") | test("^coderabbitai(\\[bot\\])?$"))] | length' \
        <<<"$revs" 2>/dev/null || echo 0)" -gt 0 ]; then
    { echo "REFUSED: CodeRabbit has already reviewed ${e_sha:0:12}; not spending a slot."; } >&2 || true
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
  { echo "DRY RUN: would post to $e_repo #$e_pr at ${e_sha:0:12}:"
    echo "         $TRIGGER"
  } || true
  exit 0
fi

if ! post_out="$(gh pr comment "$e_pr" --repo "$e_repo" --body "$TRIGGER" 2>&1)"; then
  { echo "setup error: the post failed; entry stays queued for the next tick."
    printf '%s\n' "$post_out"
  } >&2 || true
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
# EVERY command below the successful post is failure-TOLERANT, because `set -e` turns
# any of them into an exit 1 - the one code the contract reserves for "POSTED NOTHING".
# `date` is guarded rather than trusted (an empty timestamp is COUNTED as a recent post
# by the cap, the conservative direction), and the basename is parameter expansion so
# there is no subprocess to fail at all.
now="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
base="${entry##*/}"
tmp="$drained/.$base.tmp.$$"
# EVERY value crosses into jq as an `--arg`, INCLUDING the trigger. Splicing
# "'"$TRIGGER"'" into the PROGRAM TEXT worked only because today's literal happens to
# contain no quote or backslash; a future literal carrying either would produce an
# INVALID JQ PROGRAM, and the drain would then fail AFTER a successful post - the one
# genuinely bad state this section documents (the next tick could re-post). `--arg`
# makes that failure class unrepresentable rather than merely unlikely.
if jq --arg t "$now" --arg out "$post_out" --arg trig "$TRIGGER" \
     '. + {triggered_at: $t, trigger: $trig, response: $out}' \
     "$entry" > "$tmp" 2>/dev/null && mv -f "$tmp" "$drained/$base"; then
  # THE POST HAS HAPPENED. From here on, no failure may exit 1: the contract defines
  # 1 as "POSTED NOTHING", and the loop runbook tells the operator to hand the entry
  # back to the TL on a 1. A successful post reported as rc=1 makes every downstream
  # human decision rest on a false premise, about the one outcome that spends a slot.
  #
  # `rm -f` does NOT succeed unconditionally - it fails on an unwritable inbox
  # (permissions drift, a restored backup), and under `set -e` that failure used to
  # kill the script at rc=1 right here, with the success lines below never printed.
  # The drained/ check in scan_queue now prevents the resulting double-post (and
  # reports the stranded file on every later tick), so this is reported loudly as
  # exit 2 and never mistaken for a refusal. The reporting echoes are guarded for the
  # same reason as the success ones below: a failed write must not turn a deliberate
  # 2 into a 1.
  if ! rm -f "$entry" 2>/dev/null || [ -e "$entry" ]; then
    { echo "elmer-tick: POSTED $e_repo #$e_pr at ${e_sha:0:12}, drained to $drained/$base"
      echo "setup error: could not remove the inbox entry $entry after a SUCCESSFUL post."
      echo "             The drain record exists, so the next tick will NOT re-post it."
      echo "             Remove the stale inbox file by hand."
    } >&2 || true
    exit 2
  fi
  # EVERY command from here to the exit is failure-TOLERANT, and the exit is an
  # explicit unconditional 0. A bare `echo` is not safe under `set -e`: a stdout write
  # error (EBADF/EIO - a timer loop redirecting into a closed or rotated fd) fails, and
  # the script would exit 1 AFTER a successful post, a written drain record, and a
  # removed inbox entry. Reproduced with `elmer-tick.sh >&-`: rc=1 with posted=1.
  # EPIPE is a different animal (SIGPIPE gives 141, which no caller reads as a refusal);
  # it is the EBADF class that produces the forbidden 1.
  { echo "elmer-tick: POSTED an incremental review request -- $e_repo #$e_pr at ${e_sha:0:12}"
    echo "            drained: $drained/$base"
  } || true
  exit 0
fi
rm -f "$tmp" 2>/dev/null || true
{ echo "setup error: POSTED but could not write the drain record for $entry."
  echo "             Move or delete it by hand before the next tick, or it may re-post."
} >&2 || true
exit 2
