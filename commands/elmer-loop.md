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

Detect and run the helper in the SAME Bash call - each tool call is a fresh shell. Every helper
path in this command is LITERAL, never a variable (the "Helper exec paths" rule in `prep-pr.md`),
and the deployed `~/.claude/scripts/` leg is checked before the plugin leg on purpose: that is what
keeps the unattended loop inside the existing wrapper grant (see Notes).

```bash
if [ -f scripts/elmer-tick.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f ~/.claude/scripts/elmer-tick.sh ]; then leg=stable
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/elmer-tick.sh' ]; then leg=plugin
else leg=none; fi
tick_rc=2
[ "$leg" = repo ]   && { bash scripts/elmer-tick.sh; tick_rc=$?; }
[ "$leg" = stable ] && { bash ~/.claude/scripts/elmer-tick.sh; tick_rc=$?; }
[ "$leg" = plugin ] && { bash '${CLAUDE_PLUGIN_ROOT}/scripts/elmer-tick.sh'; tick_rc=$?; }
[ "$leg" = none ]   && echo "elmer-tick.sh not found (repo-local, deployed, or plugin)" >&2
echo "tick_rc=$tick_rc leg=$leg"
(exit "$tick_rc")
```

On `--dry-run`, set `ELMER_DRY_RUN=1` in front of the command: it does everything except the post
and prints the exact command it would have run. Use this the first time you run the loop in a new
environment - it exercises the lock, the cap, the quota read, and the queue pick without spending a
review slot.

### Reading the exit code (`tick_rc`)

| Exit | Meaning | Next |
|---|---|---|
| 0 | Did its job, **including** every no-op: queue empty, throttled, cap spent, lock held by another tick. | Sleep, then tick again. |
| 1 | Refused a specific entry (stale SHA, closed PR, non-incremental form, a review already at this head). The entry stays queued. | Surface it to the TL; the fix is theirs (usually re-run `/prep-pr`). Keep looping. |
| 2 | Setup error: bad args, a `gh` or quota READ FAILURE, a failed post, or a drain that failed after a successful post. | Read the message. A read failure is never "nothing to do"; a failed drain after a post is the one state needing a human, because the next tick could re-post. |

**Exit 0 is not "posted".** Most healthy ticks post nothing. Do not treat a quiet tick as a
malfunction to investigate.

**`elmer-tick: RE-ADMITTED <repo> #<pr> ...`** (exit 0) means CR answered an earlier trigger
"Review rate limited.", so the tick re-queued it and posted nothing that tick. It happens at most
ONCE per PR+SHA; a second rate-limited reply leaves the request drained, and that SHA cannot be
re-queued (enqueue refuses it as ALREADY TRIGGERED). To request another review, push a new commit
and re-run `/prep-pr`.

---

## Step 2 -- Sleep until the next window, not on a fixed clock

A fixed hourly tick drifts out of phase with the real window and wastes slots. Ask the quota oracle
when the current limit expires and wake then:

```bash
PR_FOR_QUOTA="${PR_FOR_QUOTA:?set to a PR number from the queue (ls the inbox; entries are named <repo-slug>--<pr>--<sha12>.json)}"
if [ -f scripts/cr-quota-watch.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f ~/.claude/scripts/cr-quota-watch.sh ]; then leg=stable
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/cr-quota-watch.sh' ]; then leg=plugin
else leg=none; fi
QUOTA_RC=0
[ "$leg" = repo ]   && { bash scripts/cr-quota-watch.sh "$PR_FOR_QUOTA" || QUOTA_RC=$?; }
[ "$leg" = stable ] && { bash ~/.claude/scripts/cr-quota-watch.sh "$PR_FOR_QUOTA" || QUOTA_RC=$?; }
[ "$leg" = plugin ] && { bash '${CLAUDE_PLUGIN_ROOT}/scripts/cr-quota-watch.sh' "$PR_FOR_QUOTA" || QUOTA_RC=$?; }
[ "$leg" = none ]   && { echo "quota: NOT RUN -- cr-quota-watch.sh not found (repo-local, deployed, or plugin)"; QUOTA_RC=2; }
echo "quota rc=$QUOTA_RC leg=$leg"
```

`PR_FOR_QUOTA` is a `:?` guard, not a `<a-PR#>` placeholder: a bare `<...>` is a shell
REDIRECTION, so that form is a `bash -n` parse error and the whole block dies before anything
below it could run. It is not hardcoded either - an unattended loop pinned to one PR number would
poll a stale PR forever. Nothing swallows the guard, so a missing value fails loudly.

The quota call's status is CAPTURED into `QUOTA_RC` rather than left as the block's own status.
Exit 1 here is the EXPECTED "limited" reading and the whole reason to call the oracle, so letting
it become the block's failure status would make the normal path look like a fault (and abort it
under a caller running `set -e`). The missing-PR guard still fails loudly - the capture is scoped
to the helper call alone.

