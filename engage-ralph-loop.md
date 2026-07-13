# Adversarial critic loop (reusable brief)

A target-agnostic brief for running a BOUNDED adversarial critic loop to converge an
artifact (a guard/hook, a parser, a state machine, a spec, an API, a migration) to "dry."
Point a `/ralph-loop` (or any iterate-until-done harness) at this file, or paste it, with
the INSTANTIATION block below filled in. Use `--max-iterations N` + a `--completion-promise`
so it terminates.

## Instantiate per target (fill this in)
- **TARGET:** the file(s)/component under test + where its tests live.
- **THREAT/SCOPE MODEL:** what failure modes are IN scope vs explicitly OUT of scope.
  A loop with no declared scope never converges - it keeps finding "issues" outside the
  point of the artifact. State the boundary and refuse to chase past it.
- **GATES:** the exact commands that must stay green after any fix (lint, test harness,
  self-test, build). List them; they are the definition of "still correct."
- **ISOLATION:** how to exercise the target WITHOUT perturbing it. If the artifact inspects
  its own invocation environment (a hook reading the command line, a linter linting its own
  config, a formatter formatting itself), naive testing trips the artifact. Drive it through
  a harness/driver/fixture so the test invocation is inert. (Example: the orchestrate guard
  inspects the Bash command line, so its harness builds payloads INSIDE a Python driver -
  `python3 driver.py` - never on the command line.)

## RIGOR TIER - pick this FIRST, before round 1 (#287)
Rigor is scaled by BLAST RADIUS, not by which file was touched. Ask what a defect in this diff
can actually DO:

| Tier | The diff can... | Protocol |
|---|---|---|
| **DENY-AUTHORITY** | deny/permit an action: the floor guard, the merge-auth token issuer, anything whose exit code gates a tool call or a merge | **FULL loop, K=2 convergence.** A defect here permits a bad push or merge. |
| **ADVISORY** | only emit a nudge: a hook that provably `exit 0`s on every path and cannot block anything | **ONE multi-lens pass + ONE fix-scoped verify round.** No K loop. |
| **STANDARD** | everything else (prose, docs, ordinary scripts) | **ONE multi-lens pass.** |

The ADVISORY tier is earned by a VERIFIED PROPERTY, never by a filename: "advisory" is a property
of the CURRENT file, and the moment a diff adds an `exit 2` or a stdout write, that same diff is
the one being reviewed. Verify on the POST-diff file: no `exit [1-9]` outside a self-test block, no
stdout write on any live path, and the hook wiring still swallows failure. **Cannot verify it ->
DENY-AUTHORITY tier.** (This is why cc-orchestrator's `orchestrate-steer.sh` is advisory and
`orchestrate-guard.sh` is not, despite sitting side by side in the same "floor" directory.)

ESCALATION HATCH (the reason the fast tiers are safe): a **CRITICAL or IMPORTANT** finding in a
single-pass tier PROMOTES that diff to the FULL loop. The speed win is therefore taken ONLY on
diffs that come back clean - which is exactly where it is free. A diff that deserved ten rounds
still gets them; it just has to earn them.

STATE THE BUDGET BEFORE ROUND 1 (tier, expected rounds, rough token/wall-clock cost), so the cost
is visible up front instead of discovered at round 10.

## Per round - run the LENSES IN PARALLEL, then report
A "pass" is NOT one critic hunting one defect class. That framing is what made a single pass a
COVERAGE CUT rather than a speed-up: one critic finds one class, and the other classes ship. Run
the lenses CONCURRENTLY and merge their findings into one set. Same coverage, a fraction of the
wall-clock.

Standing lenses (adapt per target; each is a separate critic):
- **CORRECTNESS / SAFETY** - can it produce a wrong verdict in either direction? For a gate: what
  real finding does this now MISS? (Recall loss is the defect that hides best.)
- **TEST VACUITY** - would each new test actually FAIL if the fix were reverted? MUTATION-PROVE it.
  A regression test that cannot fail is theater, and it is common.
- **DOC-TRUTH** - does every comment, doc, message string and playbook line describe the SHIPPED
  behavior? A doc that teaches the old behavior re-creates the bug in the next agent.
- **ADVERSARIAL INPUT** - malformed, hostile, empty, huge, injected, encoding-weird.

1. **Hunt** with every lens at once. Breadth across classes beats depth on one, and parallel lenses
   buy the breadth without buying the rounds.
