# Design: reconciling the merge oracle against an EXPECTED check set (#375)

Status: DECIDED 2026-08-13 — **Option C**, fail-closed on an unreadable expected set.

The maintainer delegated the choice rather than ratifying it, so the reasoning below is
the record of WHY, not a rubber stamp. It is deliberately reversible: C narrows to A by
deleting one fallback branch, and the two differ only on a shape (a protected release
base) this repo does not currently have. Revisit if the fallback proves noisy.

Scope: `scripts/ship-gate-preflight.sh` FULL mode only. No floor change, no allow-list
change, no new permission.

---

## Problem

`ship-gate-preflight.sh` answers "is anything wrong with the checks that ran?" It never
asks "did the checks that should have run, run?" On a pull request based off a
non-default branch those are different questions, and the gap is a live false PASS.

Measured on `sydlexius/stillwater#3021` while it was open:

```
base:  fix/2712-snapshot-permission-fixtures   (non-trunk)
mergeStateStatus: CLEAN
rollup (12): claude x8, re-request, Signed Commits,
             Signed Commits / local check tests, CodeRabbit
```

The `Protect main` ruleset requires **18** contexts. Exactly one of them (`Signed
Commits`) was present. Build, Test, Lint, Coverage Floor and both CodeQL analyses never
ran. Nothing red, nothing pending, nothing reported missing.

The oracle blocked that PR only by accident, on an unrelated unreplied review finding
evaluated *after* the checks gate. Clear that one thread and it returns PASS on a pull
request missing 17 of 18 required checks.

#3021 has since merged, and the measurement above is NOT reproducible from it: GitHub
AUTO-RETARGETED its base to `main` when the parent merged (`automatic_base_change_
succeeded`, 01:56Z), so the merged PR reports `baseRefName: main` and a 70-context
rollup. The timeline corroborates the shape — it provably carried a different,
non-default base that was auto-changed — but a reader checking the numbers today will
find a main-based PR and conclude this section is wrong. Recorded rather than quietly
dropped, per this doc's own commitment below.

### Why the existing guards do not cover it

**The empty-rollup guard is a count, not a coverage check** (`ship-gate-preflight.sh:424`):

```sh
if [ "$count" -eq 0 ]; then
  echo "BLOCK: no checks on #$pr (empty statusCheckRollup)..." >&2
```

One green irrelevant check satisfies it. Everything after is a purely NEGATIVE test
hunting for entries that are `bad` (`:436-500`), and a check that never ran is not bad,
it is absent.

**The expected-set read exists, in the wrong mode.** `branches/<base>/protection` is
fetched at `:199` — inside DIAGNOSE only. The FULL gating path never fetches it, so it
has no expected set to diff against.

**`mergeStateStatus` does not save us, and this is the part that matters given #334.**
That fix deliberately adopted GitHub's aggregate verdict to stop hand-picking rule
subsets. Here the aggregate reads CLEAN — correctly, from GitHub's perspective. The
ruleset targets `~DEFAULT_BRANCH`, so off that branch no rule is violated. **There is no
rule at all.** Silence is not consent, but both gates read it that way.

---

## Facts established before choosing (all measured 2026-08-13)

**F1. GitHub genuinely requires nothing on a non-default base.** Not a misread:

```
rules/branches/main             -> 5 rule types incl. required_status_checks
rules/branches/<feature>        -> 0 rules            (cc-orchestrator)
```

**F2. The two authorities disagree, on this very repo.** Legacy branch protection
reports **1** required context (`gates (ubuntu-latest)`); ruleset `19136685` reports
**4** (`gates (ubuntu-latest)`, `gates (macos-latest)`, `Analyze Python`, `Analyze
Actions`). They are evaluated as a union. Reading either alone under-reports.

**F3. `rules/branches/<ref>` answers on every repo probed; the legacy endpoint does not.**

CORRECTED after review. The first draft of this fact said the legacy endpoint "404s here
(`Branch not found`)". That was wrong in both halves and is recorded rather than quietly
edited, because it was the evidence cited for depending on the rules endpoint alone:

```
cc-orchestrator  branches/main/protection  -> HTTP 200, contexts ["gates (ubuntu-latest)"]
stillwater       branches/main/protection  -> 404 "Branch not protected"
```

It does NOT 404 on cc-orchestrator, and where it does 404 the reason is "no legacy
protection configured", not a missing branch or a scope failure. The original claim
conflated two probes, one of them against a nonexistent ref.

What survives, and is what the design actually rests on: `rules/branches/<ref>` answered
on both repos, needs no admin scope, and is the endpoint that reflects rulesets — which
is where this fleet's required checks live. Its error contract is worth stating because
the fail-closed branch depends on it:

