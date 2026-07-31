---
description: "Run the unattended review requester: service the queue, post one incremental CR review per open window"
argument-hint: "[--dry-run] [--once]"
allowed-tools: ["Bash", "ScheduleWakeup"]
---

# elmer-loop -- the unattended review requester

Service the elmer queue: when CodeRabbit's quota window is open, post ONE incremental review
request for the head entry, then sleep until the next window. TLs file requests with
`/orchestrate:request-review`; this window is the only thing that posts them.

**Run this in its OWN dedicated Claude Code window**, and prefer launching it with
`claude --model haiku`. The judgment was deliberately pushed into deterministic scripts, so a tick
is a DISPATCHER, not a reasoner: read a quota exit code, pick the queue head, compare a SHA string,
run one fixed command, move a file. A loop waking every ~59 minutes all night on Opus is real spend
for zero added correctness.

**Arguments:** $ARGUMENTS

---

## What this window is authorized to do (read before running)

Triggering a CodeRabbit review is normally the maintainer's EXCLUSIVE purview. This loop runs the
ONE carve-out recorded in the user-global `~/.claude/CLAUDE.md` (approved 2026-07-30), and that
carve-out does NOT grant an agent trigger authority. It records that the maintainer MECHANIZED HIS
OWN trigger, delegating the TIMING to a script whose behavior is fixed in reviewable code instead
of per-invocation judgment.

Every bound is enforced INSIDE `elmer-tick.sh`, not by this document and not by your judgment:

- **One writer.** Only the tick posts. A second `/elmer-loop` window exits quietly on the lock.
- **Queue-derived only.** It posts solely for entries the receipt gate admitted. It never invents a
  target.
- **Incremental only.** `full review` is refused outright, whatever an entry says.
- **Hard posts-per-hour cap**, independent of queue depth.
- **Silence on doubt.** Any unrecognized state: do nothing, log it, retry later.

**You must not post a review trigger by hand in this window, or anywhere else.** If the tick
declines to post, that is the design working. Do not "help" it along, do not paste the trigger
yourself, and do not widen a bound because the queue looks stuck. Widening any bound above means
amending CLAUDE.md FIRST.

---

## Step 1 -- One tick

Resolve the helper in the SAME Bash call that uses it - each tool call is a fresh shell.

```bash
TK=""
if [ -f scripts/elmer-tick.sh ]; then TK=scripts/elmer-tick.sh
elif [ -f "$HOME/.claude/scripts/elmer-tick.sh" ]; then TK="$HOME/.claude/scripts/elmer-tick.sh"
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/scripts/elmer-tick.sh" ]; then
  TK="${CLAUDE_PLUGIN_ROOT}/scripts/elmer-tick.sh"
fi
[ -n "$TK" ] || { echo "elmer-tick.sh not found (repo-local, deployed, or plugin)" >&2; exit 2; }

bash "$TK"
```

On `--dry-run`, set `ELMER_DRY_RUN=1` in front of the command: it does everything except the post
and prints the exact command it would have run. Use this the first time you run the loop in a new
environment - it exercises the lock, the cap, the quota read, and the queue pick without spending a
review slot.

### Reading the exit code

| Exit | Meaning | Next |
|---|---|---|
| 0 | Did its job, **including** every no-op: queue empty, throttled, cap spent, lock held by another tick. | Sleep, then tick again. |
| 1 | Refused a specific entry (stale SHA, closed PR, non-incremental form, a review already at this head). The entry stays queued. | Surface it to the TL; the fix is theirs (usually re-run `/prep-pr`). Keep looping. |
| 2 | Setup error: bad args, a `gh` or quota READ FAILURE, a failed post, or a drain that failed after a successful post. | Read the message. A read failure is never "nothing to do"; a failed drain after a post is the one state needing a human, because the next tick could re-post. |

**Exit 0 is not "posted".** Most healthy ticks post nothing. Do not treat a quiet tick as a
malfunction to investigate.

---

## Step 2 -- Sleep until the next window, not on a fixed clock

A fixed hourly tick drifts out of phase with the real window and wastes slots. Ask the quota oracle
when the current limit expires and wake then:

```bash
QW=""
if [ -f scripts/cr-quota-watch.sh ]; then QW=scripts/cr-quota-watch.sh
elif [ -f "$HOME/.claude/scripts/cr-quota-watch.sh" ]; then QW="$HOME/.claude/scripts/cr-quota-watch.sh"
fi
[ -n "$QW" ] && bash "$QW" <a-PR#-from-the-queue> || true
```

Exit 1 means limited, and the output carries the remaining time plus a Pacific-labeled deadline.
Exit 0 means no announced limit.

Then call `ScheduleWakeup` with a delay derived from that reading, and pass this same `/elmer-loop`
input back as the prompt so the next firing re-enters the loop.

Two measured behaviors constrain the pacing, and both argue for re-reading rather than
counting down locally:

- **The countdown is NON-MONOTONIC** (53 minutes, then 51 minutes an hour LATER - CR's limits are
  adaptive). A locally-decremented timer is wrong by construction, so RE-QUERY on every wake.
- **"Available now" is PERISHABLE.** Triggering consumes the slot immediately and resets the
  counter to a full window, which is why the tick posts ONE entry and then re-reads.

**CR never publishes a remaining-slot count** - only a countdown, and only once the limit is
ALREADY reached. That ceiling is a product decision, not a parser gap: do not go hunting for a
better matcher or a hidden endpoint. It means an all-clear reading is genuinely ambiguous (plenty
of budget, OR one review from the wall), which is exactly why the tick never batches.

When the queue is empty there is nothing to pace against - sleep long (20-30 min is fine) and
re-check.

On `--once`, do Step 1 and stop. No wakeup is scheduled.

---

## Step 3 -- Morning triage drop (optional, read-only)

Overnight the loop triggers reviews and CR posts findings. `elmer-triage.sh` composes those into a
per-PR maildir digest so a TL wakes to a readable queue instead of a raw comment dump:

```bash
TR=""
if [ -f scripts/elmer-triage.sh ]; then TR=scripts/elmer-triage.sh
elif [ -f "$HOME/.claude/scripts/elmer-triage.sh" ]; then TR="$HOME/.claude/scripts/elmer-triage.sh"
fi
[ -n "$TR" ] && bash "$TR" || true
```

No model is involved, which is what keeps this a dumb pipe: every field is a read-only helper's
output. Entries record `triaged_sha` on its own line, so a reader greps it and compares to HEAD -
equal means the report is live, different means re-derive. Staleness is DETECTABLE rather than
assumed.

---

## Notes

**If the queue never drains**, check in this order before suspecting the tick: is a
`/elmer-loop` window actually running; is CR throttled (`cr-quota-watch.sh`); is the head entry
stale against its PR (exit 1 says so by name). An entry sitting in `inbox/` is safe - it is a
request not yet made, never a lost one.

**Never hand-edit `inbox/` or `drained/`.** The drain record is the idempotency mechanism, and
`drained/` is the permanent audit trail the carve-out requires. Moving files by hand can cause a
double-post, which spends a scarce review slot and cannot be undone.

**Scope.** The loop reads the queue, reads PR state and quota via `gh`, and posts at most one fixed
comment per tick. No git mutation, no allow-list broadening, no floor change. Every script it calls
lives at the stable `~/.claude/scripts/` path, which is what keeps the whole loop inside the
existing wrapper grant - so an unattended run never stalls on a permission prompt.