2. **Verify before believing.** Reproduce the candidate against the real artifact through the
   ISOLATION harness and capture the actual behavior. Many "findings" (especially relayed
   from another agent or a bug report) are wrong on contact - ground-truth every claim before
   acting on it. Distinguish a real defect from an accepted/documented limitation.
3. **If real, fix minimally + add a regression case + re-run ALL gates green**, then commit
   (locally; never push unless the owner says so). A fix that breaks a gate is not a fix.
4. **Pushback vs document.** If a proposed fix is over-reach (it pushes the artifact past its
   declared scope, adds disproportionate complexity, or has a cheaper backstop), do NOT
   implement it - capture the reasoned technical pushback in the artifact's design doc. If a
   defect is real but out of the threat model (or backstopped elsewhere), DOCUMENT it as an
   accepted limitation rather than chasing it. Over-engineering a narrow tool is itself a bug.
5. **Capture method lessons.** Any "right gate / right method" learned (a new isolation trick,
   a verify step that caught a false claim) goes into the design doc so the next pass inherits it.

## THE VERIFY ROUND IS HOSTILE AND FIX-SCOPED (#287)
The fixes are NEW, UNREVIEWED CODE. In practice they are where the longest-surviving defects come
from: a fix writes a comment that is false, or a message string whose advice is causally wrong, or
it silently breaks a guard that an earlier round installed. A verify round that merely re-runs the
gates and confirms "the reported finding is fixed" is STRUCTURALLY BLIND to all of that.

So the verify round takes the FIX DIFF as its target (`HEAD_at_pass_1..HEAD_after_fixes`) and its
brief is: *"these fixes are unreviewed code; the comments and message strings they wrote are CLAIMS
to be falsified."* Real examples this caught: a canonical-file list that a fix left 4 entries short;
a lockstep test whose regex silently captured 12 of 15 names, so it could not fail on the very files
it guarded; a WARN whose remedy was causally FALSE ("name it" does not make an agent async).

## Convergence + honesty
- Converge when **K consecutive iterations (default 2) find nothing new AND all gates are
  green.** A single quiet round is not convergence; the tail of a defect distribution hides in
  the second dry round. (K=2 applies to the DENY-AUTHORITY tier; the fast tiers converge on their
  one pass + verify, escalating to the full loop on any CRITICAL/IMPORTANT.)
- **MAX_ROUNDS (default 6). The cap is a BUDGET ALARM, not a quality bound.** A cap does not reduce
  defects; it stops looking for them. So hitting it NEVER means "ship anyway." STOP and hand the
  owner: (a) rounds run, (b) findings fixed, (c) **the lens classes NOT yet probed** - the honest
  statement of what is still unreviewed - and (d) the choice: continue / ship-with-known-risk /
  **split the diff**. A diff that needs more than ~6 rounds is usually telling you it is too big;
  decomposition, not more rounds, is the remedy.
- **SEVERITY-AWARE K.** A CRITICAL/IMPORTANT finding resets K. A purely cosmetic one is FIXED but
  does NOT reset it - otherwise a missing space costs two more full rounds (it has). TWO GUARDRAILS,
  because this rule is the easiest one to abuse:
  - **The LEAD assigns severity, not the finding critic.** A critic that wants the loop to end has
    an incentive to downgrade.
  - **A finding in a user-facing message string, a behavior-asserting comment, or a doc that
    describes shipped behavior is NEVER cosmetic.** Only pure typography/whitespace/formatting is.
    (A causally-false remedy string reads like a "wording nit" and is not one.)
- The completion promise must be **genuinely true** - do NOT emit it to escape the loop. If the
  loop's own completion detection misfires (tooling bug) while the work is genuinely done, use
  the harness's sanctioned cancel/off-switch; that is not the same as lying a false promise.
- Scale effort to the ask: a few finder-classes + single-vote verify for a quick check; a
  larger class sweep + multi-perspective verify + a synthesis pass for "audit thoroughly."

## Anti-patterns
- Rubber-stamping ("looks fine") instead of an isolated reproduction.
- Chasing out-of-scope spellings the threat model excludes (e.g. adversarial evasion of a
  guardrail that is, by design, a guardrail-not-a-sandbox).
- Fixing a false-positive in one matcher while leaving its twin (sweep the whole CLASS, not the
  one reported instance).
- Declaring dry after one quiet round; silently capping coverage without logging what was skipped.
