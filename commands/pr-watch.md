---
description: "Wait for a PR to reach a terminal state (CI finished + CR reviewed). Silent until done; one stdout line on completion."
argument-hint: "<PR number> [timeout_secs]"
allowed-tools: ["Bash"]
---

# PR Watch

Wait for a pull request to reach a TERMINAL state. The script is silent during the wait and emits exactly one line on stdout when done. Three possible outcomes:

1. **`settled head=<sha8> mergeable=<state>`** -- ready to merge. All of:
   - CodeRabbit has reviewed HEAD with a non-DISMISSED, non-CHANGES_REQUESTED review (APPROVED or COMMENTED).
   - Every CI check on HEAD has reached a terminal state (no IN_PROGRESS / QUEUED / PENDING).
   - GitHub's `mergeable_state` is in the merge-ready set: `clean`, `unstable`, or `has_hooks`.

   Exit 0. Next action: `/merge-pr <pr>`.

2. **`review-blocked head=<sha8> by=cr`** -- CodeRabbit's latest review on HEAD is CHANGES_REQUESTED. Distinct terminal because the next action differs (address feedback, not merge). Exit 0. Next action: `/handle-review <pr>`.

3. **`timeout: waited <secs>s pending=<list>`** (stderr) -- timeout elapsed. Exit 1. Re-arm with a longer timeout or check `gh pr view <pr>` manually.

Setup errors (bad PR number, can't resolve repo) print `setup error: ...` to stderr and exit 2.

## Why mergeable_state matters

`gh pr checks` only returns checks GitHub has been told about so far. Late-registering workflows (re-runs from label changes, codecov post-back, paths-filter dispatch) can appear AFTER the visible set is terminal -- a false-settle. mergeable_state aggregates branch protection's view of "every required check has reported AND no CHANGES_REQUESTED is active", so it stays `blocked` until every required-but-not-yet-registered check arrives and clears. Codecov coverage states naturally flap during multi-shard CI runs as different shards report at different times; mergeable_state absorbs the flapping and only clears when the aggregate stabilizes.

## Why this script (not a hand-rolled jq loop)

GitHub's check `conclusion` field can be the empty string `""` mid-flight. jq's `// alternative` operator only falls back on null/false, so a hand-rolled `(.conclusion // "in_progress")` reports `=` (empty), and a grep for `=in_progress` returns 0 -> premature SETTLED. This script uses `state` (which goes through gh's bucket-mapping and never returns `""`) to avoid the trap.

## Why the quiet-period gate

CodeRabbit posts inline comments seconds AFTER its CI check transitions to SUCCESS. If `mergeable_state` happens to read `clean` in that window before CR's review-state actually lands, a strict snapshot would emit `settled` prematurely and hand the consumer a half-formed triage list. The script counts items from allow-listed bot authors (`coderabbitai[bot]`, `github-actions[bot]`, `greptile-apps[bot]`, and `codoki-pr-intelligence[bot]`) across reviews + pull-comments + issue-comments, and requires the count to be unchanged across two consecutive 30s polls before terminating. Adds ~30s of latency in the happy path; eliminates the trickle race. Applies to both `settled` and `review-blocked` terminals.

## Args

`$ARGUMENTS` parses to: `<pr_number> [timeout_secs]`. Defaults:
- `pr_number` -- if omitted, resolve from the current branch via `gh pr view`.
- `timeout_secs=1800` (30 min). Bump to 3600 when test shards are slow or many waves are queued.

The poll interval and reviewer set are not configurable -- the script polls every 30s and requires CodeRabbit. That is the only configuration that has ever been needed.

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
bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-watch.sh $pr_number "" $timeout_secs
```

(The empty second arg lets the script auto-detect the repo via `gh repo view`.)

## Step 3 -- Dispatch on the terminal line

When the Monitor reports the script's exit, branch off the single stdout line:
- **`settled head=...`** + Exit 0 -> invoke `/merge-pr <pr>`.
- **`review-blocked head=...`** + Exit 0 -> invoke `/handle-review <pr>`.
- Exit 1 (`timeout: ...` on stderr) -> re-arm with a longer timeout or check `gh pr view <pr>` manually for what is still in flight.
- Exit 2 (`setup error: ...` on stderr) -> the script failed to query the PR. Check `gh` auth state.