The reading is ACCOUNT-WIDE (#456): besides `PR_FOR_QUOTA` it scans the repo's 10 most recently
updated PRs, because CR's limit is per account and a notice often lands on a different PR.

Exit 1 means limited, and the output carries the remaining time plus a Pacific-labeled deadline.
If the line says `deadline UNKNOWN`, CR announced a limit whose duration did not parse; the
shown time is an assumed 1h ceiling, not CR's countdown, so pace on it but expect to re-query.
Exit 0 means no announced limit. `quota rc=2` is a FAILURE, never an all-clear: either the
helper's own setup/read error, or (`leg=none`, `quota: NOT RUN`) no quota helper was found on
any leg. There is no reading to pace against - report it, sleep the long default (20-30 min),
and re-query; never schedule an early wake on the assumption that no limit is active. A
missing helper does not heal between wakes, so surface `leg=none` to the maintainer.

Then call `ScheduleWakeup` with a delay derived from that reading, and pass this same `/elmer-loop`
input back as the prompt so the next firing re-enters the loop.

Two measured behaviors constrain the pacing, and both argue for re-reading rather than
counting down locally:

- **The countdown is NON-MONOTONIC** (53 minutes, then 51 minutes an hour LATER - CR's limits are
  adaptive). A locally-decremented timer is wrong by construction, so RE-QUERY on every wake.
- **"Available now" is PERISHABLE.** Triggering consumes the slot immediately and resets the
  counter to a full window, which is why the tick posts ONE entry and then re-reads.

**CR's remaining-slot count is stale by construction.** Its summary banner ("N reviews are
currently available", measured 2026-09-26) is refreshed only when CR edits a summary, and its
countdown appears only once the limit is ALREADY reached. So an all-clear reading is still
ambiguous (plenty of budget, OR one review from the wall), which is exactly why the tick never
batches.

When the queue is empty there is nothing to pace against - sleep long (20-30 min is fine) and
re-check.

On `--once`, do Step 1 and stop. No wakeup is scheduled.

---

## Step 3 -- Morning triage drop (optional, read-only)

Overnight the loop triggers reviews and CR posts findings. `elmer-triage.sh` composes those into a
per-PR maildir digest so a TL wakes to a readable queue instead of a raw comment dump:

Set `TRIAGE_PRS` to the space-separated PR numbers to digest before running the block: the PRs
the loop triggered SINCE THE LAST TRIAGE. `drained/` is a permanent audit trail that only grows,
so take recent entries, not all of them. Entries are named `<repo-slug>--<pr>--<sha12>.json` (or
`...<sha12>.readmitted-<id>.json` after a re-admission), so for the last 24 hours:
`TRIAGE_PRS=$(find ~/.claude/elmer/drained -name '*.json' -mtime -1 | sed -E 's/.*--([0-9]+)--[0-9a-f]+(\.readmitted-[0-9]+)?\.json$/\1/' | sort -un | tr '\n' ' ')`.
The helper REQUIRES at least one PR number - called bare it prints its usage and exits 2 - so the block
checks the BUILT ARRAY and stops loudly when it is empty. (Not a `${TRIAGE_PRS:?}` guard: inside
a `$(...)` zsh does not abort the outer command on it, so the helper would still run bare.)

The array is built with `printf` word-splitting, NOT `read -a`: the Bash tool runs the user's
shell, and zsh rejects `read -a` (`bad option: -a`) and leaves the array EMPTY, which would call
the helper bare - the exact defect this block exists to close.

```bash
triage_prs=( $(printf '%s\n' "${TRIAGE_PRS:-}") )
if [ "${#triage_prs[@]}" -eq 0 ]; then
  echo "triage: NOT RUN -- TRIAGE_PRS is unset or empty; set it to the PR numbers to triage" >&2
  exit 2
fi
if [ -f scripts/elmer-triage.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f ~/.claude/scripts/elmer-triage.sh ]; then leg=stable
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/elmer-triage.sh' ]; then leg=plugin
else leg=none; fi
triage_rc=2
[ "$leg" = repo ]   && { bash scripts/elmer-triage.sh "${triage_prs[@]}"; triage_rc=$?; }
[ "$leg" = stable ] && { bash ~/.claude/scripts/elmer-triage.sh "${triage_prs[@]}"; triage_rc=$?; }
[ "$leg" = plugin ] && { bash '${CLAUDE_PLUGIN_ROOT}/scripts/elmer-triage.sh' "${triage_prs[@]}"; triage_rc=$?; }
[ "$leg" = none ]   && echo "triage: NOT RUN -- elmer-triage.sh not found (repo-local, deployed, or plugin); no triage drop" >&2
echo "triage_rc=$triage_rc leg=$leg"
(exit "$triage_rc")
```

`triage_rc=0` means the drop was written (a per-PR read failure is recorded INSIDE that PR's
entry, never omitted). Non-zero means no drop was written - a setup error, or `leg=none`. The
step is optional, so it never stops the loop, but report it as NOT RUN rather than as an empty
queue.

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
