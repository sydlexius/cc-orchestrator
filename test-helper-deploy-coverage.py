#!/usr/bin/env python3
"""Every helper this repo TELLS people to use must be deployable (issue #402).

THE DEFECT THIS EXISTS TO PREVENT. `HELPER_NAMES` in orchestrate-setup.py is the hand-
maintained list of scripts `configure` deploys to the stable `~/.claude/scripts/` path. Two
separate things reference helpers BY NAME and neither was compared against it:

  1. `orchestrate-steer.sh` rule 2 names six `gh-*` wrappers as the remedy for a raw
     `gh api` mutation. All six were missing from HELPER_NAMES, so following the repo's own
     nudge produced "No such file or directory" for anyone without a repo checkout beside
     them. Measured live during a post-merge cleanup.
  2. Command files and scripts invoke helpers from `$HOME/.claude/scripts/<name>`. An
     omission there is the #216 / #217 / #330 shape, which HELPER_NAMES' own comment blocks
     record three times - each found by someone hitting it, after it shipped.

THE PREDICATE IS "DOES THIS REPO TELL PEOPLE TO USE IT", not "does something invoke it".
A steer rule naming a remedy the install does not provide is worse than no nudge: it costs a
turn and teaches the reader to distrust the channel. That is why the advertised-wrapper check
below is not merely a nice-to-have - it covers the case that actually bit.

WHY A HARNESS AND NOT A ONE-TIME RE-SYNC. Re-syncing fixes today's omission and guarantees
tomorrow's: the next wrapper added to the nudge gets forgotten exactly as these six were.
Same shape as test-version-lockstep.py, test-ci-gates-lockstep.py (#364), and the #284
exact-count assertion.

PARSE WITH `ast`, NEVER A REGEX. A regex written during the original audit stopped at the
first `)` inside the tuple's comment block and reported 12 of 21 entries. "0 missing" against
a TRUNCATED list is indistinguishable from a clean result - the exact failure this file exists
to catch, reproduced in the tool built to catch it.

THREE CHECKS, because the obvious one passes while the invariant is broken:
  1. MEMBERSHIP, from both reference sources (the steer nudge, and stable-path invocations).
  2. A PARSE-SANITY FLOOR before any verdict. An empty parse compares nothing to nothing and
     PASSES, which is how a drift guard becomes decorative (#330's lesson).
  3. A FILESYSTEM CROSS-CHECK. Membership only proves the lists agree; a HELPER_NAMES entry
     that does not exist in scripts/ is a typo that deploys nothing while reading as coverage.

Stdlib only, no network, read-only.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SETUP = ROOT / "scripts" / "orchestrate-setup.py"
STEER = ROOT / "scripts" / "orchestrate-steer.sh"

# A floor well below the real count catches a broken parse while leaving room to add helpers;
# setting it AT the count would fail for the wrong reason (that pin is test-orchestrate-steer.py's
# #284 job, and it is deliberate there). The exact count is NOT restated here on purpose: this
# file exists to catch stale hand-maintained lists, and a hardcoded count in its own comment is
# one -- it drifted from 27 to 29 within a single PR and CodeRabbit caught it, which is precisely
# the read-only drift a gate cannot see.
MIN_HELPERS = 20

# Files that may reference a helper by name. Scanned for `$HOME/.claude/scripts/<name>` and
# `~/.claude/scripts/<name>` invocations.
SCAN_GLOBS = ("commands/*.md", "scripts/*.sh", "skills/orchestrate/*.md")

STABLE_PATH_RE = re.compile(r"(?:\$HOME|~|\$\{HOME\})/\.claude/scripts/([A-Za-z0-9_.-]+\.(?:sh|py))")

# DEPLOYED BY A DEDICATED FUNCTION, NOT BY HELPER_NAMES -- so their absence from that tuple is
# correct, not a gap. Each has its own `_deploy_*()` in orchestrate-setup.py because each needs
# handling the generic helper loop does not: the guard and steer are hook TARGETS wired into
# settings.json, the context meter likewise, and the setup script must refuse a no-op self-copy.
#
# THIS EXEMPTION IS VERIFIED, NOT ASSERTED: the check below requires a matching `_deploy_<x>()`
# to actually exist in orchestrate-setup.py. Otherwise this list becomes the place a genuine
# omission goes to hide -- an exemption nobody re-checks is how a coverage guard rots.
DEDICATED_DEPLOY = {
    "orchestrate-guard.sh": "_deploy_guard",
    "orchestrate-steer.sh": "_deploy_steer",
    "orchestrate-context-meter.sh": "_deploy_ctxmeter",
    "orchestrate-setup.py": "_deploy_setup",
}

failures = []


def check(label, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")
    if not cond:
        if detail:
            for line in detail.splitlines():
                print(f"         {line}")
        failures.append(label)


def helper_names():
    """Parse HELPER_NAMES via ast. Returns [] if the assignment is not found."""
    tree = ast.parse(SETUP.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "HELPER_NAMES" for t in node.targets
        ):
            return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    return []


def advertised_wrappers():
    """Helper names the steer hook names as a remedy, from its emit_warn strings."""
    src = STEER.read_text()
    found = set()
    for msg in re.findall(r'emit_warn\s+"((?:[^"\\]|\\.)*)"', src):
        found.update(re.findall(r"\b([a-z0-9-]+\.sh)\b", msg))
    return found


def stable_path_invocations():
    """Helper names invoked from the stable path across the scanned surfaces."""
    found = set()
    for glob in SCAN_GLOBS:
        for path in ROOT.glob(glob):
            # A script never needs to deploy ITSELF via its own stable-path reference.
            for name in STABLE_PATH_RE.findall(path.read_text()):
                if name != path.name:
                    found.add(name)
    return found


def main():
    print("== helper deploy coverage (HELPER_NAMES) ==")

    for f in (SETUP, STEER):
        if not f.is_file():
            print(f"FAIL: {f} not found", file=sys.stderr)
            return 1

    names = helper_names()

    # CHECK 2 FIRST, deliberately: every assertion below is vacuous on an empty parse, and a
    # vacuous pass is worse than a failure because it reads as coverage.
    check(
        f"parse sanity: HELPER_NAMES has >= {MIN_HELPERS} entries",
        len(names) >= MIN_HELPERS,
        f"parsed {len(names)}; the ast lookup likely broke -- verdicts below are meaningless",
    )
    if len(names) < MIN_HELPERS:
        print(f"\nFAILED: {len(failures)} check(s)", file=sys.stderr)
        return 1

    nameset = set(names)
    check("HELPER_NAMES has no duplicate entries", len(names) == len(nameset),
          f"duplicates: {sorted(n for n in nameset if names.count(n) > 1)}")

    # THE EXEMPTIONS MUST BE REAL. An exemption list nobody re-checks is where a genuine
    # omission hides, so each claimed dedicated deployer must exist in orchestrate-setup.py.
    setup_src = SETUP.read_text()
    bogus = sorted(h for h, fn in DEDICATED_DEPLOY.items() if f"def {fn}(" not in setup_src)
    check(
        f"every dedicated-deploy exemption names a real function ({len(DEDICATED_DEPLOY)})",
        not bogus,
        f"no such deploy function for: {bogus}\n"
        "An unverified exemption silently converts a real gap into a pass.",
    )

    # CHECK 1a: everything the steer hook advertises must be deployable.
    advertised = advertised_wrappers()
    missing_adv = sorted(advertised - nameset - set(DEDICATED_DEPLOY))
    check(
        f"every helper the steer nudge names is in HELPER_NAMES ({len(advertised)} named)",
        not missing_adv,
        f"missing: {missing_adv}\n"
        "A steer rule naming a remedy the install does not provide is worse than no nudge.",
    )

    # CHECK 1b: everything invoked from the stable path must be deployable.
    invoked = stable_path_invocations()
    missing_inv = sorted(invoked - nameset - set(DEDICATED_DEPLOY))
    check(
        f"every stable-path invocation is in HELPER_NAMES ({len(invoked)} invoked)",
        not missing_inv,
        f"missing: {missing_inv}\n"
        "This is the #216/#217/#330 shape: the helper degrades to its not-found branch only "
        "in deployment, while passing every test here (where the repo-local copy is found).",
    )

    # CHECK 3: filesystem cross-check.
    absent = sorted(n for n in nameset if not (ROOT / "scripts" / n).is_file())
    check(
        "every HELPER_NAMES entry exists in scripts/",
        not absent,
        f"absent from scripts/: {absent}\n"
        "An entry that does not exist deploys nothing while reading as coverage.",
    )

    print(f"\n  {len(names)} helpers, {len(advertised)} advertised, {len(invoked)} stable-path invoked")

    if failures:
        print(f"\nFAILED: {len(failures)} check(s)", file=sys.stderr)
        return 1
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
