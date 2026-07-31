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
        status_missing=False):
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
            "  pr)   printf '%s' \"${PR_JSON:-{}}\"; exit 0;;\n"
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

    p = subprocess.run([SCRIPT] + args, env=env, capture_output=True, text=True, timeout=30)
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

    print("== multi-PR sweep: one bad PR does not abort the rest ==")
    rc, out, err, entries, home = run(["354", "355", "owner/repo"])
    check("two PRs -> two entries", rc == 0 and len(entries) == 2)

    # THE POINT OF THE SWEEP being fail-soft: an UNWRITABLE entry must cost that ONE
    # PR's report, never the others. Simulated by making the triage dir read-only
    # after the first PR would have been written -- so PR #355 cannot be written and
    # the run must still succeed with #354's report intact. Without this case the
    # `continue` on write failure is untested and could silently become `exit 1`.
    rc, out, err, entries, home = run(["354", "owner/repo"])
    tdir = os.path.join(home, "triage")
    os.chmod(tdir, 0o500)  # readable/executable, NOT writable
    try:
        rc2, out2, err2, entries2, _ = run(["355", "owner/repo"], home=home)
        check("unwritable triage dir -> still exit 0 (sweep survives)", rc2 == 0)
        check("unwritable entry is reported, not silent", "WARN" in err2 or "could not write" in err2)
        check("the earlier PR's report is untouched", any("354" in e for e in entries2))
    finally:
        os.chmod(tdir, 0o700)

    print("== re-triage OVERWRITES rather than accreting ==")
    # A triage entry is a current-state report, not an append-only log: a second
    # run at the same sha must refresh it, not leave two rival reports for one PR.
    rc, _, _, entries2, _ = run(["354", "owner/repo"], home=home)
    n354 = [e for e in entries2 if "354" in e]
    check("re-triage of the same PR leaves ONE entry for it", len(n354) == 1)

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
