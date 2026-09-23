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
  every push must go through `safe-push.sh` (repo-local `scripts/` only inside cc-orchestrator
  itself, else the plugin copy or the
  deployed `~/.claude/scripts/safe-push.sh`) so the pipe-swallow exit-code bug can't
  silently mask a failed push -- and never pipe it (`| tail`), which rebuilds that same bug
  one layer out (#432). This stub itself never pushes directly --
  it delegates to `/handle-review` -- but the FIX branch below verifies
  the remote ref moved after handle-review returns. Any future
  enhancement to this skill that adds a direct push step MUST use
  `safe-push.sh` by a LITERAL path (prep-pr Step 7's shape), not `git push`.
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

Before round 1, the agent running this loop starts two pieces of AGENT-HELD state
(values it remembers between steps, NOT shell variables - each fenced block below
runs in its own shell, so nothing assigned in one block survives into another):
`prev_thread_unresolved` (empty) and `last_outcome` (empty). Each round the agent
records its 2b branch name (FIX / THREAD / ...) as `last_outcome`, which the
round-cap message in 2c reads.

For each `round` from 1:

### 2a-pre. Bring PR forward if behind base

Check whether the PR is behind its OWN base; if so, refresh it server-side
(default merge-commit mode, never `--rebase`) -- but ONLY when there is no
pending review/triage work. If findings are still open, SKIP the pre-round
refresh and let the fix round advance the branch itself (its push moves HEAD
anyway):

```bash
state_pre=$(gh pr view "$pr_number" --json mergeStateStatus --jq .mergeStateStatus)
base_ref=$(gh pr view "$pr_number" --json baseRefName --jq .baseRefName)
if [ "$state_pre" = "BEHIND" ]; then
  # GATE (#282): only refresh when review is COMPLETE (no unreplied bot findings).
  # A HEAD-moving update-branch dismisses a bot's prior approval and disturbs the
  # incremental-review delta, so it must NOT run while triage is still pending --
  # this contradicts the BEHIND-BASE ROUTING rule in SKILL.md otherwise.
  # Literal helper path in every leg (the "Helper exec paths" rule in prep-pr.md). A missing
  # helper or a failed read leaves unreplied=1, which SKIPS the refresh (fail toward not acting).
  if [ -f scripts/pr-unreplied-comments.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
  elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh' ]; then leg=plugin
  elif [ -f ~/.claude/scripts/pr-unreplied-comments.sh ]; then leg=stable
  else leg=none; fi
  unreplied=1
  [ "$leg" = repo ]   && unreplied=$(bash scripts/pr-unreplied-comments.sh --count-only "$pr_number" 2>/dev/null || echo 1)
  [ "$leg" = plugin ] && unreplied=$(bash '${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh' --count-only "$pr_number" 2>/dev/null || echo 1)
  [ "$leg" = stable ] && unreplied=$(bash ~/.claude/scripts/pr-unreplied-comments.sh --count-only "$pr_number" 2>/dev/null || echo 1)
  if [ "$unreplied" = "0" ]; then
    echo "round <round>: PR #$pr_number is BEHIND $base_ref and review is complete; running update-branch (default merge-commit mode)"
    gh pr update-branch "$pr_number"
    # ADDITIVE: a merge commit advances the HEAD; every existing commit SHA
    # (and every fix SHA cited in a review reply) survives untouched.
    # Loop into 2a so /pr-watch handles the wait.
  else
    echo "round <round>: PR #$pr_number is BEHIND $base_ref but $unreplied finding(s) still pending; SKIPPING the pre-round refresh -- the fix round's push will advance the branch."
    # Do NOT update-branch now: the FIX branch (2b) pushes fixes this round,
    # and that push moves HEAD past base on its own without dismissing a review
    # that has not yet been fully triaged.
  fi
fi
```

**Caveat:** the refresh moves HEAD, so a bot's prior approval is dismissed and
CI re-runs -- an already-CR-approved PR re-spends a review-slot. That is why it
is GATED on review being complete (`pr-unreplied-comments.sh --count-only`
returning `0`): with findings still open the sweep would dismiss a review that
has not been fully triaged and orphan the fix SHAs already cited in replies, so
it defers to the fix round, whose own push advances the branch. What it must
NEVER do is pass `--rebase` (#282): that rewrites every commit SHA, orphans the
fix SHAs cited in review replies, and empties the bot's incremental-review delta.

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
| exit 0 + stdout `thread-blocked head=...` | **THREAD** (#441) |
| exit 0 + stdout `merged head=...` | **MERGED** (#435) |
| exit 0 + stdout `closed head=...` | **CLOSED** (#435) |
| exit 1 + stderr `timeout: ...` | **STALL** |
| exit 2 + stderr `setup error: ...` | **ABORT** |

Branch on the stdout line, not the exit code alone: five different terminals
share exit 0. The stderr `pr-watch: pending=<list>` progress lines (#399) are
informational only and never select a branch.

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
  > Retry the push manually via `cd <worktree> && bash <safe-push.sh> <head_ref>`, where
  > `<safe-push.sh>` is the LITERAL path of the leg `/prep-pr` Step 7 resolves: repo-local
  > `scripts/safe-push.sh` (the first leg, but ONLY inside cc-orchestrator itself - a consumer
  > repo's same-named script never substitutes), else the plugin copy, else the deployed
  > `~/.claude/scripts/` copy. Then re-run `/autofix-pr <pr>`."
  > Exit with status **ABORT**.
- `post_head != pre_head` AND `remote_head == post_head` -> fix pushed
  and verified. Clear `prev_thread_unresolved` (a commit resets the THREAD
  no-progress comparison), increment round counter and loop back to 2a.

#### THREAD

CI is terminal, nobody requested changes, at least one review thread is
unresolved, and GitHub reports the PR `blocked` (#441). The line is
`thread-blocked head=<sha8> unresolved=<n> failing=<n> by=<logins>`. `by=` names
who opened the unresolved threads; `failing=` counts checks in a failed terminal
state from the same read (CI terminal is NOT CI green, so `failing` may be
nonzero). This is DISTINCT from FIX in one way that matters: a thread round is
often closed by reply-and-resolve alone, so handle-review may legitimately make
NO commit. A no-commit THREAD round is NOT by itself a stall; the FIX branch's
"no commits -> STALL" rule must NOT be applied here. What IS a stall is a
no-commit round that changed nothing, which the pre-dispatch check below catches.

**State crosses blocks ONLY as pasted literals.** Each fenced bash block in this
command runs in its OWN shell (the same rule `/prep-pr` documents), and the
pr-watch line arrives from a Skill call, not from any shell. So no variable set
in one block (or by a previous round) exists in the next. Every value a block
needs from outside itself is written into that block, by the agent, as a literal
assignment at its top; every value a later step needs is read by the agent off a
block's PRINTED output and remembered as agent-held state.

`prev_thread_unresolved` is such agent-held state (initialized empty before round
1). It holds the `unresolved` count recorded by the PREVIOUS round's no-commit
THREAD leg. Every other outcome (SUCCESS, FIX, a THREAD round that committed, and
every exit) clears it, so it only ever compares two CONSECUTIVE no-commit thread
rounds.

Pre-dispatch check (run BEFORE invoking handle-review). No helper is executed
here, only text tests. Fill in the two literal assignments at the top before
running it:

```bash
# FILL IN BOTH LITERALS (this block's shell inherits nothing from earlier steps):
# - watch_line: this round's pr-watch stdout line, pasted VERBATIM.
# - prev_thread_unresolved: the unresolved= number this loop recorded after the LAST
#   no-commit THREAD round, or empty ('') if none was recorded / it was cleared.
# Single quotes are safe here ONLY because a pr-watch line carries just a hex sha,
# digits, and GitHub logins (no quote character can occur). A line that somehow
# contained one is not the documented shape: do not paste it, treat it as abort-parse.
# An unfilled placeholder is caught, not guessed: a placeholder watch_line fails the
# anchored parse, and a non-numeric prev_thread_unresolved routes to abort-parse.
watch_line='<paste the pr-watch stdout line verbatim>'
prev_thread_unresolved='<the unresolved= number recorded last THREAD round, or empty>'
# ONE anchored parse of the whole documented shape: it yields all three fields or
# nothing, so a single emptiness test covers every missing/garbled field.
tb=$(printf '%s\n' "$watch_line" | sed -n 's/^thread-blocked head=[0-9a-f]\{8\} unresolved=\([0-9][0-9]*\) failing=\([0-9][0-9]*\) by=\([^ ][^ ]*\)$/\1 \2 \3/p')
tb_unresolved=""; tb_failing=""; tb_by=""
[ -n "$tb" ] && read -r tb_unresolved tb_failing tb_by <<EOT
$tb
EOT
case "$prev_thread_unresolved" in *[!0-9]*) prev_ok=no ;; *) prev_ok=yes ;; esac
if [ -z "$tb" ] || [ "$prev_ok" = no ]; then thread_next=abort-parse
elif [ "$tb_failing" -gt 0 ]; then thread_next=stall-ci
elif ! printf '%s\n' "$tb_by" | tr ',' '\n' | grep -q '\[bot\]$'; then thread_next=stall-human
elif [ -n "$prev_thread_unresolved" ] && [ "$tb_unresolved" -ge "$prev_thread_unresolved" ]; then thread_next=stall-noprogress
else thread_next=handle-review; fi
echo "thread_next=$thread_next unresolved=${tb_unresolved:-?} failing=${tb_failing:-?} by=${tb_by:-?}"
```

Dispatch on `thread_next`, in this order (first match wins):

- `abort-parse` -> the line does not match the documented shape (an older
  pr-watch without `failing=`, a truncated line, or an unfilled placeholder), or
  the pasted `prev_thread_unresolved` is neither empty nor a number. Print the
  line verbatim and exit with status **ABORT**; never guess the missing fields.
- `stall-ci` -> `failing > 0`: a check FAILED. handle-review answers bot
  comments; it does not fix a red CI, so looping would only re-spend rounds.
  Name the failing checks and exit with status **STALL** (the "thread round, CI
  failing" row of the Step 3 matrix):

  ```bash
  # Select on .state with EXACTLY the six-state set scripts/pr-watch.sh counts as
  # failing=, never on .bucket: gh buckets STARTUP_FAILURE as "pending", so a bucket
  # filter would return no names for a check pr-watch counted.
  gh pr checks "$pr_number" --json name,state --jq '.[] | select(.state == "FAILURE" or .state == "ERROR" or .state == "CANCELLED" or .state == "TIMED_OUT" or .state == "ACTION_REQUIRED" or .state == "STARTUP_FAILURE") | .name'
  ```

  Print "round <round>: thread-blocked with <failing> failing check(s): <names>.
  CI failure is not something handle-review's thread replies fix; fix CI first."
- `stall-human` -> no login in `by=` ends in `[bot]`, so every unresolved thread
  was opened by a human. handle-review only triages bot comments, so it cannot
  make progress here. Print "round <round>: thread-blocked by human reviewer(s)
  <by> only; NEEDS YOU - answer or resolve those threads (handle-review handles
  bot comments only)." and exit with status **STALL** (the "thread round, human
  threads" row).
- `stall-noprogress` -> the previous round was a no-commit THREAD round whose
  replies all landed, and the watch still reports the same or MORE unresolved
  threads. Another handle-review round would reply to nothing new. Print
  "round <round>: <unresolved> thread(s) still unresolved after last round's
  replies (was <prev_thread_unresolved>). Likely CodeRabbit declined to
  auto-resolve after the reply, or a human-opened thread is among them. RAISE
  this to the maintainer; NEVER force-resolve a CodeRabbit thread." and exit
  with status **STALL** (the "thread round, no progress" row).
- `handle-review` -> proceed below.

Capture `pre_head` exactly as in FIX, invoke `/handle-review <pr_number>` via
Skill, then read `post_head` and `remote_head` the same way.

- `post_head != pre_head` -> the round committed a fix. Clear
  `prev_thread_unresolved`, then apply the FIX branch's push verification
  unchanged (ABORT on `remote_head != post_head`, else increment the round and
  loop back to 2a).
- `post_head == pre_head` -> verify progress from the comment state instead of
  from a commit:

  ```bash
  # Literal helper path in every leg (the "Helper exec paths" rule in prep-pr.md). A missing
  # helper or a failed read leaves unreplied=unknown, which is NOT progress (fail toward STALL).
  if [ -f scripts/pr-unreplied-comments.sh ] && jq -e '.name == "orchestrate"' .claude-plugin/plugin.json >/dev/null 2>&1; then leg=repo
  elif [ -f '${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh' ]; then leg=plugin
  elif [ -f ~/.claude/scripts/pr-unreplied-comments.sh ]; then leg=stable
  else leg=none; fi
  unreplied=unknown
  [ "$leg" = repo ]   && unreplied=$(bash scripts/pr-unreplied-comments.sh --count-only "$pr_number" 2>/dev/null || echo unknown)
  [ "$leg" = plugin ] && unreplied=$(bash '${CLAUDE_PLUGIN_ROOT}/scripts/pr-unreplied-comments.sh' --count-only "$pr_number" 2>/dev/null || echo unknown)
  [ "$leg" = stable ] && unreplied=$(bash ~/.claude/scripts/pr-unreplied-comments.sh --count-only "$pr_number" 2>/dev/null || echo unknown)
  ```

  - `unreplied == 0` -> every finding is replied. Record, as agent-held state,
    `prev_thread_unresolved` = the `unresolved=` value printed on THIS round's
    `thread_next=` output line (read off the printed line; the pre-dispatch
    block's shell and its `tb_unresolved` are gone by now). Next THREAD round,
    paste that number into the pre-dispatch block's `prev_thread_unresolved`
    literal. Print "round <round>: thread
    round replied without a commit; re-watching to confirm resolution.", and
    increment the round and loop back to 2a. The next `/pr-watch` IS the
    resolution check: if the threads are now resolved it moves on to `settled`
    (or another terminal); if it emits `thread-blocked` again with the same or
    higher `unresolved`, the `stall-noprogress` check above exits at once rather
    than burning rounds to the cap.
  - anything else (a non-zero count, or `unknown`) -> handle-review neither
    committed nor replied. Print "round <round>: thread-blocked by <by>, but
    handle-review made no commit and <unreplied> finding(s) remain unreplied."
    and exit the loop with status **STALL** (the "Inspect the open threads" row
    of the Step 3 matrix).

#### MERGED

The PR merged while the loop was running (#435). Print "round <round>: PR #<pr>
is already merged. Next: /post-merge-cleanup <pr>." and exit the loop with
status **MERGED**. Do NOT invoke `/post-merge-cleanup` yourself.

#### CLOSED

The PR was closed without merging (#435). Print "round <round>: PR #<pr> was
closed without merging; nothing to fix." and exit the loop with status
**CLOSED**.

#### STALL

`/pr-watch` timed out, OR a FIX-round handle-review made no commits, OR a
THREAD round stalled (a failing check, human-only threads, no progress after a
replied round, or neither committed nor replied - each prints its own line and
maps to its own Step 3 row; the CR-state case analysis below is for the timeout
and FIX sub-cases). Distinguish sub-cases so we report
something useful. Before timing out, the watch's stderr `pr-watch: pending=<list>`
lines (#399) name what it was holding on; quote the last one in the report.

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

If `round >= max_rounds` after a FIX or THREAD iteration completes, exit the loop
with status **CAP**. The message depends on `last_outcome` (the agent-held branch
name recorded in 2b, not a shell variable):

- `last_outcome == FIX`:

  > "Hit round cap of <max_rounds>. CR is still flagging findings; this PR
  > may be in a sticky pattern (e.g. a fix introduces a new finding next
  > round). Manual triage recommended: `gh pr view <pr>` +
  > `bash <pr-unreplied-comments.sh> <pr>` (the LITERAL path of the resolved leg: repo-local
  > `scripts/pr-unreplied-comments.sh` first, but ONLY inside cc-orchestrator itself; else the
  > plugin copy; else the deployed `~/.claude/scripts/` copy)."

- `last_outcome == THREAD`:

  > "Hit round cap of <max_rounds> on a thread-blocked PR. Review threads kept
  > re-opening or staying unresolved across rounds (last watch: unresolved=<n>
  > by=<by>). Inspect the open threads with `bash <pr-unreplied-comments.sh> <pr>`
  > (same LITERAL-leg rule as above). If CodeRabbit declined to auto-resolve a
  > thread, RAISE it to the maintainer; NEVER force-resolve a CodeRabbit thread."

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
Exit:      <SUCCESS | MERGED | CLOSED | STALL | CAP | ABORT | USER-ABORT>
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
| MERGED | `/post-merge-cleanup <pr>` |
| CLOSED | (no suggestion -- the PR was closed without merging) |
| STALL (case 1 or 2) | Wait 15-30 min, then re-run `/autofix-pr <pr>` |
| STALL (case 3) | Inspect `gh pr checks <pr>`; resolve the holdout |
| STALL (thread round, no commit and no reply) | Inspect the open threads with the unreplied-comments script, then `/handle-review <pr>` |
| STALL (thread round, CI failing) | Fix the failing check(s) the report names; handle-review does not fix CI |
| STALL (thread round, human threads) | NEEDS YOU: answer or resolve the human-opened threads |
| STALL (thread round, no progress) | RAISE to the maintainer (CR declined to auto-resolve, or a human thread); never force-resolve a CR thread |
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
