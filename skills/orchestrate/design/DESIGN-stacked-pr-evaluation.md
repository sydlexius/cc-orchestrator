# Stacked PRs: re-test of the three failure modes (#355)

**VERDICT: THE RULE STANDS, and it applies to EVERY repo running this
bot-review workflow, not just this one.** All three original failure modes
reproduce against current GitHub, measured 2026-08-13. But the mechanical modes
are not what actually costs the maintainer time, and this doc is ordered wrong if
it implies they are -- see "What the cost actually is" immediately below.

Two things carry the verdict, and neither is repo-local:

1. **ORCHESTRATION OVERHEAD, the lived cost** (maintainer, 2026-08-13, from two
   stacked PRs in `stillwater` the day before: "the orchestration was the PITA").
2. **The SHA-rewrite hazard**, which is what makes stacks unfixable rather than
   merely annoying.

The CI mode is real but was RANKED TOO HIGH in the first draft of this doc:
the maintainer's assessment is that it "wasn't a deal breaker (since it would
eventually run)". Recorded as measured, demoted in weight.

Scope: an empirical re-test only. It changes no code, wires nothing into any
skill, and widens no CI trigger. Every follow-on named below is a separate,
CR-required change.

## What the cost actually is: the orchestration, not the mechanics

This section is first because the empirical modes below, taken alone, mis-rank the
problem. The maintainer ran two stacked PRs in `stillwater` on 2026-08-12 and
reported the cost plainly: **the orchestration was the pain**, and the missing CI
on the child PR "wasn't a deal breaker (since it would eventually run)".

That is a measurement this evaluation could not have produced from scratch
branches, and it changes the ordering. A failure mode that BLOCKS is visible and
gets handled; a mode that makes every routine operation cost N times as much is
paid silently, every round, by the lead.

**The mechanism, verified in-tree: the PR-lifecycle tooling has no concept of a
dependent PR.** Eight of the nine `commands/*.md` skills -- `prep-pr`,
`handle-review`, `merge-pr`, `pr-watch`, `post-merge-cleanup`, `autofix-pr`,
`request-review`, `push-release` -- take a SINGLE `pr_number` and know nothing
about a base that is itself a PR. (`review-stack` is ordering-aware, but its
"stack" is the SHIPPER QUEUE, not a dependent-base chain.) So with a stack the
lead must:

- run every skill once per PR, in an order nothing enforces;
- hold "which PR is blocked on which" in its own context, where it does not
  survive compaction and is not in `SESSION-STATE.md`'s reconstructable set;
- re-derive the whole graph after each merge, because each merge moves a base;
- and re-run gates on the child for a reason unrelated to the child's own code.

None of that is a bug in any one skill. It is the absence of a shape, and adding
that shape is a large, CR-required change to nine skills -- which is precisely
the follow-on this issue says NOT to do without a favorable verdict. The verdict
is unfavorable, so the shape stays unbuilt, and the cost stays real for anyone
who stacks anyway.

## Scope: this is a WORKFLOW rule, not a repo rule

The dispositive fact lives in the USER-GLOBAL `~/.claude/CLAUDE.md`, so it travels
to every repo:

- bot-review order is "push -> **reply citing the fix SHA** -> resolve threads";
- "never amend/squash/rebase-rewrite already-pushed history. A new SHA **orphans
  every cited SHA** and empties the incremental-review delta".

`gh stack sync` cascade-rebases and force-pushes WHEN THE TRUNK HAS MOVED, so the
conflict is with the workflow, not with any repo's settings. The trunk-moved
condition is not a mitigation: a stack exists precisely to track a moving base, and
the sync that does nothing is the sync you did not need. The auto-close mode
involves no config at all.

Even the CI mode is not repo-local, though it was assumed to be: `stillwater`'s
`ci.yml` carries the same `pull_request: branches: [main]` filter, and it has SIX
filtered workflows (`ci`, `gate`, `codeql`, `security`, `bruno-ci`, `pages`) to
this repo's one. A non-`main`-base PR there is missing MORE required checks, not
fewer.

## What to do instead (the positive procedure)

The rule is a prohibition and kept reading as one, which left "so what do I do"
unanswered. Stated directly:

