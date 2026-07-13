#!/usr/bin/env python3
"""Proof harness for patch-coverage.sh (the Codecov-parity patch-coverage gate).

The bug this harness exists to prevent (#288): a single `go test -coverpkg=./... ./...`
emits each coverage block ONCE PER TEST BINARY -- every binary instruments every package,
so binaries that never executed a block contribute a `0` count for it. patch-coverage.sh
applied an ALL-HIT rule per block OCCURRENCE, so one `0` from an unrelated test binary
marked a genuinely-covered line as MISSED. A 100%-covered patch reported 0.00% and FAILED
the gate -- a confidently WRONG number, which is worse than refusing to measure, because
0% is a plausible answer an agent acts on (it goes and writes tests for covered lines).

patch-coverage.sh had NO harness at all before this file. That is how an all-hit rule with
no dedup shipped.

Run: python3 test-patch-coverage.py
"""
import os
import atexit
import shutil
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "patch-coverage.sh")
FAILS = []
_TMPDIRS = []


@atexit.register
def _cleanup_tmpdirs():
    """Sweep the throwaway git repos on EVERY exit path (CodeRabbit, PR #290).

    A sweep at the end of main() leaks them whenever a case raises before reaching it --
    `_git()` runs with check=True, so a git failure aborts mid-case -- and it would also
    be skipped by the sys.exit(1) failure path. atexit covers all three.
    """
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILS.append(label)


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True,
                   capture_output=True, text=True)


def run_case(profile_lines, *, go_src, added_src, threshold="70"):
    """Build a throwaway Go repo with a BASE commit and a HEAD commit that ADDS `added_src`
    to lib.go, write `profile_lines` as the coverage profile, and run patch-coverage.sh
    against it. Returns (exit_code, stdout+stderr)."""
    repo = tempfile.mkdtemp()
    _TMPDIRS.append(repo)          # cleaned in main(); mkdtemp does NOT self-clean
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "go.mod"), "w") as fh:
        fh.write("module example.com/m\n\ngo 1.22\n")
    with open(os.path.join(repo, "lib.go"), "w") as fh:
        fh.write(go_src)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    # HEAD adds the lines under test -- these are the "patch" lines the gate measures.
    with open(os.path.join(repo, "lib.go"), "w") as fh:
        fh.write(go_src + added_src)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "patch")

    prof = os.path.join(repo, "cover.out")
    with open(prof, "w") as fh:
        fh.write("mode: set\n" + "".join(ln if ln.endswith("\n") else ln + "\n"
                                         for ln in profile_lines))
    env = dict(os.environ, COVER_OUT=prof, BASE=base,
               PATCH_COVERAGE_THRESHOLD=threshold)
    p = subprocess.run(["bash", SCRIPT], cwd=repo, env=env,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


# The patch adds 3 executable lines (6,7,8) to lib.go.
GO_BASE = "package m\n\nfunc Base() int {\n\treturn 1\n}\n"
GO_ADDED = "\nfunc Added() int {\n\treturn 2\n}\n"
# Block covering the added body. Lines 6-9; ecol>2 so no trailing-brace drop weirdness.
BLOCK = "example.com/m/lib.go:7.19,9.2"


def main():
    print("patch-coverage.sh harness (#288)")

    # --- THE BUG: a NON-UNIONED profile (the same block emitted once per test binary) ---
    # The block is genuinely COVERED (count 1 from its own binary) but another binary that
    # never executed it contributes a 0. The all-hit rule must NOT let that 0 poison it.
    rc, out = run_case([f"{BLOCK} 2 1", f"{BLOCK} 2 0"], go_src=GO_BASE, added_src=GO_ADDED)
    print(f"    non-unioned -> rc={rc}\n    " + "\n    ".join(
        ln for ln in out.splitlines() if "%" in ln or "Total" in ln))
    # NB: assert on the FULL total line, not a bare `"0.00%" not in out` -- "100.00%" CONTAINS
    # the substring "0.00%", so the naive form is a false-failure trap (it bit this harness).
    check("#288: duplicate block (count 1 + count 0) -> 100%, gate PASSES (union applied)",
          rc == 0 and "Total patch coverage: 100.00%" in out)

    # --- IDEMPOTENCE: the already-unioned profile must produce the SAME result, so a repo
    # whose gate ALREADY pre-unions (e.g. stillwater's pre-push-gate) sees zero change.
    rc_u, out_u = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED)
    check("#288: already-unioned profile -> 100%, gate PASSES", rc_u == 0 and "100.0" in out_u)
    body = [ln for ln in out.splitlines() if "lib.go" in ln]
    body_u = [ln for ln in out_u.splitlines() if "lib.go" in ln]
    check("#288: union is IDEMPOTENT (non-unioned and unioned give identical per-file output)",
          body == body_u)

    # --- ORDER-INDEPENDENCE: the 0 arriving FIRST must not win either.
    rc_o, out_o = run_case([f"{BLOCK} 2 0", f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED)
    check("#288: union is order-independent (0 before 1 still -> 100%)",
          rc_o == 0 and "100.0" in out_o)

    # --- NO REGRESSION of the real all-hit semantics. Two DISTINCT blocks that overlap a
    # line, one hit and one missed, must STILL count that line as MISSED (this mirrors
    # Codecov's partial accounting and is NOT what the union changes -- the union collapses
    # IDENTICAL block keys only, never distinct ones).
    rc_m, out_m = run_case(
        ["example.com/m/lib.go:7.19,8.10 1 1", "example.com/m/lib.go:8.10,9.2 1 0"],
        go_src=GO_BASE, added_src=GO_ADDED, threshold="100")
    check("#288: DISTINCT overlapping blocks (hit + miss) still count the line MISSED "
          "(all-hit semantics preserved -- the union must not collapse distinct blocks)",
          rc_m != 0 and "100.0%" not in out_m)

    # --- A genuinely UNCOVERED patch must still FAIL (the fix must not simply pass everything).
    rc_z, out_z = run_case([f"{BLOCK} 2 0"], go_src=GO_BASE, added_src=GO_ADDED)
    check("#288: genuinely uncovered block -> still 0%, gate FAILS (no false PASS)",
          rc_z != 0 and "Total patch coverage: 0.00%" in out_z)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("All patch-coverage checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