```
rules/branches/<nonexistent-ref>   -> []     exit 0    (no rules, not an error)
rules/branches on a bad repo       -> 404    exit 1    (genuinely unreadable)
```

So "ref has no rules" and "cannot read" are distinguishable, which is what makes a
fail-closed BLOCK on the latter safe.

**F4 — THE DECISIVE ONE. "The base has rules" is NOT a usable proxy for "the base is
protected."** stillwater returns exactly one rule for a non-default ref:

```
rules/branches/fix/2712-fanart-snapshot-caps
  -> [{ruleset: 13340438, type: "copilot_code_review"}]
```

That is an auto-review ruleset, with **zero** `required_status_checks`. A hybrid keyed
on rule PRESENCE would see a governed base, reconcile against an empty required-set, and
pass the identical false green through a longer path. The predicate must be on
`required_status_checks` specifically.

This fact killed the first draft of Option C. It is exactly the shape this issue is
about — a check that looks like it measured something.

---

## The question this design settles

On a pull request whose base is not the default branch, what is the EXPECTED set?

This is a POLICY question, not a mechanical one. GitHub's answer is "nothing," and
that answer is correct-and-useless: it is why the bug exists.

---

## Options

### A. Always reconcile against the default branch's required set

Every PR is measured against what `main` requires, whatever it targets.

- **For:** the code must eventually satisfy the default branch's gate, so a PR whose
  Build/Test/Lint never ran is unverified no matter what it targets. Simple, one
  authority, no proxy predicate to get wrong.
- **Against:** wrong for a genuinely protected release base. A `release/1.2` branch with
  its own (deliberately narrower or different) required set would be measured against
  `main`'s and block on contexts that legitimately do not apply to it.
- **Effect on stacks:** blocks them until their checks run. Consistent with the #355
  verdict that stacks are unsuitable for reviewed work under this repo's workflow.

### B. Reconcile against the base ref's own set, honestly empty

Ask GitHub what applies to THIS ref; reconcile against that.

- **For:** faithful to GitHub's model. Never invents policy. No proxy predicate.
- **Against:** returns empty for exactly the stacked case, so the gate stays blind
  precisely where the bug lives. This formalizes the current behavior rather than fixing
  it, and would let #3021 pass again tomorrow.
- **Verdict:** rejected. It documents the hole instead of closing it.

### C. Base's own required set when it HAS one; else the default branch's, loudly

Union both authorities for the base ref. If that union contains no
`required_status_checks`, fall back to the default branch's set and say so in the output.

- **For:** correct for the motivating case (stacked PR blocked) AND for a genuinely
  protected release base (its own rules win). Invents policy in exactly one place, and
  announces it there.
- **Against:** more moving parts, and it depends on F4 being handled correctly — the
  predicate is "has required_status_checks", never "has rules". Get that wrong and it
  degrades to Option B silently.
- **Mitigation:** F4 is a harness case, not a comment. A fixture reproducing stillwater's
  `copilot_code_review`-only response must fail if the predicate regresses to rule
  presence.

---

## Recommendation: C, with the F4 predicate stated as an invariant

C is the only option that is correct on both shapes we can actually observe, and its
one failure mode is a predicate error that a fixture can pin. A is defensible and
simpler; it is wrong only on a shape this repo does not currently have (a protected
release base), and if the maintainer prefers to trade that for simplicity, that is a
reasonable call — the two differ only on a case that does not exist here today.

B is rejected outright: a gate whose expected set is empty on the one shape that
motivated the issue has not been fixed.

### Fail direction when the expected set is UNREADABLE: BLOCK

Consistent with #334's UNKNOWN precedent — "still computing" must never read as "fine"
in a fail-closed gate. A gate that cannot verify has not passed.

The cost is real and should be stated: a transient GitHub API failure blocks merges
until retried. That is the correct direction for this class of gate, and if it proves
noisy in practice that is a tuning decision with evidence behind it, not a reason to
start permissive. Note F3 materially reduces the risk — the endpoint we depend on does
not need admin scope, so the common "unreadable" case (missing scope) largely disappears.

### What this does NOT do

Deliberately does not enumerate rule types. #334's lesson stands: a hand-picked subset
silently treats the remainder as absent. This reconciles the required-CONTEXT set only,
and leaves every other rule to GitHub's aggregate verdict.

---

## Acceptance criteria (mapping #375)

- [x] FULL mode reconciles the rollup against an expected set and BLOCKs on a missing
      required context, NAMING the missing ones
