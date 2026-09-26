# Design: the `# prep-pr-ok` push override - self-attestation or mechanism?

Date: 2026-09-25
Status: PROPOSAL - needs a MAINTAINER DECISION. Nothing here changes code. Any guard change
(including rewording the advisory message) is a deny-authority change and gets its OWN issue,
the full K=2 loop, and maintainer merge (CLAUDE.md "Working ON the security floor").
Issue: #318
Companions: `DESIGN-deterministic-floor.md` (threat model, evaluation order),
`DESIGN-tl-context-minimization.md` (Bet #1 KILLED), `DESIGN-merge-gate-readiness-vs-authority.md`
(the one sanctioned floor-reads-a-local-token relaxation).

## The question

The push override is a literal string. Can "the gate genuinely passed" be made deterministic
WITHOUT teaching the floor to read the gate receipt?

## The current mechanism (as shipped)

- The floor records a real push INVOCATION per clause (`scripts/orchestrate-guard.sh:899`,
  reset at `:808`), after every Tier-1/Tier-2 deny has already had its chance to exit 2.
- The advisory gate runs LAST (`scripts/orchestrate-guard.sh:904-922`). Its entire test is
  `grep -q 'prep-pr-ok'` over the whole command (`:913`). Present -> exit 0. Absent -> exit 2
  with the #345 message that names the `refs/tags/` remedy first (`:920`).
- Evaluation order guarantees the token can only satisfy the advisory, never a hard deny
  (`git push origin main # prep-pr-ok` is still blocked by Tier-1; `DESIGN-deterministic-floor.md`
  "Evaluation order (Finding F18)").
- Who emits it:
  - `/prep-pr` Step 7 carries the token on the push block (`commands/prep-pr.md:831`) and says
    in so many words that it is an INSTRUCTION-LEVEL assertion: nothing checks that Steps 1-6 ran
    (`commands/prep-pr.md:834-837`).
  - The pr-shipper appends it when the stack entry has `prep_ok: true`, which is the LEAD's
    attestation that the prep gate went green (`skills/orchestrate/templates/pr-shipper-brief.md:31`,
    `skills/orchestrate/SKILL.md:145`).
  - A fix-round push (`/handle-review`) runs `gate-runner.py` with NO `--receipt`
    (`commands/handle-review.md:497-499`), so today no receipt exists for a fix round at all.

So the token is a self-attestation twice over: the agent that ran (or skipped) the gate is the
agent that types the string.

## Threat model (unchanged)

Honest bot on the obvious path, NOT adversarial evasion (`DESIGN-deterministic-floor.md`
"Threat model"; CLAUDE.md guard summary). The failure this issue is about is not a forger. It is
an honest agent that learns the token as "the thing that makes the BLOCKED message go away" and
appends it reflexively - the corrosion #345 documented for bare tag pushes. Any design is judged
on whether the OBVIOUS path still produces a push that skipped the gate.

## Why the floor must not read the receipt

The rationale already exists; it is restated here so the push case is covered explicitly.

1. **Forgeability converts a deny into an allow-if-file-exists.** A receipt is a file any
   in-session agent can write (`DESIGN-tl-context-minimization.md:187-196`, defeater (a)). For
   the push advisory this is weaker than it sounds - the advisory is already bypassable by a
   string - but a floor that "verifies" a forgeable artifact READS as a mechanism while being the
   same attestation, which is worse than an honest string.
2. **Fail-open inverts it.** The floor must exit 0 on any internal error
   (`DESIGN-deterministic-floor.md` "Error behavior (Finding F19)"). Every error in a receipt
   check (stray cwd, reaped path, missing jq) therefore ALLOWS (`DESIGN-tl-context-minimization.md`
   defeater (b)).
3. **Binding a receipt to the pushed commit needs repo state.** The receipt is keyed on
   `commit_sha`/`tree_sha` (`scripts/gate-runner.py:445-466`). A push command line carries a
   BRANCH NAME, not a SHA (`safe-push.sh <branch>`; `/prep-pr` passes `$(git branch --show-current)`,
   `commands/prep-pr.md:826-828`). Comparing the two requires running `git rev-parse` inside the
   hook. The guard never runs git today (no `git` invocation outside comments/fixtures in
   `scripts/orchestrate-guard.sh`); it only greps its own command line and reads two local files
   (the marker, `:683`, and the merge-auth token, `:706-780`). Adding git would break the
   "greps its own command line" isolation and put per-call repo I/O on EVERY Bash call.
4. **The one precedent does not transfer.** The #263 merge-auth token IS a floor-read local
   artifact, relaxed deliberately with maintainer consent
   (`DESIGN-merge-gate-readiness-vs-authority.md:173-186`). It is safe only because the COMMAND
   carries the binding: `--match-head-commit <sha>` is on the line, the floor compares it to the
   token locally (`scripts/orchestrate-guard.sh:742-780`), and `gh` itself refuses the merge if
   that SHA is not the PR head. A push has no equivalent server-side pin, so a push token could
   bind only to a branch name and an expiry - i.e. "some gate passed recently on something".

Conclusion: keep the floor ignorant of receipts. Any determinism has to live OUTSIDE the floor,
in a script that may run git and may fail closed.

## Options considered

### A. Status quo, documented (do nothing mechanical)

Keep the string; keep the Step 7 disclaimer. Cost: zero. Weakness: the override remains
exactly as strong as the agent's honesty about having run Steps 1-6, and the fix-round path has
no artifact to consult even in principle.

### B. Receipt check inside `safe-push.sh` (outside the floor) - RECOMMENDED

`safe-push.sh` already runs git, already fails closed on a definitive BEHIND
(the freshness block, `scripts/safe-push.sh:187-270`; the BEHIND refusal is `:237-245`), and already takes caller-declared intent flags (`--rewrite`,
`--stale-ok`, `:157-176`). Add a receipt leg there:

- Read `$(git rev-parse --git-dir)/prep-pr-receipt.json` (the path `/prep-pr` writes,
  `commands/prep-pr.md:267`), validate it the way `elmer-enqueue.sh` does (schema, `producer ==
  gate-runner`, `result == pass`; `scripts/elmer-enqueue.sh:166-199`).
- Bind it to what is being pushed. NOTE a real constraint: `/prep-pr` gates in Step 2 and may
  SQUASH in Step 6 (`commands/prep-pr.md:238`, `:742`), so the pushed commit's SHA can differ
  from the receipt's `commit_sha` while the TREE is identical. The receipt's documented
  consumer contract binds `commit_sha` plus a live clean-worktree check
  (`DESIGN-tl-context-minimization.md:129-134`), and that bind would refuse every squashed
  first push. The body also records `tree_sha` (`:128`), so the proposed bind is
  `tree_sha == $(git rev-parse refs/heads/<branch>^{tree})` plus the same clean check - a
  deliberate, stated deviation from that contract, and part of what the maintainer decides.
- Policy on a missing/stale receipt: REFUSE with a pointer to `/prep-pr` (or the fix-round
  gate), plus a declared-intent escape (e.g. `--ungated`) for the human's `!` path and for repos
  without `gate-runner`, mirroring `--stale-ok`.

Tradeoffs: deterministic for every caller that uses safe-push (the house push path); no floor
change at all; the `# prep-pr-ok` string can stay as the floor-side advisory, now backed by a
real check one layer out. Costs: a script-FUNCTION change (CR-required); `/handle-review` must
start passing `--receipt` so fix rounds produce one; the pr-shipper must push from a cwd whose
`--git-dir` holds that receipt (per-worktree by construction, `commands/prep-pr.md:281-284`),
which needs verifying against where the shipper actually runs. Still forgeable by a dishonest
agent - out of the threat model, same as elmer (`scripts/elmer-enqueue.sh:23-24`).

### C. Elmer-style check at a lead-side step (no push-path change)

Have the lead (or `/prep-pr` Step 8 / the shipper's PR-open) verify the receipt against the PR
head, exactly as `elmer-enqueue.sh:223-231` does, before setting `prep_ok: true`. Cheaper than B
and touches no push script, but it verifies AFTER the push (or only on the stacked path), so a
direct `safe-push.sh ... # prep-pr-ok` still pushes ungated. It hardens the attestation the
shipper consumes; it does not make the push itself deterministic.

### D. Short-TTL push token armed by `gate-runner`, read by the floor

Mirror merge-auth: `gate-runner --receipt` on PASS also arms `FLOOR_DIR/push-auth/<session-key>`
`{branch, tree_sha, expiry}`; the floor allows the push iff a fresh token names the pushed
branch. REJECTED as the primary: it re-opens every item in "Why the floor must not read the
receipt" (the branch-name-only bind of item 4; a floor read that fails open, item 2) for an
ADVISORY, and it needs a fresh maintainer relaxation of the never-read-an-artifact invariant for
far less benefit than the merge case earned. It also gates only the floor-visible spelling,
not the push itself.

### E. Drop the override entirely

Make the advisory unconditional for feature pushes and let safe-push's receipt leg (B) be the
only gate. Tidier, but it removes the floor's only nudge for a raw `git push` that bypasses
safe-push, and it is a deny-authority change. Worth revisiting only after B has run for a while.

## Recommendation (PROPOSAL)

Adopt **B**, keep the floor untouched, and keep `# prep-pr-ok` as the floor-side advisory. The
determinism lands where git is already available and failing closed is already the contract.
Sequence, each its own issue:

1. `/handle-review` passes `--receipt` to `gate-runner.py` (doc/command change).
2. `safe-push.sh` receipt leg with a tree-SHA bind and a declared-intent escape (CR-required,
   harness cases for missing / failing / stale / squashed-same-tree / escape).
3. Only after 1-2 ship: reword the guard message (`scripts/orchestrate-guard.sh:920`) to say the
   override is backed by safe-push's receipt check. Deny-authority tier, separate issue.

Open points for the maintainer: whether the escape flag should exist at all; whether a
consumer repo with no `gate-runner` gets a WARN (degrade) or a refusal; and whether the
`elmer-enqueue.sh` `commit_sha` bind (`:223-231`) needs the same tree-SHA treatment for a PR
whose first push was squashed after gating (observed from the code, not reproduced live).
