---
description: "Check all PRs in a stack for pending bot reviews, then triage/fix/reply in dependency order"
argument-hint: "[PR numbers (e.g. 32-36 or 32,33,35) -- defaults to auto-detect from current branch]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Edit", "Write", "Agent", "Task"]
---

# Review Stack

Find all PRs in a stack that have pending bot reviews, then process them in
dependency order using `/handle-review` discipline. Fixes cascade: after fixing a
base PR, restack before handling the next PR up the chain.

**Arguments (optional):** "$ARGUMENTS"

**Special arguments:**
- `--clear`: Clear the session breadcrumb file and exit. Use at the start of a new
  working session to reset state from previous sessions.
- `--list`: Show the contents of the session breadcrumb file (unique, still-open PRs)
  without processing any reviews. Useful to see what would be reviewed.

```bash
# Derive a per-repo breadcrumb path so the command is not tied to any one repo.
REPO_NAME="$(basename "$(git remote get-url origin 2>/dev/null)" .git)"
REPO_NAME="${REPO_NAME:-$(basename "$PWD")}"
SESSION_PRS_FILE="/tmp/${REPO_NAME}-session-prs.txt"

if [ "$ARGUMENTS" = "--clear" ]; then
  rm -f "$SESSION_PRS_FILE"
  echo "Session PR breadcrumbs cleared."
  exit
fi

if [ "$ARGUMENTS" = "--list" ]; then
  if [ -f "$SESSION_PRS_FILE" ]; then
    echo "Session PRs:"
    sort -un "$SESSION_PRS_FILE"
  else
    echo "No session PRs recorded."
  fi
  exit
fi
```

**Parallelism strategy:** Data gathering (poll, fetch, pre-triage) is parallelized
across PRs using foreground agents. Fixing is serial because base PR fixes cascade into
dependent PRs via rebase.

---

## Step 1 -- Identify the stack

This step runs first because all subsequent parallel work depends on knowing the PR list.

Resolve `repo` and `me`:

```bash
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
me=$(gh api user --jq .login)
```

### Option A: Explicit PR numbers

If `$ARGUMENTS` contains numbers (e.g. `32-36`, `32,33,35`, or `32 33 35`):
- Parse into a list of PR numbers
- `32-36` means PRs 32, 33, 34, 35, 36
- `32,33,35` or `32 33 35` means those specific PRs

### Option B: Graphite auto-detect

If `$ARGUMENTS` is empty and `gt` is available:

```bash
command -v gt >/dev/null 2>&1 && gt log short 2>/dev/null
```

If Graphite outputs a stack, extract PR numbers from its output.

### Option C: Session breadcrumb file (default)

If `$ARGUMENTS` is empty and Graphite is unavailable, read PR numbers from the
session breadcrumb file. Other skills (`/prep-pr`, `/handle-review`) append PR
numbers to this file as they push PRs, so it tracks exactly what was worked on.

```bash
REPO_NAME="$(basename "$(git remote get-url origin 2>/dev/null)" .git)"
REPO_NAME="${REPO_NAME:-$(basename "$PWD")}"
SESSION_PRS_FILE="/tmp/${REPO_NAME}-session-prs.txt"

if [ -f "$SESSION_PRS_FILE" ]; then
  # Read unique PR numbers, filter to still-open PRs
  session_numbers="$(sort -u "$SESSION_PRS_FILE")"
  open_prs="$(gh pr list --state open --json number,baseRefName,headRefName)"
  # Intersect: keep only PRs that are both in the file and still open
fi
```

Filter `open_prs` to those whose `number` appears in `session_numbers`.
Sort in dependency order (base PRs first, using the base->head chain).

If the breadcrumb file is missing or empty, or the intersection yields nothing,
fall back to the current branch's PR and its chain:

```bash
current_pr=$(gh pr view --json number,baseRefName,headRefName \
  --jq '{number, base: .baseRefName, head: .headRefName}')
```

Walk UP: check if the base branch also has an open PR, repeat until `main`/`master`.
Walk DOWN: check if any other open PR uses the current branch as its base.

