---
description: "Loop /pr-watch -> /handle-review until CR settles or round cap hits. Stub -- single PR only."
argument-hint: "<PR#> [max-rounds=6] [timeout-per-round=1800]"
allowed-tools: ["Bash", "Skill", "Read", "Grep"]
---

# Auto-Fix PR Loop

Drive a PR through repeated `/pr-watch -> /handle-review -> push` cycles
until CodeRabbit has no new findings on HEAD or a round cap kicks in.

This is the post-push companion to `/prep-pr`: `/prep-pr` is the pre-push
review-toolkit gate that minimizes initial findings; `/autofix-pr` then
absorbs whatever CR (and Copilot, if active) flag after the push, batching
fixes round-by-round.

**Inputs:** "$ARGUMENTS"

Parse: first arg = PR number (REQUIRED), second = max rounds (default 6),
third = per-round watch timeout in seconds (default 1800 = 30 min).

## Scope and limits of this stub

- **Single PR only.** For multi-PR sweeps run this skill serially per PR --
  never in parallel. Cross-worktree concurrent gates blow up the go-build
  cache (see `feedback_no_parallel_heavy_gates`).
- **CR silence is ambiguous.** The stall branch below explicitly distinguishes
  "CR silent = not yet reviewed / rate-limited" from "CR reviewed, nothing to
  say" -- do not treat an empty review as a clean pass.
- **No auto-merge.** Even on the success terminal this skill stops and
  defers to the user. Merging is `/merge-pr <pr>` once the user confirms.
- **Round cap = 6 by default.** PR #1484's 12-round history (per
  `feedback_cap_cr_rounds`) is the precedent for offering an early exit
  ramp rather than grinding to convergence.
- **Push is via the safe-push wrapper.** Per `feedback-use-safe-push`,
  every push must go through `${CLAUDE_PLUGIN_ROOT}/scripts/safe-push.sh` (the
  repo-agnostic gist version) so the pipe-swallow exit-code bug can't
  silently mask a failed push. This stub itself never pushes directly --
  it delegates to `/handle-review` -- but the FIX branch below verifies
  the remote ref moved after handle-review returns. Any future
  enhancement to this skill that adds a direct push step MUST use
  `bash ${CLAUDE_PLUGIN_ROOT}/scripts/safe-push.sh`, not `git push`.
