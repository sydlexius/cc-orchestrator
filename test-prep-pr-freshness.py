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
import shlex
import subprocess
import sys
import tempfile

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

print("\n== Step 7 / handle-review carry the verdict to safe-push (#492) ==")
HR = os.path.join(REPO, "commands", "handle-review.md")
hr_text = open(HR, encoding="utf-8").read()
m7 = re.search(r"^## Step 7 -- Push\b.*?(?=^## Step 8\b)", text, re.S | re.M)
step7 = m7.group(0) if m7 else ""
LEG = re.compile(r'^.*\[ "\$leg" = (?:repo|plugin|stable) \].*safe-push\.sh.*$', re.M)
push_lines7 = LEG.findall(step7)
check("prep-pr Step 7 has the three safe-push exec legs", len(push_lines7) == 3)
check("prep-pr Step 7 legs pass the stale flag and the base flag",
      len(push_lines7) == 3 and all("$stale_flag" in ln and "$base_flag" in ln for ln in push_lines7))
check("prep-pr Step 7 never hard-codes --stale-ok on a leg (it is derived)",
      all("--stale-ok" not in ln for ln in push_lines7))

# #496 review (CR + Copilot): a stale_flag="" default followed by "the lead sets it" prose is
# DEAD - each fenced block is its own shell, so nothing set outside reaches it. EXECUTE the
# block's derivation lines (from `stale_flag=""` through the `case`) against a stubbed gh and
# assert the resulting flag, so a revert to prose-only reddens here instead of passing a grep.
md = re.search(r'^stale_flag=""\n.*?^case "\$pr_activity".*?esac\n', step7, re.S | re.M)
derive = md.group(0) if md else ""
check("prep-pr Step 7 derives stale_flag in-block from the PR's review activity", bool(derive))
# The stub gh below ignores its argv, so pin the QUERY statically: it must be Step 1c's own
# predicate (reviews + comments), or the two steps silently disagree on "reviewed".
check("prep-pr Step 7's activity query is Step 1c's predicate (reviews + comments)",
      "(.reviews|length) + (.comments|length)" in derive and "reviews,comments" in derive)


def run_derive(gh_body, gh_rc=0):
    with tempfile.TemporaryDirectory() as d:
        gh = os.path.join(d, "gh")
        with open(gh, "w") as f:
            f.write("#!/bin/sh\nprintf '%s' " + shlex.quote(gh_body) + "\nexit " + str(gh_rc) + "\n")
        os.chmod(gh, 0o755)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""))
        r = subprocess.run(["bash", "-c", derive + '\nprintf "%s" "$stale_flag"'],
                           capture_output=True, text=True, env=env)
        return r.stdout


if derive:
    check("reviewed PR (activity 3) -> --stale-ok", run_derive("3") == "--stale-ok")
    check("unreviewed PR (activity 0) -> empty, safe-push still refuses", run_derive("0") == "")
    check("no PR (gh fails) -> empty", run_derive("", gh_rc=1) == "")
    check("unreadable count (non-numeric) -> empty (fail closed)", run_derive("null") == "")
check("prep-pr Step 7 passes --base ONLY when the base differs from the default branch",
      'base_flag="--base $pr_base"' in step7 and '"$pr_base" != "$def_base"' in step7)
check("prep-pr Step 7 resolves the PR base via baseRefName", "baseRefName" in step7)

mh = re.search(r"GATED PUSH block.*?^```bash\n(.*?)^```", hr_text, re.S | re.M)
hr_block = mh.group(1) if mh else ""
hr_lines = LEG.findall(hr_block)
check("handle-review gated push block has the three safe-push exec legs", len(hr_lines) == 3)
check("handle-review legs ALL pass --stale-ok (a fix round is on a reviewed PR)",
      len(hr_lines) == 3 and all("--stale-ok" in ln for ln in hr_lines))
check("handle-review legs pass the base flag, resolved from baseRefName only when non-default",
      len(hr_lines) == 3 and all("$base_flag" in ln for ln in hr_lines)
      and "baseRefName" in hr_block and '"$pr_base" != "$def_base"' in hr_block)
check("handle-review never hard-codes a base flag", "--base main" not in hr_text)
check("handle-review runs gh pr update-branch AFTER replies + resolves, default mode",
      'gh pr update-branch "$pr_number"' in hr_text and "AFTER this round's replies" in hr_text)
check("handle-review forbids --rebase on that refresh", "NEVER `--rebase`" in hr_text)
check("handle-review re-arms the watch on the new head", "RE-ARM `/pr-watch`" in hr_text)
check("handle-review orders a worktree resync after update-branch (#492)",
      "git fetch origin && git merge --ff-only origin/<branch>" in hr_text)
import json as _json, os as _os
_sch = _json.load(open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                  "skills/orchestrate/templates/stack.schema.json")))
check("stack.schema.json parses and declares stale_ok as boolean (#492)",
      _sch["items"]["properties"].get("stale_ok", {}).get("type") == "boolean")
for name, blk in (("prep-pr Step 7", step7), ("handle-review", hr_block)):
    check(f"{name}: no double-quoted plugin-root or variable exec path (Helper exec paths)",
          'bash "${CLAUDE_PLUGIN_ROOT}' not in blk and re.search(r'bash "\$(?!\()', blk) is None)

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    print("\nThe /prep-pr freshness wiring drifted. Fix commands/prep-pr.md rather than")
    print("relaxing these assertions: each one encodes a failure mode #329 measured.")
    sys.exit(1)
print("all prep-pr freshness wiring assertions passed")