If still no PRs found, stop: "No session PRs detected. Pass PR numbers explicitly: `/review-stack 32-36`"

Save the original branch name so you can return to it at the end.

---

## Step 2 -- Parallel: poll readiness, fetch comments

After identifying the stack, launch **one foreground Agent per PR** in a single message
(all agents in parallel). Each agent performs the full data-gathering pipeline for its PR.

### Agent prompt template (one per PR)

Each agent receives this task:

> You are gathering bot review data for PR #<number> in repo <repo>.
>
> **Step A -- NEVER trigger a CodeRabbit review.**
>
> Triggering a CR review is the maintainer's exclusive purview; this command
> must never post `@coderabbitai review` (or invoke any CR-trigger). CR
> auto-review is OFF org-wide, so CR is opt-in/maintainer-allocated -- a stacked
> PR with no CR review is a normal state, not something to fix. **Codoki**
> (`codoki-pr-intelligence[bot]`) is the primary auto-reviewer and reviews every
> push on its own. Poll for and triage whatever reviews already exist; if a CR
> pass would help, note it and let the maintainer allocate one.
>
> **Step B -- Poll for review readiness (geometric cooldown):**
> Poll at 15s, 30s, 60s, 120s intervals. At each interval check:
>
> ```bash
> pending=$(bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh --pending-only <number>)
> unreplied=$(bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh --count-only <number>)
> ```
>
> Ready when `pending == 0` AND `unreplied` count matches the previous check.
> If not stable after 4 polls, report the PR as WAITING with details.
>
> **Step C -- Fetch all unreplied bot comments (overview then full bodies):**
>
> ```bash
> bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh <number>
> bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-read-comments.sh <number>
> bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-read-comments.sh --reviews <number>
> ```
>
> For specific comment IDs only:
>
> ```bash
> bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-read-comments.sh <number> <id1> <id2> ...
> ```
>
> **Return format:**
>
> ```text
> PR: #<number>
> Branch: <head_branch>
> Status: NEEDS WORK / WAITING / CLEAN / NO REVIEWS
> Unreplied comments: <count>
> CHANGES_REQUESTED reviews: <count>
> Coverage: <patch_pct>% (<threshold_state>) | none
> Comments:
> - id: <id>, user: <login>, path: <path>, line: <line>, commit: <sha>, stale: <true/false>, body: <full body>
> - ...
> ```

**Bot identity notes (important -- include in each agent prompt):**
- CodeRabbit reviews: `coderabbitai[bot]`
- Codoki reviews: `codoki-pr-intelligence[bot]` -- COMMENTED reviews with an
  EMPTY review body; all findings are inline comments + a `### Codoki PR Review`
  issue-comment summary, so never infer "no findings" from its review state.
- Codecov (`codecov[bot]`) is NOT a reviewer -- skip it in the unreplied flow.
  Instead, capture the coverage advisory separately:

  ```bash
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh --coverage-only <number> <repo>
  ```

  Returns a JSON object with `status`, `patch_pct`, `threshold_state`
  (`pass`/`fail`/`unknown`), `report_url`. Include `patch_pct` and
  `threshold_state` in the agent's return payload under a `Coverage:` field.

  **Self-skip when no coverage service is active.** If `--coverage-only`
  returns `{"status":"none"}` (no codecov comment on the PR, e.g. a repo with
  no codecov integration), report the Coverage field as `N/A` and treat it as
  a SKIP -- not a failure and not a NEEDS WORK trigger.

**Skip the cooldown** if the user explicitly says reviews are done or asks to proceed
immediately. In that case, skip Step B in the agent prompt and go straight to Step C.

### After all agents return

Collect results from all agents into a unified status table:

```text
| PR   | Branch              | Status     | Unreplied | CR blocked? | Coverage   |
|------|---------------------|------------|-----------|-------------|------------|
| #32  | feat/32-foo         | NEEDS WORK | 3         | Yes         | 92% pass   |
| #33  | feat/33-bar         | CLEAN      | 0         | No          | 78% fail   |
| #34  | feat/34-baz         | WAITING    | ?         | --          | none       |
```

