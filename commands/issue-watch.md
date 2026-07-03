---
description: "Wait for a GitHub issue to show new activity (comment / close / label / assignment). Silent until done; one stdout line on completion."
argument-hint: "[--author <login>] <issue number> [owner/repo] [timeout_secs]"
allowed-tools: ["Bash"]
---

# Issue Watch

Wait for a GitHub ISSUE to show new activity, then dispatch off the single terminal line. The issue-side counterpart to `/pr-watch`. An issue has no CI or review-decision, so instead of evaluating a rich terminal state this polls a lightweight snapshot `{comment_count, state, labels, assignees}` captured at watch-start and fires on the FIRST change. Only activity AFTER the watch starts counts.

The script is silent during the wait and emits exactly one line on stdout when done.

## Terminal lines

1. **`closed issue=<n>`** -- the issue was closed (e.g. by a merged PR's closing keyword). Exit 0. Next action: post-merge verification.
2. **`new-comment issue=<n> author=<login> id=<cid>`** (+ the full body on the following lines) -- a new comment appeared (default mode, any author). Exit 0. Next action: read / reply.
3. **`plan-ready issue=<n> author=<login> id=<cid>`** (+ body) -- in `--author` mode, a comment from that author appeared AND stabilized (see below). Exit 0. Next action: steer the CodeRabbit Coding Plan.
4. **`labeled issue=<n> +<label>... -<label>...`** -- labels added/removed. Exit 0.
5. **`assigned issue=<n> +<login>... -<login>...`** -- assignees changed. Exit 0.
6. **`reopened issue=<n>`** -- a CLOSED issue reopened. Exit 0.

`timeout: waited <secs>s` (stderr) -> Exit 1: re-arm with a longer timeout or check `gh issue view <n>` manually. `setup error: ...` (stderr) -> Exit 2: bad args / can't resolve repo.

When several signals move in one poll the priority is: closed, then the comment terminal, then labeled, then assigned, then reopened.

## `--author` mode (the CodeRabbit Coding-Plan case)

A CR issue Coding Plan lands as ONE comment that self-edits over ~10-15 min, so firing on first appearance would surface a half-written plan. `--author <login>` narrows the comment trigger to that author AND auto-stabilizes: once a new comment from `<login>` appears, the loop keeps polling until its body is byte-identical across two consecutive polls, then emits `plan-ready`. `closed` still fires immediately regardless of `--author`.

Note (2026-07 org state): with CR auto-review OFF org-wide (#214), CodeRabbit does NOT auto-post an issue Coding Plan; a plan appears only when the maintainer explicitly requests a CR pass. So `--author coderabbitai` is for watching a MAINTAINER-triggered plan, not an auto-arriving one.

## Args

`$ARGUMENTS` parses to: `[--author <login>] <issue_number> [owner/repo] [timeout_secs]`. Defaults:
- `issue_number` -- required (numeric).
- `owner/repo` -- optional; auto-detected via `gh repo view` when omitted. NOTE the positional order: to set a timeout you must also give the repo (or an empty `""`) before it, e.g. `/issue-watch 217 "" 600`, because a bare `/issue-watch 217 600` binds `600` as the repo.
- `timeout_secs=1800` (30 min).

The poll interval is not configurable in normal use -- the script polls every 30s (`ISSUE_WATCH_POLL_INTERVAL` exists only so the test harness can drive the loop without the wait).

## Step 1 -- Arm the watch

The script is silent until done; the single terminal stdout line becomes the only event. Wait for it without polling.

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/issue-watch.sh $ARGUMENTS
```

(Pass the repo explicitly as the positional after the issue number to target a repo other than the current one; otherwise the script auto-detects it via `gh repo view`.)

## Step 2 -- Dispatch on the terminal line

- **`plan-ready ...`** + Exit 0 -> vet + steer the CR Coding Plan (a consolidated `@coderabbitai <feedback>` reply on the issue), per the BINDING GATE.
- **`new-comment ...`** + Exit 0 -> read the body; reply if it needs action.
- **`closed ...`** + Exit 0 -> run post-merge verification (linked issue closed as expected).
- **`labeled ...` / `assigned ...` / `reopened ...`** + Exit 0 -> triage per the change.
- Exit 1 (`timeout: ...` on stderr) -> re-arm with a longer timeout or inspect `gh issue view <n>` manually.
- Exit 2 (`setup error: ...` on stderr) -> the script failed to query the issue. Check `gh` auth state.