- [x] Expected set is the UNION of the rulesets API and legacy branch protection (F2).
      IMPLEMENTED, after review caught that the first draft shipped the rulesets leg
      only while this AC and F2 both asserted a union. The union is a NO-OP on both
      repos today (legacy is a strict subset: 1 context vs 4), which is exactly why the
      omission looked correct — it would have become a false green the moment a
      legacy-only required context appeared. The two legs are deliberately ASYMMETRIC:
      the rules read is fail-closed, the legacy read is best-effort, because legacy
      404s ("Branch not protected") wherever it is simply not configured and blocking
      on that would wedge every merge on a rulesets-only repo.
- [x] The fallback predicate is `has required_status_checks`, never `has rules` (F4),
      with a fixture reproducing stillwater's `copilot_code_review`-only response
- [x] The fallback is REPORTED in the output, never silent
- [x] Unreadable expected set BLOCKS, with the reason named. ALL FOUR unreadable inputs
      are now proven SEPARATELY (base ref, base rules, default-branch name,
      default-branch rules). A single blanket failure knob exited at the first read, so
      two of the four exits were unreachable by any fixture and could be neutered into a
      silent skip without failing a test.
- [x] A correctly-based PR with all checks green still PASSES (no regression)
- [x] Mutation-proven: a test that FAILS against current code, per
      `prove-a-new-gate-can-fail`. The #3021 shape (1 of 18 present, mss=CLEAN) is the
      canonical fixture.

## What review found after the first draft, and the invariant it produced

Three adversarial passes found SIX instances of ONE defect, all in code written for this
change: an unchecked status or type turning "I could not read this" into "there was
nothing to read", which routes to PASS, which arms the merge-auth token. Recorded because
the pattern matters more than any of the six:

1. `|| echo ""` on a lookup whose failure writes its BODY TO STDOUT — twice. The second
   was in the very round that fixed the first and documented it at length three comment
   blocks above the call site it missed.
2. `jq -e .` proving a body is JSON but never that it is the ARRAY OF RULES contracted, so
   an enveloped `{"rules":[...]}`, a poisoned array, or `{"message":...}` served 200 all
   read as "requires nothing".
3. An extraction `jq` whose exit status was discarded by plain assignment — `pipefail`
   does not see one.
4. `gh --jq` exiting 1 for two unrelated reasons (endpoint 404 vs filter failure),
   conflating ABSENT with UNPARSEABLE on the legacy leg.
5. `select(. != null)` DROPPING a malformed context — the under-reporting direction,
   which is the one that PASSes.

THE STRUCTURAL FIX, not just the five patches: the type/newline invariant is asserted
ONCE ON THE UNION rather than per-leg. The newline guard was first written on the rules
leg alone, passed its own harness case, and left an identical live false PASS on the
legacy leg one call site over — a rule applied at one site and not its sibling reads as
enforced while the other half sails through. A per-leg omission is now not expressible.

The harness STUB was what made three of these invisible. It printed the protection
fixture verbatim regardless of `--jq`, so the entire legacy half of the union was
untested (deleting either half of the filter failed ZERO tests, and the case that looked
like coverage passed only because the context name appeared as a SUBSTRING of a dumped
JSON blob). Separately its default-branch failure wrote to STDERR — the one place real
`gh` writes to STDOUT. A stub kinder than reality makes every test around it vacuous.

## Known limitation: a conditionally-required check

A required context whose workflow is path-filtered will not appear in the rollup on a PR
that touches none of those paths, and this reconciliation would then block permanently.
That is consistent with the standing repo rule against `paths-ignore` on triggers when
required status checks exist (a check that never runs already reads as failed to
GitHub's own gate), so it is a documented constraint rather than a new defect — but it
is the shape most likely to surprise someone adopting this in another repo.

## Rigor

Not a floor file, so the STANDARD tier applies: one multi-lens adversarial pass, plus a
hostile fix-scoped verify round. Escalates to the full loop on any Critical/Important
finding.

The blast radius is nonetheless real and should be said plainly: this oracle gates
`orchestrate-authorize-merge.sh`, which arms the session-scoped merge-auth token the
deterministic floor reads (#263 Piece B). A false PASS here converts into a
floor-authorized merge. The change direction is toward MORE blocking, which is the safe
direction, but a defect that made the reconciliation itself wrong could block every
merge — so the no-regression criterion above is load-bearing, not ceremony.

## Related

- #334 — adopted `mergeStateStatus` as the aggregate verdict. This is the gap that fix
  does not cover, and the reason it does not (ruleset scoping) is F1.
- #379 — 16 harness steps never run in CI, including the one covering
  `orchestrate-authorize-merge.sh`. Same family: a check that reads as coverage.
- #355 — the stacked-PR evaluation that surfaced this. The defect is INDEPENDENT of
  stacks and bites any non-default-base PR.
