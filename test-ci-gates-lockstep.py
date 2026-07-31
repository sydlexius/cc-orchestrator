#!/usr/bin/env python3
"""Lockstep harness: the CI lint enumerations must match .gates.toml's (issue #364).

THE BUG THIS EXISTS TO PREVENT. `.gates.toml` and `.github/workflows/ci.yml` each carry a
HAND-MAINTAINED list of what to lint, and nothing compared them. They drifted to the point
where CI shellchecked 28 of the 37 scripts the local gate covered - including
`orchestrate-authorize-merge.sh`, which arms the merge-auth token the security floor reads.
A shellcheck error in any of the other nine passed CI silently.

WHY A HARNESS AND NOT A ONE-TIME RE-SYNC. Re-syncing by hand fixes today's drift and
guarantees tomorrow's: the next script added to `.gates.toml` gets forgotten exactly as
those nine were. The repo has solved this shape twice already - `test-version-lockstep.py`
for the SKILL.md/plugin.json pair, and the `#284` exact-count assertion for HELPER_NAMES -
so this copies that pattern rather than inventing one.

WHY NOT DERIVE CI's LIST FROM .gates.toml. That makes drift impossible, but CI would then
depend on parsing repo config at CI time, and `ci.yml` would lose its self-contained,
digest-pinned shape. Duplication-plus-detection matches existing practice here.

THREE CHECKS, because two of them can pass while the invariant is broken:
  1. SET EQUALITY, BOTH DIRECTIONS. A one-way check misses a CI-only entry, which is a
     stale path that lints nothing and looks like coverage.
  2. A PARSE-SANITY FLOOR. An empty or truncated parse compares {} against {} and passes,
     which is exactly how a drift guard becomes decorative. (Learned the hard way in #330:
     a `split("]")` truncated on a `[ -x ]` inside a comment and reported a present entry
     as missing. Assert the parse before trusting the verdict.)
  3. A FILESYSTEM CROSS-CHECK. Set equality only proves the two lists agree - they can
     agree and both omit a script that exists. Every `scripts/*.sh` must be linted
     somewhere, or the drift guard blesses a shared blind spot.

Stdlib only, no network. Run: python3 test-ci-gates-lockstep.py
"""
import glob
import os
import re
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    sys.exit("FAIL: tomllib unavailable (needs Python 3.11+)")

ROOT = os.path.dirname(os.path.abspath(__file__))
GATES = os.path.join(ROOT, ".gates.toml")
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")

# Scripts deliberately exempt from the filesystem cross-check, each with a stated reason.
# An entry here is a decision to leave a file unlinted, so it must not be silently editable.
FS_EXEMPT: dict[str, str] = {}


def fail(msg):
    sys.exit(f"FAIL: {msg}")


def expand(tokens):
    """Resolve glob tokens against the repo so both sides compare real paths."""
    out = set()
    for t in tokens:
        if not t or t.startswith("-"):
            continue
        if any(ch in t for ch in "*?["):
            out.update(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, t)))
        else:
            out.add(t)
    return out


def gates_step_run(name):
    try:
        with open(GATES, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as e:
        fail(f"cannot read/parse .gates.toml: {e}")
    for step in data.get("prep_pr", {}).get("steps", []):
        if step.get("name") == name:
            run = step.get("run", "")
            if not run:
                fail(f".gates.toml step '{name}' has an empty run string")
            return run
    fail(f".gates.toml has no prep_pr step named '{name}'")


def ci_text():
    try:
        with open(CI, encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        fail(f"cannot read ci.yml: {e}")


print("CI <-> .gates.toml lint lockstep (#364)")

ci_src = ci_text()

# --- shellcheck ------------------------------------------------------------------------
gates_sc = expand(gates_step_run("shellcheck").split()[1:])
m = re.search(r"scripts=\((.*?)\)", ci_src, re.S)
if not m:
    fail("ci.yml has no `scripts=( ... )` shellcheck array (parse failed, not a drift)")
ci_sc = expand(m.group(1).split())

# --- ruff ------------------------------------------------------------------------------
gates_ruff = expand(gates_step_run("ruff").split()[1:])
m = re.search(r"run: ruff check[^\n]*", ci_src)
if not m:
    fail("ci.yml has no `ruff check` line (parse failed, not a drift)")
ci_ruff = expand(m.group(0).split()[2:])

# --- 2. parse sanity, BEFORE any verdict ------------------------------------------------
# An empty parse compares {} to {} and passes. These floors are deliberately well below the
# real counts (37 / ~40) so ordinary growth never trips them, but a collapsed parse does.
for label, s in (("gates shellcheck", gates_sc), ("ci shellcheck", ci_sc),
                 ("gates ruff", gates_ruff), ("ci ruff", ci_ruff)):
    if len(s) < 10:
        fail(f"{label} parsed only {len(s)} entries - the parse broke; fix it rather than "
             f"the lists (an empty-vs-empty comparison passes and proves nothing)")
print(f"  [ok  ] parses are non-degenerate "
      f"(shellcheck {len(gates_sc)}/{len(ci_sc)}, ruff {len(gates_ruff)}/{len(ci_ruff)})")

# --- 1. set equality, BOTH directions ---------------------------------------------------
problems = []
for label, a, b in (("shellcheck", gates_sc, ci_sc), ("ruff", gates_ruff, ci_ruff)):
    only_gates = sorted(a - b)
    only_ci = sorted(b - a)
    if only_gates:
        problems.append(f"{label}: in .gates.toml but NOT linted by CI -> {only_gates}")
    if only_ci:
        problems.append(f"{label}: in ci.yml but NOT in .gates.toml -> {only_ci} "
                        f"(a CI-only entry is often a stale path that lints nothing)")
if problems:
    fail("the CI and .gates.toml lint lists have drifted:\n  " + "\n  ".join(problems)
         + "\n\nAdd the missing entries to BOTH lists. Do not delete from .gates.toml to "
           "make this pass - that removes local coverage instead of restoring CI's.")
print("  [ok  ] shellcheck lists match in both directions")
print("  [ok  ] ruff lists match in both directions")

# --- 3. filesystem cross-check ----------------------------------------------------------
# Both lists agreeing does not mean they are complete: they can agree and both omit a file.
on_disk = {os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, "scripts", "*.sh"))}
unlinted = sorted(on_disk - gates_sc - set(FS_EXEMPT))
if unlinted:
    fail("these scripts exist but are linted by NEITHER list:\n  " + "\n  ".join(unlinted)
         + "\n\nAdd them to the shellcheck step in .gates.toml AND ci.yml, or add an entry "
           "to FS_EXEMPT in this harness with a written reason.")
print(f"  [ok  ] every scripts/*.sh is linted ({len(on_disk)} on disk)")

stale = sorted(gates_sc - on_disk)
if stale:
    fail(f"shellcheck targets that no longer exist on disk: {stale}")
print("  [ok  ] no shellcheck target is missing from disk")

print("\nok: the CI and .gates.toml lint enumerations are in lockstep")
