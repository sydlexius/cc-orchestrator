# Commit model: honest commits at write time, merge method derived from the branch

Design of record for amending cc-orchestrator's always-squash policy. Brainstormed and approved
2026-07-30.

**Supersedes** the scope and wording of the earlier scratchpad drafts
(`PROPOSAL-atomic-commits.md`, `PROPOSAL-squash-amendment.md`); their INVESTIGATION findings are
carried forward below and still stand.

---

## Problem

Commit `4856726` ("fix(elmer): treat the new helpers as canonical source, and document them")
carries a 2-line behavioral fix under 50 lines of documentation:

    CLAUDE.md                    | 52 ++++++++++++++++++++++++++++++-----
    scripts/orchestrate-steer.sh |  2 +-
    test-orchestrate-steer.py    |  3 ++-

Checking it changed the design. It is NOT a fix bundled with unrelated docs - every CLAUDE.md hunk
documents the same two helpers the steer fix is about. Under a "one logical concern per commit"
test it PASSES. **A rule written on "one logical concern" would not have caught the commit that
prompted it.**

The defect that survives scrutiny is narrower: the subject says `fix:` while 96% of the diff is
prose. A `git bisect` landing here points at documentation; a `git revert` to back out the steer
change also reverts a screenful of accurate docs.

The maintainer wants this served for four readers at once (stated in session): bisect/blame
debuggability, PR reviewability, revert granularity, and honest subject lines. They share one root
cause - a commit whose contents do not match its stated purpose - so one mechanism serves all four,
provided the commit is shaped WHEN WRITTEN rather than repaired afterward.

## Constraints discovered during exploration

These are measured facts about this machine and repo, not assumptions. Each one killed a candidate
design.

1. **`commit.gpgsign=true`, signed via 1Password (`op-ssh-sign`).** Every commit is signed.
   Rewriting a commit re-signs it, which can raise an interactive 1Password prompt. A background
   agent that hits a prompt STALLS SILENTLY - the failure mode elmer's design calls worse than
   doing nothing. **Therefore the design never rewrites a commit.** This killed the "prep-pr
   reshapes the series" plan outright.
2. **`core.hookspath` is an absolute path** into the main repo's `.git/hooks`. Git config is
   per-repo, so every worktree shares ONE hook directory: a hook installs once and covers
   `-352`, `-elmer`, and every future worktree.
3. **No hooks are currently installed.** `.git/hooks/` holds only samples, and the well-built
   `scripts/pre-push-hook.sh` is unused. The hook surface exists and is free.
4. **`main` is currently 100% squash-merge commits** (every subject ends `(#NNN)`). An earlier
   measurement of "25 of 60 commits mix logic and docs" was DISCARDED as an artifact of that: those
   commits SHOULD mix, because the squash IS the PR.
5. **The deterministic floor needs no change.** `orchestrate-guard.sh` matches `gh pr merge`
   regardless of method and gates on `--match-head-commit`; `--squash` appears only in help text.
   This is not a deny-authority change.
6. **No GitHub settings change.** All three merge methods are already allowed on cc-orchestrator
   and stillwater; `required_linear_history=false`.
7. **No `git config` knob expresses any of this.** `merge.ff` / `pull.rebase` govern LOCAL merges,
   not GitHub's merge button. Enforcement must live in a hook and in the merge command.

## The load-bearing dependency

`templates/implementer-charter.md:16` mandates an early WIP checkpoint commit BEFORE the code
compiles, and justifies it explicitly: *"A WIP commit on an unpushed branch costs NOTHING (the lead
squashes at PR time)."*

Squash is load-bearing for a charter rule that looks unrelated. Remove it naively and every
deliberately-broken-build `wip(...)` commit lands in permanent `main` - which does not merely fail
to help `git bisect`, it BREAKS it, since bisect would land on commits that never built.

The charter rule itself must NOT be dropped: it exists because an uncommitted worktree is
unrecoverable and voids the respawn promise - the single unprotected-data hole in the pipeline.

## Architecture

Commit shape is decided AT COMMIT TIME by a mechanical honesty check, and the merge method is
DERIVED from the branch rather than fixed by policy.

The leverage is replacing a **policy constant** ("always squash") with a **derived predicate**
("squash iff the branch has WIP"). Constants get relitigated in every edge case; predicates answer
the edge cases themselves. Same move as #337's verify-don't-classify: check the fact, do not encode
the judgment.

### A. `scripts/commit-msg-hook.sh` - the only blocking rule

Follows `pre-push-hook.sh`'s established shape: self-locating, standalone, installed by symlink.
Git invokes it as `commit-msg <path-to-message-file>`.

    message file ──> parse subject ──> Conventional-Commits type
                                            │
    git diff --cached --name-only ──────────┤
        └──> classify: doc | logic ─────────┘
                                            ▼
                                 type/content agreement?
                                  yes ──> exit 0 (silent)
                                  no  ──> exit 1 + explain + how to fix