**Dependent work is SERIALIZED, not stacked.** For B depending on A:

1. Build A. Open PR A. Merge it. **B stays a LOCAL branch the whole time** -- do
   not open a PR for it.
2. Pull `main`.
3. `git rebase main` on B. This is the rewrite the rule forbids elsewhere, and it
   is FREE here: B has no PR, no reviews, and no cited SHAs. CLAUDE.md already
   permits amend/squash/rebase freely BEFORE the first push.
4. Open PR B off `main`. It is an independent PR; all three failure modes are
   structurally absent, and every skill's single-`pr_number` shape fits.

This is exactly the hand-serialization performed for #352/#353 on 2026-07-30. The
finding is that it WAS the correct procedure, not a workaround for absent tooling.

**If B cannot wait for A: fold, do not stack.** One PR containing both, with a
stated size override. #382 is the worked example -- one issue, one mechanism, 1078
LOC, and it reviewed cleanly because the halves prove each other. Splitting it
would have forced a reviewer to hold three PRs in their head to judge any one.
A foundation refactor that genuinely stands alone is still its own PR first -- that
is the existing "decompose before building" rule, and it is independent-off-`main`,
not a stack.

**The residual case with no good answer:** genuinely dependent work where A is slow
to merge (blocked on review budget or an unallocated CR pass). B waits, and that is
a real cost this verdict does not remove. The escape valve, if it ever bites hard,
is `gh pr update-branch` (merge-commit, ADDITIVE) which keeps cited SHAs intact --
per-PR and manual, never `gh stack sync`. That is a valve, not a workflow.

## Why this was re-opened

The standing rule against dependent PR stacks came from the #84-87 thrash and
cited three failure modes. GitHub has since shipped first-class stacked-PR support
and a `gh stack` CLI extension, which is enough of a landscape change to justify
re-testing -- but not enough to overturn the rule on a reading of a help text,
which is all anyone had done. The motivating case was real: in the 2026-07-30
session #352's fix depended on #353's base and was serialized by hand.

## Method

A three-level scratch stack (`scratch/355-trunk` <- `-base` <- `-head`) and a
second two-level stack for the merge-commit path, on `sydlexius/cc-orchestrator`,
2026-08-13. Every test ran against real GitHub on real PRs, never a fixture or a
help text. NOTHING TOUCHED `main`: the scratch trunk was the merge target
throughout, and all six scratch branches are deleted.

FOUR PRs, not two, and their lifecycles differ -- the distinction is the finding,
so it is recorded per-PR rather than summarised:

