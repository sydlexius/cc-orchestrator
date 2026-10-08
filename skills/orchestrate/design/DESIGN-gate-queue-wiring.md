# Design: wiring the commands and charters to the gate queue (#538, unit D / #542), and the deferred unit E

Date: 2026-10-07
Status: PROPOSAL, split out in FIX ROUND 3. Prose only: no script logic changes in this unit. The maintainer decisions on #538 and
the two later ones (the runner makes no commit; commits need not be squashed) are BINDING and are designed to, not reopened.
RECONCILED 2026-10-07 against the MERGED pool document (PR #543).
Issues: #538 is the EPIC; #542 is unit D. Unit E is deliberately NOT filed.
Companions: `DESIGN-gate-queue.md` (unit B: the queue, the helper's verbs and exit codes; "queue section N"),
`DESIGN-gate-queue-push.md` (unit C: the tail, the result fields, `stale-base`; "push section N"), `DESIGN-gate-pool.md` (unit A;
"pool section N").
Depends on: unit C complete (C2), unit B's B4 (the cleanup check), and pool PRs A2 (the pool-on command prose this unit builds on)
and A3 (the SKILL.md machine-resource bullet this unit extends).

Scope: a QUEUE PATH in `commands/prep-pr.md` and `commands/handle-review.md`; the `/prep-pr` Step 6 default; one paragraph in
`commands/stack-prs.md`; one rule in three charters; `required-permissions.md`; SKILL.md; the CLAUDE.md architecture entries. It
changes who pushes, so it routes for MAINTAINER MERGE under the operating-model carve-out even though it is prose.

Evidence labels as in the companions. Nothing was run for this document. Line numbers are approximate (READ at main f157846).

---

## Decisions this unit is bound by

13. `/prep-pr` KEEPS its inline Step 2 gate, and the queued tail reuses its receipt.
14. The runner makes no commit; a behind first push is refused `stale-base` and the lead refreshes in its own session.
15. Commits need not be squashed before a push.

Both commands take the queue path ONLY when `gate-enqueue.py enabled` exits 0. Otherwise every block runs exactly as today. No
existing gate is removed. `enabled` can exit 0 only with the pool on, and the pool's budget is written only after pool PRs A1 to A4
are released and deployed and doctor reports every copy pool-capable (pool sections 4 and 9), so the queue path never predates the
pool-on prose or the watchdog. The POOL-ON prose (run a gate block in the background when a budget is configured; exit 75 is NOT RUN,
never "fix the failing gate") is the pool's and lands with PR A2 (pool section 6); this unit assumes it is there.

---

## 1. `/prep-pr`

| Step | Change |
|---|---|
| 1, 1b, 1c | Unchanged and inline. Step 1c still STOPS on an unreviewed behind branch: refreshing before the inline gate is cheaper than gating a stale tree |
| 2 | Unchanged (the pool's background and 75 rules already apply) |
| 2b, 3, 3b, 4a, 5 | Unchanged and inline |
| 6 | Squashing becomes OPTIONAL and the default flips to KEEP the commits (decision 15; section 2) |
| 7 + 8 | REORDERED on the queue path: Step 8a and 8b (issue refs, labels, the template-aware body, the advisory prose-lint, and the "create the PR?" offer, now asked BEFORE the push as open / draft / no PR) run first, then ONE enqueue block, then a background wait. The "no PR" answer takes today's inline Step 7 push instead of the queue (push section 3) |

The enqueue block (leg selection as in "Helper exec paths"; the deployed leg first, like the elmer commands, because the grant names
that path):

```bash
python3 ~/.claude/scripts/gate-enqueue.py add --worktree "$PWD" --tail push [--base "$base"] \
  --title-file "$title_file" --body-file "$body_file" [--label "$label"]... [--milestone "$ms"] [--draft]
```

`--base` is passed only when the PR's base is not the repository default. The block prints `GATE-ENQUEUE: ENQUEUED|REPLACED id=<job>
position=<n> runner=...` and exits 0, 1 (REFUSED, nothing written) or 2 (setup). Then, as a BACKGROUND task:

```bash
python3 ~/.claude/scripts/gate-enqueue.py wait "$job_id" --timeout 1800
```

The timeout matters: without it a hung dispatcher, or a job whose class cannot be read (`gh` down), never returns and the
`runner=hung` report is never seen. On exit 75 the session reads the report and re-arms (the table below).

`wait` prints exactly ONE stdout line, for example `GATE-JOB: DONE id=<job> branch=<b> kind=first gate=reused pushed=yes sha=<sha>
pr=<n> created=yes`. The session reads that LINE and the result file, never the notification's exit code (a backgrounded command's
reported status is the wrapper's). A wait is a watch: never foreground.

| Exit | State | The session does |
|---|---|---|
| 0 | `done` | report the line; for a first push, the PR URL from the result |
| 1 | `failed` | read `log_path`; fix; re-enqueue (back of the class). EXCEPT a `push-failed` whose log shows a hook line `gate-runner: NOT RUN`: that gate did not run (pool section 6, push section 5); re-enqueue and fix nothing |
| 3 | `refused` | act on `reason` (commit, refresh a `stale-base` branch, ...) and re-enqueue. `pushed` says whether an EARLIER attempt may have sent something; `no` means nothing was sent |
| 4 | `superseded` | nothing to do for this submission |
| 5 | `error` | read `pushed` and `pr_number` in the result and reconcile from origin before anything else; re-enqueueing is safe |
| 6 | `cancelled` | somebody withdrew the job on purpose; read `pushed` |
| 7 | `no-runner` | no result exists and the job is still queued: run `/orchestrate:configure`, or `cancel` the job and take the inline path |
| 75 | still in-progress at `--timeout` | re-arm the wait; never treat as failure |
| 2 | usage or setup (including `queue-off`) | fix the invocation, or take the inline path |

Step 2 STAYS INLINE (decision 13): the review and the patch-coverage gate need a green tree and a coverage profile BEFORE the
judgment steps. The queued tail then REUSES the receipt whenever the tree is unchanged, and only that reuse, in a repo with no
pre-push hook, costs 0 (queue section 2): the tail of the common first push waits for no budget and runs no second gate. A branch
refused `stale-base` and refreshed by the lead pays a SECOND gate, and the prose says so (push section 4).

## 2. Squash or keep: neither is assumed

READ (`commands/prep-pr.md`, Step 6, about lines 765 to 830): today the step asks and RECOMMENDS squash. Decision 15 flips the
recommendation to "keep"; squashing stays available as a choice. Nothing in units B or C depends on a squash, in either case: the
receipt binds `HEAD^{tree}` (a squash keeps the tree, so it is reused either way); the `head_sha` pin is whatever the head is when
the enqueue block runs; the title and body are the lead's composition over the whole `base..head` range, never derived from a
commit; a lead-made refresh merge stays in the branch; fix-round replies cite `pushed_sha`. THE ONE ORDERING RULE: whatever Step 6
does, it is done BEFORE the enqueue block, because a commit OR a squash made after enqueueing is a moved head and is `refused`.

## 3. `/handle-review`

Steps 1 to 6 are unchanged, including the Step 5.5 gate and the Step 5.6 hostile pass, which still precede any push. In Step 7 the
GATED PUSH block (about lines 758 to 786) becomes `gate-enqueue.py add --worktree "$PWD" --tail push [--base "$pr_base"]` with NO
PR details (the PR exists), plus the background wait.

- The job is class 1 and has no receipt for the new commit (READ: the block that wrote one is the gated-push block the enqueue
  replaces), so it gates under slots, then pushes.
- `--stale-ok` is no longer typed by the session: the worker derives it from the PR's review activity (push section 5). READ: today
  the block always passes it. The difference is a fix round on a PR with no review activity at all, which is now refused when
  behind instead of pushed behind.
- If no PR is open the job is `refused` and nothing is pushed: `pr-merged`, `pr-closed`, or `no-pr-details` (push section 3).
- PUSH-FIRST is preserved: replies cite the SHA only after `DONE`, using `pushed_sha` from the result.
- `behind_base > 0` in the result is the old `WARN`: the `gh pr update-branch` refresh is still owed AFTER the replies and
  resolves, by the lead. Step 8.5 uses the same block. Fix-round commits are ADDED, never squashed, as today.

## 4. `/stack-prs`, and the draft default

`/stack-prs` is NOT routed through the queue in the first delivery. READ (`commands/stack-prs.md`): `gh stack link` pushes by
itself, which is why `stack-preflight.sh` exists; the queue has no job shape for a link. Its DRAFT DEFAULT is unchanged: link
creates new PRs as drafts and `--open` marks them ready.

The maintainer's ask that `/stack-prs` default to draft is tracked on #537, outside this epic; nothing here depends on it. Unit D
adds ONE paragraph to `commands/stack-prs.md` and one default to `/prep-pr`:

- `commands/stack-prs.md` says that a slice's PR may have been opened through the queue (a PR-number slice), and that nothing about
  the link, the preflight or the draft default changes when the queue is on.
- In `/prep-pr`'s reordered offer (open / draft / no PR), the RECOMMENDED answer is `draft` when `--base` is not the repository
  default branch, that is, when the PR is an upper slice of a stack: it matches what link would have created, and only the bottom
  PR of a stack is reviewed by CodeRabbit. For a PR on the default branch the recommendation stays `open`, as today.

## 5. `# prep-pr-ok` and a push made by a detached process

The enqueue command line is not a push: it contains no upload wording and no `safe-push` word, so the floor's advisory does not
fire and NO `# prep-pr-ok` token is added to it (that would teach the token a second meaning). The inline path is unchanged. The
token's two halves survive: "a gate passed on this tree" is `safe-push.sh`'s receipt leg, and "Steps 1 to 6 ran" stays
instruction-level but gains the `head_sha` pin: what is pushed is EXACTLY the commit that was enqueued.

## 6. Charters, SKILL.md, permissions, CLAUDE.md

- THE ENQUEUE RULE, added to `implementer-charter.md`, `adversarial-review-charter.md` and `adversarial-prep-charter.md`:

  > A push or a PR is requested only by the LEAD, through `gate-enqueue.py add`. A teammate never runs `add`, `cancel` or `stop`
  > and never writes a file under `~/.claude/gate-queue/`; `gate-enqueue.py status` is fine.

  THIS IS A CHARTER RULE AND NOTHING MORE (queue section 8): a queue entry needs no push or PR grant, so the permission system does
  not stand behind it. The earlier draft's "the same charter-level wall as `safe-push.sh`" is withdrawn: that wall has a grant
  behind it, and this one does not. Closing it mechanically is unit E's subject.
- `pr-shipper-brief.md` is NOT CHANGED BY THIS UNIT (ASSUMED): the shipper's stacked pushes stay inline, through the deployed
  `safe-push.sh`; a hook's gate there is already pooled by unit A, and pool PR A2 has already given its step 1 push the pool-on
  prose (pool section 6).
- SKILL.md, the machine-resource bullet (about line 236): PR A3 writes the pool half; this unit adds "pushes and first PRs go
  through the queue when it is on; the lead enqueues, waits in the background, and reads the verdict line".
- `required-permissions.md`: the ONE entry, `Bash(python3 ~/.claude/scripts/gate-enqueue.py *)` plus the plugin-path form, written
  WITHOUT a backtick-Perm wrapper so it is PRINTED for the maintainer and never harvested (the #169 pattern). The same paragraph
  states that no `Edit(...)` rule for the queue directory may ever be granted.
- CLAUDE.md: architecture entries for `gate_queue.py`, `gate-enqueue.py`, the `cleanup-worktree.sh` check, and the two lint lists.

---

## 7. Decomposition: unit D (#542) as one PR

- D1: everything in sections 1 to 6.
- DEPENDENCIES. D1 needs C2, B4, A2 and A3.
- Tier: prose, MAINTAINER MERGE (it changes who pushes). Agent hints: `[mode: default] [model: sonnet] [effort: medium]`
- Acceptance criteria:
  - [ ] With the queue off, both commands run exactly today's blocks.
  - [ ] With it on, Steps 1b, 1c, 2, 2b, 4a (and 5.5, 5.6) still run inline and before the enqueue; no step is removed; Step 6,
        whatever it does, runs before the enqueue block.
  - [ ] `/prep-pr` Step 6 recommends keeping the branch's commits, and no later step, on either path, requires a single commit.
  - [ ] The session waits in the background and reads the verdict line and result file, never a notification's exit code; replies
        cite `pushed_sha` only after `DONE`; an `error` is reconciled from `pushed` before anything is retried; a `stale-base`
        refusal is answered by a refresh in the lead's own session and a re-enqueue, and the prose says this costs a second gate;
        a `push-failed` whose log carries `gate-runner: NOT RUN` is re-enqueued, never "fixed".
  - [ ] No `# prep-pr-ok` token appears on an enqueue line; the new grant is printed for the maintainer, not harvested; the three
        charters carry the enqueue rule and say it is not backed by a grant.
- Test plan: `test-command-positional-args.py`, `test-prep-pr-freshness.py` and the helper-exec-path rules still pass; a dry run of
  both commands with the queue off (identical to today) and on, each with commits kept and with commits squashed.

---

## 8. Later: enforcing the queue as the only push path (unit E, DEFERRED, not filed)

Maintainer note, 2026-10-07: "if this works well, we may want to put hooks to block agents from using git push, safe-push.sh,
etc." This is a later phase, conditional on the queue proving itself. It is a FLOOR change, which the first delivery is not, and it
depends on units A to D, a period of clean running, and the two open questions below.

- WHAT WOULD BE DENIED: a raw upload in any spelling, a direct call of `safe-push.sh`, and a raw PR creation (`gh pr create` or its
  alias `gh pr new`). In their place the agent runs the enqueue helper. Tag pushes stay exempt.
- FOR WHOM (open question 3). SUBAGENTS ONLY turns the existing charter rule into a mechanism and strands nobody when the queue is
  down. EVERY AGENT makes the queue the only agent push path; it needs the escape hatch and needs the queue to cover every
  legitimate push first. What a PreToolUse hook can see is the stdin payload: per the #426 measurement (2026-09-18; cited, not
  re-run) a subagent's payload carries `agent_id` and a tmux teammate's does not, so "subagents only" is decidable; under the
  floor's fail-open rule an unreadable payload ALLOWS. The everyone scope must be inert when the queue is off.
- DIRECT GATE RUNS (open question 4). Denying an agent's direct `gate-runner.py` gate would buy not load (a direct gate already
  waits on the pool) but ORDER and ONE PATH, using unit B's `tail = "gate"` job. It would break, and unit E must resolve first:
  `--pool-run <name>` (the deny must match the gate form only), `/prep-pr` Steps 2 and 5 and the `/handle-review` gate (Step 2b
  would need the coverage profile from the job's run), and the implementer and adversarial-prep charters.
- WHAT IT MUST ALSO CLOSE: the forgeable entry and worker (queue section 8) become the obvious way around a deny, as do
  `GATEQ_HOME` and a hand-set `GATEQ_HOLDER` (pool section 7). Unit E owns all of them.
- THE ESCAPE HATCH (decided by the maintainer): when the queue is broken, the agent ASKS THE MAINTAINER to run the command himself
  with the `!` prefix, which no PreToolUse hook sees. There is NO agent-usable bypass. The ask names the EXACT command and what
  failed, and goes to Slack in that PR's thread.
- STAGING: (1) an advisory rule in `orchestrate-steer.sh` first, to measure how often the old path is still taken; (2) the deny only
  after the queue has run cleanly for a while (the criterion is the maintainer's); (3) the deny lives in `orchestrate-guard.sh` and
  is a DENY-AUTHORITY change: a `_PF_*` fragment with an isolating vector, a BLOCK vector in `--assert-coverage`, the full
  multi-lens review with a permit/deny differential, and maintainer merge.
- WHAT UNITS B TO D KEEP OPEN: nothing in them depends on an agent being ABLE to run the three commands; the helper's name and
  flags contain no upload or merge wording; the inline path stays in both commands. Pushes the queue cannot express (a declared
  rewrite, an `--ungated` push, a first push with NO PR, the lead's refresh merge, `/stack-prs`, the pr-shipper's stack) would be
  stranded by an everyone-scope deny: each gains a declared job field or stays a maintainer `!` action.

## Open questions for the maintainer (unit E only; neither blocks units B to D)

3. Should the deny apply to subagents only, or to every agent including leads?
4. Should a direct `gate-runner.py` gate run by an agent also be denied, leaving `--pool-run <name>` and the enqueue helper as the
   only agent entry points? (yes / no)

(Questions 1 and 2, about a runner-made or GitHub-made refresh merge, are in `DESIGN-gate-queue-push.md`.)

---

## Appendix: what was read

Nothing was run. Read for fix round 3: `commands/prep-pr.md` (Step 6 asks and recommends squash; Step 7 carries `# prep-pr-ok`;
Step 8, 8a, 8b); `commands/handle-review.md` (the Step 7 gated-push block, which writes the receipt and always passes
`--stale-ok`; Steps 5.5, 5.6, 8.5); `commands/stack-prs.md` (link creates drafts; `--open`); `required-permissions.md` (the #169
entry is written without a backtick-Perm wrapper; the deployed-helper glob matches `.sh` only). Not read: SKILL.md line 236 and the
three charters (their line references are carried from the pool document).