**Classification** (mechanical, deliberately crude - better to under-reach than over-reach): a path
is a DOC if it ends `.md`. Everything else is LOGIC.

One decision worth recording, because it looks like an omission: an earlier draft added "or lives
under `skills/*/design/`", which is REDUNDANT - every file there is already `.md`. Dropping it
keeps the classifier a single test with no second rule to drift. Note the consequence: `CLAUDE.md`,
`SKILL.md`, and every template are DOCS, so a commit touching only those under a `fix:` subject is
rejected. That is the intended behavior and is exactly the `4856726` shape.

| Subject type | Requires | Rejects |
|---|---|---|
| `fix` / `feat` / `perf` / `refactor` | >=1 logic file | docs-only commit lying as a fix |
| `docs` | 0 logic files | a logic change hiding under `docs:` |
| `wip` / `chore` / `ci` / `test` / `build` / `style` / merge / revert / fixup | - | exempt, always passes |

Two exemptions that MUST exist or the hook becomes a menace:

- **`--no-verify` is honored** - git's own escape hatch. No conflict with the floor, which denies
  `--no-verify` on PUSH, not on commit.
- **An unparseable subject PASSES.** Fail-open on doubt, matching the guard's own posture.

Explicitly NOT enforced (each considered and declined): a size/ratio guard on mixed commits (a
heuristic with false positives on the correct-and-common script+its-own-docs pairing), a pre-push
WIP block, and Conventional-Commits format enforcement.

#### What the hook does NOT catch (stated plainly, because it is counterintuitive)

**The hook does not reject `4856726`, the very commit that prompted this work.** That commit
contains `orchestrate-steer.sh` and `test-orchestrate-steer.py`, so a `fix:` subject satisfies the
">=1 logic file" rule and it PASSES.

Catching it mechanically requires a RATIO judgment (2 logic lines vs 50 doc lines), and the
ratio guard was considered and declined: it fires on the correct-and-common case of a script
change shipped with its own architecture-section entry, which this repo's house style REQUIRES.
A guard whose false positives land on the dominant correct path trains people to bypass it.

So the split of labor is deliberate:

| Failure | Caught by |
|---|---|
| docs-only commit wearing a `fix:` subject | THE HOOK (blocking) |
| logic change hiding under `docs:` | THE HOOK (blocking) |
| a real fix buried under disproportionate docs | THE CLAUDE.md RULE + human/bot review (judgment) |

This is the honest scope. The mechanism handles what a matcher can decide; the written rule
handles what needs a reader. Do not let a future session "fix" this by adding a ratio guard
without re-deciding the false-positive tradeoff above.

