#!/usr/bin/env python3
"""Content-assertion harness for the /prep-pr base-freshness wiring (issue #329).

WHY A CONTENT TEST. The wiring lives in PROSE (`commands/prep-pr.md`), which no runtime
harness executes, so nothing would notice if the step were dropped, softened, or edited
into a hard-coded `main`. #329's own diagnosis is that a check which exists but is not
wired where it matters is indistinguishable from no check - a content test is what keeps
this from silently becoming that again.

WHAT IT DELIBERATELY DOES NOT DO. It asserts the invariants that make the step CORRECT,
not the wording that happens to express them. Pinning prose verbatim produces a harness
that fails on every copy-edit, which trains people to update the expected string without
reading it - the same corrosion as an override that means "dismiss".

Modeled on test-version-lockstep.py: stdlib-only, no network, pure file-content checks.

Run: python3 test-prep-pr-freshness.py
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
DOC = os.path.join(REPO, "commands", "prep-pr.md")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"
    print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


if not os.path.isfile(DOC):
    sys.exit(f"ERROR: {DOC} not found")

text = open(DOC, encoding="utf-8").read()

# The freshness step only, so an assertion cannot be satisfied by unrelated prose
# elsewhere in a 900-line command file (e.g. the Step 1b size-gate override).
m = re.search(r"^## Step 1c\b.*?(?=^## Step 2\b)", text, re.S | re.M)
step = m.group(0) if m else ""

print("prep-pr base-freshness wiring (#329)")

print("\n== the step exists and delegates ==")
check("a base-freshness step is present in the prep-pr flow", bool(step))
check("it delegates to base-freshness.sh (no reimplemented fetch + rev-list)",
      "base-freshness.sh" in step)
check("it does NOT reimplement the behind-count itself",
      "rev-list" not in step and "--count" not in step)
check("it runs BEFORE the gate run (cheap check first)",
      text.find("## Step 1c") < text.find("## Step 2 --"))

print("\n== the base is resolved, never assumed ==")
check("it resolves the PR's own base (baseRefName)", "baseRefName" in step)
check("it falls back to the recorded default branch", "refs/remotes/origin/HEAD" in step)
check("it has a gh default-branch fallback", "defaultBranchRef" in step)
# The failure this guards: a hard-coded base silently mismeasures every backport branch.
bare_main = re.search(r'base(?:_name)?\s*=\s*["\']?main\b', step)
check("it never hard-codes `main` as the base", bare_main is None)

print("\n== exit-code contract: only a DEFINITIVE behind is actionable ==")
check("exit 0 (fresh or unknown) is non-blocking", re.search(r"`0`.*(fresh|continue)", step, re.S) is not None)
check("unknown is explicitly called out as non-blocking", "Never block on unknown" in step or "never block on unknown" in step.lower())
check("exit 2 (malformed) is non-blocking", re.search(r"`2`.*(warn|continue)", step, re.S | re.I) is not None)
check("exit 1 is the only blocking case", "BEHIND" in step)

print("\n== the state-dependent policy (the whole point) ==")
check("an unreviewed PR / no PR stops the push", "STOP" in step)
# ANCHOR BOTH VERDICTS TO THEIR OWN BULLET. Searching the whole step for "WARN and
# continue" is satisfied by the explanatory prose that follows, so flipping the reviewed
# branch to **STOP** - which destroys the entire state-dependent policy - passed. Caught
# by mutation; the loose form asserted that the words appear, not that the branch decides.
unreviewed_bullet = re.search(r"^- \*\*No PR yet.*?(?=^- \*\*)", step, re.S | re.M)
reviewed_bullet = re.search(r"^- \*\*The PR has review activity.*?(?=^\*\*Never)", step, re.S | re.M)
check("the unreviewed bullet exists and its verdict is STOP",
      unreviewed_bullet is not None and "**STOP.**" in unreviewed_bullet.group(0))
check("the REVIEWED bullet exists and its verdict is WARN, never STOP",
      reviewed_bullet is not None
      and "**WARN and continue.**" in reviewed_bullet.group(0)
      and "**STOP.**" not in reviewed_bullet.group(0))
check("the reason is named: refreshing dismisses a prior review",
      "dismiss" in step.lower())
# The hinge that must not drift: activity, never reviewDecision.
check("the reviewed predicate uses review ACTIVITY (reviews/comments)",
      "reviews" in step and "comments" in step)
check("it explicitly rejects reviewDecision as the predicate",
      "reviewDecision" in step)
check("an unreadable count fails toward SURFACING, not acting",
      "unreadable" in step.lower())

print("\n== remedy prose is additive-only ==")
check("it names the additive merge remedy", "git merge origin/" in step)
check("it names the server-side update-branch remedy", "gh pr update-branch" in step)
check("it forbids --rebase explicitly", "--rebase" in step and "Never `--rebase`" in step)
check("it documents the override channel", "override" in step.lower())
check("the override rationale is carried into the PR body",
      "Base-freshness override" in step)

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    print("\nThe /prep-pr freshness wiring drifted. Fix commands/prep-pr.md rather than")
    print("relaxing these assertions: each one encodes a failure mode #329 measured.")
    sys.exit(1)
print("all prep-pr freshness wiring assertions passed")
