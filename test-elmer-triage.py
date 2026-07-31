#!/usr/bin/env python3
"""Proof harness for elmer-triage.sh -- the MECHANICAL triage maildir drop.

WHAT THIS IS: the loop's morning-report leg. It composes EXISTING read-only helpers
(orchestrate-status.sh for state/checks/review/merge/unreplied, pr-read-comments.sh
for the finding bodies) into one maildir entry per PR, so a TL wakes to a digest
instead of a raw comment dump. NO MODEL IS INVOLVED -- every field is a helper's
output or a git/gh read. That is what keeps the loop a dumb pipe.

THE ONE NON-OBVIOUS FIELD: `triaged_sha`. Judgment about a PR is only valid for the
code it was computed against, so the entry records the head SHA it saw. A consumer
compares that to HEAD at read time: equal means the triage is live, different means
stale and re-derive. This is what makes a LATER Sonnet triage subagent safe to add --
staleness becomes DETECTABLE rather than assumed. (Staleness needs the code to
change; nothing pushes overnight, so in practice these match.)

Every external dependency is stubbed -- the composed helpers are temp scripts on
PATH//ELMER_* overrides -- so the harness never touches the network and never writes
outside a temp dir.

Contract asserted:
  exit 0  wrote at least one triage entry (or had nothing to triage -- both are fine)
  exit 2  setup error (bad args, unresolvable repo)
A per-PR failure must NEVER abort the sweep: a bad PR is reported and skipped, and
the remaining PRs are still triaged. Losing four good reports because the fifth PR
404s is the failure mode this exists to avoid.

Run: python3 test-elmer-triage.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "scripts", "elmer-triage.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


SHA1 = "1" * 40
SHA2 = "2" * 40

STATUS_LINE = ("#354 OPEN checks:GREEN review:APPROVED merge:CLEAN unreplied:3  "
               "fix(#352): bind probe")

FINDINGS = """=== Review-body comments (1) ===
---
ID:   12345
File: (review body)
By:   coderabbitai[bot]