### B. `scripts/merge-method.sh` - read-only oracle

    git log --format=%s <base>..<head> ──> any ^wip( ?
            yes ──> "squash"   (branch checkpointed; today's behavior)
            no  ──> "merge"    (atomic series survives into main)

Base is **caller-supplied, never inferred** - the same rule that makes `base-freshness.sh` correct
by construction on a backport or release-base branch. The `^wip(` match is ANCHORED: `wip` mid-subject
must not match.

Exit 0 either way; exit 2 only on malformed invocation. **Fails toward `squash`** on any doubt
(unreadable ref, git error, ambiguous base), because squash is today's behavior, so degradation is
never a surprise.

Consumed by `/merge-pr` (picks the flag) and `/prep-pr` (turns its squash prompt into an informed
recommendation).

### Why merge-commit, not rebase-merge

Rebase-merge would give linear history but **rewrites every commit SHA**. This repo cites fix SHAs
in bot-review replies as standing policy, and the existing rule against mid-PR rebasing exists for
exactly this reason. Rebase-merge would orphan every cited SHA at the merge boundary. Merge-commit
preserves branch SHAs, so a reply citing `abc1234` still resolves forever.

Cost, stated plainly: `main` stops being linear. `git log --first-parent` recovers the
one-line-per-PR view that squash gave for free, and belongs in the docs.

### Why "squash only WIP branches", and its cost

It is the only option that is purely a DECISION RULE rather than a REWRITE, so it never re-signs a
commit and never risks the 1Password prompt. `main` structurally cannot receive a commit that never
built.

The property that sold it is the incentive gradient: a branch that leaves WIP behind forfeits its
atomic series wholesale; a branch that cleans up keeps it. That converts atomicity from a mandate
someone must police into something implementers are rewarded for, and it degrades safely - the
failure mode of forgetting is "today's behavior", not "broken history".

**The cost, stated honestly: one WIP commit forfeits the atomicity of every good commit on that
branch.** That is blunt. The alternative - preserving the good commits and dropping only the WIP -
is exactly the rewrite-and-re-sign step this design exists to avoid.

Rejected alternatives: *implementer cleans up before reporting done* (honor-system, and the
implementer is PR-blind by design); *accept WIP in main* (undercuts the debuggability goal);
*block WIP at push* (forces a rewrite somewhere).

## Edits to existing surfaces

1. `commands/merge-pr.md` - call the oracle, pass `--squash` or `--merge`. Keep
   `--match-head-commit` so the floor's merge-auth path is untouched.
2. `commands/prep-pr.md` - the squash prompt becomes a recommendation driven by the oracle: WIP
   present -> recommend squash; WIP-free -> recommend keeping the series, with the reason.
3. `templates/implementer-charter.md:16` - restate the WIP justification. WIP stays mandatory for
   respawn protection, but a branch ending with WIP commits forfeits its atomic series at merge.
   Same rule, now with a visible incentive to fold.
4. `commands/post-merge-cleanup.md` - note that the ff-only path is now the normal one. Its
   squash-divergence fallback exists because squash rewrites the SHA; under merge-commit local
   `main` stays a strict ancestor. Leave the fallback for history predating this.
5. `.gates.toml` + the CLAUDE.md gate listing - register the two new harnesses.
6. User-global `~/.claude/CLAUDE.md` - the atomicity bullet below. **The maintainer's edit, not the
   agent's.**

## Proposed CLAUDE.md wording

Placement: user-global `~/.claude/CLAUDE.md`, "Git and PR workflow", after the "Commit hygiene on
an OPEN PR" bullet it qualifies.

> - ONE REVERTIBLE UNIT PER COMMIT, AND THE SUBJECT MUST NOT LIE ABOUT IT. The test is not "one
>   topic" - it is: could someone revert this commit to back out ONE thing without losing an
>   unrelated other thing? A behavioral fix and the prose describing it may share a commit; a
>   2-line fix under 50 lines of docs behind a `fix:` subject may not, because a bisect that lands
>   there should point at the fix, not at the prose. A `commit-msg` hook enforces the mechanical
>   half: a `fix:`/`feat:` commit must contain at least one non-doc file, and a `docs:` commit must
>   contain none.
>   - THESE COMMITS CAN BE PERMANENT. A branch with NO `wip(` commits merges with a MERGE COMMIT,
>     so its commits and SHAs survive into `main` forever - a cited fix SHA still resolves, and
>     `git bisect` reads what you actually wrote. `git log --first-parent` gives the
>     one-line-per-PR view.
>   - A BRANCH THAT LEAVES `wip(` COMMITS BEHIND IS SQUASHED, whole. WIP checkpoints stay mandatory
>     mid-work (an uncommitted worktree is unrecoverable and voids the respawn promise), but they
>     cost the branch its atomic history at merge. Fold them as you go and keep it.
>   - ORTHOGONAL TO BATCHING, not in tension with it: atomicity governs COMMIT BOUNDARIES, batching
>     ("don't push trivial commits to a reviewed PR") governs PUSH TIMING. Three atomic commits in
>     one push satisfies both. It is never a license to amend or rewrite already-pushed history.

## Testing

Two stub harnesses in house style - `test-commit-msg-hook.py`, `test-merge-method.py`. Stdlib only,
subprocess-driven, temp git repos, no network. Neither script touches `gh`, so only git needs
stubbing.

**Hook:** each blocking rule in both directions (`fix:` with logic passes / docs-only fails;
`docs:` with logic fails / pure docs passes), every exempt type, `--no-verify`, an unparseable
subject, an empty commit, and a `.gates.toml` path classifying as LOGIC (a non-`.md` config file
must not be mistaken for a doc).

**`4856726` is a fixture that must PASS, and that is not a bug - see "What the hook does not
catch" below.** The honest regression fixture for the blocking rule is the inverse shape: a
CLAUDE.md-only commit under a `fix:` subject.

**Oracle:** WIP present, WIP-free, `wip` mid-subject (must NOT match), empty range, unresolvable
base, malformed invocation.

**Both harnesses are mutation-proved** before being trusted (per `prove-a-new-gate-can-fail`):
deliberately break each rule, confirm the harness catches it. A gate that cannot fail is worse than
no gate - #324 shipped a coverage check that passed 2 of 5 mutations.

**Rigor tier: ADVISORY** (#287). Neither script is deny-authority: the hook is a git hook, not a
PreToolUse floor hook, and cannot permit a bad push or merge. One multi-lens adversarial pass plus
one hostile fix-scoped verify round. The tier is earned by a property of the POST-diff file, not the
filename - if the diff ends up touching anything the floor reads, it escalates to the full loop.

## Rollout order

Sequencing is load-bearing.

1. **Oracle + harness.** Read-only, changes no behavior. Run against recent merged branches to see
   what it WOULD have chosen.
2. **Hook + harness, installed but observed.** Verify against real commits before it gates
   anything.
3. **Flip `/merge-pr` to consult the oracle.** First behavior change; only now can a non-squash
   merge occur.
4. **Charter + CLAUDE.md wording**, once the mechanism is proven.

Steps 1-2 are useful even if 3 is never taken.

## Trial verdict (2026-07-31)

The maintainer chose to TRY the model on one branch rather than adopt the policy on paper first.
That trial was `feat/cr-quota-watch` (the elmer series, PR #357), and it is now merged.

**What the trial exercised.** 18 commits, written honestly at write time - no `wip(` prefixes, each
commit naming what it did - across a feature build plus three adversarial fix rounds and two
CodeRabbit review rounds. By the predicate in this document, that branch carries no WIP commits, so
the oracle would have chosen MERGE-COMMIT. It was in fact squash-merged, because `/merge-pr` still
hard-codes the policy constant (rollout step 3 is not taken).

**What it confirms.** The load-bearing claim held: writing honest commits at the moment of the
change cost nothing and needed no discipline that was not already there. No commit wanted a `wip(`
prefix in retrospect. The predicate was never ambiguous on a real branch - at no point was "is this
a WIP branch?" a judgment call, which is the property the derived predicate exists to buy over the
relitigated constant.

**What it did NOT test, and is the real risk.** An open PR forbids rewriting pushed history (a new
SHA orphans every cited fix SHA and empties the incremental-review delta). So the trial never
exercised the case this model most changes: a branch that DID accumulate WIP commits and needs them
squashed. The trial shows the honest-commit half works; the squash-iff-WIP half remains unexercised
on real work.

**Recommendation.** Proceed with rollout steps 1-2 (oracle + hook, read-only and observed). They are
useful independent of step 3, and step 3 should wait for a branch that genuinely accumulates WIP
commits, which this one did not.

## Trial round 2 (2026-07-31, same day): three more PRs

The verdict above was written after ONE branch. Three more shipped the same session (#357 elmer,
#359 floor-hardening, #360 four doc fixes), and they add two findings the single-branch trial could
not produce.

### The predicate generalizes beyond commits - measured accidentally

#359 fell behind base mid-review and needed refreshing. The choice between rebase and
`update-branch` turned entirely on ONE branch property: **does this branch carry cited fix SHAs?**
It did (`f9db038` was cited in a CodeRabbit reply), so a rebase would have orphaned the citation
and emptied the incremental-review delta; the additive merge was correct.

That is the SAME SHAPE as the WIP predicate this document proposes - a property OF THE BRANCH
selects the method, rather than a standing policy applied uniformly and relitigated at each edge
case. The repo already reasons this way in one place and not the other. This is evidence FOR the
derived-predicate architecture that arrived from outside the trial, which makes it worth more than
the trial's own confirmation: nobody was looking for it.

### The review-time and history-time audiences want DIFFERENT granularity

#359 accumulated four commits - the feature, a CR fix round, a doc addition, and a merge commit -
and squashing it at merge is RIGHT, not a compromise. During review the per-commit history was
genuinely useful: the fix round is legible as its own unit, and a reviewer can see exactly what
changed in response to feedback. In `main` that same structure is noise.

The single-branch trial could not surface this, because #357 was one clean series where both
audiences happened to want the same thing. The generalization: **merge method should be DERIVED AT
MERGE TIME from the branch's final shape, not decided when the commits are written.** That is
already this document's architecture, and it now has a second, independent argument - the first
was bisect/blame legibility, this one is review legibility, and they point the same way.

### Still unexercised, now across four branches

The squash-iff-WIP leg has STILL never run on real work. Every branch this session was honest end
to end, so the predicate has only ever been evaluated on inputs where it returns MERGE-COMMIT. Four
branches of evidence for one leg and zero for the other is worth stating plainly rather than
letting the count read as broader validation than it is.

The recommendation is unchanged and now better supported: rollout steps 1-2 (read-only oracle +
observed hook) are useful independent of step 3, and step 3 should wait for a branch that genuinely
accumulates WIP commits. Note the mild irony that the honest-commit discipline this model
encourages makes such a branch RARER, so the squash leg may need a deliberate test rather than an
organic one.

## Open items (not blockers)

- **Hook install is manual and per-machine**
  (`ln -s ../../scripts/commit-msg-hook.sh .git/hooks/commit-msg`). `orchestrate-setup.py configure
  --apply` could install it with consent, matching how the floor hook is deployed - a real scope
  addition, deliberately left out. `core.hookspath` means one install covers every worktree.
- **Routing:** this is an operating-model change, so per the self-imposed carve-out it routes for
  MAINTAINER MERGE even though much of it is prose.