| PR | Role | Outcome | How its base branch was deleted |
|---|---|---|---|
| [#383](https://github.com/sydlexius/cc-orchestrator/pull/383) | lower, squash path | MERGED | n/a (it was the lower PR) |
| [#384](https://github.com/sydlexius/cc-orchestrator/pull/384) | upper, squash path | **CLOSED** | `gh pr merge 383 --squash --delete-branch` (CLI) |
| [#385](https://github.com/sydlexius/cc-orchestrator/pull/385) | lower, merge-commit path | MERGED | n/a |
| [#386](https://github.com/sydlexius/cc-orchestrator/pull/386) | upper, merge-commit path | **CLOSED** | `DELETE /repos/:o/:r/git/refs/heads/...` via `gh-delete-branch.sh` (API), issued SEPARATELY, after the merge |

Tooling: `gh` with the `github/gh-stack` extension installed. `gh stack` itself
was NOT used to create, sync, or merge these PRs -- the failure modes are about
what GitHub does to a dependent PR, and `gh stack merge` is a merge path that
carries the same human gate as `gh pr merge`.

## Failure mode 1: CI does not run on a non-`main` base -- CONFIRMED LIVE

`.github/workflows/ci.yml` filters `on.pull_request.branches` to `[main]` (cited by
key, not line number, so this record does not rot as the workflow moves), so a
PR against any other base never starts the workflow.

Measured on #383 (base `scratch/355-trunk`): four checks appeared -- `Analyze
Actions`, `Analyze Python`, `CodeQL`, `CodeRabbit` -- and **neither `gates
(ubuntu-latest)` nor `gates (macos-latest)` ran at all**. CodeQL runs because its
own workflow carries no `branches` filter, which is exactly why a glance at the
Checks tab is misleading: it looks busy while the required checks are absent.

This is a one-line repo config fact, not a law of GitHub. Removing the filter
would fix it -- and would also run the full matrix on every PR against every base,
which is a deliberate scope decision, not a side effect of a stacks evaluation.
Left unchanged.

### What this mode independently confirmed (#375, merged the same day)

PR #383 reported `mergeStateStatus=CLEAN` while two required checks had never run --
the exact shape #375 was opened for, arising here spontaneously rather than by
construction. The oracle merged hours earlier was run against it:

```
BLOCK: required check(s) did not run on #383 (absent from the rollup):
  gates (macos-latest), gates (ubuntu-latest)
  expected set from default branch 'main' (fallback: base 'scratch/355-trunk'
  requires no checks); an absent check is not a passing check (#375).
```

It blocked, naming both, and correctly reported that it had fallen back to the
default branch's set because the scratch base required nothing. That is
independent confirmation on a live PR, and it is worth recording here because the
the #375 design doc's own motivating measurement is no longer reproducible (its PR was
auto-retargeted before merge).

## Failure mode 2: the upper PR auto-closes -- CONFIRMED LIVE, and the cause is isolated

Both merge shapes were tested, and the delete was separated from the merge so the
cause is established rather than assumed:

| Action | Upper PR |
|---|---|
| squash-merge lower + `--delete-branch` (#383 -> #384) | **CLOSED** |
| merge-commit lower, base branch KEPT (#385 -> #386) | stayed **OPEN** |
| ...then delete the base branch (#386) | **CLOSED** |

**The merge does not close the upper PR. Deleting the base branch does.** Both
timelines record `base_ref_deleted` followed by `closed`, with no retarget event.

**THE CAUSE IS THE DELETION PATH, NOT GITHUB REFUSING TO RETARGET** -- an earlier
draft of this doc said the auto-retarget "did NOT apply", which over-claims and
would mislead the next reader into thinking stacks are unsalvageable by GitHub's
own behavior. GitHub DOES auto-retarget dependent PRs when a merged head branch is
deleted THROUGH THE WEB UI. Neither path tested here is that path:

- #384's base went via `gh pr merge --squash --delete-branch` -- a known CLI gap
  (cli/cli#1168) where the dependent PR is closed rather than retargeted.
- #386's base went via a raw `DELETE git/refs` API call issued separately from the
  merge, which GitHub cannot associate with a merge at all.

This makes the finding NARROWER and MORE certain, not weaker: **both paths this
repo actually uses close the dependent PR.** `/merge-pr` squash-merges via the CLI
and `/post-merge-cleanup` deletes via the API, so the closing path is the DEFAULT
path here, not an edge case. A maintainer merging by hand in the web UI would see
a retarget instead -- which is worth knowing precisely because it means the
behavior is inconsistent across the paths one workflow uses.

A closed PR loses its review threads from the merge queue's view and has to be
reopened or recreated, and recreating it starts the bot-review budget over.

## Failure mode 3 / the SHA-rewrite hazard -- DISPOSITIVE

The issue predicted this would be the axis that kills stacks, and it is -- but the
finding is worse than the issue anticipated. The hazard is not confined to an
explicit `gh stack rebase`; it is in the ROUTINE sync path.

`gh stack sync --help`, verbatim: it "Cascade-rebases stack branches onto their
updated parents" and "Pushes all branches atomically (using `--force-with-lease
--atomic`)". Per GitHub's own docs the cascade-rebase fires WHEN THE TRUNK HAS
MOVED -- which is not a mitigation, because a stack exists precisely to track a
moving base. A sync that rewrites nothing is a sync you did not need. There is no
additive mode.

Measured directly:

| Path | Cited SHA afterward |
|---|---|
| rebase onto the moved base (what `sync`/`rebase` do) | **ORPHANED** -- not reachable from the branch |
| merge-commit sync (what `gh pr update-branch` does by default) | **REACHABLE** -- survives |

That is disqualifying because of a workflow property that has nothing to do with
stacks: every bot-review finding is answered with a reply CITING A FIX SHA, and
CLAUDE.md already forbids amend/squash/rebase on an open reviewed PR for exactly
this reason. An orphaned SHA 404s in the reply that cites it, and the incremental-
review delta empties, which CR-confirmed causes the bot to silently skip the
changed code.

SCOPED PRECISELY, because the conclusion is about SHA REWRITES and not about
review state in general: what breaks is a rewrite applied to a branch whose SHAs
have already been CITED. A reviewed PR that is never rewritten is fine (that is the
normal additive fix-round, and it is what this repo does every day). An unreviewed
branch may be rebased freely (step 3 of the procedure above depends on it). The
collision is specifically REWRITE x ALREADY-CITED, and a stack's routine
keep-current operation is a rewrite.

The escape hatch is real but narrow: `gh pr update-branch` (merge-commit, additive)
keeps the SHAs. It is a per-PR command, not stack-aware -- so a stack can be kept
current only by NOT using the stack tooling for the one operation stacks exist to
automate. That is the whole value proposition inverted.

## Verdict

**The rule stands, unamended.** Stated in its now-verified form:

> Open INDEPENDENT PRs off `main`, one at a time, for serialized work. Do not use
> dependent PR stacks -- in ANY repo running this bot-review workflow, since the
> disqualifying fact is in the user-global CLAUDE.md, not a repo setting. The cost
> that is actually paid is ORCHESTRATION: eight of nine PR-lifecycle skills take a
> single `pr_number` and know nothing about a dependent base, so the lead carries
> the graph in its own context and re-derives it after every merge (maintainer,
> from two live stacked PRs: "the orchestration was the PITA"). What makes it
> UNFIXABLE rather than merely expensive is the stack tooling's routine `sync`,
> which cascade-rebases and force-pushes, orphaning the fix SHAs cited in review
> replies -- dispositive for any PR that has been reviewed. Also measured: deleting
> a merged base branch AUTO-CLOSES the PR stacked on it (the default `/merge-pr`
> path), and a non-`main` base gets no `gates` CI -- the latter real but NOT a deal
> breaker, since it runs once the PR is retargeted.
>
> Instead: keep the dependent branch LOCAL and unopened until its parent merges,
> rebase it then (free -- no PR, no reviews, no cited SHAs), open it off `main`. If
> it cannot wait, FOLD both into one PR with a size override.

Two clarifications the original rule lacked, both worth carrying:

1. **A stack is never the answer to an oversized PR.** Evaluate stacks for genuine
   DEPENDENCY only. A split that mechanically satisfies a line-count gate while
   forcing a reviewer to hold three PRs in their head has relocated review burden,
   not reduced it (#353 is the worked example: 1309 LOC, correctly taken as a size
   override rather than split, because the harnesses prove the code they ship with).
2. **The dispositive hazard is review-state, not mechanics.** An UNREVIEWED stack
   could in principle be rebased freely. The rule holds regardless because a PR
   in this repo does not stay unreviewed, but this is the axis to re-test if the
   review workflow ever stops citing SHAs.

## What would have to change to revisit this

Not proposed, just recorded so the next re-test starts further along:

- `ci.yml` would need its `branches: [main]` filter dropped (a real scope change:
  full matrix on every PR against every base).
- The bot-review workflow would need to stop citing fix SHAs, or `gh stack` would
  need an additive sync mode. Neither is in view.
- `gh stack merge` was NOT tested and must not be, casually: it is a merge path and
  carries the same human gate as `gh pr merge`. The deterministic floor's merge deny
  applies to it identically.

## Related

- The standing rule's in-repo footprint is ONE inline parenthetical at
  `skills/orchestrate/SKILL.md:102` (`no-dependent-pr-stacks`). CLAUDE.md is silent
  on it, and the auto-memory entry the issue names as the lockstep target
  (`no-dependent-pr-stacks-for-serialized-work`) HAS NO BACKING FILE -- see #347.
  This doc is now the durable record.
- #375 -- the expected-check-set reconciliation, independently confirmed by mode 1
  above.
- #347 -- auto-memory index/file drift, which is why the memory half of this
  issue's acceptance criteria could not be satisfied.
