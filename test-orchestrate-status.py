#!/usr/bin/env python3
"""Proof harness for orchestrate-status.sh (#223).

A READ-ONLY status oracle: one compact line per in-flight (open) PR, COMPOSED
from existing digests -- `gh pr view` (state / checks / review / merge) plus
`pr-unreplied-comments.sh --count-only` (unreplied count). It never mutates and
never grows a reason to widen the `gh pr` allow-list.

Both external deps are stubbed so the harness is deterministic and
host-independent (never a real gh, never the real pr-unreplied helper):

  - `gh` is a temp 0755 script first on PATH. It serves:
      * `gh pr list --state open ...`  -> the PR-number set from $PRLIST_JSON
      * `gh pr view <n> ...`           -> per-PR doc from $PRVIEW_<n>_JSON
      * `gh repo view ...`             -> a fixed slug
      * $GH_LIST_FAIL=1 makes `pr list` exit non-zero (list-failure path)
    Every `gh` argv is appended to $GH_LOG so the test can assert the oracle
    only ever issued read verbs (no -X / --method / mutation).
  - pr-unreplied-comments.sh is stubbed under a temp $HOME/.claude/scripts/
    (the oracle resolves it via $HOME, matching ship-gate-preflight.sh). It
    echoes the count from $UNREPLIED_<n> (default 0); $UNREPLIED_FAIL_<n>=1
    makes it exit 2 for that PR (count reads as "?").

Line contract asserted (one per PR):
  #<num> <state> checks:<GREEN|RED|PENDING|NONE> review:<decision|none> \
    merge:<mergeStateStatus|?> unreplied:<N|?>  <title>

Run: python3 test-orchestrate-status.py
"""
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "orchestrate-status.sh")

FAILS = []


def check(label, ok):
    status = "ok  " if ok else "FAIL"; print(f"  [{status}] {label}")
    if not ok:
        FAILS.append(label)


GH_STUB = r'''#!/usr/bin/env python3
import json, os, sys, subprocess
args = sys.argv[1:]
# Record every invocation for the read-only assertion. JSON-encode so an argv
# containing newlines (e.g. a multi-line --jq program) stays one log line.
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")

def emit(data):
    if "--jq" in args:
        expr = args[args.index("--jq") + 1]
        p = subprocess.run(["jq", "-r", expr], input=data, capture_output=True, text=True)
        if p.returncode != 0:
            sys.stderr.write(p.stderr); sys.exit(p.returncode)
        sys.stdout.write(p.stdout)
    else:
        sys.stdout.write(data)
    sys.exit(0)

if args[:2] == ["pr", "list"]:
    if os.environ.get("GH_LIST_FAIL") == "1":
        sys.stderr.write("gh: list boom\n"); sys.exit(1)
    emit(os.environ.get("PRLIST_JSON", "[]"))
if args[:2] == ["pr", "view"]:
    n = args[2]
    emit(os.environ.get("PRVIEW_%s_JSON" % n, "{}"))
if args[:2] == ["repo", "view"]:
    emit('{"nameWithOwner":"owner/repo"}')
# Any other gh call (e.g. a mutation) -> loud failure so the test notices.
sys.stderr.write("gh stub: unexpected call %r\n" % args); sys.exit(3)
'''

UNREPLIED_STUB = r'''#!/usr/bin/env python3
import os, sys
# Args: --count-only <pr> [repo]
args = sys.argv[1:]
pr = None
for a in args:
    if a.isdigit():
        pr = a; break
if os.environ.get("UNREPLIED_FAIL_%s" % pr) == "1":
    sys.stderr.write("unreplied boom\n"); sys.exit(2)
sys.stdout.write(os.environ.get("UNREPLIED_%s" % pr, "0") + "\n")
sys.exit(0)
'''


def run(args, *, prlist="[]", views=None, unreplied=None, unreplied_fail=None,
        gh_list_fail=False):
    views = views or {}
    unreplied = unreplied or {}
    unreplied_fail = unreplied_fail or []
    with tempfile.TemporaryDirectory() as td:
        bindir = os.path.join(td, "bin"); os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(GH_STUB)
        os.chmod(gh, 0o755)
        home = os.path.join(td, "home")
        helperdir = os.path.join(home, ".claude", "scripts"); os.makedirs(helperdir)
        helper = os.path.join(helperdir, "pr-unreplied-comments.sh")
        with open(helper, "w") as f:
            f.write(UNREPLIED_STUB)
        os.chmod(helper, 0o755)
        gh_log = os.path.join(td, "gh.log"); open(gh_log, "w").close()

        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["HOME"] = home
        env["GH_LOG"] = gh_log
        env["PRLIST_JSON"] = prlist
        if gh_list_fail:
            env["GH_LIST_FAIL"] = "1"
        for n, doc in views.items():
            env["PRVIEW_%s_JSON" % n] = doc
        for n, cnt in unreplied.items():
            env["UNREPLIED_%s" % n] = str(cnt)
        for n in unreplied_fail:
            env["UNREPLIED_FAIL_%s" % n] = "1"

        p = subprocess.run(["bash", SCRIPT] + args, env=env,
                           capture_output=True, text=True, timeout=20)
        import json
        gh_calls = [json.loads(ln) for ln in open(gh_log).read().splitlines() if ln]
        return p.returncode, p.stdout, p.stderr, gh_calls