- **Stale-base awareness.** Between rounds, another merge can land on
  the PR's base branch (whatever `baseRefName` says -- never assume
  `main`) and leave this PR behind base. mergeStateStatus reports BEHIND
  in that case. The pre-round check below tests for it and triggers the
  PLAIN `gh pr update-branch <pr>` -- its DEFAULT merge-commit mode,
  which is ADDITIVE: it advances the ref without rewriting a single
  existing commit. NEVER pass `--rebase` here (#282): this loop runs on
  exactly the reviewed PR that carries bot findings and cited fix SHAs,
  and a rebase rewrites every commit SHA, orphaning every SHA cited in a
  review reply and emptying the bot's incremental-review delta. Without
  the refresh, `/pr-watch` would still report `settled` (CR happy + CI
  green on the stale HEAD), but the actual merge would fail or
  auto-update-branch on the merge attempt -- creating a wasted round.

---

## Step 0 -- Parse inputs

```bash
pr_number="$1"
max_rounds="${2:-6}"
per_round_timeout="${3:-1800}"
```

If `pr_number` is empty, stop: "PR number required: /autofix-pr <PR#>".

---

## Step 1 -- Pre-flight

Resolve repo + PR head + worktree:

```bash
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
state=$(gh pr view "$pr_number" --json state --jq .state)
head_ref=$(gh pr view "$pr_number" --json headRefName --jq .headRefName)
author_login=$(gh pr view "$pr_number" --json author --jq '.author.login // ""')
worktree=$(git worktree list | grep -F "[$head_ref]" | cut -d' ' -f1)
```

Gate on each:
- `state != OPEN` -> stop: "PR #<n> is <state>; nothing to autofix."
- `author_login == dependabot[bot]` -> stop: "PR #<n> is a Dependabot PR;
  `/autofix-pr` is for user-owned PRs only (the foreign-author merge commit
  makes Dependabot refuse to rebase -- see `feedback_no_update_branch_on_dependabot`).
  Do not proceed."
- `worktree` empty -> stop: "No local worktree on `<head_ref>` -- can't
  apply fixes. Recreate the worktree, then re-run."

Print the starting line:

```text
Auto-fix loop for PR #<n> in <worktree>.
Max <max_rounds> rounds, <per_round_timeout>s per /pr-watch.
```

Ask the user once before entering the loop:

> "Start the loop? (go / abort)"

If anything other than `go`, exit with USER-ABORT.

---

## Step 2 -- Loop (round 1..max_rounds)

For each `round` from 1:

### 2a-pre. Bring PR forward if behind base

Check whether the PR is behind its OWN base; if so, refresh it server-side
(default merge-commit mode, never `--rebase`) before watching:

```bash
state_pre=$(gh pr view "$pr_number" --json mergeStateStatus --jq .mergeStateStatus)
base_ref=$(gh pr view "$pr_number" --json baseRefName --jq .baseRefName)
if [ "$state_pre" = "BEHIND" ]; then
  echo "round <round>: PR #$pr_number is BEHIND $base_ref; running update-branch (default merge-commit mode)"
  gh pr update-branch "$pr_number"
  # ADDITIVE: a merge commit advances the HEAD; every existing commit SHA
  # (and every fix SHA cited in a review reply) survives untouched.
  # Loop into 2a so /pr-watch handles the wait.
fi
```

**Caveat:** the refresh moves HEAD, so a bot's prior approval is dismissed and
CI re-runs -- an already-CR-approved PR re-spends a review-slot. This stub does
it anyway because a stale base eventually blocks merge. What it must NEVER do is
pass `--rebase` (#282): that rewrites every commit SHA, orphans the fix SHAs
cited in review replies, and empties the bot's incremental-review delta.

**Never** call `gh pr update-branch` on a Dependabot PR (per
`feedback_no_update_branch_on_dependabot` -- the foreign-author merge
commit makes Dependabot refuse to rebase). This skill is for user-owned
PRs only.

### 2a. Watch for settle

Print one line: `round <round>/<max_rounds>: watching PR #<pr> for settle`.

Invoke `/pr-watch` via the Skill tool with arguments `<pr_number>
<per_round_timeout>`. Capture the terminal outcome:

| pr-watch result | branch |
|---|---|
| exit 0 + stdout `settled head=...` | **SUCCESS** |
| exit 0 + stdout `review-blocked head=...` | **FIX** |
| exit 1 + stderr `timeout: ...` | **STALL** |
| exit 2 + stderr `setup error: ...` | **ABORT** |

### 2b. Dispatch on outcome

#### SUCCESS

PR is settled per CR + CI + branch-protection's combined view. Print:

```text
round <round>: PR #<pr> settled (CR approved/commented, CI green,
mergeable_state in {clean, unstable, has_hooks}).
Next: /merge-pr <pr> when ready.
```

Exit the loop with status **SUCCESS**. **Do NOT** invoke `/merge-pr`
yourself -- merge is the user's call.

#### FIX

CR posted CHANGES_REQUESTED against HEAD. Capture pre-fix HEAD so we can
detect a no-op handle-review:

```bash
pre_head=$(git -C "$worktree" rev-parse HEAD)
```

Invoke `/handle-review` via Skill with arg `<pr_number>`. handle-review
will: parse all unreplied bot comments, fix in one pass, reply in batch,
and push once.

After handle-review returns, check whether it actually pushed AND the
remote received the new commit (per `feedback-use-safe-push` --
handle-review's internal push uses raw `git push`, which can silently
fail through pipe-swallow):

```bash
post_head=$(git -C "$worktree" rev-parse HEAD)
remote_head=$(git -C "$worktree" ls-remote origin "refs/heads/$head_ref" | cut -f1)
```

- `post_head == pre_head` -> handle-review made no commits. Treat as
  **STALL** -- print "round <round>: handle-review made no commits;
  treating as stall." and fall through to the STALL branch.
- `post_head != pre_head` AND `remote_head != post_head` -> a fix was
  committed locally but did NOT reach origin. Print:
  > "round <round>: local HEAD advanced to `<post_head>` but origin/<head_ref>
  > is still `<remote_head>`. This is the pipe-swallow silent-failure mode.
  > Retry the push manually via `cd <worktree> && bash ${CLAUDE_PLUGIN_ROOT}/scripts/safe-push.sh`,
  > then re-run `/autofix-pr <pr>`."
  > Exit with status **ABORT**.
- `post_head != pre_head` AND `remote_head == post_head` -> fix pushed
  and verified. Increment round counter and loop back to 2a.

#### STALL

`/pr-watch` timed out, OR handle-review made no commits. Distinguish
sub-cases so we report something useful.

**Important: do NOT use review.commit_id to decide "has CR reviewed
HEAD".** GitHub silently rewrites the `commit_id` field on every
existing review when the PR is rebased, so `commit_id == HEAD` returns
the pre-rebase review's verdict as if it applied to the new HEAD. See
`reference_cr_review_commit_id_quirk`. Use the committer-date
comparison instead:

```bash
current_head=$(gh pr view "$pr_number" --json headRefOid --jq .headRefOid)
head_committer_date=$(gh api "repos/$repo/commits/$current_head" \
  --jq '.commit.committer.date' 2>/dev/null || true)

# Any CR review at all, regardless of age:
cr_any=$(gh api "repos/$repo/pulls/$pr_number/reviews" \
  --jq '[.[] | select(.user.login=="coderabbitai[bot]")] | length')

# CR review on the CURRENT HEAD (review submitted at or after the
# HEAD commit's committer date):
cr_on_head_state=$(gh api "repos/$repo/pulls/$pr_number/reviews" \
  --jq --arg head_date "$head_committer_date" '
    [.[] | select(.user.login=="coderabbitai[bot]" and .submitted_at >= $head_date)]
    | sort_by(.submitted_at) | last | .state // ""')
```

Three cases:

1. `cr_any == 0` -- CR has never reviewed this PR at all. Print:
   > "round <round>: CR has not posted any review on PR #<pr>. Likely
   > rate-limited or queue backlog. Per `feedback_cr_rate_limit_budget`
   > the budget is ~6/hr; if this PR was pushed in a burst, wait 10-30
   > minutes and re-run `/autofix-pr <pr>`. STALL."

2. `cr_any > 0` AND `cr_on_head_state == ""` -- CR has reviewed an
   earlier state of this PR but hasn't reviewed the current HEAD (the
   stale-review-after-rebase case). Print:
   > "round <round>: CR has prior reviews on PR #<pr> but none against
   > the current HEAD `<short(current_head)>` (committer.date
   > `<head_committer_date>`). CR has not caught up to the latest push.
   > Wait, then re-run. STALL."

3. `cr_on_head_state in {COMMENTED, APPROVED}` -- CR reviewed HEAD and
   is satisfied, but mergeable_state is blocking. Probably a
   late-arriving CI check or branch protection. Print:
   > "round <round>: CR reviewed HEAD as <state> but mergeable_state is
   > still blocked. Inspect `gh pr view <pr>` and `gh pr checks <pr>`
   > for the holdout. STALL."

Exit the loop with status **STALL**.

#### ABORT

`/pr-watch` returned a setup error. Print the stderr line verbatim and
exit with status **ABORT**.

### 2c. Round cap check

If `round >= max_rounds` after a FIX iteration completes, exit the loop
with status **CAP**:

> "Hit round cap of <max_rounds>. CR is still flagging findings; this PR
> may be in a sticky pattern (e.g. a fix introduces a new finding next
> round). Manual triage recommended: `gh pr view <pr>` + `${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh <pr>`."

Per `feedback_cap_cr_rounds`, do NOT silently continue past the cap.
Offer the user an explicit "bump cap" path: "Re-run with
`/autofix-pr <pr> 12` if you want to extend."

---

## Step 3 -- End-of-loop summary

Always print at the end:

```text
== /autofix-pr summary ==
PR:        #<pr_number>
Worktree:  <worktree>
Rounds:    <consumed>/<max_rounds>
Exit:      <SUCCESS | STALL | CAP | ABORT | USER-ABORT>
Final state:
  state=<gh pr view state>
  reviewDecision=<gh pr view reviewDecision>
  mergeStateStatus=<gh pr view mergeStateStatus>
Next:      <one-line suggestion based on exit>
```

Suggested next-step matrix:

| Exit | Suggested next |
|------|----------------|
| SUCCESS | `/merge-pr <pr>` |
| STALL (case 1 or 2) | Wait 15-30 min, then re-run `/autofix-pr <pr>` |
| STALL (case 3) | Inspect `gh pr checks <pr>`; resolve the holdout |
| CAP | Manual triage via `gh pr view <pr>` + unreplied-comments script |
| ABORT | Fix the setup issue surfaced by pr-watch, re-run |
| USER-ABORT | (no suggestion -- user explicitly stopped) |

---

## Future enhancements (intentionally not in the stub)

- **Multi-PR mode** (`--all-open-by-me` or accept a list of PR numbers).
  Must loop serially across worktrees, never parallel (gate cache
  contention per `feedback_no_parallel_heavy_gates`).
- **Reviewer-bot satisfaction.** This stub treats CR APPROVED as the sole
  satisfaction signal. Other reviewer bots (Greptile, Codoki, and Copilot
  when active) have their findings surfaced and triaged through the inner
  /handle-review, but do not yet gate the loop's SUCCESS condition. All
  reviewer bots are on equal footing -- to make one block SUCCESS, add its
  satisfaction check there.
- **Severity filter** (`--severity=critical+important` to skip
  triaging nit-class findings into fix rounds).
- **Auto-merge on SUCCESS** (opt-in via `--merge` flag with an explicit
  confirmation prompt).
- **Inter-round push debounce** if cross-worktree multi-PR mode lands --
  must coordinate to ensure only one pre-push hook runs at a time.
