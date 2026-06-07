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

## Per iteration - do ONE focused thing, then report it
1. **Hunt** one more class of defect (a bypass / false-positive / edge case / inconsistency).
   Pick a class you have not yet probed; breadth across classes beats depth on one.
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

## Convergence + honesty
- Converge when **K consecutive iterations (default 2) find nothing new AND all gates are
  green.** A single quiet round is not convergence; the tail of a defect distribution hides in
  the second dry round.
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