def view(state="OPEN", title="t", merge="CLEAN", review="APPROVED", rollup=None):
    import json
    doc = {"state": state, "title": title, "mergeStateStatus": merge,
           "reviewDecision": review, "statusCheckRollup": rollup or []}
    return json.dumps(doc)


def cr(name, conclusion, status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def line_for(out, pr):
    for ln in out.splitlines():
        if ln.startswith("#%s " % pr):
            return ln
    return None


def main():
    print("== #223 orchestrate-status: one composed line per in-flight PR ==")

    # Two open PRs -> exactly two data lines, one each, in list order.
    rc, out, err, calls = run(
        ["owner/repo"],
        prlist='[{"number":248},{"number":250}]',
        views={
            "248": view(title="fix coverage glyph", rollup=[cr("gates", "SUCCESS")]),
            "250": view(title="add status oracle", review="REVIEW_REQUIRED",
                        merge="BLOCKED", rollup=[cr("gates", "FAILURE")]),
        },
        unreplied={"248": 0, "250": 3},
    )
    check("exit 0 on a clean read", rc == 0)
    check("PR #248 has a line", line_for(out, "248") is not None)
    check("PR #250 has a line", line_for(out, "250") is not None)
    l248 = line_for(out, "248") or ""
    l250 = line_for(out, "250") or ""
    check("#248 checks GREEN (all SUCCESS)", "checks:GREEN" in l248)
    check("#248 review APPROVED", "review:APPROVED" in l248)
    check("#248 merge CLEAN", "merge:CLEAN" in l248)
    check("#248 unreplied 0", "unreplied:0" in l248)
    check("#248 title surfaced", "fix coverage glyph" in l248)
    check("#250 checks RED (a FAILURE present)", "checks:RED" in l250)
    check("#250 unreplied 3 (composed from --count-only)", "unreplied:3" in l250)

    # Pending check -> PENDING; no checks -> NONE; null reviewDecision -> none.
    rc, out, err, calls = run(
        [],
        prlist='[{"number":10},{"number":11}]',
        views={
            "10": view(review=None, rollup=[cr("gates", None, status="IN_PROGRESS")]),
            "11": view(rollup=[]),
        },
        unreplied={"10": 1, "11": 0},
    )
    check("pending check -> checks:PENDING", "checks:PENDING" in (line_for(out, "10") or ""))
    check("null reviewDecision -> review:none", "review:none" in (line_for(out, "10") or ""))
    check("empty rollup -> checks:NONE", "checks:NONE" in (line_for(out, "11") or ""))

    # No open PRs -> a clean one-line note, exit 0.
    rc, out, err, calls = run([], prlist='[]')
    check("no in-flight PRs -> exit 0", rc == 0)
    check("no in-flight PRs -> a note, no data lines", "#" not in out and out.strip() != "")

    # Explicit PR-number args scope the report (no pr list call).
    rc, out, err, calls = run(
        ["248", "owner/repo"],
        views={"248": view(rollup=[cr("gates", "SUCCESS")])},
        unreplied={"248": 0},
    )
    check("explicit PR arg -> line present", line_for(out, "248") is not None)
    check("explicit PR arg -> no `pr list` call issued",
          not any(c[:2] == ["pr", "list"] for c in calls))

    # Helper failure for a PR -> unreplied reads '?', line still emitted, exit 0.
    rc, out, err, calls = run(
        [],
        prlist='[{"number":9}]',
        views={"9": view(rollup=[cr("gates", "SUCCESS")])},
        unreplied_fail=["9"],
    )
    check("helper failure -> unreplied:? (degrades, not a crash)", "unreplied:?" in (line_for(out, "9") or ""))
    check("helper failure -> still exit 0", rc == 0)

    # gh pr list failure -> non-zero exit, stderr explains (can't determine in-flight set).
    rc, out, err, calls = run([], gh_list_fail=True)
    check("gh pr list failure -> non-zero exit", rc != 0)
    check("gh pr list failure -> stderr names the problem", "list" in err.lower() or "flight" in err.lower())

    # READ-ONLY: every gh call is a read verb; no mutation flag ever issued.
    rc, out, err, calls = run(
        [],
        prlist='[{"number":248}]',
        views={"248": view(rollup=[cr("gates", "SUCCESS")])},
        unreplied={"248": 0},
    )
    mutating = [c for c in calls if any(t in ("-X", "--method", "--field", "-f", "-F")
                                        or t.startswith("-X") for t in c)]
    check("read-only: no mutation flag on any gh call", mutating == [])
    verbs = {tuple(c[:2]) for c in calls}
    check("read-only: only pr list/view + repo view verbs used",
          verbs <= {("pr", "list"), ("pr", "view"), ("repo", "view")})

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):"); [print("  - " + f) for f in FAILS]; sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