Actionable comments posted: 3
"""


def run(args, *, status_out=STATUS_LINE, status_rc=0, findings_out=FINDINGS,
        findings_rc=0, pr_json=None, home=None, repo_fail=False,
        status_missing=False, close_fd=None, pr_view_fail=False):
    """Invoke triage with stubbed helpers + isolated ELMER_HOME."""
    td = tempfile.mkdtemp()
    bindir = os.path.join(td, "bin"); os.makedirs(bindir)

    gh = os.path.join(bindir, "gh")
    with open(gh, "w") as f:
        f.write(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "case \"${1:-}\" in\n"
            "  repo) [ -n \"${GH_REPO_FAIL:-}\" ] && exit 1; echo 'owner/repo'; exit 0;;\n"
            # The `pr` arm can be made to FAIL, which is the only way to exercise the
            # script's documented read-failure path. The guard is the same shape the
            # `repo` arm already uses, and it is safe under `set -eu`: the failing
            # command is the `[`, which is not the command FOLLOWING the final `&&`,
            # so errexit is exempt and an unset GH_PR_FAIL falls straight through.
            # The `${PR_JSON:-{}}` default is left EXACTLY as-is: escaping the braces
            # would emit `${PR_JSON:-\\{\\}}`, whose default expands to a literal `\{}`
            # -- a backslash in the JSON that would break every other case.
            "  pr)   [ -n \"${GH_PR_FAIL:-}\" ] && exit 1; printf '%s' \"${PR_JSON:-{}}\"; exit 0;;\n"
            "esac\n"
            "exit 0\n"
        )
    os.chmod(gh, 0o755)

    status = os.path.join(td, "orchestrate-status.sh")
    if not status_missing:
        with open(status, "w") as f:
            f.write("#!/usr/bin/env bash\nprintf '%s\\n' \"${STATUS_OUT}\"\nexit ${STATUS_RC:-0}\n")
        os.chmod(status, 0o755)

    reader = os.path.join(td, "pr-read-comments.sh")
    with open(reader, "w") as f:
        f.write("#!/usr/bin/env bash\nprintf '%s' \"${FINDINGS_OUT}\"\nexit ${FINDINGS_RC:-0}\n")
    os.chmod(reader, 0o755)

    elmer_home = home or os.path.join(td, "elmer")

    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["ELMER_HOME"] = elmer_home
    env["ELMER_STATUS_ORACLE"] = status
    env["ELMER_COMMENT_READER"] = reader
    env["STATUS_OUT"] = status_out
    env["STATUS_RC"] = str(status_rc)
    env["FINDINGS_OUT"] = findings_out
    env["FINDINGS_RC"] = str(findings_rc)
    env["PR_JSON"] = pr_json if pr_json is not None else json.dumps(
        {"headRefOid": SHA1, "state": "OPEN", "number": 354, "title": "fix(#352): bind probe"})
    if repo_fail:
        env["GH_REPO_FAIL"] = "1"
    if pr_view_fail:
        env["GH_PR_FAIL"] = "1"

    full = [SCRIPT] + args
    if close_fd == 1:
        # `>&-` cannot be expressed through subprocess's stdout parameter (every value
        # it accepts is an OPEN fd), so the closing is done by an exec'd bash wrapper --
        # the same construct that reproduced the defect by hand. The other fd stays
        # captured so the assertions can still see what the run reported.
        full = ["bash", "-c", 'exec "$@" >&-', "sh"] + full
    elif close_fd == 2:
        full = ["bash", "-c", 'exec "$@" 2>&-', "sh"] + full
    p = subprocess.run(full, env=env, capture_output=True, text=True, timeout=30)
    tdir = os.path.join(elmer_home, "triage")
    entries = sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []
    return p.returncode, p.stdout, p.stderr, entries, elmer_home


def read_entry(home, name):
    return open(os.path.join(home, "triage", name)).read()


def main():
    print("== arg validation ==")
    rc, out, _, _, _ = run(["--help"])
    check("--help -> exit 0, prints usage", rc == 0 and "elmer-triage" in out)
    rc, _, _, _, _ = run(["notanumber"])
    check("non-numeric PR -> exit 2", rc == 2)
    rc, _, err, _, _ = run(["354"], repo_fail=True)
    check("repo unresolvable -> exit 2", rc == 2 and "setup error" in err)

    print("== the happy path: one entry per PR ==")
    rc, out, err, entries, home = run(["354", "owner/repo"])
    check("valid PR -> exit 0", rc == 0)
    check("exactly one triage entry written", len(entries) == 1)
    check("entry name carries repo and PR",
          entries and "354" in entries[0] and "owner" in entries[0])

    body = read_entry(home, entries[0])
    print("== the entry composes the helpers, and pins the SHA ==")
    check("carries the status oracle's line", "checks:GREEN" in body and "unreplied:3" in body)
    check("carries the finding bodies", "Actionable comments posted" in body)
    check("RECORDS triaged_sha (what makes staleness detectable)", SHA1 in body)
    check("names the PR and repo", "354" in body and "owner/repo" in body)
    check("is timestamped", "triaged_at" in body or "UTC" in body or "Z" in body)
    check("states it is mechanical (no model judgment)",
          "mechanical" in body.lower() or "no model" in body.lower())

    print("== staleness is CONSUMABLE: the recorded sha is machine-readable ==")
    # A consumer must be able to extract the sha without parsing prose.
    import re
    m = re.search(r"^triaged_sha:\s*([0-9a-f]{40})\s*$", body, re.M)
    check("triaged_sha appears on its own line, machine-greppable", bool(m))
    check("triaged_sha matches the head the oracle saw", bool(m) and m.group(1) == SHA1)
    # `state` is FETCHED by the same `gh pr view --json` read as the sha and the title,
    # so discarding it left the entry with no PR state at all on the degraded path,
    # while the data to fill it had already been paid for. Same own-line convention as
    # triaged_sha, so a consumer greps rather than parsing prose.
    ms = re.search(r"^state:\s*(\S+)\s*$", body, re.M)
    check("state appears on its own line, machine-greppable", bool(ms))
    check("state carries what the read returned", bool(ms) and ms.group(1) == "OPEN")

    print("== a PR with NOTHING to report still succeeds ==")
    rc, out, err, entries, home = run(["354", "owner/repo"],
                                      status_out="#354 OPEN checks:GREEN review:none merge:CLEAN unreplied:0  t",
                                      findings_out="No unreplied bot comments on PR #354.")
    check("zero findings -> exit 0 (not an error)", rc == 0)
    check("zero findings -> still writes an entry (absence is a real report)", len(entries) == 1)
    check("entry records unreplied:0", "unreplied:0" in read_entry(home, entries[0]))

    print("== FAIL-SOFT: a broken helper must not lose the whole report ==")
    rc, out, err, entries, home = run(["354", "owner/repo"], findings_rc=1, findings_out="")
    check("comment reader fails -> still exit 0", rc == 0)
    check("comment reader fails -> entry still written", len(entries) == 1)
    check("entry says the finding read failed (never silently empty)",
          "unavailable" in read_entry(home, entries[0]).lower()
          or "failed" in read_entry(home, entries[0]).lower())

    rc, out, err, entries, home = run(["354", "owner/repo"], status_rc=1, status_out="")
    check("status oracle fails -> still exit 0, entry written", rc == 0 and len(entries) == 1)
    check("entry says status was unavailable",
          "unavailable" in read_entry(home, entries[0]).lower()
          or "failed" in read_entry(home, entries[0]).lower())

    rc, out, err, entries, home = run(["354", "owner/repo"], status_missing=True)
    check("status oracle ABSENT -> still exit 0, entry written (degraded, loud)",
          rc == 0 and len(entries) == 1)
    # THE DEGRADED PATH is exactly where the discarded `state` hurt: with no status
    # oracle the entry has no other source of PR state, and the `gh pr view` read that
    # would supply it has already run.
    check("degraded (no status oracle) -> state is STILL recorded",
          bool(re.search(r"^state:\s*OPEN\s*$", read_entry(home, entries[0]), re.M)))

    print("== a FAILED `gh pr view` read is RECORDED, never fatal ==")
    # The script's header promises this path: a read failure is not fatal, the entry is
    # still worth writing, and it says so rather than implying a pin it lacks. The stub
    # always exited 0 for `pr`, so the path was never exercised -- a regression making
    # the read fatal, or dropping the triaged_sha line, would have passed unnoticed.
    rc, out, err, entries, home = run(["354", "owner/repo"], pr_view_fail=True)
    check("gh pr view fails -> still exit 0", rc == 0)
    check("gh pr view fails -> the entry IS still written", len(entries) == 1)
    fbody = read_entry(home, entries[0]) if entries else ""
    check("unpinnable entry records triaged_sha: unknown, on its own line",
          bool(re.search(r"^triaged_sha:\s*unknown\s*$", fbody, re.M)))
    check("unpinnable entry records state: unknown, on its own line",
          bool(re.search(r"^state:\s*unknown\s*$", fbody, re.M)))
    # `title` is the ONE field written conditionally -- an unread title is OMITTED
    # rather than filled with a placeholder, because a fake title is worse than none.
    check("no title was read -> the title line is ABSENT (not a placeholder)",
          not re.search(r"^title:", fbody, re.M))
    check("the composed helpers still ran (a gh failure is per-field, not per-entry)",
          "checks:GREEN" in fbody and "Actionable comments posted" in fbody)

    print("== multi-PR sweep: one bad PR does not abort the rest ==")
    rc, out, err, entries, home = run(["354", "355", "owner/repo"])
    check("two PRs -> two entries", rc == 0 and len(entries) == 2)

    # THE POINT OF THE SWEEP being fail-soft: an unwritable entry must cost that ONE
    # PR's report, never the others.
    #
    # THE BLOCKED PR GOES FIRST, AND THAT ORDERING IS THE WHOLE TEST. In a SINGLE-PR
    # invocation `continue` and a normal loop exit are INDISTINGUISHABLE -- both reach
    # the summary line and exit 0 -- so a sweep that ran #354 to completion and then
    # started a SEPARATE process for #355 proved nothing about the `continue`, and the
    # documented failure mode (losing a good report because a LATER PR failed) went
    # uncovered. One invocation, blocked PR first: the only way #354's entry can exist
    # afterward is if the loop CONTINUED past #355's failure.
    #
    # The block is a mode-0500 DIRECTORY at #355's entry path, so `mv -f` cannot
    # replace it. A plain WRITABLE directory there would not do: `mv -f file dir/`
    # SUCCEEDS on one, moving the file inside, and the case would pass vacuously.
    home = tempfile.mkdtemp()
    tdir = os.path.join(home, "triage")
    blocked = os.path.join(tdir, "owner-repo--355.md")
    os.makedirs(blocked)
    os.chmod(blocked, 0o500)
    try:
        rc2, out2, err2, entries2, _ = run(["355", "354", "owner/repo"], home=home)
        check("blocked FIRST PR -> the sweep still exits 0", rc2 == 0)
        check("the blocked PR is REPORTED by number, not silent",
              "WARN" in err2 and "355" in err2)
        check("the LATER PR was still triaged (the `continue` is live)",
              "owner-repo--354.md" in entries2)
        check("no .tmp file orphaned by the blocked PR",
              not any(".tmp." in e for e in os.listdir(tdir)))
    finally:
        os.chmod(blocked, 0o700)

    print("== re-triage OVERWRITES rather than accreting ==")
    # A triage entry is a current-state report, not an append-only log: a second
    # run at the same sha must refresh it, not leave two rival reports for one PR.
    rc, _, _, entries2, _ = run(["354", "owner/repo"], home=home)
    n354 = [e for e in entries2 if "354" in e]
    check("re-triage of the same PR leaves ONE entry for it", len(n354) == 1)

    print("== a FAILED RENAME costs one PR's report, never the sweep ==")
    # `set -e` is active and the `mv -f` that installs an entry was UNGUARDED, so a
    # rename failure exited 1 - a code this script's header does not document at all
    # (only 0 and 2) - and ABORTED THE WHOLE SWEEP, leaving every later PR untriaged
    # and a `.tmp` file orphaned. Reproduced with a mode-0500 DIRECTORY at the entry
    # path: the composing write into `$tmp` succeeds (the tmp file is a sibling, and
    # the dir itself is still writable), so the pre-existing unwritable-dir case does
    # not reach this failure. The sweep must survive it and still triage #355.
    rc, out, err, entries, home = run(["354", "355", "owner/repo"])
    tdir = os.path.join(home, "triage")
    blocker = os.path.join(tdir, "owner-repo--354.md")
    os.remove(blocker)
    os.makedirs(blocker)            # a DIRECTORY where the entry file must go
    os.chmod(blocker, 0o500)        # ... and non-empty-proof: mv cannot replace it
    try:
        rc2, out2, err2, entries2, _ = run(["354", "355", "owner/repo"], home=home)
        check("failed rename -> sweep still exits 0", rc2 == 0)
        check("failed rename is REPORTED, not silent",
              "WARN" in err2 and "354" in err2)
        check("the OTHER PR was still triaged", "355" in out2)
        check("no .tmp file orphaned",
              not any(e.startswith(".") and e.endswith(".tmp") for e in os.listdir(tdir))
              and not any(".tmp." in e for e in os.listdir(tdir)))
    finally:
        os.chmod(blocker, 0o700)

    print("== a CLOSED output fd must never change the exit code ==")
    # THE CLASS: under `set -e` a bare `echo`/`printf`/`awk` onto a CLOSED or BROKEN fd
    # (EBADF/EIO - an unattended loop redirecting into a rotated or closed log) FAILS,
    # and that failure becomes the exit status, OVERRIDING the intended code. Every
    # write site here was unguarded, so with stdout closed the SUCCESS path returned
    # rc=1 - a code the header does not document - and every deliberate `exit 2`
    # downgraded to 1. This is the whole-file sweep's proof, not a per-site patch's.
    rc, _, _, entries, _ = run(["354", "owner/repo"], close_fd=1)
    check("SUCCESS with stdout closed -> still exit 0", rc == 0)
    check("SUCCESS with stdout closed -> the entry IS written", len(entries) == 1)
    rc, _, _, _, _ = run(["notanumber"], close_fd=2)
    check("non-numeric PR with stderr closed -> still exit 2", rc == 2)
    rc, _, _, _, _ = run([], close_fd=2)
    check("no args with stderr closed -> still exit 2", rc == 2)
    rc, _, _, _, _ = run(["--badflag"], close_fd=2)
    check("unknown flag with stderr closed -> still exit 2", rc == 2)
    rc, _, _, _, _ = run(["354"], repo_fail=True, close_fd=2)
    check("unresolvable repo with stderr closed -> still exit 2", rc == 2)
    rc, _, _, _, _ = run(["--help"], close_fd=1)
    check("--help with stdout closed -> still exit 0", rc == 0)
    # The fail-soft paths write a WARN to stderr; a closed fd 2 must not turn a
    # survivable per-PR failure into an abort.
    rc, _, _, entries, home = run(["354", "owner/repo"])
    os.chmod(os.path.join(home, "triage"), 0o500)
    try:
        rc2, _, _, _, _ = run(["355", "owner/repo"], home=home, close_fd=2)
        check("unwritable entry + closed stderr -> still exit 0", rc2 == 0)
    finally:
        os.chmod(os.path.join(home, "triage"), 0o700)

    print("== OVER-HARDENING: a guard must not become a mute ==")
    # `{ ... } >&2 || true` is one careless edit from `{ ... } >/dev/null`, and BOTH
    # satisfy every rc assertion above while the second silently discards the report.
    rc, out, err, entries, home = run(["354", "owner/repo"])
    check("the per-PR line still prints on stdout",
          "triaged: owner/repo #354" in out)
    check("the summary line still prints on stdout",
          "elmer-triage: wrote 1 entry" in out)
    rc, _, err, _, _ = run(["notanumber"])
    check("the non-numeric setup error still names the value",
          "setup error" in err and "notanumber" in err)
    rc, _, err, _, _ = run(["--badflag"])
    check("the unknown-flag error still names the flag", "--badflag" in err)
    rc, _, err, _, _ = run([])
    check("the usage line still prints", "usage: elmer-triage.sh" in err)
    rc, _, err, _, _ = run(["354"], repo_fail=True)
    check("the unresolvable-repo error still explains the fix", "owner/repo" in err)
    rc, out, _, _, _ = run(["--help"])
    check("--help still prints the header", "elmer-triage" in out and "Exit codes" in out)

    print("== READ-ONLY: triage never mutates and never posts ==")
    src = open(SCRIPT).read()
    for forbidden in ["gh pr comment", "gh pr merge", "gh pr edit", "-X POST",
                      "--method POST", "coderabbitai", "reply-comment",
                      "resolve-thread", "git push"]:
        check(f"source contains no '{forbidden}'", forbidden not in src)
    check("source does not call gh api directly (composes helpers instead)",
          "gh api" not in src)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
