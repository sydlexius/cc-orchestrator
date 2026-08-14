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


def run_case(profile_lines, *, go_src, added_src, threshold="70",
             commit_patch=True, dirty_extra=None, extra_files=None, env_extra=None,
             break_index=False, committed_extra=None, run_from=None, git_config=None,
             empty_profile=False):
    """Build a throwaway Go repo with a BASE commit and a HEAD commit that ADDS `added_src`
    to lib.go, write `profile_lines` as the coverage profile, and run patch-coverage.sh
    against it. Returns (exit_code, stdout+stderr).

    #335 knobs, all default to the original clean-tree behavior:
      commit_patch=False  leave `added_src` UNCOMMITTED (face 1: the silent skip -- the
                          diff scope comes back empty because it is taken from HEAD).
      dirty_extra         extra source appended to lib.go AFTER the patch commit, so the
                          tree is dirty on top of a non-empty scope (face 2: the chimera).
      extra_files         {relpath: content} written after the patch commit and left
                          UNTRACKED. Used to prove the guard is scoped to *.go -- the
                          coverage profile itself already lives untracked in the repo.
    """
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
    for rel, content in (committed_extra or {}).items():
        path = os.path.join(repo, rel)
        if os.path.dirname(rel):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
    if commit_patch:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "patch")
    if dirty_extra:
        with open(os.path.join(repo, "lib.go"), "a") as fh:
            fh.write(dirty_extra)
    for rel, content in (extra_files or {}).items():
        path = os.path.join(repo, rel)
        if os.path.dirname(rel):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    prof = os.path.join(repo, "cover.out")
    with open(prof, "w") as fh:
        # empty_profile writes a ZERO-BYTE file, which is what the script's `[ -s ]` check
        # rejects -- a repo with no coverage tooling. A "mode: set" header alone would be
        # non-empty and sail past it.
        fh.write("" if empty_profile else
                 "mode: set\n" + "".join(ln if ln.endswith("\n") else ln + "\n"
                                         for ln in profile_lines))
    env = dict(os.environ, COVER_OUT=prof, BASE=base,
               PATCH_COVERAGE_THRESHOLD=threshold, **(env_extra or {}))
    for k, v in (git_config or {}).items():
        _git(repo, "config", k, v)
    index = os.path.join(repo, ".git", "index")
    if break_index:
        # Make `git status` fail (exit 128) while leaving rev-parse/merge-base -- which do
        # not read the index -- working, so the fail-closed branch is reached on its own.
        os.chmod(index, 0o000)
    cwd = os.path.join(repo, run_from) if run_from else repo
    try:
        p = subprocess.run(["bash", SCRIPT], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=60)
    finally:
        if break_index:
            # Restore before atexit, or rmtree cannot clean the repo up.
            os.chmod(index, 0o644)
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

    # --- #335: the scope comes from committed HEAD, the profile from the WORKING TREE.
    # Whenever a *.go file is dirty those two halves describe different versions of the
    # same source, so line N means one thing in the diff and another in the profile. Both
    # faces below exited 0 with a plausible-looking result before this guard.
    print("\n  #335 (scope-vs-profile mismatch)")

    # FACE 1 -- fully uncommitted. `git diff BASE..HEAD` is EMPTY, so the old code printed
    # "no Go source changes in scope" and exited 0: the gate cannot catch a regression in
    # the edit -> gate -> commit workflow, and says nothing about having skipped.
    rc_u1, out_u1 = run_case([f"{BLOCK} 2 0"], go_src=GO_BASE, added_src=GO_ADDED,
                             commit_patch=False)
    check("#335 face 1: fully-uncommitted patch REFUSES with exit 3 (no silent skip)",
          rc_u1 == 3)
    check("#335 face 1: refusal names the uncommitted change, not 'nothing to measure'",
          "uncommitted" in out_u1 and "no Go source changes in scope" not in out_u1)

    # FACE 2 -- the chimera: patch committed, then MORE uncommitted edits on top. The scope
    # is non-empty so the old code produced a number, but it is the intersection of HEAD's
    # diff lines with the working tree's coverage blocks -- a measurement of nothing.
    rc_u2, out_u2 = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                             dirty_extra="\nfunc More() int {\n\treturn 3\n}\n")
    check("#335 face 2: partly-committed (chimera) REFUSES (exit 3) and emits NO figure",
          rc_u2 == 3 and "Total patch coverage:" not in out_u2)

    # THE FALSE-POSITIVE THAT WOULD MAKE THE GUARD USELESS: a bare `git status --porcelain`
    # check is non-empty on essentially EVERY legitimate run, because the coverage profile
    # is normally written INTO the repo untracked (COVER_OUT defaults to ./coverage.out).
    # A guard that refuses every real invocation gets bypassed, so it must be scoped to
    # *.go. The six #288 cases above already run with an untracked cover.out and must keep
    # passing; this case pins the rule explicitly against other untracked non-Go files.
    rc_ok, out_ok = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                             extra_files={"coverage.out": "mode: set\n",
                                          "notes.md": "scratch\n",
                                          "sub/build.log": "noise\n"})
    check("#335: untracked NON-Go files (incl. the profile itself) do NOT trip the guard",
          rc_ok == 0 and "Total patch coverage: 100.00%" in out_ok)

    # A dirty *_test.go is a REAL divergence -- test code compiles into the profile and
    # changes which blocks are hit -- even though *_test.go is excluded from patch SCOPE.
    # Scoping the guard to the diff-scope pathspec instead of all *.go would miss it.
    rc_t, out_t = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                           extra_files={"lib_test.go": "package m\n"})
    check("#335: a dirty *_test.go DOES trip the guard (it changes the profile)",
          rc_t == 3 and "Total patch coverage:" not in out_t)

    # FAIL-CLOSED on an unreadable status. "I could not check whether the tree is clean"
    # must never route to the same place as "the tree is clean" -- that is the defect class
    # this repo keeps re-growing. The branch is genuinely reachable: `git status` needs the
    # index while the BASE checks upstream of it (rev-parse, merge-base) do not, so an
    # unreadable index reaches this guard and nothing before it.
    if os.geteuid() == 0:
        # chmod 000 is a NO-OP for root, so this case would pass without exercising
        # anything. Fail rather than print a green tick for a check that never ran.
        check("#335: unreadable git status FAILS CLOSED "
              "(NOT RUN -- running as uid 0 makes the chmod a no-op)", False)
    else:
        rc_fc, out_fc = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                                 break_index=True)
        check("#335: unreadable git status FAILS CLOSED (exit 3, no figure emitted)",
              rc_fc == 3 and "Total patch coverage:" not in out_fc)

    # THE GUARD MUST ASK A REPO-GLOBAL QUESTION, not one scoped by ambient context. Both
    # cases below were found in hostile review as measured FALSE-CLEANs -- the guard
    # reported a clean tree while a .go file was genuinely dirty, which is #335's own
    # defect class surviving inside its fix.

    # A bare '*.go' pathspec is CWD-RELATIVE. A multi-module repo runs the estimator from
    # the module dir (COVER_OUT is per-module), and from there the guard could not see a
    # dirty .go elsewhere in the tree. `:(top)` anchors it. NB a BARE `git status
    # --porcelain` IS repo-global from a subdir -- the pathspec introduced the scoping.
    rc_sd, out_sd = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                             committed_extra={"sub/go.mod": "module example.com/m/sub\n\ngo 1.22\n",
                                              "sub/s.go": "package sub\n"},
                             dirty_extra="\nfunc More() int {\n\treturn 3\n}\n",
                             run_from="sub")
    check("#335: dirty .go OUTSIDE the CWD still trips the guard (pathspec is :(top)-anchored)",
          rc_sd == 3)

    # `git status` honors status.showUntrackedFiles from user config. With it set to `no`
    # -- a legitimate perf setting on large repos -- a brand-new untracked .go file was
    # invisible, and that is the single most common shape of "the patch is uncommitted".
    rc_ut, out_ut = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                             extra_files={"brand_new.go": "package m\nfunc N() int { return 9 }\n"},
                             git_config={"status.showUntrackedFiles": "no"})
    check("#335: an untracked .go trips the guard even under status.showUntrackedFiles=no",
          rc_ut == 3)

    # A GITIGNORED .go WARNS BUT NEVER REFUSES (CR, PR #388 -- finding taken, patch not).
    # Go does not consult .gitignore, so an ignored .go DOES compile into the profile: the
    # finding is real, and by the same argument that put *_test.go inside the guard. But
    # the proposed fix -- refuse when `--ignored=matching` reports one -- keys on PRESENCE,
    # and git prints the identical `!! path` for a file rewritten one second ago and one
    # untouched for a year (measured both ways). A repo that ignores a generated .go would
    # therefore refuse FOREVER: the refuses-every-legitimate-run failure that already ruled
    # out the whole-tree pathspec. A condition git cannot evaluate becomes information,
    # never a gate.
    rc_ig, out_ig = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                             committed_extra={".gitignore": "generated.go\n"},
                             extra_files={"generated.go": "package m\nfunc G() int { return 7 }\n"})
    check("#388: a gitignored .go does NOT refuse (presence is not evidence of dirtiness)",
          rc_ig == 0 and "Total patch coverage: 100.00%" in out_ig)
    check("#388: a gitignored .go IS surfaced as a caveat on the number",
          "gitignored .go file(s) present" in out_ig and "UNKNOWABLE" in out_ig)

    # THE TWO EXIT-3 CAUSES HAVE DIFFERENT REMEDIES, and the message must say which applies
    # (CR, PR #388). The status-fault branch exits BEFORE the ALLOW_DIRTY check, so the
    # override is genuinely inapplicable there -- documenting "commit, or set ALLOW_DIRTY=1"
    # for both sent a reader with a broken .git down a path that cannot work, which reads as
    # a broken gate rather than a broken repo. Asserted here so the distinction cannot rot
    # back into one generic message.
    if os.geteuid() != 0:
        rc_sf, out_sf = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                                 break_index=True,
                                 env_extra={"PATCH_COVERAGE_ALLOW_DIRTY": "1"})
        check("#335: ALLOW_DIRTY does NOT override the status-fault branch (still exit 3)",
              rc_sf == 3 and "Total patch coverage:" not in out_sf)
        check("#335: the status-fault message names a repo-access fault and disclaims ALLOW_DIRTY",
              "REPO-ACCESS fault" in out_sf and "does NOT apply" in out_sf
              and "uncommitted Go changes are present" not in out_sf)

    # The escape hatch must exist and must be explicit, so a caller that genuinely wants
    # the old behavior declares it rather than learning to ignore a broken gate.
    rc_ov, out_ov = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                             dirty_extra="\nfunc More() int {\n\treturn 3\n}\n",
                             env_extra={"PATCH_COVERAGE_ALLOW_DIRTY": "1"})
    # Assert the label is on the TOTAL LINE ITSELF, not merely somewhere in the output:
    # the stderr warning also contains the string "PATCH_COVERAGE_ALLOW_DIRTY", so a bare
    # `"ALLOW_DIRTY" in out` passes even with the label deleted (measured -- that vacuous
    # form is the same substring trap that has bitten this harness before). The label has
    # to travel WITH the number, because the number is what gets quoted into a PR body.
    total_line = next((ln for ln in out_ov.splitlines()
                       if ln.startswith("Total patch coverage:")), "")
    check("#335: PATCH_COVERAGE_ALLOW_DIRTY=1 proceeds but LABELS the TOTAL LINE unreliable",
          rc_ov == 0 and "UNRELIABLE" in total_line)

    # EXIT 2 OUTRANKS EXIT 3, and the ordering is load-bearing rather than incidental.
    # The config checks (BASE, COVER_OUT, go.mod) all run BEFORE the dirty guard, so a repo
    # with no coverage tooling gets the exit-2 self-skip even when its tree is dirty. Move
    # the guard above them and every such repo would start REFUSING instead of skipping --
    # converting a self-skip into a hard stop for every consumer. Nothing pinned that, so
    # the reorder would have been silent. (CR raised this as "Trivial"; the severity label
    # is input, not a verdict -- this guards exactly what makes exit 3 safe to introduce.)
    rc_pre, out_pre = run_case([], go_src=GO_BASE, added_src=GO_ADDED,
                               dirty_extra="\nfunc More() int {\n\treturn 3\n}\n",
                               empty_profile=True)
    check("#335: a config error OUTRANKS the dirty guard (empty profile + dirty tree -> 2, not 3)",
          rc_pre == 2 and "profile not found or empty" in out_pre)

    # The label must ride EVERY stdout terminal path, not just the total line. Under
    # ALLOW_DIRTY with all changes uncommitted (the original face-1 shape) the script takes
    # the empty-scope path, whose message asserts the committed range holds no Go changes
    # while one sits uncommitted right there. Unlabelled, that reads as a clean skip.
    rc_es, out_es = run_case([f"{BLOCK} 2 1"], go_src=GO_BASE, added_src=GO_ADDED,
                             commit_patch=False,
                             env_extra={"PATCH_COVERAGE_ALLOW_DIRTY": "1"})
    scope_line = next((ln for ln in out_es.splitlines()
                       if ln.startswith("patch-coverage: no Go source changes in scope")), "")
    check("#335: ALLOW_DIRTY labels the EMPTY-SCOPE line too (not just the total)",
          rc_es == 0 and "UNRELIABLE" in scope_line)

    # The genuinely-empty scope must still exit 0 quietly -- and now say WHICH emptiness it
    # means, so it cannot be confused with the uncommitted case above (which no longer
    # reaches this path at all).
    # `added_src=""` alone would make the patch commit EMPTY, which git refuses -- so the
    # commit has to touch a real file that is simply not Go source.
    rc_e, out_e = run_case([], go_src=GO_BASE, added_src="",
                           committed_extra={"README.md": "docs only\n"})
    check("#335: genuinely-empty scope still exits 0 and names the committed range",
          rc_e == 0 and "no Go source changes in scope (committed range" in out_e)

    # --- #336: the header must not promise an accuracy the script does not deliver.
    # An 8-PR corpus measured against real Codecov numbers (comment on the issue) puts the
    # divergence at -3.24 .. +6.60 points: it reads HIGH on 4 of 8 and LOW on 1. The old
    # header called itself "a slight under-estimate" and told the reader to treat a pass as
    # authoritative because "codecov will pass too". That is measurably FALSE in the
    # direction that matters -- a local PASS can be a Codecov FAIL -- and a documented
    # promise the script does not keep is itself the defect. Asserted on the rendered
    # --help output, which is what a reader actually sees.
    print("\n  #336 (honest error bar)")
    hp = subprocess.run(["bash", SCRIPT, "--help"], capture_output=True, text=True, timeout=30)
    help_text = hp.stdout
    # Assert on WHITESPACE-NORMALIZED text. These are multi-word phrases inside WRAPPED
    # comment prose, so a raw substring test measures the TYPOGRAPHY, not the claim: the
    # phrase "codecov will pass too" straddles a line break in the current header, which
    # meant the negative assertion below passed because of where the line broke. That is
    # wrong in both directions -- a cosmetic reflow would FALSE-RED it, and a header that
    # REINSTATED the guarantee across the same wrap would sail through.
    flat = " ".join(help_text.split())
    check("#336: --help still renders (exit 0, non-trivial)",
          hp.returncode == 0 and len(help_text) > 500)
    # The phrase may appear ONLY inside the retraction, never as a live claim. Testing for
    # its mere absence is wrong (the retraction quotes it verbatim, so that would fail on
    # correct text); testing raw substrings is wrong (the wrap hid it). So assert the
    # STRUCTURE: every occurrence must be governed by "Earlier versions ... measurably
    # false". A header that reinstates the guarantee states it OUTSIDE that frame.
    # EVERY occurrence, not the first. `flat.index()` returns only the FIRST match, so a
    # header that KEPT the retraction and then REINSTATED the guarantee further down
    # passed this check (measured -- Copilot caught it on PR #390, and the exploit was
    # reproduced before fixing). The comment above already said "every occurrence"; the
    # code did not do it, which is the same defect class as the three vacuity traps this
    # PR already fixed: a check that reads as stronger than it is.
    _quote = '"codecov will pass too"'
    _lead = "Earlier versions of this header"

    def _governed(hay, needle, lead):
        """True iff EVERY occurrence of `needle` sits inside the retraction frame:
        preceded by `lead` and followed by the disavowal."""
        start = 0
        while True:
            i = hay.find(needle, start)
            if i == -1:
                return True
            before = hay.rfind(lead, 0, i)
            if before == -1 or "That was measurably false" not in hay[i:]:
                return False
            start = i + len(needle)

    _retraction_ok = _governed(flat, _quote, _lead)
    check("#336: 'codecov will pass too' appears ONLY inside the retraction, never as a claim",
          _retraction_ok)
    # NB: assert the CLAIM is gone, not the WORD. The corrected header names the old
    # "slight under-estimate" phrasing in order to disavow it, so a bare substring test
    # fails on the retraction itself -- the same vacuity trap as the ALLOW_DIRTY label,
    # inverted. What must be absent is the ASSERTION that the script under-estimates.
    check("#336: no longer CLAIMS to be an under-estimate (the retraction may name it)",
          "remains a slight under-estimate" not in flat
          and "Earlier versions" in flat)
    check("#336: states the MEASURED two-sided range, with both signs",
          "-3.24" in flat and "+6.60" in flat)
    # AN AFFIRMATIVE ASSERTION IS REQUIRED, not just negative ones. Four of these checks
    # assert a false claim is ABSENT, and absence is satisfiable by ANY rewording -- an
    # inverted header that dodges the exact substrings passed all of them (found in
    # hostile review). This pins the load-bearing sentence the correct header opens with,
    # which an inverting rewrite must delete rather than merely rephrase.
    check("#336: AFFIRMATIVELY states the two-sided / not-a-bound framing",
          "TWO-SIDED ERROR BAR, NOT A BOUND" in flat.upper())
    check("#336: says explicitly that a local PASS can still fail Codecov",
          "LOCAL PASS CAN STILL FAIL" in flat.upper())

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
