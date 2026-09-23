---
description: "Wait for a PR to reach a terminal state (CI finished + CR reviewed). Silent until done; one stdout line on completion."
argument-hint: "<PR number> [timeout_secs]"
allowed-tools: ["Bash"]
---

# PR Watch

Wait for a pull request to reach a TERMINAL state. The script is silent on stdout during the wait and emits exactly one line on stdout when done. Its outcomes:

1. **`settled head=<sha8> mergeable=<state>`** -- ready to merge. All of:
   - CodeRabbit has reviewed HEAD with a non-DISMISSED, non-CHANGES_REQUESTED review (APPROVED or COMMENTED).
   - Every CI check on HEAD has reached a terminal state (no IN_PROGRESS / QUEUED / PENDING).
   - GitHub's `mergeable_state` is in the merge-ready set: `clean`, `unstable`, or `has_hooks`.

   Exit 0. Next action: `/merge-pr <pr>`.

2. **`review-blocked head=<sha8> by=<login[,login...]>`** -- the latest HEAD review from ANY reviewer (bot or human) is CHANGES_REQUESTED. Reviewer-agnostic (#195): not just CodeRabbit; the `by=` field lists every such reviewer's login. Distinct terminal because the next action differs (address feedback, not merge). Exit 0. Next action: `/handle-review <pr>`. The blocking set defaults to "any reviewer"; set `PR_WATCH_BLOCKING_REVIEWERS` to a comma/space-separated login list to restrict which reviewers can trip this terminal. `settled` is unchanged (still green + merge-ready); the rejected "stop on comment-count increment / not-necessarily-green" alternative is documented in #195.

3. **`thread-blocked head=<sha8> unresolved=<n> failing=<n> by=<login[,login...]>`** (#441) -- CI is terminal (failing=<n> may be nonzero: it counts checks in a FAILURE/ERROR/CANCELLED/TIMED_OUT/ACTION_REQUIRED/STARTUP_FAILURE/STALE state from the same checks read, so terminal is NOT green; classification is fail-closed: only SUCCESS/NEUTRAL/SKIPPED count as done, and every other state, including EXPECTED/REQUESTED/WAITING and any state GitHub adds later, pends as `ci(<n>)`; an unreadable checks read pends as `ci(unknown)` and never reaches this terminal), nothing else is pending, `review-blocked` did not fire (it keeps priority), `mergeable_state` is `blocked`, and a GraphQL `reviewThreads` read shows at least one thread with `isResolved: false`. `by=` lists the first-comment author of each unresolved thread, de-duplicated. The quiet-period gate applies. Exit 0. Next action: `/handle-review <pr>`. Positive evidence only: branch-protection settings are never read, and `blocked` by itself never terminates the watch. An UNREADABLE thread read (gh failure, GraphQL errors, malformed body) is NOT zero threads; the PR stays pending as `threads(unreadable)` and neither `thread-blocked` nor `settled` fires. Only 100 threads are fetched; a larger `totalCount` is reported on stderr (`unresolved` is then a lower bound), or pends as `threads(truncated)` when every fetched thread is resolved. Known limit: the CR-trigger check is not scoped to the head, so a historical `@coderabbitai review` plus a push after CR's last review leaves `cr-review` pending and this terminal unreachable; the stderr `pr-watch: pending=cr-review,merge(blocked)` line names it.

4. **`merged head=<sha8>`** / **`closed head=<sha8>`** (#435) -- the PR is already MERGED, or CLOSED without merging. Checked first on every poll, with no quiet period (neither state reverts to in-progress). Exit 0. Next action: `merged` -> `/post-merge-cleanup <pr>`; `closed` -> report it, no action.

5. **`timeout: waited <secs>s pending=<list>`** (stderr) -- timeout elapsed. Exit 1. Re-arm with a longer timeout or check `gh pr view <pr>` manually.

Setup errors (bad PR number, can't resolve repo) print `setup error: ...` to stderr and exit 2.

Progress (#399): stdout stays silent until a terminal, but stderr gets ONE `pr-watch: pending=<list>` line each time the pending set CHANGES (never on an unchanged poll). A watch held on a human gate, for example `merge(blocked)` after a push dismissed a required approval, therefore names what it is waiting on long before the timeout. This is information only: it is not a terminal and does not change the exit code.

## Why mergeable_state matters

`gh pr checks` only returns checks GitHub has been told about so far. Late-registering workflows (re-runs from label changes, codecov post-back, paths-filter dispatch) can appear AFTER the visible set is terminal -- a false-settle. mergeable_state aggregates branch protection's view of "every required check has reported AND no CHANGES_REQUESTED is active", so it stays `blocked` until every required-but-not-yet-registered check arrives and clears. Codecov coverage states naturally flap during multi-shard CI runs as different shards report at different times; mergeable_state absorbs the flapping and only clears when the aggregate stabilizes.

## Why this script (not a hand-rolled jq loop)

GitHub's check `conclusion` field can be the empty string `""` mid-flight. jq's `// alternative` operator only falls back on null/false, so a hand-rolled `(.conclusion // "in_progress")` reports `=` (empty), and a grep for `=in_progress` returns 0 -> premature SETTLED. This script uses `state` (which goes through gh's bucket-mapping and never returns `""`) to avoid the trap.

## Why the quiet-period gate

CodeRabbit posts inline comments seconds AFTER its CI check transitions to SUCCESS. If `mergeable_state` happens to read `clean` in that window before CR's review-state actually lands, a strict snapshot would emit `settled` prematurely and hand the consumer a half-formed triage list. The script counts items from allow-listed bot authors (`coderabbitai[bot]`, `github-actions[bot]`, `greptile-apps[bot]`, `codoki-pr-intelligence[bot]`, and Copilot under BOTH of its REST logins: `copilot-pull-request-reviewer[bot]` on its review object and `Copilot` on its inline review comments) across reviews + pull-comments + issue-comments, and requires the count to be unchanged across two consecutive 30s polls before terminating. Adds ~30s of latency in the happy path; eliminates the trickle race. Applies to the `settled`, `review-blocked`, and `thread-blocked` terminals (not to `merged`/`closed`). The baseline is tagged with the HEAD sha, so a count taken before a push never confirms the new head.

## Args

`$ARGUMENTS` parses to: `<pr_number> [timeout_secs]`. Defaults:
- `pr_number` -- if omitted, resolve from the current branch via `gh pr view`.
- `timeout_secs=1800` (30 min). Bump to 3600 when test shards are slow or many waves are queued.

The poll interval is not configurable -- the script polls every 30s. The `review-blocked` reviewer set defaults to ANY reviewer (bot or human) and is narrowed only via `PR_WATCH_BLOCKING_REVIEWERS` (see the `review-blocked` terminal above, #195); `settled` is unchanged and still gates on the CodeRabbit-opt-in + Codoki + CI + mergeable criteria.

## Step 1 -- Resolve PR number

```bash
pr_number="$1"
timeout_secs="${2:-1800}"
if [ -z "$pr_number" ]; then
  pr_number=$(gh pr view --json number --jq .number 2>/dev/null)
fi
if [ -z "$pr_number" ]; then
  echo "Need a PR number." ; exit 2
fi
```

## Step 2 -- Arm the Monitor

Invoke the Monitor tool with the watch script. The script is silent until done; the single terminal stdout line becomes the only Monitor event. Wait for it without polling.

```bash
if [ -f scripts/pr-watch.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/pr-watch.sh' ]; then leg=plugin
elif [ -f ~/.claude/scripts/pr-watch.sh ]; then leg=stable
else leg=none; fi
rc=2
[ "$leg" = repo ]   && { bash scripts/pr-watch.sh $pr_number "" $timeout_secs; rc=$?; }
[ "$leg" = plugin ] && { bash '${CLAUDE_PLUGIN_ROOT}/scripts/pr-watch.sh' $pr_number "" $timeout_secs; rc=$?; }
[ "$leg" = stable ] && { bash ~/.claude/scripts/pr-watch.sh $pr_number "" $timeout_secs; rc=$?; }
[ "$leg" = none ]   && echo "pr-watch.sh not found (repo-local, plugin, or ~/.claude/scripts/)" >&2
(exit "$rc")
```

(The empty second arg lets the script auto-detect the repo via `gh repo view`.) The helper path
is LITERAL in every leg, never a variable (the "Helper exec paths" rule in `prep-pr.md`); the
detection prints nothing on stdout, so the watcher's terminal line stays the only stdout event,
and `(exit "$rc")` hands the watcher's own exit code back as the block's.

PASS THAT BLOCK DIRECTLY as the backgrounded command. NO `nohup`, NO trailing `&`, NO output
redirect (#331):

```bash
# WRONG - the verdict is orphaned:
nohup bash .../pr-watch.sh 123 "" 1800 > /tmp/watch.log 2>&1 &
```

Inside a `run_in_background: true` task, that wrapper RETURNS IMMEDIATELY, so the completion
notification fires on the WRAPPER, not the watcher. The watch then runs correctly, writes its
terminal line to the logfile, and exits with NOBODY READING IT. The failure is silent and
plausible-looking: an exit-0 notification seconds after arming is indistinguishable from a
healthy detached launch, and nothing surfaces the problem until a human asks why the watch is
not working. Measured (stillwater 2026-07-18/19): two PR watches armed this way both wrote
terminal verdicts nobody consumed, leaving a bot review unhandled ~14 minutes. It WAS working -
the wiring was wrong.

The task output file already IS the logfile and the completion notification already IS the
signal; a wrapper only hides both. This applies to ANY long-running helper whose EXIT CODE is
the signal - `issue-watch.sh`, a `gate-runner.py` run - not just this one. And when the
notification arrives, READ THE OUTPUT FILE: a backgrounded command's reported "exit code 0" is
the wrapper's, not the tool's, so confirm the tool's own terminal line before acting on it.

## Step 3 -- Dispatch on the terminal line

When the Monitor reports the script's exit, branch off the single stdout line:
- **`settled head=...`** + Exit 0 -> invoke `/merge-pr <pr>`.
- **`review-blocked head=...`** + Exit 0 -> invoke `/handle-review <pr>`.
- **`thread-blocked head=...`** + Exit 0 -> if `failing=` is nonzero, fix CI first (handle-review's thread replies do not fix a red check). Otherwise route by `by=`, which may MIX authors: every login in it that does NOT end in `[bot]` opened a human thread, so name those logins and tell the maintainer those threads need them; if it also holds any `[bot]` login, invoke `/handle-review <pr>` for the bot threads in the same round (reply to and resolve them; a round that closes threads by reply-and-resolve may make no commit). A `by=` with only human logins goes to the maintainer alone; one with only `[bot]` logins goes to `/handle-review` alone.
- **`merged head=...`** + Exit 0 -> invoke `/post-merge-cleanup <pr>`.
- **`closed head=...`** + Exit 0 -> report that the PR was closed without merging; no further action.
- Exit 1 (`timeout: ...` on stderr) -> re-arm with a longer timeout or check `gh pr view <pr>` manually for what is still in flight.
- Exit 2 (`setup error: ...` on stderr) -> the script failed to query the PR. Check `gh` auth state.
