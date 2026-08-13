# Stacked PRs: re-test of the three failure modes (#355)

**VERDICT: THE RULE STANDS, and it is now narrower and better-founded than the
version it replaces.** All three original failure modes reproduce against current
GitHub, measured 2026-08-13 on this repo. A fourth hazard -- the one the issue
predicted would be dispositive -- is confirmed dispositive: the stack tooling's
ROUTINE keep-current command rewrites every SHA, which orphans the fix SHAs this
repo's review workflow cites. No amendment is warranted.

Scope: an empirical re-test only. It changes no code, wires nothing into any
skill, and widens no CI trigger. Every follow-on named below is a separate,
CR-required change.

## Why this was re-opened

The standing rule against dependent PR stacks came from the #84-87 thrash and
cited three failure modes. GitHub has since shipped first-class stacked-PR support
and a `gh stack` CLI extension, which is enough of a landscape change to justify
re-testing -- but not enough to overturn the rule on a reading of a help text,
which is all anyone had done. The motivating case was real: in the 2026-07-30
session #352's fix depended on #353's base and was serialized by hand.

## Method

A three-level scratch stack (`scratch/355-trunk` <- `-base` <- `-head`) and a
second two-level stack for the merge-commit path. Every test ran against real
GitHub on real PRs (#383-#386), never a fixture or a help text. NOTHING TOUCHED
`main`: the scratch trunk was the merge target throughout, and all six branches
plus both PRs are gone. The two scratch PRs closed THEMSELVES, which is the
finding rather than the cleanup.

## Failure mode 1: CI does not run on a non-`main` base -- CONFIRMED LIVE

`.github/workflows/ci.yml:14-15` filters the trigger to `branches: [main]`, so a
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

#383 reported `mergeStateStatus=CLEAN` while two required checks had never run --
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
#375 design doc's own motivating measurement is no longer reproducible (its PR was
auto-retargeted before merge).

## Failure mode 2: the upper PR auto-closes -- CONFIRMED LIVE, and the cause is isolated

Both merge shapes were tested, and the delete was separated from the merge so the
cause is established rather than assumed:

| Action | Upper PR |
|---|---|
| squash-merge lower + `--delete-branch` (#383 -> #384) | **CLOSED** |
| merge-commit lower, base branch KEPT (#385 -> #386) | stayed **OPEN** |
| ...then delete the base branch (#386) | **CLOSED** |

**The merge does not close the upper PR. Deleting the base branch does.** The
timeline records `base_ref_deleted` followed by `closed`, with no retarget event
of any kind. GitHub's documented auto-retarget did NOT apply to either shape here.

This matters for the rule's practical bite because `/merge-pr` squash-merges and
this repo has auto-delete-branch on: the closing path is the DEFAULT path, not an
edge case. A closed PR loses its review threads from the merge queue's view and
has to be reopened or recreated, and recreating it starts the bot-review budget
over.

## Failure mode 3 / the SHA-rewrite hazard -- DISPOSITIVE

The issue predicted this would be the axis that kills stacks, and it is -- but the
finding is worse than the issue anticipated. The hazard is not confined to an
explicit `gh stack rebase`; it is in the ROUTINE sync path.

`gh stack sync --help`, verbatim: it "Cascade-rebases stack branches onto their
updated parents" and "Pushes all branches atomically (using `--force-with-lease
--atomic`)". So the ordinary keep-current command rewrites every commit SHA in the
stack and force-pushes. There is no additive mode.

Measured directly:

| Path | Cited SHA afterward |
|---|---|
| rebase onto the moved base (what `sync`/`rebase` do) | **ORPHANED** -- not reachable from the branch |
| merge-commit sync (what `gh pr update-branch` does by default) | **REACHABLE** -- survives |

That is disqualifying for this repo specifically, because of a workflow property
that has nothing to do with stacks: every bot-review finding is answered with a
reply CITING A FIX SHA, and CLAUDE.md already forbids amend/squash/rebase on an
open reviewed PR for exactly this reason. An orphaned SHA 404s in the reply that
cites it, and the incremental-review delta empties, which CR-confirmed causes the
bot to silently skip the changed code. A stack that has been reviewed therefore
cannot be kept current with its own tooling without destroying its review record.

The escape hatch is real but narrow: `gh pr update-branch` (merge-commit, additive)
keeps the SHAs. It is a per-PR command, not stack-aware -- so a stack can be kept
current only by NOT using the stack tooling for the one operation stacks exist to
automate. That is the whole value proposition inverted.

## Verdict

**The rule stands, unamended.** Stated in its now-verified form:

> Open INDEPENDENT PRs off `main`, one at a time, for serialized work. Do not use
> dependent PR stacks. Three failure modes are measured live as of 2026-08-13: a
> non-`main` base gets no `gates` CI (a repo config fact, `ci.yml` `branches:
> [main]`); deleting a merged base branch AUTO-CLOSES the PR stacked on it (the
> default `/merge-pr` path); and the stack tooling's routine `sync` cascade-rebases
> and force-pushes, orphaning the fix SHAs cited in bot-review replies. The third is
> dispositive for any PR that has been reviewed.

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