The Coverage column surfaces codecov's patch % plus threshold state, or `N/A`
when `--coverage-only` returned `{"status":"none"}` (no coverage service on the
repo). A `fail` state does NOT automatically move the PR into NEEDS WORK --
coverage regressions are policy signals, not review comments. An `N/A` is a
self-skip, never a blocker. Flag fails to the user in the triage confirmation
(Step 3) so they can decide whether to add tests or accept the patch coverage.

If any PRs are WAITING, report which bots are still pending and ask:
"N PRs still waiting for reviews. Proceed with available reviews, or keep waiting?"

If no PRs need work, say: "All PRs in the stack are clean. Nothing to do." and stop.

---

## Step 3 -- Parallel: pre-triage analysis

For each PR with status `NEEDS WORK`, launch **one foreground Agent per PR** in a single
message to pre-compute the triage. These agents run in parallel.

### Agent prompt template (one per PR)

Each agent receives:

> You are pre-triaging bot review comments for PR #<number> in repo <repo>.
> The PR is on branch <branch>. Here are the unreplied bot comments:
>
> <paste all comments from Step 2 agent output for this PR>
>
> The full PR stack is: <list all PR numbers in dependency order>
> This PR is at position <N> in the stack.
>
> For each comment, read the relevant source code and assign a category:
>
> | Category | Description | Default action |
> |----------|-------------|----------------|
> | **spec-drift** | Generated/spec artifact out of sync with implementation (e.g. an OpenAPI spec, a generated client, a checked-in schema) | Fix now |
> | **error-handling** | Missing error check, raw error in response, swallowed error | Fix now |
> | **test-gap** | Missing test, untested edge case, data race in test | Fix now |
> | **rename-incomplete** | Old name still referenced somewhere | Fix now (batch) |
> | **style** | Formatting, naming, comment wording | Fix now |
> | **false-positive** | Incorrect or inapplicable suggestion | Rebut |
> | **stacked-pr-repeat** | Flags "dead/unused" code wired in a later PR in the stack | Dismiss |
> | **architectural** | Requires design change beyond this PR's scope | Defer (must justify) |
>
> **Stacked PR repeats:** Comments flagging code as "dead" or "unused" when it is
> wired in by a later PR (#<later_pr_numbers>) should be categorized as
> `stacked-pr-repeat`. Note which PR wires it in.
>
> **Stale-diff check:** If ALL comments have `stale: true`, note that this PR is a
> candidate for the stale-diff fast path (batch reply "already fixed in HEAD").
>
> **Grouping:** If multiple comments share the same root cause (e.g., "rename X" at
> lines 50, 120, and 200), group them as a single fix item.
>
> **Return format:**
>
> ```text
> PR: #<number>
> Stale-diff fast path: yes/no
>
> | # | Category | File:Line | Commit | Stale? | Summary | Action | Group |
> |---|----------|-----------|--------|--------|---------|--------|-------|
> | 1 | spec-drift | schema.json:45 | abc1234 | no | Missing "aliases" field in spec | Fix now | -- |
> | 2 | error-handling | worker.py:92 | abc1234 | no | Raw error string in response | Fix now | -- |
> | 3 | false-positive | image.sh:300 | def5678 | yes | Suggests unnecessary nil check | Rebut | -- |
>
> Fix items: <count>
> Rebut items: <count>
> Dismiss items: <count>
> Defer items: <count>
> ```
>
> Do NOT make any code changes. This is analysis only.

### After all triage agents return

Merge the triage results into a combined view. Present to the user:

```text
## Pre-triage results

### PR #32 (3 comments)
| # | Category | File:Line | Summary | Action |
|---|----------|-----------|---------|--------|
| 1 | ... | ... | ... | Fix now |
...

### PR #33 (1 comment)
...

Process N PRs that need work? (yes / pick specific PRs / skip)
```

Ask the user to confirm or adjust using selectable choices:
- "Looks good, proceed with fixes"
- "Adjust category for comment N on PR #X"
- "Show me more context for comment N on PR #X"

Wait for confirmation before proceeding to fixes.

---

## Step 4 -- Serial: fix PRs in dependency order

For each PR that `NEEDS WORK`, in dependency order (base PR first):

### 4a. Stale-diff fast path

If the pre-triage flagged this PR as stale-diff-fast-path eligible (all comments are
stale), offer the fast path:

"All N comments on PR #X target commit XXXX (HEAD is YYYY). Batch-reply all as
'already fixed in HEAD'?"

If confirmed, skip to Step 4f (reply) with "Already addressed in HEAD." for each comment.

### 4b. Switch to the PR's branch

```bash
git checkout <branch_name>
git pull --rebase origin <branch_name>
```

### 4c. Execute fixes

Using the pre-computed triage (no need to re-analyze), for each "Fix now" item:
1. Read the relevant file and understand the surrounding context
2. Make the fix
3. Track what was changed for the reply

For each "Fix now (batch)" group:
1. Find ALL instances across the codebase (not just the lines flagged)
2. Fix all instances in one pass

For each "Defer" item:
1. Create a tracking issue using the `task` template
2. Note the issue number for the reply

**Do not commit between fixes.** Make all changes first, then commit once.

### 4d. Run verification (delegate to gate-runner)

Run the target repo's own gates by delegating to the bundled `gate-runner.py`
(it reads the repo's `.gates.toml`, or falls back through the umbrella ->
CLAUDE.md `## Gates` -> language-basics -> warn-and-proceed chain), the same
runner `/prep-pr` Step 2 uses -- one source of truth, no per-stack detection
re-implemented here:

```bash
if [ -f scripts/gate-runner.py ]; then
  python3 scripts/gate-runner.py
else
  python3 ~/.claude/scripts/gate-runner.py
fi
gate_rc=$?
```

*Illustrative -- this repo's gates (from its `.gates.toml`):* `shellcheck` on
the shell scripts, `ruff check --select F,E741` on the `.py` files, the
guard/steer self-tests, and the `python3 test-*.py` harnesses. Another target
repo declares a different set (or relies on the fallback chain).

If `gate_rc` is non-zero, fix the failures before proceeding.

If the repo uses templ and any `.templ` files were changed (self-skip when no
`.templ` files exist), regenerate:

```bash
if compgen -G '*.templ' >/dev/null 2>&1 || git ls-files '*.templ' | grep -q .; then
  templ generate
fi
```

### 4e. Commit

```bash
git add <specific files that were changed>
git commit -m "address PR review feedback

- <one-line summary per fix>

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### 4f. Reply to all comments in batch

**CRITICAL -- two reply forms, never mix them up:**

- `reply_type: "inline"` comments (inline diff annotations) -- use the **3-arg form**:

  ```bash
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh <PR> <comment_id> '<text>'
  ```

  This posts a threaded reply under the code annotation.

- `reply_type: "top-level"` comments (review body summaries, issue-level) -- these are
  review object IDs, NOT inline comment IDs. The `pulls/comments/{id}/replies` endpoint
  will 404 for them. **Do NOT pass them to the 3-arg form.**

**For inline comments only -- use 3-arg form:**

Fix now:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh <PR> <comment_id> 'Fixed in <short-sha>.'
```

Rebut:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh <PR> <comment_id> '<evidence-based rebuttal>'
```

Stacked-PR repeat:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh <PR> <comment_id> 'This is wired in PR #<later_pr>. Per-PR review limitation on stacked PRs.'
```

Defer:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh <PR> <comment_id> 'Tracked in #<issue-number>. Requires <brief justification for deferral>.'
```

**For review body IDs (reply_type: "top-level") -- use the 4-arg form:**

Review body IDs are review objects (from the reviews API), not inline comment
IDs. The 3-arg form of `reply-comment.sh` will 404 for them. The old
workaround -- a consolidated top-level PR comment -- is blocked by the
no-toplevel-summaries hook and produces Conversation-tab noise anyway.

**Preferred path:** post an inline review comment anchored to the line your
fix touched (or a nearby in-diff line for rebuttals) using the 4-arg form:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/reply-comment.sh <PR> \
  --file <path> --line <n> \
  '<reply text>'
```

The helper auto-resolves PR HEAD SHA, defaults `--side RIGHT`, and errors
cleanly if `--file` / `--line` aren't paired.

Per-category pattern (anchor each to the relevant file:line in the diff):

- **spec-drift / error-handling / test-gap (fixed)**:
  `'Fixed in <short-sha>.'`
- **rename-incomplete (batch)**: anchor to one representative fix line:
  `'Fixed in <short-sha> across <N> call sites.'`
- **false-positive**: anchor to the referenced line (or nearest in-diff
  line in the same file) with the rebuttal.
- **stacked-pr-repeat**:
  `'Wired in PR #<later_pr>. Per-PR review limitation on stacked PRs.'`
- **defer**:
  `'Tracking in #<issue>. <one-line justification>.'`

**Skip the inline reply entirely** when ALL review body findings in this PR
are bug/spec-drift/test-gap addressed by the commit. The commit message
carries the record, `@coderabbitai resolve` sweeps CR's internal tracking,
and an extra inline comment adds no signal.

**Never post a top-level PR comment for dispositions.** The
no-toplevel-summaries hook blocks it. If a review body finding references a
file that is not in the PR's diff at all (rare), the inline API will reject
the `--line`; do not fall back to top-level -- rely on the commit message
plus `@coderabbitai resolve` and note the unthreaded finding in the Step 6
summary.

### 4g. Push

```bash
git push
```

After a successful push, record the PR number in the session breadcrumb file:

```bash
REPO_NAME="$(basename "$(git remote get-url origin 2>/dev/null)" .git)"
REPO_NAME="${REPO_NAME:-$(basename "$PWD")}"
SESSION_PRS_FILE="/tmp/${REPO_NAME}-session-prs.txt"
echo "<PR>" >> "$SESSION_PRS_FILE"
```

### 4h. Cascade check

After pushing fixes to a base PR, ask:

```text
Fixes pushed to #32. This may affect PRs higher in the stack.
Restack now? (yes / no / check first)
```

If yes:

```bash
# Without Graphite (manual rebase chain):
git checkout <next_branch>
git rebase <fixed_branch>
# ... repeat up the chain
```

If rebase conflicts occur, stop and report them. Do not force-resolve conflicts.

**Important:** If a restack changes code that was pre-triaged in Step 3, the triage
for affected PRs may be stale. After restacking, re-check whether pre-triaged comments
still apply to the rebased code. If a comment's target file/line shifted significantly,
note this during the fix phase and adjust.

### 4i. Move to next PR

After restacking (or skipping it), continue to the next PR that needs work.

---

## Step 5 -- Summary

```text
## Stack review complete

| PR   | Fixed | Dismissed | Rebut | Deferred | Pushed |
|------|-------|-----------|-------|----------|--------|
| #32  | 2     | 1         | 0     | 0        | abc123 |
| #33  | 1     | 0         | 0     | 0        | def456 |

Parallelism: Steps 2-3 ran N agents concurrently (data gathering + pre-triage).
Step 4 ran serially in dependency order (cascade constraint).

Codoki auto-reviews the pushed PRs. CodeRabbit re-reviews only if the maintainer allocates a pass - this command never triggers one (CR auto-review is OFF org-wide).

This is the Way.
```

---

## Important rules

- **Dependency order is mandatory for fixes.** Always fix base PRs before their dependents.
- **Data gathering and triage are parallelized.** Launch all agents in a single message.
- **One commit per PR.** All fixes for a single PR go in one commit.
- **Commit first, reply with SHA, then push.** Same as `/handle-review`.
- **Never force-push** unless the user explicitly asks.
- **Never skip the triage confirmation** for genuinely new comments.
- **Auto-dismiss obvious cross-stack repeats** without asking (e.g. "dead code" that's wired in a later PR).
- **If a restack fails with conflicts,** stop and report. Don't auto-resolve.
- **If a restack invalidates pre-triage,** note it during the fix phase and adjust.
- **Codoki re-reviews automatically; CodeRabbit does not (auto-review OFF org-wide -- a CR pass is maintainer-allocated).**
- **Save the original branch** so you can return to it at the end.
